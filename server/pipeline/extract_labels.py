"""Caption-paired label image extraction from COLA PDFs.

Registry shape: label pages follow the "AFFIX COMPLETE SET OF LABELS
BELOW" marker. Each label image is preceded (in document order) by a text
caption:

    Image Type:
    Brand (front) or keg collar
    Actual Dimensions: 3.5 inches W X 4 inches H

Pairing is strictly by document order — captions and their images can be
split across a page boundary (caption at the foot of one page, image at
the head of the next), so pairing must run over the whole document, never
per page. The only non-label image in the label region's pages is the TTB
stamp banner, which sits *above* the AFFIX marker on the marker's page.

Application shape (bare 04/2023 form): images are affixed directly in the
AFFIX area at the foot of page 1, with a typed one-word caption (FRONT /
BACK / NECK) just below each image — or no caption at all when the
applicant pasted in a single photograph of the physical containers. An
uncaptioned image becomes kind "photo": there is nothing deterministic to
crop or classify, so the runner always hands it to Tier B.

Label images are JPEG XObjects; raw bytes are extracted without
recompression. Pixel dims / caption inches (registry) or placement inches
(application) gives effective DPI — a free OCR trust signal used by
escalation later in the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import fitz

from .parse_form import (
    AFFIX_MARKER,
    _app_template,
    _find_affix_page,
    _spans,
    detect_shape,
)

CAPTION_RE = re.compile(
    r"Image Type:\s*(?P<type>.*?)\s*"
    r"Actual Dimensions:\s*(?P<w>[\d.]+)\s*inch(?:es)?\s*W\s*X\s*"
    r"(?P<h>[\d.]+)\s*inch(?:es)?\s*H",
    re.S,
)
# Page footers can interleave with captions split across a page boundary.
FOOTER_RE = re.compile(r"^\s*TTB F 5100\.31.*$", re.M)

KIND_BY_CAPTION = {
    "brand (front) or keg collar": "front",
    "back": "back",
}


@dataclass
class LabelCrop:
    index: int  # 0-based, document order
    caption_type: str  # verbatim caption, e.g. "Brand (front) or keg collar"
    kind: str  # front | back | other (other => skipped for field matching)
    width_in: float
    height_in: float
    px_width: int
    px_height: int
    dpi: int  # effective DPI, min of the two axes
    page: int  # 0-based page index where the image is placed
    ext: str  # image format as embedded, e.g. "jpeg"
    data: bytes
    aspect_ok: bool  # caption aspect ratio agrees with pixel aspect ratio

    @property
    def matchable(self) -> bool:
        # A photo of the containers carries the front/back labels too —
        # it joins matching, but only after Tier B has read it.
        return self.kind in ("front", "back", "photo")


def extract_labels(doc: fitz.Document) -> list[LabelCrop]:
    if detect_shape(doc) == "application":
        return _extract_application(doc)
    affix_page = _find_affix_page(doc)
    if affix_page is None:
        raise ValueError("no label section: AFFIX marker not found")
    marker_y = doc[affix_page].search_for(AFFIX_MARKER)[0].y1

    captions = _captions(doc, affix_page)
    images = _label_images(doc, affix_page, marker_y)
    if len(captions) != len(images):
        raise ValueError(
            f"caption/image count mismatch: {len(captions)} captions, "
            f"{len(images)} images"
        )

    crops = []
    for i, ((ctype, w_in, h_in), (pno, xref)) in enumerate(zip(captions, images)):
        info = doc.extract_image(xref)
        px_w, px_h = info["width"], info["height"]
        caption_aspect = w_in / h_in
        pixel_aspect = px_w / px_h
        aspect_ok = abs(pixel_aspect - caption_aspect) / caption_aspect < 0.25
        kind = KIND_BY_CAPTION.get(ctype.lower(), "other")
        crops.append(
            LabelCrop(
                index=i,
                caption_type=ctype,
                kind=kind,
                width_in=w_in,
                height_in=h_in,
                px_width=px_w,
                px_height=px_h,
                dpi=round(min(px_w / w_in, px_h / h_in)),
                page=pno,
                ext=info["ext"],
                data=info["image"],
                aspect_ok=aspect_ok,
            )
        )
    return crops


def _extract_application(doc: fitz.Document) -> list[LabelCrop]:
    """Application shape: images below the AFFIX marker on the form page,
    classified by the typed caption just under each image rect. DPI comes
    from the placement rect — the size the label occupies on the form,
    which is what the reviewer (and OCR) actually gets."""
    affix_page = _find_affix_page(doc)
    if affix_page is None:
        raise ValueError("no label section: AFFIX marker not found")
    page = doc[affix_page]
    marker_y = page.search_for(AFFIX_MARKER)[0].y1

    placements = []
    for img in page.get_images(full=True):
        xref = img[0]
        for rect in page.get_image_rects(xref):
            if rect.y0 > marker_y:
                placements.append((rect, xref))
    placements.sort(key=lambda p: (p[0].y0, p[0].x0))

    typed = [
        s
        for s in _spans(page, template=_app_template)
        if not s.template and s.y0 > marker_y
    ]
    crops = []
    for i, (rect, xref) in enumerate(placements):
        caption = " ".join(
            s.text
            for s in typed
            if rect.y1 - 2 <= s.y0 <= rect.y1 + 20
            and rect.x0 - 5 <= (s.x0 + s.x1) / 2 <= rect.x1 + 5
        ).strip()
        up = caption.upper()
        if not caption:
            kind = "photo"
        elif "FRONT" in up:
            kind = "front"
        elif "BACK" in up:
            kind = "back"
        else:
            kind = "other"
        info = doc.extract_image(xref)
        px_w, px_h = info["width"], info["height"]
        w_in, h_in = rect.width / 72, rect.height / 72
        crops.append(
            LabelCrop(
                index=i,
                caption_type=caption or "(uncaptioned photograph)",
                kind=kind,
                width_in=round(w_in, 2),
                height_in=round(h_in, 2),
                px_width=px_w,
                px_height=px_h,
                dpi=round(min(px_w / w_in, px_h / h_in)),
                page=affix_page,
                ext=info["ext"],
                data=info["image"],
                aspect_ok=abs(px_w / px_h - w_in / h_in) / (w_in / h_in) < 0.25,
            )
        )
    return crops


def _captions(doc: fitz.Document, affix_page: int) -> list[tuple[str, float, float]]:
    text = "\n".join(
        FOOTER_RE.sub("", doc[pno].get_text())
        for pno in range(affix_page, len(doc))
    )
    out = []
    for m in CAPTION_RE.finditer(text):
        ctype = re.sub(r"\s+", " ", m.group("type")).strip()
        out.append((ctype, float(m.group("w")), float(m.group("h"))))
    return out


def _label_images(
    doc: fitz.Document, affix_page: int, marker_y: float
) -> list[tuple[int, int]]:
    """(page, xref) for each label image placement, in document order."""
    placements = []
    for pno in range(affix_page, len(doc)):
        page = doc[pno]
        for img in page.get_images(full=True):
            xref = img[0]
            for rect in page.get_image_rects(xref):
                # The TTB stamp banner sits above the AFFIX marker.
                if pno == affix_page and rect.y1 <= marker_y:
                    continue
                placements.append((pno, rect.y0, rect.x0, xref))
    placements.sort()
    return [(pno, xref) for pno, _, _, xref in placements]
