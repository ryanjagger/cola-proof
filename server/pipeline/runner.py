"""Per-record pipeline orchestration.

parse -> extract crops -> Tier A OCR -> match -> warning -> auto-status,
plus the escalation signals that will route a record to Tier B (phase 6).
Also the CLI corpus harness:

    python -m server.pipeline.runner sample-forms/registry/*.pdf [--out OUT_DIR] [--no-ocr]

Escalation policy (spec): escalate on doubt, never reject on doubt. The
signals are computed here even though Tier B is not wired yet, so the
harness can report the would-be escalation rate.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from .extract_labels import LabelCrop, extract_labels
from .match import (
    Outcome,
    SourcedText,
    Verdict,
    aggregate_outcomes,
    format_check_abv,
    format_check_net_contents,
    format_check_origin,
    locate_box,
    match_abv,
    match_bottler,
    match_class_type,
    match_name,
    match_net_contents,
)
from .ocr import OcrResult, ocr_crop
from .parse_form import ParsedForm, parse_form
from .vision import VisionClient, VisionResult
from .warning import WarningResult, WarningStatus, validate_warning_across

# Tier A trust thresholds that trigger Tier B (phase 6).
ESCALATE_MEAN_CONF = 65.0
ESCALATE_LOW_FRACTION = 0.30

# Corroboration rule floor: a vision-only reading of memorized-boilerplate-
# shaped text (warning, bottler line, origin statement) counts only if Tier A
# independently saw something at least this similar.
VISION_CORROBORATION_FLOOR = 55.0

_WARNING_OUTCOME = {
    WarningStatus.EXACT: Outcome.EXACT,
    WarningStatus.NEAR: Outcome.NEAR_MISS,  # escalate before flagging
    WarningStatus.PREFIX_NOT_CAPS: Outcome.MISMATCH,  # deterministic format fail
    WarningStatus.MISMATCH: Outcome.MISMATCH,
    WarningStatus.MISSING: Outcome.MISSING,  # unreadable != absent
}


@dataclass
class RecordResult:
    path: Path
    form: ParsedForm | None = None
    crops: list[LabelCrop] = field(default_factory=list)
    ocr: list[OcrResult] = field(default_factory=list)  # parallel to crops
    vision: dict[int, VisionResult] = field(default_factory=dict)  # by crop index
    verdicts: list[Verdict] = field(default_factory=list)
    warning: WarningResult | None = None
    auto_status: str | None = None  # Pass | Needs Review | Fail
    escalation_reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def process_pdf(path: Path, run_ocr: bool = False) -> RecordResult:
    result = RecordResult(path=path)
    try:
        doc = fitz.open(path)
    except Exception as e:
        result.errors.append(f"cannot open PDF: {e}")
        return result
    try:
        result.form = parse_form(doc)
    except Exception as e:
        result.errors.append(f"form parse failed: {e}")
    try:
        result.crops = extract_labels(doc)
    except Exception as e:
        result.errors.append(f"label extraction failed: {e}")

    if run_ocr and result.ok and result.form:
        result.ocr = [ocr_crop(c.data, c.dpi) for c in result.crops]
        evaluate(result)
    return result


def evaluate(result: RecordResult) -> None:
    """Match extracted text against the form and set auto-status +
    escalation signals. Uses Tier A OCR plus any Tier B transcriptions
    already gathered (result.vision)."""
    form = result.form
    matchable_texts = [
        SourcedText(o.text, "ocr", c.index)
        for c, o in zip(result.crops, result.ocr)
        if c.matchable and o.readable
    ]
    other_texts = [
        SourcedText(o.text, "ocr", c.index)
        for c, o in zip(result.crops, result.ocr)
        if not c.matchable and o.readable
    ]
    all_texts = [
        SourcedText(o.text, "ocr", c.index)
        for c, o in zip(result.crops, result.ocr)
        if o.readable
    ]
    # Tier A's reading pool, kept aside before vision text is mixed in:
    # the corroboration rules below need to ask what OCR alone saw.
    tier_a_texts = matchable_texts + other_texts
    for c in result.crops:
        v = result.vision.get(c.index)
        if v and v.ok and v.combined_text:
            st = SourcedText(v.combined_text, "vision", c.index)
            all_texts.append(st)
            if c.matchable:
                matchable_texts.append(st)
            else:
                other_texts.append(st)
    # Net contents and ABV routinely live on neck/strip labels, so the
    # numeric matchers may use 'other' crops too: they only accept
    # unit/%/proof patterns, so stray text can't false-match the way a
    # name could. Brand and class/type stay front/back-only.
    numeric_texts = matchable_texts + other_texts

    verdicts = []
    verdicts.append(match_name("brand_name", form.brand_name or "", matchable_texts))
    if form.has_net_contents_field and form.net_contents:
        v = match_net_contents(form.net_contents, numeric_texts)
        v = _container_wording_fallback(
            v, lambda texts: match_net_contents(form.net_contents, texts), form
        )
        verdicts.append(v)
    else:
        verdicts.append(format_check_net_contents(numeric_texts))
    if form.has_alcohol_content_field and form.alcohol_content:
        v = match_abv(form.alcohol_content, numeric_texts)
        v = _container_wording_fallback(
            v, lambda texts: match_abv(form.alcohol_content, texts), form
        )
        verdicts.append(v)
    else:
        verdicts.append(format_check_abv(numeric_texts))
    if form.class_type_description:
        verdicts.append(
            match_class_type(form.class_type_description, matchable_texts)
        )
    # Bottler and origin statements routinely live on strip/neck labels,
    # and a long company name can't false-match the way a short brand
    # could, so both checks read every crop.
    verdicts.append(match_bottler(form.applicant, all_texts))
    if form.source == "Imported":
        verdicts.append(format_check_origin(all_texts))
    if result.vision:
        _demote_vision_only_mismatches(verdicts, form, tier_a_texts)
        _demote_uncorroborated_presence(verdicts, form, tier_a_texts)
    result.verdicts = verdicts

    result.warning = validate_warning_across(all_texts)
    if result.warning.status == WarningStatus.EXACT and result.vision:
        # Corroboration rule: the statutory warning is memorized
        # boilerplate, so a vision model can fabricate it wholesale. If
        # Tier A saw nothing warning-like anywhere (it alone produced no
        # match at all), a vision-only "exact" is demoted to near ->
        # review rather than auto-passing.
        tier_a_only = validate_warning_across(
            [o.text for o in result.ocr if o.readable]
        )
        if tier_a_only.score < VISION_CORROBORATION_FLOOR:
            result.warning = WarningResult(
                WarningStatus.NEAR,
                result.warning.found_text,
                result.warning.score,
                note="Only the backup reader could read the warning — "
                "confirm the wording on the label image.",
                source=result.warning.source,
                source_crop=result.warning.source_crop,
            )
    outcomes = [v.outcome for v in verdicts]
    outcomes.append(_WARNING_OUTCOME[result.warning.status])
    result.auto_status = aggregate_outcomes(outcomes)
    # A photograph of the containers is never trusted on Tier A alone:
    # labels in a photo sit at an angle under glare, so Tesseract
    # agreement there is luck, not evidence. Until the backup reader has
    # read the photo, the record can at best be Needs Review.
    photo_unread = any(
        c.kind == "photo"
        and not ((v := result.vision.get(c.index)) is not None and v.ok)
        for c in result.crops
    )
    if photo_unread and result.auto_status == "Pass":
        result.auto_status = "Needs Review"
    _attach_boxes(result)
    result.escalation_reasons = _escalation_reasons(result)


def escalate(result: RecordResult, client: VisionClient, max_crops: int = 3) -> None:
    """Tier B: re-read the doubtful crops with the vision model,
    re-evaluating the record after each read and stopping as soon as it
    resolves to Pass — further reads can't improve a Pass, and on the
    CPU model every skipped crop is seconds off the batch tail. (The
    stop test is auto_status, not empty escalation reasons: low-OCR-conf
    reasons never clear, since Tier A confidences don't change.)

    Crop choice: matchable crops first (front/back carry the fields);
    'other' crops (strips, necks) when the warning still isn't exact or a
    presence-checked field (volume, ABV, bottler, origin) is unresolved —
    those statements are often printed there. Per-crop failures and timeouts are swallowed —
    the record just keeps its Tier A verdicts and stays in review:
    couldn't-read-clearly is never a rejection.
    """
    warning_open = result.warning and result.warning.status != WarningStatus.EXACT
    presence_missing = any(
        v.field in ("net_contents", "alcohol_content", "bottler", "country_of_origin")
        and v.outcome == Outcome.MISSING
        for v in result.verdicts
    )
    candidates = [c for c in result.crops if c.matchable]
    if warning_open or presence_missing:
        others = [c for c in result.crops if not c.matchable]
        others.sort(key=lambda c: c.px_width * c.px_height, reverse=True)
        candidates.extend(others)

    for crop in candidates[:max_crops]:
        vr = client.read_crop(crop.data, crop.ext)
        result.vision[crop.index] = vr
        if vr.ok:
            evaluate(result)
            if result.auto_status == "Pass":
                break


def _attach_boxes(result: RecordResult) -> None:
    """Resolve each OCR-sourced value back to its word boxes on the crop,
    so the UI can highlight where it was read. Vision-sourced values stay
    box-less: the vision reader returns text without geometry."""

    def crop_ocr(crop_index: int | None) -> OcrResult | None:
        for c, o in zip(result.crops, result.ocr):
            if c.index == crop_index:
                return o
        return None

    for v in result.verdicts:
        o = crop_ocr(v.source_crop)
        if v.source == "ocr" and o and v.label_value:
            v.box = locate_box(v.label_value, o.words, o.word_boxes)
    w = result.warning
    if w:
        o = crop_ocr(w.source_crop)
        if w.source == "ocr" and o and w.found_text:
            w.box = locate_box(w.found_text, o.words, o.word_boxes)


def _demote_vision_only_mismatches(
    verdicts: list[Verdict], form: ParsedForm, tier_a_texts: list[SourcedText]
) -> None:
    """Numeric mirror of the warning corroboration rule: a MISMATCH whose
    label value exists only in a Tier B transcription is doubt, not
    evidence — the two readers never agreed on anything. A small VLM
    misreads hard label art (Cotton Hollow's "750ml" came back as
    "500 ml"), and a fabricated volume must not flip an unreadable field
    into a Fail recommendation. Demoted to near-miss -> Needs Review."""
    rematchers = {
        "net_contents": lambda texts: match_net_contents(form.net_contents, texts),
        "alcohol_content": lambda texts: match_abv(form.alcohol_content, texts),
    }
    for v in verdicts:
        if v.outcome != Outcome.MISMATCH or v.field not in rematchers:
            continue
        if v.form_value is None:  # format checks never mismatch, but be safe
            continue
        if rematchers[v.field](tier_a_texts).outcome == Outcome.MISSING:
            v.outcome = Outcome.NEAR_MISS
            v.note = "only the backup reader saw this value"


def _demote_uncorroborated_presence(
    verdicts: list[Verdict], form: ParsedForm, tier_a_texts: list[SourcedText]
) -> None:
    """Presence mirror of the warning corroboration rule: a bottler line
    or "PRODUCT OF ..." statement is exactly the memorized-boilerplate
    shape a small VLM can fabricate, so a vision-only EXACT on those is
    doubt, not evidence. Demote to near-miss -> Needs Review unless
    Tier A independently saw something similar enough."""
    for v in verdicts:
        if v.outcome != Outcome.EXACT or v.source != "vision":
            continue
        if v.field == "bottler":
            tier_a = match_bottler(form.applicant, tier_a_texts)
            corroborated = (tier_a.score or 0.0) >= VISION_CORROBORATION_FLOOR
        elif v.field == "country_of_origin":
            corroborated = (
                format_check_origin(tier_a_texts).outcome != Outcome.MISSING
            )
        else:
            continue
        if not corroborated:
            v.outcome = Outcome.NEAR_MISS
            v.note = "only the backup reader saw this value"


def _container_wording_fallback(verdict, rematch, form: ParsedForm):
    """A value absent from the labels may be blown/branded/embossed on
    the container instead — the form's container-wording item (15/18)
    records that wording, so it legitimately satisfies the check."""
    if verdict.outcome != Outcome.MISSING or not form.container_wording:
        return verdict
    retry = rematch([SourcedText(form.container_wording, "form")])
    if retry.outcome == Outcome.EXACT:
        retry.note = "stated on container (form: blown/branded/embossed wording)"
        return retry
    return verdict


def _escalation_reasons(result: RecordResult) -> list[str]:
    reasons = []
    for c in result.crops:
        # Labels affixed as a photograph of the containers always get the
        # backup reader — there is no per-label crop for Tier A to trust.
        if c.kind == "photo":
            reasons.append(
                f"labels arrived as a photo of the containers (crop {c.index}) "
                "— always re-read with AI"
            )
    for c, o in zip(result.crops, result.ocr):
        if not c.matchable:
            continue
        if o.mean_conf < ESCALATE_MEAN_CONF or o.low_conf_fraction > ESCALATE_LOW_FRACTION:
            reasons.append(
                f"crop {c.index} ({c.kind}) read poorly "
                f"(conf {o.mean_conf}, weak words {o.low_conf_fraction:.0%})"
            )
    for v in result.verdicts:
        # MISMATCH escalates too: flag only after the best reader agrees.
        if v.outcome in (Outcome.MISSING, Outcome.NEAR_MISS, Outcome.MISMATCH):
            reasons.append(f"{v.field}: {v.outcome.value}")
    if result.warning and result.warning.status in (
        WarningStatus.NEAR, WarningStatus.MISSING, WarningStatus.MISMATCH
    ):
        reasons.append(f"government warning: {result.warning.status.value}")
    return reasons


def write_crops(result: RecordResult, out_dir: Path) -> list[Path]:
    record_id = result.form.ttb_id if result.form and result.form.ttb_id else result.path.stem
    crop_dir = out_dir / record_id
    crop_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for crop in result.crops:
        p = crop_dir / f"{crop.index}_{crop.kind}.{crop.ext}"
        p.write_bytes(crop.data)
        paths.append(p)
    return paths


def _print_record(result: RecordResult) -> None:
    print("=" * 72)
    print(result.path.name)
    for e in result.errors:
        print(f"  ERROR: {e}")
    f = result.form
    if f:
        rows = [
            ("shape", f.shape),
            ("ttb_id", f.ttb_id),
            ("revision", f.revision),
            ("status", f.status),
            ("brand_name", f.brand_name),
            ("fanciful_name", f.fanciful_name),
            ("product_type", f.product_type),
            ("source", f.source),
            ("applicant", f.applicant.replace("\n", " | ") if f.applicant else None),
            ("serial_number", f.serial_number),
            ("class_type", f.class_type_description),
            ("net_contents", f.net_contents if f.has_net_contents_field else "(no field on this revision)"),
            ("alcohol_content", f.alcohol_content if f.has_alcohol_content_field else "(no field on this revision)"),
            ("container_wording", f.container_wording),
        ]
        for k, v in rows:
            print(f"  {k:18s} {v if v is not None else ''}")
        for w in f.warnings:
            print(f"  WARN: {w}")
    for i, c in enumerate(result.crops):
        aspect = "" if c.aspect_ok else "  ASPECT MISMATCH"
        ocr_note = ""
        if i < len(result.ocr):
            o = result.ocr[i]
            inv = " inv" if o.inverted else ""
            ocr_note = f"  ocr: conf={o.mean_conf} weak={o.low_conf_fraction:.0%}{inv} {o.elapsed_ms}ms"
        tier_b = ""
        if (v := result.vision.get(c.index)) is not None:
            err = f" ({v.error})" if v.error else ""
            tier_b = f"  tierB: ok={v.ok} {v.elapsed_ms}ms{err}"
        print(
            f"  crop[{c.index}] {c.kind:6s} {c.caption_type:28s} "
            f'{c.width_in}"x{c.height_in}" {c.px_width}x{c.px_height}px '
            f"{c.dpi}dpi p{c.page} {c.ext}{aspect}{ocr_note}{tier_b}"
        )
    def _src(source: str | None, crop: int | None) -> str:
        if not source:
            return ""
        return f"  <- {source}" + (f" crop {crop}" if crop is not None else "")

    for v in result.verdicts:
        score = f" {v.score:.0f}" if v.score is not None else ""
        norm = " (normalized)" if v.normalized else ""
        note = f"  [{v.note}]" if v.note else ""
        print(
            f"  {v.field:18s} {v.outcome.value:10s}{score} "
            f"form={v.form_value!r} label={v.label_value!r}{norm}{note}"
            f"{_src(v.source, v.source_crop)}"
        )
    if result.warning:
        w = result.warning
        print(
            f"  warning            {w.status.value} ({w.score:.0f})"
            f"{_src(w.source, w.source_crop)}"
        )
    if result.auto_status:
        print(f"  AUTO-STATUS        {result.auto_status}")
    for r in result.escalation_reasons:
        print(f"  ESCALATE: {r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="COLA Proof corpus harness")
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out/crops"))
    ap.add_argument("--no-ocr", action="store_true", help="parse/extract only")
    ap.add_argument(
        "--vision",
        metavar="BASE_URL",
        help="Tier B endpoint (e.g. http://localhost:8090/v1); enables escalation",
    )
    args = ap.parse_args(argv)
    client = VisionClient(args.vision, "qwen3-vl-4b") if args.vision else None

    failures = 0
    n_crops = 0
    statuses: dict[str, int] = {}
    escalations = 0
    tier_b_crops = 0
    tier_b_ms = 0
    for path in args.pdfs:
        result = process_pdf(path, run_ocr=not args.no_ocr)
        needed_escalation = result.ok and bool(result.escalation_reasons)
        if client and needed_escalation:
            escalate(result, client)
            tier_b_crops += len(result.vision)
            tier_b_ms += sum(v.elapsed_ms for v in result.vision.values())
        _print_record(result)
        if result.ok:
            write_crops(result, args.out)
            n_crops += len(result.crops)
            if result.auto_status:
                statuses[result.auto_status] = statuses.get(result.auto_status, 0) + 1
            if needed_escalation:
                escalations += 1
        else:
            failures += 1

    print("=" * 72)
    print(
        f"{len(args.pdfs)} PDFs, {failures} failures, "
        f"{n_crops} crops written to {args.out}"
    )
    if statuses:
        summary = " · ".join(f"{k}: {v}" for k, v in sorted(statuses.items()))
        verb = "escalated" if client else "would escalate"
        print(f"auto-status: {summary} · {verb}: {escalations}")
    if tier_b_crops:
        print(
            f"tier B: {tier_b_crops} crops read, "
            f"{tier_b_ms / tier_b_crops / 1000:.1f}s avg per crop"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
