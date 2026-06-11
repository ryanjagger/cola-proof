"""Field-aware normalization and three-valued matching.

Each form field is compared against label-extracted text with a
field-specific normalizer (unit canonicalization for net contents,
numeric-% extraction for ABV, case/punct/accent folding for brand,
description-term mapping for class/type). Outcomes are three-valued:

    EXACT      — matches after normalization
    NEAR_MISS  — almost matches -> human review
    MISMATCH   — substantially different -> recommended fail
    MISSING    — expected on the label but not found -> review

Differences that normalization removes (casing, punctuation, accents,
unit spelling) count as EXACT with normalized=True so the UI can tag
"(normalized)". Fuzzy thresholds: token ratio >= 97 is exact, 85-97 is a
near-miss, below is a mismatch.

On 06-2016 forms net contents and ABV have no typed field; for those the
format_check_* functions verify the value is present and plausible on
the label instead of comparing form vs label.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

EXACT_THRESHOLD = 97.0
NEAR_THRESHOLD = 85.0


class Outcome(str, Enum):
    EXACT = "exact"
    NEAR_MISS = "near_miss"
    MISMATCH = "mismatch"
    MISSING = "missing"


@dataclass(frozen=True)
class SourcedText:
    """A label text plus where it came from, so a verdict can say which
    reader produced the value it matched. Matchers also accept plain
    strings (source unknown) — tests and ad-hoc callers stay simple."""

    text: str
    source: str | None = None  # "ocr" | "vision" | "form"
    crop_index: int | None = None


def _sourced(texts) -> list[SourcedText]:
    return [t if isinstance(t, SourcedText) else SourcedText(t or "") for t in texts]


@dataclass
class Verdict:
    field: str
    form_value: str | None
    label_value: str | None  # what was found on the label
    outcome: Outcome
    score: float | None = None
    normalized: bool = False  # comparison used normalized forms
    note: str | None = None
    source: str | None = None  # which reader produced label_value
    source_crop: int | None = None


def normalize_text(s: str) -> str:
    """Case/punctuation/accent-insensitive canonical form."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


# --- brand / fanciful name ------------------------------------------------


def match_name(field: str, form_value: str, label_texts: list[str]) -> Verdict:
    """Match a name (brand or fanciful) against crop texts.

    The label side is free text, so the comparison looks for the best
    window of the crop text rather than whole-string equality.
    """
    needle = normalize_text(form_value)
    if not needle:
        return Verdict(field, form_value, None, Outcome.MISSING, note="empty form value")
    sourced = _sourced(label_texts)
    best_score, best_text, best_src, scattered = 0.0, None, None, False
    for st in sourced:
        hay = normalize_text(st.text)
        if not hay:
            continue
        a = fuzz.partial_ratio_alignment(needle, hay)
        if a is not None and a.score > best_score:
            best_score = a.score
            best_text = hay[a.dest_start : a.dest_end].strip()
            best_src = st
            scattered = False
        # Names are often split across visual lines ("TOMMYROTTER" ...
        # "DISTILLERY") so a contiguous window under-scores them;
        # token_set_ratio scores the words regardless of adjacency.
        ts = fuzz.token_set_ratio(needle, hay)
        if ts > best_score:
            best_score = ts
            best_src = st  # best_text may lag a prior window; source follows the score
            scattered = True
    # "(normalized)" is shown only when normalization did real work: the
    # form string never appears verbatim on the label.
    normalized = not any(form_value in st.text for st in sourced)
    note = "words found non-adjacent on label" if scattered else None
    src = best_src.source if best_src else None
    src_crop = best_src.crop_index if best_src else None
    if best_score >= EXACT_THRESHOLD:
        if best_text and not scattered:
            conflict = _conflicting_spelling(needle, sourced, best_text)
            if conflict:
                c_score, c_window, c_st = conflict
                return Verdict(
                    field, form_value, c_window, Outcome.NEAR_MISS, c_score,
                    normalized,
                    note=f'the label also shows "{best_text}" — '
                    "two spellings of this name on the label",
                    source=c_st.source, source_crop=c_st.crop_index,
                )
        return Verdict(field, form_value, best_text, Outcome.EXACT, best_score,
                       normalized, note, src, src_crop)
    if best_score >= NEAR_THRESHOLD:
        return Verdict(field, form_value, best_text, Outcome.NEAR_MISS, best_score,
                       normalized, note, src, src_crop)
    # Below the near band a fuzzy name score is noise: "different brand"
    # and "unreadable label" are indistinguishable, so a name never
    # hard-fails on OCR — it reads as not-found and goes to review.
    return Verdict(field, form_value, None, Outcome.MISSING, best_score)


