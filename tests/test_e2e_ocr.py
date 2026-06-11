"""End-to-end Tier A check on known-easy samples (spec phase-3).

Slower than the rest of the suite (runs real Tesseract) but kept in the
default run so the OCR path can't silently rot. Sample choice: 12207 is
a non-English import (Pisco, Spanish front label), 13158 a domestic
wine; both auto-Pass on Tier A alone.
"""

from pathlib import Path

import pytest

from server.pipeline.match import Outcome
from server.pipeline.runner import process_pdf
from server.pipeline.warning import WarningStatus

SAMPLES = Path(__file__).parent.parent / "sample-forms"


@pytest.fixture(scope="module")
def pisco():
    return process_pdf(SAMPLES / "12207001000539.pdf", run_ocr=True)


def test_easy_records_pass_tier_a(pisco):
    assert pisco.auto_status == "Pass"
    assert pisco.warning.status == WarningStatus.EXACT


def test_verdicts_cover_all_checked_fields(pisco):
    fields = {v.field for v in pisco.verdicts}
    assert fields == {"brand_name", "net_contents", "alcohol_content", "class_type"}
    assert all(v.outcome == Outcome.EXACT for v in pisco.verdicts)


def test_ocr_results_parallel_to_crops(pisco):
    assert len(pisco.ocr) == len(pisco.crops)
    front = pisco.ocr[0]
    assert front.readable
    assert front.mean_conf > 70
    assert front.elapsed_ms > 0


def test_domestic_wine_passes():
    r = process_pdf(SAMPLES / "13158001000059.pdf", run_ocr=True)
    assert r.auto_status == "Pass"
    assert not r.escalation_reasons


def test_tier_a_sources_attributed(pisco):
    crop_indexes = {c.index for c in pisco.crops}
    for v in pisco.verdicts:
        assert v.source == "ocr", v.field
        assert v.source_crop in crop_indexes, v.field
    assert pisco.warning.source == "ocr"
    assert pisco.warning.source_crop in crop_indexes
