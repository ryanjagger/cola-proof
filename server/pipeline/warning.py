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

import re
from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

STATUTORY_PREFIX = "GOVERNMENT WARNING:"
STATUTORY_BODY = (
    "(1) According to the Surgeon General, women should not drink "
    "alcoholic beverages during pregnancy because of the risk of birth "
    "defects. (2) Consumption of alcoholic beverages impairs your ability "
    "to drive a car or operate machinery, and may cause health problems."
)

# Body similarity at or above this is "almost the statutory text":
# escalate / review rather than calling it absent.
NEAR_THRESHOLD = 90.0
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


# Most-favorable-first, for picking one result across a record's crops.
_PRECEDENCE = [
    WarningStatus.EXACT,
    WarningStatus.PREFIX_NOT_CAPS,
    WarningStatus.NEAR,
    WarningStatus.MISMATCH,
    WarningStatus.MISSING,
]

_PREFIX_RE = re.compile(r"GOVERNMENT\s+WARNING\s*:?", re.IGNORECASE)


def _normalize(text: str) -> str:
    # Join words hyphenated across line breaks (statutory wording has no
    # hyphenated words, so this is lossless for comparison), then
    # collapse all whitespace.
    text = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def validate_warning(text: str | None) -> WarningResult:
    """Validate one crop's extracted text."""
    if not text or not text.strip():
        return WarningResult(WarningStatus.MISSING, None, 0.0)
    norm = _normalize(text)

    m = _PREFIX_RE.search(norm)
    if not m:
        # No recognizable prefix; the body might still be present
        # (e.g. OCR mangled "GOVERNMENT").
        score = fuzz.partial_ratio(STATUTORY_BODY.casefold(), norm.casefold())
        if score >= NEAR_THRESHOLD:
            return WarningResult(WarningStatus.NEAR, norm, score)
        return WarningResult(WarningStatus.MISSING, None, score)

    prefix_found = m.group(0)
    # Window the candidate body: statutory length plus slack for OCR noise.
    body = norm[m.end() :].strip()[: len(STATUTORY_BODY) + 60]

    body_exact = body.casefold().startswith(STATUTORY_BODY.casefold())
    prefix_caps = prefix_found == STATUTORY_PREFIX
    found = f"{prefix_found} {body}".strip()

    if body_exact and prefix_caps:
        return WarningResult(WarningStatus.EXACT, found, 100.0)
    if body_exact:
        return WarningResult(WarningStatus.PREFIX_NOT_CAPS, found, 100.0)

    score = fuzz.partial_ratio(STATUTORY_BODY.casefold(), body.casefold())
    if score >= NEAR_THRESHOLD:
        return WarningResult(WarningStatus.NEAR, found, score)
    if score >= MISMATCH_THRESHOLD:
        return WarningResult(WarningStatus.MISMATCH, found, score)
    return WarningResult(WarningStatus.MISSING, found, score)


def validate_warning_across(texts: list[str | None]) -> WarningResult:
    """Best result across all of a record's readable crops.

    The warning only has to appear somewhere on the container, so the
    most favorable crop wins.
    """
    results = [validate_warning(t) for t in texts] or [
        WarningResult(WarningStatus.MISSING, None, 0.0)
    ]
    return min(
        results, key=lambda r: (_PRECEDENCE.index(r.status), -r.score)
    )
