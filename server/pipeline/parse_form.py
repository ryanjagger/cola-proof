"""Layout-aware parser for Part I of TTB F 5100.31 COLA application PDFs.

Two PDF shapes carry the same form. The *registry* shape is the COLA
Public Registry's print view of an approved record; the *application*
shape is the bare 04/2023 fillable form as submitted by an applicant
(legal-size, instruction pages trailing, labels affixed on page 1).
`detect_shape` tells them apart; each shape has its own field map but
shares the cell-grid machinery below.

The registry shape is machine-generated with a real text layer. Three
facts make parsing deterministic across all observed revisions
(6/2006, 5/2011, 07/2012, 06-2016):

- Cell borders are drawn as vector line segments, so every Part I field
  lives in a cell that can be reconstructed from the line grid around its
  label.
- Form boilerplate is set in Arial Bold / Bold Italic; typed values are
  plain ArialMT. The font alone separates label from value.
- Checked checkboxes are small line-only vector drawings (the check mark)
  sitting just left of their option text; unchecked boxes have no mark.

Fields are anchored by *name*, never by item number — numbering shifts
between revisions (NET CONTENTS is item 11 on 6/2006 and item 12 on
07/2012, and is absent entirely on 06-2016).

The FOR TTB USE ONLY block (status, class/type, qualifications) is a
single cell with stacked bold headings, parsed by walking lines and
attaching plain-font values to the nearest heading above in the same
column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

# Canonical field name -> regex matched against a Part I cell's label text
# (uppercased, whitespace-collapsed).
FIELD_PATTERNS: dict[str, re.Pattern] = {
    "ttb_id": re.compile(r"^TTB ID$"),
    "plant_registry": re.compile(r"PLANT REGISTRY/BASIC"),
    "serial_number": re.compile(r"\d+\. SERIAL NUMBER"),
    "brand_name": re.compile(r"\d+\. BRAND NAME"),
    "fanciful_name": re.compile(r"\d+\. FANCIFUL NAME"),
    "applicant": re.compile(r"\d+\. NAME AND ADDRESS OF APPLICANT"),
    "net_contents": re.compile(r"\d+\. NET CONTENTS"),
    "alcohol_content": re.compile(r"\d+\. ALCOHOL CONTENT"),
    "wine_appellation": re.compile(r"\d+\. WINE APPELLATION"),
    "wine_vintage": re.compile(r"\d+\. WINE VINTAGE"),
    "grape_varietal": re.compile(r"\d+\. GRAPE VARIETAL"),
    "formula": re.compile(r"\d+\. FORMULA"),
    "phone": re.compile(r"\d+\. PHONE NUMBER"),
    "email": re.compile(r"\d+\. EMAIL ADDRESS"),
    "container_wording": re.compile(r"\d+\. SHOW ANY (WORDING|INFORMATION)"),
    "application_date": re.compile(r"\d+\. DATE OF APPLICATION"),
    "date_issued": re.compile(r"\d+\. DATE ISSUED"),
}

# Checkbox cells: canonical field -> (cell label regex, option texts)
CHECKBOX_FIELDS: dict[str, tuple[re.Pattern, list[str]]] = {
    "product_type": (
        re.compile(r"\d+\. TYPE OF PRODUCT"),
        ["WINE", "DISTILLED SPIRITS", "MALT BEVERAGE"],
    ),
    "source": (
        re.compile(r"\d+\. SOURCE OF PRODUCT"),
        ["Domestic", "Imported"],
    ),
}

# Headings inside the FOR TTB USE ONLY block.
TTB_PAGE_HEADINGS: dict[str, re.Pattern] = {
    "qualifications": re.compile(r"^QUALIFICATIONS\b"),
    "status": re.compile(r"^STATUS\b"),
    "class_type_description": re.compile(r"^CLASS/TYPE DESCRIPTION"),
    "expiration_date": re.compile(r"^EXPIRATION DATE"),
}

# A span that starts a Part I field label: numbered item or the TTB ID box.
ANCHOR_RE = re.compile(r"^(\d+[a-z]{0,2}\.\s|TTB ID$)")

AFFIX_MARKER = "AFFIX COMPLETE SET OF LABELS BELOW"

# --- application (bare 04/2023 fillable form) shape -----------------------
#
# Furniture is Arial-family; typed values land in Helvetica (Helvetica-Bold
# for checkbox X marks and the serial-number comb boxes), signatures in
# whatever font the filler used — so template-vs-value separation is by
# font family, not boldness. Some furniture is set with a broken ToUnicode
# map ("PHONE NUMBER" extracts as "3+O1( 1UM%(R"), so fields are anchored
# only on captions that survive extraction intact; phone/email don't and
# aren't needed for matching. There are no typed NET CONTENTS / ALCOHOL
# CONTENT items (as on 06-2016), no TTB ID, and no FOR TTB USE ONLY values
# — the record hasn't been approved yet.

APP_FIELD_PATTERNS: dict[str, re.Pattern] = {
    "plant_registry": re.compile(r"PLANT REGISTRY/BASIC"),
    "brand_name": re.compile(r"\d+ ?\. BRAND NAME"),
    "fanciful_name": re.compile(r"\d+ ?\. FANCIFUL NAME"),
    "applicant": re.compile(r"\d+ ?\. NAME AND ADDRESS OF APPLICANT"),
    "formula": re.compile(r"\d+ ?\. FORMULA"),
    "grape_varietal": re.compile(r"\d+ ?\. GRAPE VARIETAL"),
    "wine_appellation": re.compile(r"\d+ ?\. WINE APPELLATION"),
    "container_wording": re.compile(r"SHOW ANY (WORDING|INFORMATION)"),
    "application_date": re.compile(r"DATE OF APPLICATION"),
}

APP_CHECKBOX_FIELDS: dict[str, tuple[re.Pattern, list[str]]] = {
    "product_type": (
        re.compile(r"TYPE OF PRODUCT"),
        ["WINE", "DISTILLED SPIRITS", "MALT BEVERAGES"],
    ),
    "source": (
        re.compile(r"\d+ ?\. SOURCE OF PRODUCT"),
        ["Domestic", "Imported"],
    ),
}

# The form prints the plural; keep ParsedForm values shape-independent.
APP_PRODUCT_NORMALIZE = {"MALT BEVERAGES": "MALT BEVERAGE"}

# Anchors: numbered items ("4.", "2 .", "8a."), plus named fallbacks for
# cells whose item number is eaten by the broken font ("5.", "15.", "16.").
APP_ANCHOR_RES = [
    re.compile(r"^\d+ ?[a-z]{0,2} ?\.($|\s)"),
    re.compile(r"^\.? ?TYPE OF PRODUCT"),
    re.compile(r"^SHOW ANY (WORDING|INFORMATION)"),
    re.compile(r"^DATE OF APPLICATION"),
]


def _app_template(font: str) -> bool:
    return font.startswith("Arial")


def detect_shape(doc: fitz.Document) -> str:
    """"registry" (Public Registry print view) or "application" (bare
    filled form). The registry renderer appends an "Image Type: ... Actual
    Dimensions: ..." caption for every label it re-embeds; the bare form
    never contains that text — applicants affix images straight onto the
    AFFIX area of page 1."""
    for pno in range(len(doc)):
        if doc[pno].search_for("Image Type:"):
            return "registry"
    return "application"


@dataclass
class ParsedForm:
    shape: str = "registry"  # registry | application (see detect_shape)
    ttb_id: str | None = None
    revision: str | None = None  # footer string, e.g. "07/2012", "06-2016"
    serial_number: str | None = None
    brand_name: str | None = None
    fanciful_name: str | None = None
    applicant: str | None = None
    plant_registry: str | None = None
    product_type: str | None = None  # WINE | DISTILLED SPIRITS | MALT BEVERAGE
    source: str | None = None  # Domestic | Imported (06-2016 & some 2011+)
    net_contents: str | None = None  # pre-2016 only
    alcohol_content: str | None = None  # pre-2016 only
    wine_appellation: str | None = None
    wine_vintage: str | None = None
    grape_varietal: str | None = None
    formula: str | None = None
    phone: str | None = None
    email: str | None = None
    container_wording: str | None = None
    application_date: str | None = None
    date_issued: str | None = None
    # FOR TTB USE ONLY block
    status: str | None = None
    class_type_description: str | None = None
    qualifications: str | None = None
    expiration_date: str | None = None
    # Whether the typed fields exist on this revision; drives the
    # form-vs-label match vs. label-format-check policy downstream.
    has_net_contents_field: bool = False
    has_alcohol_content_field: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Span:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    template: bool  # form furniture (label text), as opposed to a typed value


def _spans(page: fitz.Page, template=None) -> list[_Span]:
    """`template` decides furniture vs typed value from the font name.
    Default is the registry rule (furniture is bold); the application
    shape passes `_app_template` (furniture is Arial-family)."""
    if template is None:
        template = lambda font: "Bold" in font  # noqa: E731
    out = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for s in line["spans"]:
                text = s["text"].replace("\xa0", " ").strip()
                if not text:
                    continue
                x0, y0, x1, y1 = s["bbox"]
                out.append(_Span(x0, y0, x1, y1, text, template(s["font"])))
    return out


def _segments(
    page: fitz.Page, skip_rects: list[fitz.Rect] = (), min_hlen: float = 0.0
) -> tuple[list, list]:
    """Border line segments: hlines [(y, x0, x1)], vlines [(x, y0, y1)].

    Borders are drawn as thin rects, line items, and full cell rects
    (which contribute their four edges). `skip_rects` drops rects that are
    known not to be cell borders; `min_hlen` drops short horizontal
    fragments (both used by the application shape, see _app_segments).
    """
    hlines, vlines = [], []

    def skipped(r: fitz.Rect) -> bool:
        return any(
            abs(r.x0 - s.x0) <= 2.5
            and abs(r.y0 - s.y0) <= 2.5
            and abs(r.x1 - s.x1) <= 2.5
            and abs(r.y1 - s.y1) <= 2.5
            for s in skip_rects
        )

    def add_rect(r: fitz.Rect) -> None:
        if r.height < 2:
            hlines.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
        elif r.width < 2:
            vlines.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
        else:
            hlines.append((r.y0, r.x0, r.x1))
            hlines.append((r.y1, r.x0, r.x1))
            vlines.append((r.x0, r.y0, r.y1))
            vlines.append((r.x1, r.y0, r.y1))

    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "re":
                r = fitz.Rect(item[1])
                if not skipped(r):
                    add_rect(r)
            elif item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 1:
                    hlines.append((p1.y, min(p1.x, p2.x), max(p1.x, p2.x)))
                elif abs(p1.x - p2.x) < 1:
                    vlines.append((p1.x, min(p1.y, p2.y), max(p1.y, p2.y)))
    if min_hlen:
        hlines = [h for h in hlines if h[2] - h[1] >= min_hlen]
    return hlines, vlines


def _app_segments(page: fitz.Page) -> tuple[list, list]:
    """Application-shape grid. The fillable form draws its AcroForm widget
    boxes into the page content, and those rects sit between a label and
    its typed value — the minimal-cell walk would clip the value out of
    the cell. Drop them, except hairline widgets, which trace real cell
    dividers. Short horizontal fragments (serial comb boxes, checkbox
    squares) aren't cell borders either."""
    widget_rects = [
        w.rect
        for w in page.widgets() or []
        if w.rect.width > 2 and w.rect.height > 2
    ]
    return _segments(page, skip_rects=widget_rects, min_hlen=40.0)


