"""Deterministic GOVERNMENT WARNING validator.

Pure string/format checking of the health warning statement required by
27 CFR part 16 (Alcoholic Beverage Labeling Act). This module must stay
deterministic regardless of which extraction tier produced the text: a
pass requires the exact statutory wording with an all-caps
"GOVERNMENT WARNING:" prefix, after whitespace/line-break normalization
only. No LLM, no fuzzy judgment ever decides a pass — fuzzy scores are
used solely to distinguish "almost matches" (escalate / needs review)
from "absent", because a false reject is worse than a slow review.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

from .match import SourcedText

STATUTORY_PREFIX = "GOVERNMENT WARNING:"
STATUTORY_BODY = (
    "(1) According to the Surgeon General, women should not drink "
    "alcoholic beverages during pregnancy because of the risk of birth "
    "defects. (2) Consumption of alcoholic beverages impairs your ability "
    "to drive a car or operate machinery, and may cause health problems."
)

# Body similarity at or above this is "almost the statutory text":
# escalate / review rather than calling it absent. The band is wide
# because OCR noise on dense small print routinely costs 10+ points and
# a warning that is present-but-misread must escalate, not fail.
NEAR_THRESHOLD = 80.0
MISMATCH_THRESHOLD = 55.0


class WarningStatus(str, Enum):
    EXACT = "exact"  # statutory wording, all-caps prefix
    PREFIX_NOT_CAPS = "prefix_not_caps"  # right wording, prefix not all caps
    NEAR = "near"  # almost matches -> escalate, never auto-fail
    MISMATCH = "mismatch"  # warning-like text, substantially wrong
    MISSING = "missing"  # no warning found


@dataclass
class WarningResult:
    status: WarningStatus
    found_text: str | None  # normalized text we judged, for the UI
    score: float  # body similarity 0-100
    note: str | None = None  # plain-language pointer at what differs
    source: str | None = None  # which reader produced found_text
    source_crop: int | None = None


# Most-favorable-first, for picking one result across a record's crops.
_PRECEDENCE = [
    WarningStatus.EXACT,
    WarningStatus.PREFIX_NOT_CAPS,
    WarningStatus.NEAR,
    WarningStatus.MISMATCH,
    WarningStatus.MISSING,
]


def _spaced(word: str) -> str:
    return r"\s*".join(word)


# Tolerates whitespace inside the words: OCR drops spaces and
# schema-constrained VLM output letter-spaces ("G O V E R N M E N T ...").
# The caps requirement is enforced separately on the matched text.
_PREFIX_RE = re.compile(
    _spaced("GOVERNMENT") + r"\s*" + _spaced("WARNING") + r"\s*:?",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    # Join words hyphenated across line breaks (statutory wording has no
    # hyphenated words, so this is lossless for comparison), then
    # collapse all whitespace.
    text = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _squash(text: str) -> str:
    """Remove all whitespace — the spec allows whitespace/line-break
    normalization only, and this is its strongest form. Makes the exact
    check immune to line wrapping and letter-spaced transcriptions."""
    return re.sub(r"\s+", "", text)


_SQ_BODY = _squash(STATUTORY_BODY)
_SQ_PREFIX = _squash(STATUTORY_PREFIX)  # "GOVERNMENTWARNING:"

_WS = re.compile(r"\s")


def _unsquash_index(spaced: str, sq_count: int) -> int:
    """Index into `spaced` just past its first `sq_count` non-whitespace
    characters — maps a position in squashed text back to readable text."""
    if sq_count <= 0:
        return 0
    seen = 0
    for i, ch in enumerate(spaced):
        if not _WS.match(ch):
            seen += 1
            if seen == sq_count:
                return i + 1
    return len(spaced)


def _trim_to_match(spaced: str, spaced_sq: str) -> str:
    """Cut readable text down to the region that aligns with the statutory
    body. OCR keeps reading past the warning into addresses, URLs, and
    barcode noise; the UI should show the warning, not the neighborhood."""
    a = fuzz.partial_ratio_alignment(_SQ_BODY.casefold(), spaced_sq.casefold())
    if a is None or a.dest_end <= a.dest_start:
        return spaced
    start = _unsquash_index(spaced, a.dest_start)
    end = _unsquash_index(spaced, a.dest_end)
    return spaced[start:end].strip()


def _context(words: list[str], start: int, end: int) -> str:
    """The differing words plus a little surrounding context, quoted."""
    lo = max(0, start - 3)
    hi = min(len(words), max(end, start) + 3)
    pre = "…" if lo > 0 else ""
    post = "…" if hi < len(words) else ""
    return f'"{pre}{" ".join(words[lo:hi])}{post}"'


def _first_difference(found_body: str) -> str | None:
    """Where the found body first departs from the statutory wording —
    a word-level, case-insensitive diff, described in plain language so
    the agent knows what to squint at on the label. Deterministic; only
    ever annotates a non-exact result, never decides one."""
    exp, got = STATUTORY_BODY.split(), found_body.split()
    matcher = difflib.SequenceMatcher(
        a=[w.casefold() for w in exp],
        b=[w.casefold() for w in got],
        autojunk=False,
    )
    diffs = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    if not diffs:
        return None
    _, i1, i2, j1, j2 = diffs[0]
    note = (
        f"The label reads {_context(got, j1, j2)}, but the required "
        f"wording is {_context(exp, i1, i2)}"
    )
    if len(diffs) > 1:
        more = len(diffs) - 1
        note += f", plus {more} more difference{'s' if more > 1 else ''} after that."
    return note


def validate_warning(text: str | None) -> WarningResult:
    """Validate one crop's extracted text."""
    if not text or not text.strip():
        return WarningResult(WarningStatus.MISSING, None, 0.0)
    norm = _normalize(text)

    m = _PREFIX_RE.search(norm)
    if not m:
        # No recognizable prefix; the body might still be present
        # (e.g. OCR mangled "GOVERNMENT").
        score = fuzz.partial_ratio(_SQ_BODY.casefold(), _squash(norm).casefold())
        if score >= NEAR_THRESHOLD:
            shown = _trim_to_match(norm, _squash(norm))
            return WarningResult(
                WarningStatus.NEAR, shown, score, note=_first_difference(shown)
            )
        return WarningResult(WarningStatus.MISSING, None, score)

    prefix_found = m.group(0)
    # Window the candidate body: statutory length plus slack for OCR noise
    # and stray tokens between the prefix and the body. Comparison runs
    # whitespace-free; the displayed text stays human-readable.
    body = norm[m.end() :].strip()[: len(STATUTORY_BODY) + 120]
    body_sq = _squash(norm[m.end() :])[: len(_SQ_BODY) + 120]

    body_exact = _SQ_BODY.casefold() in body_sq.casefold()
    prefix_caps = _squash(prefix_found) == _SQ_PREFIX

    if body_exact:
        end_sq = body_sq.casefold().find(_SQ_BODY.casefold()) + len(_SQ_BODY)
        body = body[: _unsquash_index(body, end_sq)].strip()
        found = f"{prefix_found} {body}".strip()
        if prefix_caps:
            return WarningResult(WarningStatus.EXACT, found, 100.0)
        return WarningResult(WarningStatus.PREFIX_NOT_CAPS, found, 100.0)

    a = fuzz.partial_ratio_alignment(_SQ_BODY.casefold(), body_sq.casefold())
    score = a.score if a else 0.0
    if a and a.dest_end > a.dest_start:
        body = body[: _unsquash_index(body, a.dest_end)].strip()
    found = f"{prefix_found} {body}".strip()
    if score >= NEAR_THRESHOLD:
        return WarningResult(
            WarningStatus.NEAR, found, score, note=_first_difference(body)
        )
    if score >= MISMATCH_THRESHOLD:
        return WarningResult(
            WarningStatus.MISMATCH, found, score, note=_first_difference(body)
        )
    return WarningResult(WarningStatus.MISSING, found, score)


def validate_warning_across(texts) -> WarningResult:
    """Best result across all of a record's readable crops.

    The warning only has to appear somewhere on the container, so the
    most favorable crop wins. Texts may be plain strings or SourcedText;
    the winner's source is recorded on the result (provenance only — the
    pick logic and every pass/fail decision are unchanged).
    """
    sourced = [
        t if isinstance(t, SourcedText) else SourcedText(t or "") for t in texts
    ]
    pairs = [(validate_warning(st.text), st) for st in sourced] or [
        (WarningResult(WarningStatus.MISSING, None, 0.0), SourcedText(""))
    ]
    best, src = min(
        pairs, key=lambda p: (_PRECEDENCE.index(p[0].status), -p[0].score)
    )
    if best.found_text is not None:
        best.source = src.source
        best.source_crop = src.crop_index
    return best
