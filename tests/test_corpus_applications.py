"""Corpus invariants over the 18 application-shape sample PDFs (the bare
04/2023 fillable form, as opposed to the registry print view): seven with
label artwork affixed under typed captions (01-08), seven where the
captioned images are photographs of the physical labels (11-17, including
four irongate variants), and four where the labels arrive as one
uncaptioned photograph of the labels laid out together (21-24, "-single").

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
    "TTB_F_5100-31_21_spirits_irongate_photo-single.pdf",
    "TTB_F_5100-31_22_beer_northpine_blackwater-single.pdf",
    "TTB_F_5100-31_23_wine_laurelhills_pinot-single.pdf",
    "TTB_F_5100-31_24_spirits_mariner_gin-single.pdf",
}


@pytest.fixture(scope="session")
def results():
    assert len(SAMPLES) == 18, "expected the 18-PDF application corpus"
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
        assert f.applicant, name
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
            # Captioned records always carry front and back. One sample
            # (16_pinot) additionally affixes its neck strip as an
            # uncaptioned photo, so extra "photo" crops are legitimate.
            assert "front" in kinds and "back" in kinds, (name, kinds)
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
    bottler_line = record.form.applicant.splitlines()[0]
    return (
        f"{record.form.brand_name}\n12 FL. OZ.\n5.0% ALC/VOL\n"
        f"BOTTLED BY {bottler_line}\n"
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
    assert any("photo of the containers" in r for r in photo_record.escalation_reasons)


def test_brand_spelling_conflict_goes_to_review():
    """06_graniteharbor (real Tier A run): the brand display on both
    labels spells HARBOUR while the form — and the back label's
    company-name boilerplate — spell HARBOR. The incidental exact in the
    boilerplate must not mask the display spelling; the record reviews."""
    path = next(p for p in SAMPLES if "graniteharbor" in p.name)
    record = process_pdf(path, run_ocr=True)
    brand = next(v for v in record.verdicts if v.field == "brand_name")
    assert brand.outcome.value == "near_miss", (brand.outcome, brand.label_value)
    assert "harbour" in (brand.label_value or ""), brand.label_value
    assert record.auto_status == "Needs Review"
    assert any("brand_name" in r for r in record.escalation_reasons)
    # The highlight box must point at the divergent display spelling in
    # the top half of the front label, not the boilerplate small print.
    assert brand.source_crop == 0
    assert brand.box is not None and brand.box[3] < 0.5, brand.box


def test_photo_passes_once_vision_confirms(photo_record):
    """The cap lifts when Tier B has actually read the photo."""
    photo_record.ocr = [_perfect_ocr(photo_record)]
    photo_record.vision = {
        0: VisionResult(ok=True, full_text=_perfect_read(photo_record))
    }
    evaluate(photo_record)
    assert photo_record.auto_status == "Pass"