def _cell_rect(
    px: float, py: float, hlines: list, vlines: list, page_rect: fitz.Rect
) -> fitz.Rect:
    """Smallest grid cell containing point (px, py)."""
    tol = 3.0
    top = max(
        (y for y, x0, x1 in hlines if y <= py and x0 - tol <= px <= x1 + tol),
        default=page_rect.y0,
    )
    bottom = min(
        (y for y, x0, x1 in hlines if y > py + 1 and x0 - tol <= px <= x1 + tol),
        default=page_rect.y1,
    )
    left = max(
        (x for x, y0, y1 in vlines if x <= px and y0 - tol <= py <= y1 + tol),
        default=page_rect.x0,
    )
    right = min(
        (x for x, y0, y1 in vlines if x > px + 1 and y0 - tol <= py <= y1 + tol),
        default=page_rect.x1,
    )
    return fitz.Rect(left, top, right, bottom)


def _check_marks(page: fitz.Page) -> list[fitz.Rect]:
    """Check-mark glyphs: small drawings made only of line segments."""
    marks = []
    for d in page.get_drawings():
        items = d["items"]
        if not items or not all(i[0] == "l" for i in items):
            continue
        r = d["rect"]
        if 2 < r.width < 14 and 2 < r.height < 14:
            marks.append(fitz.Rect(r))
    return marks


