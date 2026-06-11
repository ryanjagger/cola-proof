"""Corpus invariants over the 10 application-shape sample PDFs (the bare
04/2023 fillable form, as opposed to the registry print view).

Parse/extract invariants run on the real corpus like test_corpus does for
the registry shape. The photo-escalation policy (a photograph of the
containers always goes to Tier B and never auto-passes on Tier A alone)
is checked with stubbed OCR/vision results so the suite stays fast.
"""

from pathlib import Path

import pytest

from server.pipeline.ocr import OcrResult
from server.pipeline.runner import evaluate, process_pdf
from server.pipeline.vision import VisionResult
from server.pipeline.warning import STATUTORY_BODY, STATUTORY_PREFIX

SAMPLES = sorted(
    (Path(__file__).parent.parent / "sample-forms" / "applications").glob("*.pdf")
)

# Files whose labels are affixed as a single photograph of the containers.
PHOTO = {
    "TTB_F_5100-31_13_spirits_irongate_photo3.pdf",
    "TTB_F_5100-31_15_beer_northpine_blackwater.pdf",
    "TTB_F_5100-31_16_wine_laurelhills_pinot.pdf",
    "TTB_F_5100-31_17_spirits_mariner_gin.pdf",
}


@pytest.fixture(scope="session")
def results():
    assert len(SAMPLES) == 10, "expected the 10-PDF application corpus"
    return {p.name: process_pdf(p) for p in SAMPLES}


def test_every_record_parses_without_errors(results):
    errors = {name: r.errors for name, r in results.items() if not r.ok}
    assert not errors


def test_required_fields_present(results):
    for name, r in results.items():
        f = r.form
        assert f.shape == "application", name
        assert f.revision == "04/2023", name
        assert f.brand_name, name
        assert f.serial_number, name
        assert f.product_type in ("WINE", "DISTILLED SPIRITS", "MALT BEVERAGE"), name
        assert f.source in ("Domestic", "Imported"), name
        assert not f.warnings, (name, f.warnings)


def test_no_approval_only_fields(results):
    """Applications haven't been decided: no TTB ID, no FOR TTB USE ONLY
    values, and no typed net-contents/ABV items — so those two checks must
    run as label-format checks downstream, exactly like 06-2016 records."""
    for name, r in results.items():
        f = r.form
        assert f.ttb_id is None, name
        assert f.status is None, name
        assert f.class_type_description is None, name
        assert not f.has_net_contents_field, name
        assert not f.has_alcohol_content_field, name


def test_label_crops_classified(results):
    for name, r in results.items():
        kinds = [c.kind for c in r.crops]
        if name in PHOTO:
            assert kinds == ["photo"], (name, kinds)
            assert r.crops[0].matchable, name
        else:
            assert "front" in kinds and "back" in kinds, (name, kinds)
            assert "photo" not in kinds, (name, kinds)
        for c in r.crops:
            assert c.dpi > 0, (name, c.index)
            assert c.aspect_ok, (name, c.index)


def test_pinned_fields():
    r = process_pdf(SAMPLES[0])  # 01_beer_cascadia
    f = r.form
    assert f.brand_name == "CASCADIA"
    assert f.fanciful_name == "TIMBERLINE HAZE"
    assert f.serial_number == "260042"
    assert f.product_type == "MALT BEVERAGE"
    assert f.plant_registry == "BR-OR-CAS-15022"
    assert f.container_wording == "NONE"
    assert f.application_date == "05/18/2026"


def _perfect_read(record):
    """Text a flawless reader would produce for the record's labels."""
    return (
        f"{record.form.brand_name}\n12 FL. OZ.\n5.0% ALC/VOL\n"
        f"{STATUTORY_PREFIX} {STATUTORY_BODY}"
    )


def _perfect_ocr(record):
    text = _perfect_read(record)
    return OcrResult(
        text=text,
        words=[(w, 95.0) for w in text.split()],
        mean_conf=95.0,
        low_conf_fraction=0.0,
    )


@pytest.fixture()
def photo_record():
    path = next(p for p in SAMPLES if p.name in PHOTO)
    record = process_pdf(path)
    assert [c.kind for c in record.crops] == ["photo"]
    return record


def test_photo_never_passes_on_tier_a_alone(photo_record):
    """Even a perfect Tier A read of a container photograph stays in
    review and escalates: photos always get the backup reader."""
    photo_record.ocr = [_perfect_ocr(photo_record)]
    evaluate(photo_record)
    assert photo_record.auto_status == "Needs Review"
    assert any("photograph" in r for r in photo_record.escalation_reasons)


def test_photo_passes_once_vision_confirms(photo_record):
    """The cap lifts when Tier B has actually read the photo."""
    photo_record.ocr = [_perfect_ocr(photo_record)]
    photo_record.vision = {
        0: VisionResult(ok=True, full_text=_perfect_read(photo_record))
    }
    evaluate(photo_record)
    assert photo_record.auto_status == "Pass"
