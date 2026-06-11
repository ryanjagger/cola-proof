"""Tier B client and escalation tests (spec phase-6) using a mocked
OpenAI-compatible endpoint — no model needed."""

import json
from pathlib import Path

import httpx
import pytest

from server.pipeline.extract_labels import LabelCrop
from server.pipeline.match import Outcome
from server.pipeline.ocr import OcrResult
from server.pipeline.parse_form import ParsedForm
from server.pipeline.runner import RecordResult, escalate, evaluate
from server.pipeline.vision import VisionClient, VisionResult
from server.pipeline.warning import STATUTORY_BODY, STATUTORY_PREFIX, WarningStatus

CANONICAL_WARNING = f"{STATUTORY_PREFIX} {STATUTORY_BODY}"


def _tiny_jpeg() -> bytes:
    """A real decodable image: read_crop re-encodes crops before sending."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, "JPEG")
    return buf.getvalue()


def _client(handler) -> VisionClient:
    return VisionClient(
        "http://vision.test/v1", "test-model",
        transport=httpx.MockTransport(handler),
    )


def _completion(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(payload)}}]},
    )


def test_read_crop_parses_structured_transcription():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # JSON-schema-constrained output must be requested.
        assert body["response_format"]["type"] == "json_schema"
        assert body["temperature"] == 0
        schema = body["response_format"]["json_schema"]["schema"]
        # The bottler/origin checks rely on dedicated transcription
        # fields: full_text is prompted "briefly" and would skip them.
        assert {"bottler_text", "origin_text"} <= set(schema["properties"])
        assert {"bottler_text", "origin_text"} <= set(schema["required"])
        return _completion(
            {
                "brand_text": "Viejo Tonel",
                "abv_text": "Alc. 42% by Vol",
                "net_contents_text": "750 ml",
                "warning_text": CANONICAL_WARNING,
                "bottler_text": "BOTTLED BY VIEJO TONEL S.A., ICA",
                "origin_text": "PRODUCT OF PERU",
                "full_text": "PISCO ITALIA",
            }
        )

    r = _client(handler).read_crop(_tiny_jpeg(), "jpeg")
    assert r.ok
    assert r.brand_text == "Viejo Tonel"
    assert "42%" in r.abv_text
    assert CANONICAL_WARNING in r.combined_text
    # The new fields feed the matchers' pool like every other field.
    assert "BOTTLED BY VIEJO TONEL S.A., ICA" in r.combined_text
    assert "PRODUCT OF PERU" in r.combined_text


def test_read_crop_degrades_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    r = _client(handler).read_crop(_tiny_jpeg(), "jpeg")
    assert not r.ok
    assert r.error
    assert r.combined_text == ""


def test_read_crop_degrades_on_garbage_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    r = _client(handler).read_crop(_tiny_jpeg(), "jpeg")
    assert not r.ok


def test_truncated_json_salvages_complete_fields():
    """Token-cap truncation mid-string must not discard the whole read —
    that's exactly how the Black Maple Hill front-label evidence was lost."""
    cut_off = (
        '{"brand_text": "Black Maple Hill", '
        '"abv_text": null, '
        '"net_contents_text": "750ml", '
        '"bottler_text": "BOTTLED BY OLD LINE DISTILLERY", '
        '"warning_text": "GOVERNMENT WARNING: (1) Accord'  # unterminated
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": cut_off}, "finish_reason": "length"}
                ]
            },
        )

    r = _client(handler).read_crop(_tiny_jpeg(), "jpeg")
    assert r.ok
    assert r.brand_text == "Black Maple Hill"
    assert r.net_contents_text == "750ml"
    assert r.bottler_text == "BOTTLED BY OLD LINE DISTILLERY"
    assert r.warning_text is None  # the incomplete field is dropped
    assert "truncated" in r.error
    assert "750ml" in r.combined_text


def test_truncated_json_with_nothing_salvageable_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"brand_te'}, "finish_reason": "length"}
                ]
            },
        )

    r = _client(handler).read_crop(_tiny_jpeg(), "jpeg")
    assert not r.ok