def _lines(spans: list[_Span]) -> list[list[_Span]]:
    """Group spans into visual lines by y, each sorted by x."""
    lines: list[list[_Span]] = []
    for s in sorted(spans, key=lambda s: (s.y0, s.x0)):
        if lines and abs(lines[-1][0].y0 - s.y0) < 3:
            lines[-1].append(s)
        else:
            lines.append([s])
    return [sorted(line, key=lambda s: s.x0) for line in lines]


def _join(spans: list[_Span]) -> str:
    return "\n".join(
        " ".join(s.text for s in line) for line in _lines(spans)
    ).strip()


def parse_form(doc: fitz.Document) -> ParsedForm:
    form = ParsedForm()
    form.shape = detect_shape(doc)
    form.revision = _detect_revision(doc)

    if form.shape == "application":
        matched = _parse_application_part1(doc[0], form)
    else:
        matched = _parse_part1(doc[0], form)
    for name, value in matched.items():
        setattr(form, name, value or None)
    form.has_net_contents_field = "net_contents" in matched
    form.has_alcohol_content_field = "alcohol_content" in matched

    affix_page = _find_affix_page(doc)
    if affix_page is None:
        form.warnings.append("no AFFIX marker page found")
    elif form.shape == "registry":
        # Applications carry no FOR TTB USE ONLY values (nothing has been
        # decided yet), so status/class-type stay None on that shape.
        _parse_ttb_use_only(doc[affix_page], form)

    required = ("ttb_id", "brand_name", "serial_number")
    if form.shape == "application":
        required = ("brand_name", "serial_number")  # no TTB ID before approval
    for name in required:
        if not getattr(form, name):
            form.warnings.append(f"required field missing: {name}")
    return form


