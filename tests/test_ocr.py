"""Unit tests for Tier A text assembly from image_to_data rows.

_assemble_text replaced a second Tesseract invocation (image_to_string),
so these pin the parts of its output downstream consumers rely on — in
particular real newlines at line boundaries, which warning.py's
de-hyphenation needs.
"""

from server.pipeline.ocr import _assemble_text
from server.pipeline.warning import (
    STATUTORY_BODY,
    STATUTORY_PREFIX,
    WarningStatus,
    validate_warning,
)


def data_dict(rows):
    """Build an image_to_data DICT from (text, conf, block, par, line)
    rows — the only columns _assemble_text reads."""
    keys = ("text", "conf", "block_num", "par_num", "line_num")
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


def test_same_line_words_join_with_spaces():
    d = data_dict([("ROCKY", 90, 1, 1, 1), ("PEAK", 88, 1, 1, 1)])
    assert _assemble_text(d) == "ROCKY PEAK"


def test_line_par_block_boundaries_emit_newlines():
    # line_num restarts per paragraph and par_num per block, so each
    # row below starts a new line even where the raw numbers repeat.
    d = data_dict([
        ("BRAND", 90, 1, 1, 1),
        ("750", 91, 1, 1, 2),
        ("ML", 92, 1, 2, 1),
        ("12%", 93, 2, 1, 1),
    ])
    assert _assemble_text(d) == "BRAND\n750\nML\n12%"


def test_structural_and_blank_rows_skipped():
    d = data_dict([
        ("", -1, 1, 1, 0),  # page/block/par/line structural row
        ("   ", 95, 1, 1, 1),  # whitespace-only "word"
        ("REAL", "96", 1, 1, 1),  # conf arrives as str in some versions
    ])
    assert _assemble_text(d) == "REAL"


def test_empty_input():
    assert _assemble_text(data_dict([])) == ""


def test_hyphenated_line_wrap_stays_exact_for_warning():
    """A statutory word hyphenated across a line wrap must still validate
    EXACT: warning._normalize joins '-\\n' but not '- ', so assembled
    text has to keep real newlines at line boundaries."""
    words = f"{STATUTORY_PREFIX} {STATUTORY_BODY}".split()
    i = words.index("pregnancy")
    words[i : i + 1] = ["preg-", "nancy"]
    rows = [(w, 90, 1, 1, 1 if j <= i else 2) for j, w in enumerate(words)]
    text = _assemble_text(data_dict(rows))
    assert "preg-\nnancy" in text
    assert validate_warning(text).status == WarningStatus.EXACT
    # The newline is load-bearing: space-joined, the hyphen survives
    # normalization and the wording is no longer letter-exact.
    assert validate_warning(text.replace("\n", " ")).status != WarningStatus.EXACT
