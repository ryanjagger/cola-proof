"""Tier A extraction: local Tesseract OCR with per-word confidences.

Per-word confidence is why Tesseract is Tier A — it gives the runner the
trust signal that drives escalation to Tier B. The multi-language pack
covers the common import languages in the corpus.

Preprocessing: crops are stored at their embedded resolution (observed
~90-320 effective DPI); low-DPI crops are upscaled toward ~300 DPI before
OCR. Dark labels (light text on dark ground) often binarize badly, so a
crop whose first pass reads poorly is retried inverted and the better
pass wins — deterministic and local, not an escalation.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

import pytesseract
from PIL import Image, ImageOps

LANGS = "eng+spa+ita+fra+por+deu"
TARGET_DPI = 300
MAX_UPSCALE = 4.0
LOW_CONF = 60.0  # per-word confidence below this counts as a weak word
RETRY_MEAN_CONF = 65.0  # first-pass mean below this triggers inverted retry


@dataclass
class OcrResult:
    text: str
    words: list[tuple[str, float]] = field(default_factory=list)
    # Word bounding boxes parallel to `words`, as (x0, y0, x1, y1)
    # fractions of the original crop — resolution-independent so the UI
    # can overlay them at any display scale.
    word_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    mean_conf: float = 0.0
    low_conf_fraction: float = 1.0  # fraction of words below LOW_CONF
    inverted: bool = False  # the inverted pass won
    elapsed_ms: int = 0

    @property
    def readable(self) -> bool:
        return bool(self.words)


def _prepare(data: bytes, dpi: int | None) -> Image.Image:
    img = Image.open(io.BytesIO(data)).convert("L")
    if dpi and 0 < dpi < TARGET_DPI:
        scale = min(TARGET_DPI / dpi, MAX_UPSCALE)
        img = img.resize(
            (round(img.width * scale), round(img.height * scale)),
            Image.LANCZOS,
        )
    return img


def _assemble_text(data: dict) -> str:
    """Rebuild line-structured text from image_to_data word rows.

    Newlines at line boundaries matter: warning.py de-hyphenates across
    `-\\n` joins, so lines must not collapse into one space-joined blob.
    """
    lines: list[list[str]] = []
    current_line = None
    for w, c, line_key in zip(
        data["text"], data["conf"],
        zip(data["block_num"], data["par_num"], data["line_num"]),
    ):
        if not w.strip() or float(c) < 0:
            continue
        if line_key != current_line:
            current_line = line_key
            lines.append([])
        lines[-1].append(w)
    return "\n".join(" ".join(line) for line in lines)


def _run(
    img: Image.Image,
) -> tuple[str, list[tuple[str, float]], list[tuple[float, float, float, float]]]:
    data = pytesseract.image_to_data(
        img, lang=LANGS, output_type=pytesseract.Output.DICT
    )
    words, boxes = [], []
    for w, c, x, y, bw, bh in zip(
        data["text"], data["conf"], data["left"], data["top"],
        data["width"], data["height"],
    ):
        if not w.strip() or float(c) < 0:
            continue
        words.append((w, float(c)))
        # Fractions of the OCR image == fractions of the original crop:
        # _prepare only scales uniformly, so ratios survive the upscale.
        boxes.append((
            round(x / img.width, 4),
            round(y / img.height, 4),
            round((x + bw) / img.width, 4),
            round((y + bh) / img.height, 4),
        ))
    return _assemble_text(data), words, boxes


def _stats(words: list[tuple[str, float]]) -> tuple[float, float]:
    if not words:
        return 0.0, 1.0
    confs = [c for _, c in words]
    return sum(confs) / len(confs), sum(1 for c in confs if c < LOW_CONF) / len(confs)


def ocr_crop(data: bytes, dpi: int | None = None) -> OcrResult:
    start = time.monotonic()
    img = _prepare(data, dpi)
    text, words, boxes = _run(img)
    mean_conf, low_frac = _stats(words)
    inverted = False

    if mean_conf < RETRY_MEAN_CONF:
        inv_text, inv_words, inv_boxes = _run(ImageOps.invert(img))
        inv_mean, inv_low = _stats(inv_words)
        # Prefer the inverted pass only when it is clearly better.
        if inv_mean > mean_conf + 5 and len(inv_words) >= len(words):
            text, words, boxes = inv_text, inv_words, inv_boxes
            mean_conf, low_frac = inv_mean, inv_low
            inverted = True

    return OcrResult(
        text=text,
        words=words,
        word_boxes=boxes,
        mean_conf=round(mean_conf, 1),
        low_conf_fraction=round(low_frac, 3),
        inverted=inverted,
        elapsed_ms=int((time.monotonic() - start) * 1000),
    )