def _parse_part1(page: fitz.Page, form: ParsedForm) -> dict[str, str]:
    spans = _spans(page)
    hlines, vlines = _segments(page)
    marks = _check_marks(page)
    matched: dict[str, str] = {}
    seen_cells: set[tuple] = set()

    for anchor in spans:
        if not anchor.template or not ANCHOR_RE.match(anchor.text):
            continue
        cell = _cell_rect(
            anchor.x0 + 1, (anchor.y0 + anchor.y1) / 2, hlines, vlines, page.rect
        )
        key = (round(cell.x0), round(cell.y0), round(cell.x1), round(cell.y1))
        if key in seen_cells:
            continue
        seen_cells.add(key)

        cell_spans = [
            s
            for s in spans
            if cell.x0 - 1 <= (s.x0 + s.x1) / 2 <= cell.x1 + 1
            and cell.y0 - 1 <= (s.y0 + s.y1) / 2 <= cell.y1 + 1
        ]
        label = re.sub(
            r"\s+", " ", _join([s for s in cell_spans if s.template])
        ).upper()
        value = _join([s for s in cell_spans if not s.template])

        for name, pat in FIELD_PATTERNS.items():
            if pat.search(label):
                if name in matched:
                    form.warnings.append(f"duplicate cell match for {name}")
                else:
                    matched[name] = value
        for name, (pat, options) in CHECKBOX_FIELDS.items():
            if pat.search(label) and name not in matched:
                matched[name] = _checked_option(cell_spans, options, marks, form, name)
    return matched


