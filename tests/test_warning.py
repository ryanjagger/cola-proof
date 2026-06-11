"""Table-driven tests for the deterministic GOVERNMENT WARNING validator."""

import pytest

from server.pipeline.warning import (
    STATUTORY_BODY,
    STATUTORY_PREFIX,
    WarningStatus,
    validate_warning,
    validate_warning_across,
)

CANONICAL = f"{STATUTORY_PREFIX} {STATUTORY_BODY}"


def test_exact():
    r = validate_warning(CANONICAL)
    assert r.status == WarningStatus.EXACT
    assert r.score == 100.0


def test_exact_when_letter_spaced():
    """Schema-constrained VLM output letter-spaces transcriptions
    ("G O V E R N M E N T ..."); whitespace normalization must make a
    letter-perfect transcription EXACT."""
    spaced = " ".join(CANONICAL)
    assert validate_warning(spaced).status == WarningStatus.EXACT


def test_letter_spaced_with_wrong_word_is_not_exact():
    body = STATUTORY_BODY.replace("SHOULD", "SHOULO").replace("should", "shoulo")
    spaced = " ".join(f"{STATUTORY_PREFIX} {body}")
    assert validate_warning(spaced).status != WarningStatus.EXACT


def test_letter_spaced_lowercase_prefix_fails_format():
    spaced = " ".join(f"Government Warning: {STATUTORY_BODY}")
    assert validate_warning(spaced).status == WarningStatus.PREFIX_NOT_CAPS


def test_exact_with_line_breaks_and_hyphenation():
    wrapped = CANONICAL.replace("machinery", "machin-\nery").replace(
        "beverages during", "beverages\nduring"
    )
    assert validate_warning(wrapped).status == WarningStatus.EXACT


def test_exact_embedded_in_other_label_text():
    text = f"750 ML  PRODUCT OF PERU\n{CANONICAL}\nBOTTLED BY ..."
    assert validate_warning(text).status == WarningStatus.EXACT


def test_exact_when_body_is_all_caps():
    # Regulation fixes the wording; body case isn't part of the check.
    assert validate_warning(
        f"{STATUTORY_PREFIX} {STATUTORY_BODY.upper()}"
    ).status == WarningStatus.EXACT


def test_prefix_not_all_caps_fails_format():
    r = validate_warning(f"Government Warning: {STATUTORY_BODY}")
    assert r.status == WarningStatus.PREFIX_NOT_CAPS


def test_prefix_missing_colon_is_not_exact():
    r = validate_warning(f"GOVERNMENT WARNING {STATUTORY_BODY}")
    assert r.status != WarningStatus.EXACT


def test_near_single_word_wrong():
    # "should not" dropped -> almost matches -> escalate, never auto-fail
    body = STATUTORY_BODY.replace("women should not drink", "women should drink")
    r = validate_warning(f"{STATUTORY_PREFIX} {body}")
    assert r.status == WarningStatus.NEAR
    assert r.score >= 90


def test_near_ocr_noise():
    noisy = CANONICAL.replace("Surgeon General", "Surge0n Generai").replace(
        "machinery", "rnachinery"
    )
    assert validate_warning(noisy).status == WarningStatus.NEAR


def test_mismatch_truncated_body():
    r = validate_warning(f"{STATUTORY_PREFIX} (1) According to the Surgeon General.")
    assert r.status in (WarningStatus.MISMATCH, WarningStatus.NEAR)
    assert r.status != WarningStatus.EXACT


def test_missing():
    assert validate_warning("ESTATE BOTTLED 750ML").status == WarningStatus.MISSING
    assert validate_warning("").status == WarningStatus.MISSING
    assert validate_warning(None).status == WarningStatus.MISSING


def test_found_text_trimmed_to_warning():
    """OCR keeps reading past the warning into addresses and barcode
    noise; the displayed text must stop where the statutory body ends."""
    r = validate_warning(f"{CANONICAL} Bottled by Cascade Winery Grand Rapids, MI 49546")
    assert r.status == WarningStatus.EXACT
    assert r.found_text.endswith("health problems.")
    assert "Cascade" not in r.found_text
    assert r.note is None


def test_near_trims_junk_and_notes_missing_period():
    """The Cascade Winery case: body verbatim minus the final period,
    followed by unrelated label text. Near (deterministic exactness),
    junk trimmed, note points at the period."""
    text = (
        f"{STATUTORY_PREFIX} {STATUTORY_BODY[:-1].upper()} "
        "Cascade Winery Grand Rapids, MI 49546 > Q TD 2 * ® O [= © = Le)"
    )
    r = validate_warning(text)
    assert r.status == WarningStatus.NEAR
    assert "Winery" not in r.found_text
    assert "HEALTH PROBLEMS" in r.found_text
    assert '"…may cause health problems."' in r.note


def test_near_note_counts_further_differences():
    """The Single Cask Nation case: two commas missing — the note shows
    the first difference and says another follows."""
    body = STATUTORY_BODY.replace("General,", "General").replace(
        "machinery,", "machinery"
    )
    r = validate_warning(f"{STATUTORY_PREFIX} {body} JOIN US AT: www.example.com")
    assert r.status == WarningStatus.NEAR
    assert "JOIN US" not in r.found_text
    assert '"…to the Surgeon General, women should not…"' in r.note
    assert "plus 1 more difference after that" in r.note


def test_across_crops_most_favorable_wins():
    r = validate_warning_across(["front label text only", CANONICAL])
    assert r.status == WarningStatus.EXACT


def test_across_crops_all_missing():
    r = validate_warning_across(["front", None, ""])
    assert r.status == WarningStatus.MISSING


def test_across_no_crops():
    assert validate_warning_across([]).status == WarningStatus.MISSING


@pytest.mark.parametrize(
    "mutation,max_status",
    [
        # statutory wording deviations the validator must never call EXACT
        (lambda s: s.replace("birth defects", "health defects"), WarningStatus.NEAR),
        (lambda s: s.replace("(2)", ""), WarningStatus.NEAR),
        (lambda s: s.replace("impairs", "impair"), WarningStatus.NEAR),
    ],
)
def test_wording_deviations_never_exact(mutation, max_status):
    r = validate_warning(f"{STATUTORY_PREFIX} {mutation(STATUTORY_BODY)}")
    assert r.status != WarningStatus.EXACT