def _conflicting_spelling(
    needle: str, sourced: list[SourcedText], exact_text: str
) -> tuple[float, str, SourcedText] | None:
    """The form's spelling can appear on a label incidentally — the
    company-name boilerplate ("BREWED AND CANNED BY GRANITE HARBOR
    BREWING CO.") routinely repeats it — while the brand display itself
    is spelled differently (GRANITE HARBOUR). An exact window therefore
    isn't proof by itself: mask it out and look again. If any crop still
    holds a near-but-not-exact variant, the label carries two spellings
    of the name, and that is doubt to review, not a pass."""
    best = None
    flat_needle = needle.replace(" ", "")
    for st in sourced:
        hay = normalize_text(st.text)
        if not hay:
            continue
        masked = hay.replace(exact_text, " ")
        a = fuzz.partial_ratio_alignment(needle, masked)
        if a is None:
            continue
        # The optimal alignment window can stop mid-word ("granite harbo"
        # against HARBOUR); widen to word boundaries before showing it.
        start, end = a.dest_start, a.dest_end
        while start > 0 and masked[start - 1] != " ":
            start -= 1
        while end < len(masked) and masked[end] != " ":
            end += 1
        window = re.sub(r"\s+", " ", masked[start:end]).strip()
        if not window or not (NEAR_THRESHOLD <= a.score < EXACT_THRESHOLD):
            continue
        # Same letters in a different layout are not a different spelling:
        # script fonts OCR with words joined ("piscoviejotonel") and URLs
        # strip spaces ("3steveswinery com") — only a genuinely different
        # letter sequence (HARBOUR vs HARBOR) is a conflict.
        flat_window = window.replace(" ", "")
        if flat_needle in flat_window or flat_window in flat_needle:
            continue
        if best is None or a.score > best[0]:
            best = (a.score, window, st)
    return best


# --- net contents ---------------------------------------------------------

_UNIT_ML = {
    "ml": 1.0,
    "milliliter": 1.0,
    "milliliters": 1.0,
    "millilitre": 1.0,
    "millilitres": 1.0,
    "cl": 10.0,
    "centiliter": 10.0,
    "centiliters": 10.0,
    "l": 1000.0,
    "liter": 1000.0,
    "liters": 1000.0,
    "litre": 1000.0,
    "litres": 1000.0,
    "fl oz": 29.5735,
    "fluid ounce": 29.5735,
    "fluid ounces": 29.5735,
    "gallon": 3785.41,
    "gallons": 3785.41,
    "barrel": 117348.0,
    "barrels": 117348.0,
}

_NET_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(?:u\.?\s*s\.?\s+)?"  # keg collars print "5.17 U.S. GALLONS"
    r"(milliliters?|millilitres?|centiliters?|liters?|litres?|"
    r"fl\.?\s*oz|fluid\s+ounces?|gallons?|barrels?|ml|cl|l)\b",
    re.IGNORECASE,
)


def parse_net_contents_all(text: str) -> list[tuple[float, str]]:
    """All volume statements in the text -> [(milliliters, matched text)]."""
    out = []
    for m in _NET_RE.finditer(text):
        qty = float(m.group(1).replace(",", "."))
        unit = re.sub(r"\s+", " ", m.group(2).lower().replace(".", "").strip())
        factor = _UNIT_ML.get(unit) or _UNIT_ML.get(unit.rstrip("s"))
        if factor is not None:
            out.append((qty * factor, m.group(0)))
    return out


def parse_net_contents(text: str) -> tuple[float, str] | None:
    found = parse_net_contents_all(text)
    return found[0] if found else None