def _checked_option(
    cell_spans: list[_Span],
    options: list[str],
    marks: list[fitz.Rect],
    form: ParsedForm,
    name: str,
) -> str:
    checked = []
    for s in cell_spans:
        if s.text not in options:
            continue
        for m in marks:
            my = (m.y0 + m.y1) / 2
            if m.x1 <= s.x0 + 2 and s.x0 - m.x1 < 12 and s.y0 - 3 <= my <= s.y1 + 3:
                checked.append(s.text)
                break
    if len(checked) > 1:
        form.warnings.append(f"multiple {name} boxes checked: {checked}")
    return checked[0] if checked else ""


def _parse_application_part1(page: fitz.Page, form: ParsedForm) -> dict[str, str]:
    """Part I of the bare 04/2023 form. Same cell-grid walk as the
    registry shape, but template/value split by font family, checkboxes
    marked with a typed Helvetica-Bold "X" left of the option word, and
    the serial number spread over single-character comb boxes."""
    spans = _spans(page, template=_app_template)
    hlines, vlines = _app_segments(page)
    words = page.get_text("words")
    matched: dict[str, str] = {}
    seen_cells: set[tuple] = set()
    used_marks: list[_Span] = []

    for anchor in spans:
        if not anchor.template or not any(
            r.match(anchor.text) for r in APP_ANCHOR_RES
        ):
            continue
        cell = _cell_rect(
            anchor.x0 + 1, (anchor.y0 + anchor.y1) / 2, hlines, vlines, page.rect
        )
        key = (round(cell.x0), round(cell.y0), round(cell.x1), round(cell.y1))
        if key in seen_cells:
            continue
        seen_cells.add(key)

        cell_spans = [
            s
            for s in spans
            if cell.x0 - 1 <= (s.x0 + s.x1) / 2 <= cell.x1 + 1
            and cell.y0 - 1 <= (s.y0 + s.y1) / 2 <= cell.y1 + 1
        ]
        label = re.sub(
            r"\s+", " ", _join([s for s in cell_spans if s.template])
        ).upper()
        value = _join([s for s in cell_spans if not s.template])

        for name, pat in APP_FIELD_PATTERNS.items():
            if pat.search(label):
                if name in matched:
                    form.warnings.append(f"duplicate cell match for {name}")
                else:
                    matched[name] = value
        for name, (pat, options) in APP_CHECKBOX_FIELDS.items():
            if pat.search(label) and name not in matched:
                value, marks = _app_checked_option(
                    cell, words, spans, options, form, name
                )
                matched[name] = APP_PRODUCT_NORMALIZE.get(value, value)
                used_marks.extend(marks)

    matched["serial_number"] = _app_serial(spans, used_marks, hlines, vlines, page)
    return matched


def _app_checked_option(
    cell: fitz.Rect,
    words: list,
    spans: list[_Span],
    options: list[str],
    form: ParsedForm,
    name: str,
) -> tuple[str, list[_Span]]:
    """The fillable form has no vector check marks — applicants type an X
    into a box just left of the option text. Option words come from
    page words (a single furniture span can hold both "Domestic" and
    "Imported" with internal spacing); X marks are typed-value spans."""
    x_marks = [s for s in spans if not s.template and s.text.strip().upper() == "X"]
    checked: list[str] = []
    used: list[_Span] = []
    for opt in options:
        first = opt.split()[0]
        for w in words:
            wx0, wy0, wx1, wy1, wtext = w[0], w[1], w[2], w[3], w[4]
            if wtext != first:
                continue
            if not (cell.x0 - 1 <= (wx0 + wx1) / 2 <= cell.x1 + 1):
                continue
            if not (cell.y0 - 1 <= (wy0 + wy1) / 2 <= cell.y1 + 1):
                continue
            for m in x_marks:
                # Option rows sit closer together than the X is tall, so
                # test the X's center against the row band, not overlap.
                my = (m.y0 + m.y1) / 2
                if m.x1 <= wx0 + 2 and wx0 - m.x1 < 14 and wy0 - 3 <= my <= wy1 + 3:
                    checked.append(opt)
                    used.append(m)
                    break
            if opt in checked:
                break
    if len(checked) > 1:
        form.warnings.append(f"multiple {name} boxes checked: {checked}")
    return (checked[0] if checked else ""), used