def test_unconfigured_client_reports_not_configured():
    r = VisionClient("", "m").read_crop(b"x", "jpeg")
    assert not r.ok
    assert "not configured" in r.error


# --- escalation -------------------------------------------------------------


def _crop(index: int, kind: str) -> LabelCrop:
    return LabelCrop(
        index=index, caption_type=kind, kind=kind, width_in=3.0, height_in=4.0,
        px_width=300, px_height=400, dpi=100, page=1, ext="jpeg", data=b"img",
        aspect_ok=True,
    )


def _record() -> RecordResult:
    """A pre-2016 record whose front label OCRed too poorly to match."""
    form = ParsedForm(
        ttb_id="x", brand_name="VIEJO TONEL", serial_number="1",
        net_contents="750 MILLILITERS", alcohol_content="42",
        class_type_description="OTHER GRAPE BRANDY (PISCO, GRAPPA) FB",
        has_net_contents_field=True, has_alcohol_content_field=True,
        applicant="VIEJO TONEL S.A.\nCALLE LIMA 123\nICA",
    )
    result = RecordResult(path=Path("x.pdf"), form=form)
    result.crops = [_crop(0, "front"), _crop(1, "back")]
    result.ocr = [
        OcrResult(text="", words=[], mean_conf=0.0, low_conf_fraction=1.0),
        OcrResult(text="unreadable noise", words=[("noise", 40.0)],
                  mean_conf=40.0, low_conf_fraction=1.0),
    ]
    evaluate(result)
    return result


class FakeVision:
    def __init__(self, result: VisionResult):
        self.result = result
        self.crops_read: list[int] = []

    def read_crop(self, data: bytes, ext: str) -> VisionResult:
        self.crops_read.append(1)
        return self.result


def test_escalation_upgrades_corroborated_record_to_pass():
    record = _record()
    assert record.auto_status == "Needs Review"
    assert record.escalation_reasons
    # Tier A partially read the warning and bottler line on the back
    # label (a typical dense-small-print read): corroborates Tier B's
    # exact transcriptions.
    mangled = CANONICAL_WARNING.replace("Surgeon", "Surge0n").replace(
        "machinery", "rnachinery"
    )
    record.ocr[1] = OcrResult(
        text=f"{mangled}\nB0TTLED BY VIEJ0 T0NEL SA",
        words=[("x", 70.0)], mean_conf=70.0, low_conf_fraction=0.4,
    )
    evaluate(record)
    assert record.warning.status == WarningStatus.NEAR

    fake = FakeVision(VisionResult(
        ok=True, brand_text="Pisco Viejo Tonel", abv_text="Alc. 42% by Vol",
        net_contents_text="750 ml", warning_text=CANONICAL_WARNING,
        full_text="PISCO product of Peru\nBOTTLED BY VIEJO TONEL S.A., ICA",
    ))
    escalate(record, fake)
    assert record.auto_status == "Pass"
    assert record.warning.status == WarningStatus.EXACT
    assert all(v.outcome == Outcome.EXACT for v in record.verdicts)
    # Provenance: the values that resolved the record came from Tier B.
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["brand_name"].source == "vision"
    assert record.warning.source == "vision"


def test_escalation_stops_reading_once_resolved():
    """Every skipped read is seconds off the batch tail: once a record
    re-evaluates to Pass, the remaining candidate crops are not sent."""
    record = _record()
    # Corroborate the warning and bottler line on Tier A so the record
    # can actually pass (uncorroborated vision boilerplate is held in
    # review by design).
    mangled = CANONICAL_WARNING.replace("Surgeon", "Surge0n").replace(
        "machinery", "rnachinery"
    )
    record.ocr[1] = OcrResult(
        text=f"{mangled}\nB0TTLED BY VIEJ0 T0NEL SA",
        words=[("x", 70.0)], mean_conf=70.0, low_conf_fraction=0.4,
    )
    evaluate(record)
    fake = FakeVision(VisionResult(
        ok=True, brand_text="Pisco Viejo Tonel", abv_text="Alc. 42% by Vol",
        net_contents_text="750 ml", warning_text=CANONICAL_WARNING,
        full_text="BOTTLED BY VIEJO TONEL S.A., ICA",
    ))
    escalate(record, fake)
    assert record.auto_status == "Pass"
    assert len(fake.crops_read) == 1, "second crop read despite resolution"


