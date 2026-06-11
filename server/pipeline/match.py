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
    # Where label_value sits on the crop, as (x0, y0, x1, y1) fractions;
    # OCR-sourced only — the vision reader returns no geometry.
    box: tuple[float, float, float, float] | None = None


def normalize_text(s: str) -> str:
    """Case/punctuation/accent-insensitive canonical form."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


# --- brand / fanciful name ------------------------------------------------


def _best_window(
    needle: str, sourced: list[SourcedText]
) -> tuple[float, str | None, SourcedText | None, bool]:
    """Best fuzzy window for a normalized needle across crop texts:
    (score, normalized window, winning source, scattered). The label side
    is free text, so this looks for the best window rather than
    whole-string equality."""
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
    return best_score, best_text, best_src, scattered


def match_name(field: str, form_value: str, label_texts: list[str]) -> Verdict:
    """Match a name (brand or fanciful) against crop texts."""
    needle = normalize_text(form_value)
    if not needle:
        return Verdict(field, form_value, None, Outcome.MISSING, note="empty form value")
    sourced = _sourced(label_texts)
    best_score, best_text, best_src, scattered = _best_window(needle, sourced)
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


def locate_box(
    window: str,
    words: list[tuple[str, float]],
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    """Union bounding box of the contiguous OCR word run that best matches
    a verdict's label_value window. The matchers work on joined page text,
    so the window has no geometry of its own; this re-finds it among the
    per-word boxes. Fuzzy (the window may differ from the words at the
    edges of a near-miss), and returns None below a safe floor — no box
    is better than a wrong box."""
    target = normalize_text(window)
    if not target or not words or len(words) != len(boxes):
        return None
    n_tokens = len(target.split())
    norm = [normalize_text(w) for w, _ in words]
    best: tuple[float, int, int] | None = None
    for i in range(len(norm)):
        if not norm[i]:
            continue
        parts: list[str] = []
        for j in range(i, min(i + n_tokens + 3, len(norm))):
            if norm[j]:
                parts.append(norm[j])
            run = " ".join(parts)
            score = fuzz.ratio(run, target)
            if best is None or score > best[0]:
                best = (score, i, j)
            if len(run) > len(target) + 12:
                break
    if best is None or best[0] < 80:
        return None
    _, i, j = best
    span = boxes[i : j + 1]
    return (
        min(b[0] for b in span),
        min(b[1] for b in span),
        max(b[2] for b in span),
        max(b[3] for b in span),
    )


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


# --- bottler / producer (applicant name on the label) ----------------------

# The form's applicant block is multi-line: name line(s), street, city.
# Registry print views combine "TRADE NAME, LEGAL NAME, INC." on the first
# line and often append the explicit "X (Used on label)" line — the form
# stating outright which name the label shows. Street addresses never
# appear on labels, so only names and the city/state line matter here.
_USED_ON_LABEL_RE = re.compile(r"\(used on label\)\s*$", re.IGNORECASE)
_DBA_RE = re.compile(
    r"\b(?:d[./ ]?b[./ ]?a[.:]?|doing business as|trading as)\s*[:\-]?\s*(.+)",
    re.IGNORECASE,
)
# State code is validated against a real list: street lines end in tokens
# like "ST" or "CT" that the bare two-capitals pattern would swallow.
_CITY_STATE_RE = re.compile(
    r"([A-Za-z][A-Za-z .'\-]*?)\s*[,.]?\s+([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$"
)
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA "
    "WV WI WY PR VI GU".split()
)
# Bare corporate suffixes left over when the comma-combined first line is
# split — not names on their own.
_CORP_SUFFIXES = frozenset(
    {"inc", "llc", "l l c", "ltd", "co", "corp", "company", "lp", "llp", "plc"}
)
# Labels routinely print the name without its legal designator ("3 STEVES
# WINERY" for "3 STEVES WINERY LLC") — same entity, not a near-miss.
_TRAILING_SUFFIX_RE = re.compile(
    r"[\s,.]+(?:inc|l\.?l\.?c|ltd|co|corp|company|lp|llp|plc)\.?\s*$",
    re.IGNORECASE,
)


def _strip_corp_suffixes(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _TRAILING_SUFFIX_RE.sub("", name)
    return name
# Responsibility statements anchoring the bottler line; note-only, never a
# gate — a partially OCR'd line must not be punished for a missing anchor.
_RESPONSIBILITY_RE = re.compile(
    r"\b(?:bottled|produced|distilled|brewed|imported|blended|made|canned|"
    r"vinted|cellared|crafted)\s+(?:and\s+\w+\s+)?(?:by|for)\b"
)


def _applicant_name_candidates(applicant: str) -> list[str]:
    """Name strings worth seeking on a label, most specific first: any
    "(Used on label)" trade name, any DBA line, then the first line and
    its comma segments (trade vs legal name)."""
    lines = [ln.strip() for ln in applicant.splitlines() if ln.strip()]
    if not lines:
        return []
    cands: list[str] = []
    for ln in lines:
        m = _USED_ON_LABEL_RE.search(ln)
        if m:
            cands.append(ln[: m.start()].strip())
        m = _DBA_RE.search(ln)
        if m:
            cands.append(m.group(1).strip())
    first = lines[0]
    cands.append(first)
    cands.extend(seg.strip() for seg in first.split(",") if seg.strip())
    cands.extend([_strip_corp_suffixes(c) for c in list(cands)])
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        n = normalize_text(c)
        if n and len(n) >= 4 and n not in _CORP_SUFFIXES and n not in seen:
            seen.add(n)
            out.append(c)
    return out


def _applicant_city_state(applicant: str) -> str | None:
    """Normalized "city st" from the block's last city-shaped line."""
    lines = [ln.strip() for ln in applicant.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _USED_ON_LABEL_RE.search(ln):
            continue
        m = _CITY_STATE_RE.search(ln)
        if m and m.group(2) in _US_STATES:
            return normalize_text(f"{m.group(1)} {m.group(2)}")
    return None


def match_bottler(applicant: str | None, label_texts: list[str]) -> Verdict:
    """Does the applicant's name appear on any label?

    Labels carry name plus city/state only (never the street), and the
    name may be a trade name the form doesn't spell out, so this check
    can only ever be EXACT, NEAR_MISS, or MISSING — never MISMATCH: an
    absent or weak bottler line is doubt to review, not evidence of a
    wrong label.
    """
    candidates = _applicant_name_candidates(applicant or "")
    if not candidates:
        return Verdict(
            "bottler", applicant, None, Outcome.MISSING,
            note="no applicant name on the form",
        )
    sourced = _sourced(label_texts)
    best_score, best_text, best_src, scattered = 0.0, None, None, False
    for cand in candidates:
        score, text, src, scat = _best_window(normalize_text(cand), sourced)
        if score > best_score:
            best_score, best_text, best_src, scattered = score, text, src, scat
    src = best_src.source if best_src else None
    src_crop = best_src.crop_index if best_src else None
    normalized = not any(c in st.text for c in candidates for st in sourced)
    if best_score >= EXACT_THRESHOLD:
        notes = []
        if scattered:
            notes.append("words found non-adjacent on label")
        city_state = _applicant_city_state(applicant or "")
        if city_state:
            pool = " ".join(normalize_text(st.text) for st in sourced)
            if city_state in pool:
                notes.append("name and city/state found")
            else:
                notes.append("name found; city/state not readable — small print")
        if best_src and _RESPONSIBILITY_RE.search(normalize_text(best_src.text)):
            notes.append("next to a bottled/produced-by statement")
        return Verdict(
            "bottler", applicant, best_text, Outcome.EXACT, best_score,
            normalized, "; ".join(notes) or None, src, src_crop,
        )
    if best_score >= NEAR_THRESHOLD:
        return Verdict(
            "bottler", applicant, best_text, Outcome.NEAR_MISS, best_score,
            normalized,
            note="close to the applicant name — may be a trade name or "
            "hard-to-read print",
            source=src, source_crop=src_crop,
        )
    return Verdict(
        "bottler", applicant, None, Outcome.MISSING, best_score,
        note="applicant name not found — the label may use a trade name",
    )


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


# --- country of origin (imports: presence check) ---------------------------

# Country names safe to match bare anywhere on a label, in normalized
# (lowercase, punctuation-stripped) form. Deliberately a frozen literal:
# deterministic and offline. Adjectival origins ("FRENCH BRANDY") are out
# of scope for v1 — an import whose origin is only adjectival lands in
# review with a note saying why, never a Fail.
_COUNTRIES = frozenset({
    "france", "italy", "spain", "portugal", "germany", "austria",
    "switzerland", "belgium", "netherlands", "holland", "luxembourg",
    "scotland", "ireland", "wales", "united kingdom", "great britain",
    "sweden", "norway", "denmark", "finland", "iceland", "poland",
    "hungary", "romania", "bulgaria", "greece", "croatia", "slovenia",
    "slovakia", "czech republic", "czechia", "moldova", "ukraine",
    "russia", "armenia", "azerbaijan", "kazakhstan", "uzbekistan",
    "mexico", "canada", "guatemala", "nicaragua", "panama", "costa rica",
    "honduras", "belize", "el salvador", "jamaica", "haiti", "barbados",
    "bermuda", "bahamas", "trinidad", "dominican republic", "venezuela",
    "colombia", "ecuador", "peru", "bolivia", "brazil", "paraguay",
    "uruguay", "argentina", "australia", "new zealand", "fiji", "japan",
    "south korea", "taiwan", "thailand", "vietnam", "philippines",
    "indonesia", "israel", "lebanon", "south africa", "kenya",
    "ethiopia", "morocco", "tunisia", "egypt",
})
# Names that collide with ordinary label words (WILD TURKEY bourbon, NEW
# ENGLAND IPA, INDIA PALE ALE, GEORGIA the state, CHILE the pepper):
# these count only directly behind an anchoring origin phrase.
_AMBIGUOUS_COUNTRIES = frozenset({
    "turkey", "georgia", "chile", "china", "cuba", "india", "england",
    "jordan", "malta", "korea",
})


def _country_alternation(names: frozenset[str]) -> str:
    return "|".join(sorted(map(re.escape, names), key=len, reverse=True))


_COUNTRY_RE = re.compile(rf"\b(?:{_country_alternation(_COUNTRIES)})\b")
_ANY_COUNTRY_RE = re.compile(
    rf"\b(?:{_country_alternation(_COUNTRIES | _AMBIGUOUS_COUNTRIES)})\b"
)
# Applied to normalize_text()'d label text (lowercase, no punctuation).
_ORIGIN_ANCHOR_RE = re.compile(
    r"\b(?:product|produce)\s+of\b|\bimported\s+from\b|\bmade\s+in\b"
)


def format_check_origin(label_texts: list[str]) -> Verdict:
    """Origin statement present on an imported product's labels.

    Presence check in the format_check_* family: the form states only
    "Imported", so there is no value to compare — just whether any
    readable text names where the product comes from. EXACT, NEAR_MISS,
    or MISSING — never MISMATCH.
    """
    sourced = _sourced(label_texts)
    anchor_hit: tuple[str, SourcedText] | None = None
    for st in sourced:
        hay = normalize_text(st.text)
        if not hay:
            continue
        for m in _ORIGIN_ANCHOR_RE.finditer(hay):
            tail = hay[m.end() : m.end() + 40]
            cm = _ANY_COUNTRY_RE.search(tail)
            if cm:
                window = hay[m.start() : m.end() + cm.end()].strip()
                return Verdict(
                    "country_of_origin", "Imported", window, Outcome.EXACT,
                    100.0, normalized=True, note="origin statement found",
                    source=st.source, source_crop=st.crop_index,
                )
            if anchor_hit is None:
                anchor_hit = (hay[m.start() : m.end() + 20].strip(), st)
    for st in sourced:
        hay = normalize_text(st.text)
        if not hay:
            continue
        cm = _COUNTRY_RE.search(hay)
        if cm:
            return Verdict(
                "country_of_origin", "Imported", cm.group(0), Outcome.EXACT,
                100.0, normalized=True, note="country name found on the label",
                source=st.source, source_crop=st.crop_index,
            )
    if anchor_hit:
        window, st = anchor_hit
        return Verdict(
            "country_of_origin", "Imported", window, Outcome.NEAR_MISS,
            note="found an origin phrase but couldn't read the country — "
            "please check the label",
            source=st.source, source_crop=st.crop_index,
        )
    return Verdict(
        "country_of_origin", "Imported", None, Outcome.MISSING,
        note="no country of origin found — it may be worded in another "
        "language or as a style; please check the label",
    )


# --- aggregation ----------------------------------------------------------


def aggregate_outcomes(outcomes: list[Outcome]) -> str:
    """Field outcomes -> auto-status. Never encodes a rejection: a Fail
    is a recommendation the agent confirms."""
    if any(o == Outcome.MISMATCH for o in outcomes):
        return "Fail"
    if any(o in (Outcome.NEAR_MISS, Outcome.MISSING) for o in outcomes):
        return "Needs Review"
    return "Pass"