def match_net_contents(form_value: str, label_texts: list[str]) -> Verdict:
    form_parsed = parse_net_contents(form_value)
    if form_parsed is None:
        return Verdict(
            "net_contents", form_value, None, Outcome.MISSING,
            note="form value not parseable as a volume",
        )
    form_ml, _ = form_parsed
    found = [
        (ml, s, st)
        for st in _sourced(label_texts)
        if st.text
        for ml, s in parse_net_contents_all(st.text)
    ]
    if not found:
        return Verdict("net_contents", form_value, None, Outcome.MISSING)
    # Any volume statement on any crop stating the right volume satisfies
    # the check; pick the closest candidate.
    label_ml, label_str, src = min(found, key=lambda x: abs(x[0] - form_ml))
    if abs(label_ml - form_ml) < 0.5:
        return Verdict(
            "net_contents", form_value, label_str, Outcome.EXACT, 100.0,
            normalized=label_str.strip().lower() != form_value.strip().lower(),
            source=src.source, source_crop=src.crop_index,
        )
    return Verdict("net_contents", form_value, label_str, Outcome.MISMATCH, 0.0,
                   source=src.source, source_crop=src.crop_index)


# --- alcohol content ------------------------------------------------------

_ABV_RE = re.compile(
    r"(?:alc(?:ohol)?\.?\s*)?(\d{1,2}(?:[.,]\d+)?)\s*%"
    r"|(\d{1,3}(?:\.\d+)?)\s*proof\b",
    re.IGNORECASE,
)

_ABV_PLAUSIBLE = (0.5, 80.0)


def parse_abv_all(text: str) -> list[tuple[float, str]]:
    """All ABV statements in the text. Proof converts to ABV."""
    out = []
    for m in _ABV_RE.finditer(text):
        if m.group(1) is not None:
            out.append((float(m.group(1).replace(",", ".")), m.group(0)))
        else:
            out.append((float(m.group(2)) / 2.0, m.group(0)))
    if not out:
        # Form side may be a bare number ("42", "11.5").
        m = re.fullmatch(r"\s*(\d{1,2}(?:\.\d+)?)\s*", text)
        if m:
            out.append((float(m.group(1)), m.group(1)))
    return out


def parse_abv(text: str) -> tuple[float, str] | None:
    found = parse_abv_all(text)
    return found[0] if found else None


def match_abv(form_value: str, label_texts: list[str]) -> Verdict:
    form_parsed = parse_abv(form_value)
    if form_parsed is None:
        return Verdict(
            "alcohol_content", form_value, None, Outcome.MISSING,
            note="form value not parseable as ABV",
        )
    form_pct, _ = form_parsed
    found = [
        (pct, s, st)
        for st in _sourced(label_texts)
        if st.text
        for pct, s in parse_abv_all(st.text)
    ]
    # An implausible reading ("00%", "90%") is OCR garbage, not evidence
    # of a wrong label: only plausible candidates may drive a mismatch.
    plausible = [p for p in found if _ABV_PLAUSIBLE[0] <= p[0] <= _ABV_PLAUSIBLE[1]]
    if not plausible:
        note = (
            f"only implausible reading ({found[0][1]!r})" if found else None
        )
        return Verdict("alcohol_content", form_value, None, Outcome.MISSING, note=note)
    label_pct, label_str, src = min(plausible, key=lambda x: abs(x[0] - form_pct))
    if abs(label_pct - form_pct) < 0.05:
        return Verdict(
            "alcohol_content", form_value, label_str, Outcome.EXACT, 100.0,
            normalized=label_str.strip() != form_value.strip(),
            source=src.source, source_crop=src.crop_index,
        )
    return Verdict("alcohol_content", form_value, label_str, Outcome.MISMATCH, 0.0,
                   source=src.source, source_crop=src.crop_index)


# --- class / type ---------------------------------------------------------

# Generic catalogue words that don't identify the product on a label.
_CLASS_STOPWORDS = {
    "other", "than", "with", "and", "the", "usb", "fb", "specialties",
    "proprietaries", "flavored", "natural", "artificial",
}