def test_escalation_keeps_reading_while_unresolved():
    """A read that leaves the record in review must not stop the loop."""
    record = _record()  # vision-only warning stays demoted -> never Pass
    fake = FakeVision(VisionResult(
        ok=True, brand_text="Pisco Viejo Tonel", abv_text="Alc. 42% by Vol",
        net_contents_text="750 ml", warning_text=CANONICAL_WARNING,
    ))
    escalate(record, fake)
    assert record.auto_status == "Needs Review"
    assert len(fake.crops_read) == 2, "loop stopped before exhausting crops"


def test_uncorroborated_vision_warning_stays_in_review():
    """The statutory warning is memorized boilerplate a vision model can
    fabricate. If Tier A saw nothing warning-like, a vision-only exact
    must not auto-pass the record."""
    record = _record()
    fake = FakeVision(VisionResult(
        ok=True, brand_text="Pisco Viejo Tonel", abv_text="Alc. 42% by Vol",
        net_contents_text="750 ml", warning_text=CANONICAL_WARNING,
        full_text="PISCO product of Peru\nBOTTLED BY VIEJO TONEL S.A., ICA",
    ))
    escalate(record, fake)
    # Form-vs-label fields upgrade on the model's transcription...
    by_field = {v.field: v for v in record.verdicts}
    for f in ("brand_name", "net_contents", "alcohol_content", "class_type"):
        assert by_field[f].outcome == Outcome.EXACT, f
    # ...but the boilerplate-shaped reads (warning, bottler) are demoted:
    # Tier A saw nothing like them, so vision-only is doubt to review.
    assert record.warning.status == WarningStatus.NEAR
    assert by_field["bottler"].outcome == Outcome.NEAR_MISS
    assert "backup reader" in by_field["bottler"].note
    assert record.auto_status == "Needs Review"
    # The demotion must not lose who read it: vision-only is the story.
    assert record.warning.source == "vision"
    assert by_field["bottler"].source == "vision"


def test_vision_only_numeric_mismatch_demoted_to_review():
    """Qwen2.5-VL misread Cotton Hollow's '750ml' as '500 ml'. A numeric
    value born solely from Tier B — Tier A saw no statement at all — is
    doubt, not evidence: it must land in review, never a Fail."""
    record = _record()
    fake = FakeVision(VisionResult(
        ok=True, brand_text="Viejo Tonel",
        abv_text="Alc. 35% by Vol",  # form says 42
        net_contents_text="500 ml",  # form says 750 MILLILITERS
    ))
    escalate(record, fake)
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["net_contents"].outcome == Outcome.NEAR_MISS
    assert by_field["net_contents"].label_value == "500 ml"  # still shown
    assert by_field["net_contents"].source == "vision"  # demotion keeps source
    assert by_field["alcohol_content"].outcome == Outcome.NEAR_MISS
    assert record.auto_status == "Needs Review"


def test_corroborated_numeric_mismatch_still_fails():
    """When Tier A itself read a volume that contradicts the form, Tier B
    agreeing is corroboration — the mismatch stands."""
    record = _record()
    record.ocr[1] = OcrResult(
        text="500 ml", words=[("x", 90.0)], mean_conf=90.0, low_conf_fraction=0.0
    )
    evaluate(record)
    fake = FakeVision(VisionResult(ok=True, net_contents_text="500 ml"))
    escalate(record, fake)
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["net_contents"].outcome == Outcome.MISMATCH
    # Tier A's own reading wins the pool, so the mismatch is attributed
    # to OCR on the back crop — corroboration, not a vision-only claim.
    assert by_field["net_contents"].source == "ocr"
    assert by_field["net_contents"].source_crop == 1
    assert record.auto_status == "Fail"