def _app_serial(
    spans: list[_Span],
    used_marks: list[_Span],
    hlines: list,
    vlines: list,
    page: fitz.Page,
) -> str:
    """Item 4: year + serial, one typed character per comb box under the
    SERIAL NUMBER caption. Collect single-character value spans in the
    band below the caption, bounded right by the caption's grid cell so
    the TYPE OF PRODUCT check mark next door can't bleed in; checkbox
    X marks already consumed are excluded either way."""
    caption = next(
        (s for s in spans if s.template and "SERIAL NUMBER" in s.text.upper()),
        None,
    )
    if caption is None:
        return ""
    cell = _cell_rect(
        caption.x0 + 1, (caption.y0 + caption.y1) / 2, hlines, vlines, page.rect
    )
    right = min(cell.x1 + 6, page.rect.width * 0.4)
    chars = [
        s
        for s in spans
        if not s.template
        and s not in used_marks
        and len(s.text.strip()) == 1
        and caption.y1 - 2 <= s.y0 <= caption.y1 + 55
        and caption.x0 - 12 <= s.x0 <= right
    ]
    chars.sort(key=lambda s: s.x0)
    return "".join(s.text.strip() for s in chars)


def _parse_ttb_use_only(page: fitz.Page, form: ParsedForm) -> None:
    """Parse the stacked-heading FOR TTB USE ONLY block above the AFFIX
    marker: each plain-font line belongs to the nearest bold heading above
    it in the same column."""
    marker = page.search_for(AFFIX_MARKER)
    cutoff = marker[0].y0 if marker else page.rect.y1
    spans = [s for s in _spans(page) if s.y1 <= cutoff]

    headings = []  # (field, x0, y0)
    for s in spans:
        if not s.template:
            continue
        for name, pat in TTB_PAGE_HEADINGS.items():
            if pat.match(s.text):
                headings.append((name, s.x0, s.y0))
    values: dict[str, list[str]] = {}
    for line in _lines([s for s in spans if not s.template]):
        lx, ly = line[0].x0, line[0].y0
        best = None
        for name, hx, hy in headings:
            if hy <= ly and abs(hx - lx) < 20:
                if best is None or hy > best[1]:
                    best = (name, hy)
        if best:
            values.setdefault(best[0], []).append(" ".join(s.text for s in line))

    for name, lines in values.items():
        setattr(form, name, "\n".join(lines).strip() or None)

    # "THE STATUS IS SURRENDERED." -> "SURRENDERED"
    if form.status:
        m = re.search(r"THE STATUS IS\s+(.+?)\.?\s*$", form.status, re.S)
        if m:
            form.status = re.sub(r"\s+", " ", m.group(1)).strip()
    if form.class_type_description:
        form.class_type_description = re.sub(
            r"\s+", " ", form.class_type_description
        ).strip()


def _detect_revision(doc: fitz.Document) -> str | None:
    for pno in range(len(doc) - 1, -1, -1):
        m = re.search(r"TTB F 5100\.31\s*\(([^)]+)\)", doc[pno].get_text())
        if m:
            return m.group(1).strip()
    return None


def _find_affix_page(doc: fitz.Document) -> int | None:
    for pno in range(len(doc)):
        if doc[pno].search_for(AFFIX_MARKER):
            return pno
    return None
