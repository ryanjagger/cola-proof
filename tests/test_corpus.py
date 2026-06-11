"""Corpus invariants over the 30 sample COLA PDFs (spec phase-1 verification).

Every record must parse, every label page must yield caption-classified
crops, and the per-revision field policy must hold. Runs on the real
sample corpus — no fixtures, no mocks.
"""

from pathlib import Path

import pytest

from server.pipeline.runner import process_pdf

SAMPLES = sorted((Path(__file__).parent.parent / "sample-forms" / "registry").glob("*.pdf"))


@pytest.fixture(scope="session")
def results():
    assert len(SAMPLES) == 30, "expected the 30-PDF sample corpus"
    return {p.name: process_pdf(p) for p in SAMPLES}


def test_every_record_parses_without_errors(results):
    errors = {name: r.errors for name, r in results.items() if not r.ok}
    assert not errors


def test_required_fields_present(results):
    for name, r in results.items():
        f = r.form
        assert f.ttb_id == name.removesuffix(".pdf"), name
        assert f.brand_name, name
        assert f.serial_number, name
        assert f.revision, name
        assert f.product_type in ("WINE", "DISTILLED SPIRITS", "MALT BEVERAGE"), name
        assert f.status, name
        assert f.class_type_description, name
        assert not f.warnings, (name, f.warnings)


def test_per_revision_field_policy(results):
    for name, r in results.items():
        f = r.form
        if f.revision == "06-2016":
            # 06-2016 dropped the typed fields: ABV/net-contents become
            # label-format checks downstream, never form-vs-label matches.
            assert not f.has_net_contents_field, name
            assert not f.has_alcohol_content_field, name
        else:
            assert f.has_net_contents_field, name
            assert f.has_alcohol_content_field, name
            assert f.net_contents, name
            assert f.alcohol_content, name


def test_crops_classified_and_plausible(results):
    for name, r in results.items():
        assert r.crops, name
        kinds = [c.kind for c in r.crops]
        # A record can carry multiple front crops (17352 submits the
        # front label in two pieces, both captioned Brand (front)).
        assert kinds.count("front") >= 1, (name, kinds)
        for c in r.crops:
            assert c.kind in ("front", "back", "other"), name
            # 29/30 records embed JPEGs; 14323's back label is a PNG.
            assert c.ext in ("jpeg", "png"), (name, c.ext)
            assert c.px_width > 30 and c.px_height > 30, name
            assert 50 <= c.dpi <= 600, (name, c.dpi)


def test_corpus_crop_total(results):
    # Verified by hand against the sample corpus; a change here means
    # caption pairing or banner exclusion regressed.
    assert sum(len(r.crops) for r in results.values()) == 72