def test_escalation_failure_keeps_tier_a_verdicts():
    record = _record()
    before = [v.outcome for v in record.verdicts]
    fake = FakeVision(VisionResult(ok=False, error="timeout"))
    escalate(record, fake)
    # Honest degradation: still Needs Review, never an error or a reject.
    assert record.auto_status == "Needs Review"
    assert [v.outcome for v in record.verdicts] == before


def test_escalation_reads_other_crops_when_warning_or_numeric_open():
    record = _record()
    record.crops.append(_crop(2, "other"))
    record.ocr.append(OcrResult(text="", words=[]))

    fake = FakeVision(VisionResult(ok=True, full_text="nothing useful"))
    escalate(record, fake, max_crops=5)
    # warning + numerics unresolved -> matchable crops AND the 'other' strip
    assert set(record.vision.keys()) == {0, 1, 2}

    # Warning exact and presence checks satisfied -> only matchable crops
    # re-read.
    record2 = _record()
    record2.crops.append(_crop(2, "other"))
    record2.ocr.append(
        OcrResult(
            text=f"{CANONICAL_WARNING}\n750 ml\nAlc. 42% by Vol\n"
            "BOTTLED BY VIEJO TONEL S.A.",
            words=[("x", 95.0)], mean_conf=95.0, low_conf_fraction=0.0,
        )
    )
    evaluate(record2)
    assert record2.warning.status == WarningStatus.EXACT
    fake2 = FakeVision(VisionResult(ok=True, full_text="nothing useful"))
    escalate(record2, fake2, max_crops=5)
    assert set(record2.vision.keys()) == {0, 1}


def test_bottler_missing_is_review_with_reason():
    record = _record()
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["bottler"].outcome == Outcome.MISSING
    assert record.auto_status == "Needs Review"
    assert "bottler: missing" in record.escalation_reasons


def test_origin_check_only_for_imports():
    record = _record()
    assert "country_of_origin" not in {v.field for v in record.verdicts}
    record.form.source = "Imported"
    evaluate(record)
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["country_of_origin"].outcome == Outcome.MISSING
    assert "country_of_origin: missing" in record.escalation_reasons


def test_vision_only_origin_demoted_to_review():
    """ "PRODUCT OF ..." is memorized boilerplate a vision model can
    fabricate; with no Tier A trace of an origin it lands in review."""
    record = _record()
    record.form.source = "Imported"
    evaluate(record)
    fake = FakeVision(VisionResult(ok=True, full_text="PRODUCT OF PERU"))
    escalate(record, fake)
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["country_of_origin"].outcome == Outcome.NEAR_MISS
    assert "backup reader" in by_field["country_of_origin"].note
    assert by_field["country_of_origin"].source == "vision"


def test_corroborated_vision_origin_stands():
    record = _record()
    record.form.source = "Imported"
    # Tier A read a bare country mention; Tier B's anchored statement
    # then counts as corroborated evidence.
    record.ocr[1] = OcrResult(
        text="PISCO ICA PERU", words=[("x", 80.0)],
        mean_conf=80.0, low_conf_fraction=0.1,
    )
    evaluate(record)
    fake = FakeVision(VisionResult(ok=True, full_text="PRODUCT OF PERU"))
    escalate(record, fake)
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["country_of_origin"].outcome == Outcome.EXACT


def test_escalation_reads_other_crops_when_bottler_open():
    """Bottler lines live on strip labels: an unresolved bottler alone
    must widen the candidate crops, like the numeric checks do."""
    record = _record()
    # A producer whose name shares nothing with the brand (the usual
    # case) — the brand text on the back crop must not nudge the bottler
    # check into the near band.
    record.form.applicant = "ANDEAN COASTAL SPIRITS S.A.C.\nCALLE LIMA 123\nICA"
    # Resolve warning + numerics + brand on the back crop, leave bottler.
    record.ocr[1] = OcrResult(
        text=f"{CANONICAL_WARNING}\nPisco Viejo Tonel 750 ml Alc. 42% by Vol",
        words=[("x", 95.0)], mean_conf=95.0, low_conf_fraction=0.0,
    )
    record.crops.append(_crop(2, "other"))
    record.ocr.append(OcrResult(text="", words=[]))
    evaluate(record)
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["bottler"].outcome == Outcome.MISSING
    fake = FakeVision(VisionResult(ok=True, full_text="nothing useful"))
    escalate(record, fake, max_crops=5)
    assert 2 in record.vision, "strip crop not read for the open bottler check"