def _class_terms(description: str) -> list[str]:
    """Candidate label terms from a class/type description.

    Descriptions carry parenthesized aliases — e.g. "OTHER GRAPE BRANDY
    (PISCO, GRAPPA) FB" — and the alias is usually what the label says,
    especially on non-English imports.
    """
    terms = []
    for alias_group in re.findall(r"\(([^)]+)\)", description):
        for alias in re.split(r"[,/]", alias_group):
            if alias.strip():
                terms.append(alias.strip())
    bare = re.sub(r"\([^)]*\)", " ", description)
    words = [w for w in re.split(r"[^A-Za-z]+", bare) if len(w) >= 3]
    content = [w for w in words if w.lower() not in _CLASS_STOPWORDS]
    if content:
        terms.append(" ".join(content))  # full phrase, e.g. "GRAPE BRANDY"
        terms.extend(w for w in content if len(w) >= 4)
    return terms


def match_class_type(description: str, label_texts: list[str]) -> Verdict:
    """Does any term of the class/type description appear on the label?

    Whisky/whiskey-style spelling variants land in the near-miss band by
    construction and are upgraded to exact: the description maps, it
    doesn't transcribe.
    """
    terms = _class_terms(description)
    if not terms:
        return Verdict(
            "class_type", description, None, Outcome.MISSING,
            note="no usable terms in description",
        )
    best_score, best_text, best_src = 0.0, None, None
    for st in _sourced(label_texts):
        hay = normalize_text(st.text)
        if not hay:
            continue
        for term in terms:
            a = fuzz.partial_ratio_alignment(normalize_text(term), hay)
            if a is not None and a.score > best_score:
                best_score = a.score
                best_text = hay[a.dest_start : a.dest_end].strip()
                best_src = st
    src = best_src.source if best_src else None
    src_crop = best_src.crop_index if best_src else None
    if best_score >= NEAR_THRESHOLD:
        return Verdict(
            "class_type", description, best_text, Outcome.EXACT, best_score,
            normalized=True, source=src, source_crop=src_crop,
        )
    if best_score > 60:
        return Verdict(
            "class_type", description, best_text, Outcome.NEAR_MISS, best_score,
            normalized=True, source=src, source_crop=src_crop,
        )
    return Verdict("class_type", description, None, Outcome.MISSING, best_score)


# --- label-format checks (06-2016: no typed form field) --------------------


def format_check_abv(label_texts: list[str]) -> Verdict:
    """ABV present and plausible on the label (06-2016 forms)."""
    found = [
        (pct, s, st)
        for st in _sourced(label_texts)
        if st.text
        for pct, s in parse_abv_all(st.text)
    ]
    plausible = [p for p in found if _ABV_PLAUSIBLE[0] <= p[0] <= _ABV_PLAUSIBLE[1]]
    if plausible:
        return Verdict("alcohol_content", None, plausible[0][1], Outcome.EXACT,
                       100.0, note="not on form — format check only",
                       source=plausible[0][2].source,
                       source_crop=plausible[0][2].crop_index)
    note = "not on form — format check only"
    if found:
        # Garbage readings are doubt, not evidence: review, never fail.
        note += f"; only implausible reading ({found[0][1]!r})"
    return Verdict("alcohol_content", None, None, Outcome.MISSING, note=note)


def format_check_net_contents(label_texts: list[str]) -> Verdict:
    """Net contents present and plausible on the label (06-2016 forms)."""
    found = [
        (ml, s, st)
        for st in _sourced(label_texts)
        if st.text
        for ml, s in parse_net_contents_all(st.text)
    ]
    plausible = [p for p in found if 20.0 <= p[0] <= 200000.0]
    if plausible:
        return Verdict("net_contents", None, plausible[0][1], Outcome.EXACT,
                       100.0, note="not on form — format check only",
                       source=plausible[0][2].source,
                       source_crop=plausible[0][2].crop_index)
    note = "not on form — format check only"
    if found:
        note += f"; only implausible reading ({found[0][1]!r})"
    return Verdict("net_contents", None, None, Outcome.MISSING, note=note)


# --- aggregation ----------------------------------------------------------


def aggregate_outcomes(outcomes: list[Outcome]) -> str:
    """Field outcomes -> auto-status. Never encodes a rejection: a Fail
    is a recommendation the agent confirms."""
    if any(o == Outcome.MISMATCH for o in outcomes):
        return "Fail"
    if any(o in (Outcome.NEAR_MISS, Outcome.MISSING) for o in outcomes):
        return "Needs Review"
    return "Pass"
