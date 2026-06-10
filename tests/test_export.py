"""Export tests (spec phase-7): CSV columns, scope filtering, PDF bytes."""

from pathlib import Path

import pytest

from server.export import batch_csv, batch_pdf, filter_scope, record_pdf


def _record(
    rid="r1",
    auto_status="Needs Review",
    disposition=None,
    state="done",
    by=None,
):
    return {
        "id": rid,
        "batch_id": "b1",
        "filename": f"{rid}.pdf",
        "state": state,
        "error": None,
        "ttb_id": "12345678901234",
        "auto_status": auto_status,
        "disposition": disposition,
        "dispositioned_by": by,
        "dispositioned_at": "2026-06-10T00:00:00+00:00" if by else None,
        "note": "looks fine" if by else None,
        "form": {
            "brand_name": "OLD CARTER",
            "fanciful_name": None,
            "class_type_description": "STRAIGHT RYE WHISKY",
            "product_type": "DISTILLED SPIRITS",
            "source": "Domestic",
            "revision": "06-2016",
        },
        "crops": [],
        "verdicts": [
            {"field": "brand_name", "form_value": "OLD CARTER",
             "label_value": "old carter", "outcome": "exact",
             "score": 100.0, "normalized": True, "note": None},
            {"field": "net_contents", "form_value": None, "label_value": None,
             "outcome": "missing", "score": None, "normalized": False,
             "note": "not on form — format check only"},
        ],
        "warning": {"status": "near", "found_text": "GOVERNMENT WARNING: ...", "score": 92.0},
        "escalation": ["net_contents: missing"],
    }


def test_csv_keeps_audit_split():
    csv_text = batch_csv([_record(disposition="Approved", by="agent.k")])
    header, row = csv_text.strip().splitlines()
    assert "auto_status" in header and "disposition" in header
    assert "Needs Review" in row and "Approved" in row and "agent.k" in row
    assert "matches" in row  # brand verdict
    assert "not found on label" in row  # net contents verdict
    assert "almost matches" in row  # warning
    assert "yes" in row  # escalated


def test_csv_handles_error_records():
    r = _record(state="error", auto_status=None)
    r["form"] = None
    r["verdicts"] = None
    r["warning"] = None
    r["error"] = "cannot open PDF"
    csv_text = batch_csv([r])
    assert "cannot open PDF" in csv_text


def test_scope_filtering():
    records = [
        _record("r1", auto_status="Pass", disposition="Approved", by="system"),
        _record("r2", auto_status="Fail"),
        _record("r3", auto_status="Needs Review"),
        _record("r4", auto_status=None, state="error"),
    ]
    assert len(filter_scope(records, "all")) == 4
    assert {r["id"] for r in filter_scope(records, "open")} == {"r2", "r3"}
    assert {r["id"] for r in filter_scope(records, "failed")} == {"r2", "r4"}
    assert {r["id"] for r in filter_scope(records, "review")} == {"r3"}
    assert {r["id"] for r in filter_scope(records, "passed")} == {"r1"}
    with pytest.raises(ValueError):
        filter_scope(records, "everything")


def test_batch_pdf_builds(tmp_path: Path):
    summary = {
        "total": 2, "processed": 2, "failed": 1, "needs_review": 1,
        "passed": 0, "errors": 0, "open": 2, "complete": False,
    }
    pdf = batch_pdf([_record("r1"), _record("r2", auto_status="Fail")],
                    summary, tmp_path, scope="all")
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 2000


def test_record_pdf_embeds_crop(tmp_path: Path):
    record = _record()
    crop_dir = tmp_path / "b1" / "r1"
    crop_dir.mkdir(parents=True)
    # tiny valid JPEG via Pillow
    from PIL import Image as PILImage

    PILImage.new("RGB", (60, 40), "navy").save(crop_dir / "0_front.jpeg")
    record["crops"] = [
        {"index": 0, "kind": "front", "filename": "0_front.jpeg",
         "px_width": 60, "px_height": 40},
    ]
    pdf = record_pdf(record, tmp_path)
    assert pdf.startswith(b"%PDF")
    # embedded image makes it notably larger than the text-only version
    assert len(pdf) > len(record_pdf(_record(), tmp_path))