def test_numeric_fields_match_from_other_crops():
    """ABV/net contents printed on a neck or strip label count as evidence:
    the 47.5% on Black Maple Hill's neck was read at conf 83 and discarded."""
    record = _record()
    record.crops.append(_crop(2, "other"))
    record.ocr.append(
        OcrResult(
            text="Aged in Select White Oak Casks\nAlc 42 % by Vol. 750 ml",
            words=[("x", 83.0)], mean_conf=83.0, low_conf_fraction=0.1,
        )
    )
    evaluate(record)
    by_field = {v.field: v for v in record.verdicts}
    assert by_field["alcohol_content"].outcome == Outcome.EXACT
    assert by_field["net_contents"].outcome == Outcome.EXACT
    # brand still has no front/back evidence: 'other' text never feeds names
    assert by_field["brand_name"].outcome == Outcome.MISSING


def test_container_fallback_attributes_form_source():
    """A volume absent from every label but recorded as blown/branded/
    embossed container wording (form item 15/18) satisfies the check —
    and the verdict says the value came from the form, not a reader."""
    form = ParsedForm(
        ttb_id="x", brand_name="VIEJO TONEL", serial_number="1",
        net_contents="750 MILLILITERS", alcohol_content="42",
        class_type_description="OTHER GRAPE BRANDY (PISCO, GRAPPA) FB",
        has_net_contents_field=True, has_alcohol_content_field=True,
        container_wording="750 ML",
    )
    result = RecordResult(path=Path("x.pdf"), form=form)
    result.crops = [_crop(0, "front")]
    result.ocr = [OcrResult(
        text="Pisco Viejo Tonel Alc. 42% by Vol", words=[("x", 90.0)],
        mean_conf=90.0, low_conf_fraction=0.0,
    )]
    evaluate(result)
    by_field = {v.field: v for v in result.verdicts}
    assert by_field["net_contents"].outcome == Outcome.EXACT
    assert "container" in by_field["net_contents"].note
    assert by_field["net_contents"].source == "form"
    assert by_field["net_contents"].source_crop is None
    # Values actually read off the crop are attributed to OCR.
    assert by_field["alcohol_content"].source == "ocr"
    assert by_field["alcohol_content"].source_crop == 0


def test_crops_are_reencoded_clean_before_sending():
    """The corpus crop that crashed llama-server (Photoshop EXIF + ICC
    profile) must reach the wire as bare re-encoded pixels."""
    import base64
    import io

    import fitz
    from PIL import Image

    from server.pipeline.extract_labels import extract_labels

    pdf = Path(__file__).parent.parent / "sample-forms" / "registry" / "11115001000381.pdf"
    crop = extract_labels(fitz.open(pdf))[0]
    assert b"Photoshop" in crop.data  # the pathological original

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent["b64"] = body["messages"][0]["content"][1]["image_url"]["url"]
        return _completion(
            {"brand_text": None, "abv_text": None, "net_contents_text": None,
             "warning_text": None, "full_text": None}
        )

    result = _client(handler).read_crop(crop.data, crop.ext)
    assert result.ok
    wire_bytes = base64.b64decode(sent["b64"].split(",", 1)[1])
    assert b"Photoshop" not in wire_bytes
    im = Image.open(io.BytesIO(wire_bytes))
    assert im.format == "JPEG" and im.mode == "RGB"
    assert "icc_profile" not in im.info and "exif" not in im.info


def test_undecodable_crop_degrades_cleanly():
    result = _client(lambda r: _completion({})).read_crop(b"not an image", "jpeg")
    assert not result.ok
    assert "not decodable" in result.error
