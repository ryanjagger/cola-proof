"""Per-record pipeline orchestration.

parse -> extract crops -> Tier A OCR -> match -> warning -> auto-status,
plus the escalation signals that will route a record to Tier B (phase 6).
Also the CLI corpus harness:

    python -m server.pipeline.runner sample-forms/*.pdf [--out OUT_DIR] [--no-ocr]

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
    Verdict,
    aggregate_outcomes,
    format_check_abv,
    format_check_net_contents,
    match_abv,
    match_class_type,
    match_name,
    match_net_contents,
)
from .ocr import OcrResult, ocr_crop
from .parse_form import ParsedForm, parse_form
from .warning import WarningResult, WarningStatus, validate_warning_across

# Tier A trust thresholds that trigger Tier B (phase 6).
ESCALATE_MEAN_CONF = 65.0
ESCALATE_LOW_FRACTION = 0.30

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
    """Match OCR text against the form and set auto-status + escalation
    signals. Expects result.ocr parallel to result.crops."""
    form = result.form
    matchable_texts = [
        o.text
        for c, o in zip(result.crops, result.ocr)
        if c.matchable and o.readable
    ]
    all_texts = [o.text for o in result.ocr if o.readable]

    verdicts = []
    verdicts.append(match_name("brand_name", form.brand_name or "", matchable_texts))
    if form.has_net_contents_field and form.net_contents:
        v = match_net_contents(form.net_contents, matchable_texts)
        v = _container_wording_fallback(
            v, lambda texts: match_net_contents(form.net_contents, texts), form
        )
        verdicts.append(v)
    else:
        verdicts.append(format_check_net_contents(matchable_texts))
    if form.has_alcohol_content_field and form.alcohol_content:
        v = match_abv(form.alcohol_content, matchable_texts)
        v = _container_wording_fallback(
            v, lambda texts: match_abv(form.alcohol_content, texts), form
        )
        verdicts.append(v)
    else:
        verdicts.append(format_check_abv(matchable_texts))
    if form.class_type_description:
        verdicts.append(
            match_class_type(form.class_type_description, matchable_texts)
        )
    result.verdicts = verdicts

    result.warning = validate_warning_across(all_texts)
    outcomes = [v.outcome for v in verdicts]
    outcomes.append(_WARNING_OUTCOME[result.warning.status])
    result.auto_status = aggregate_outcomes(outcomes)
    result.escalation_reasons = _escalation_reasons(result)


def _container_wording_fallback(verdict, rematch, form: ParsedForm):
    """A value absent from the labels may be blown/branded/embossed on
    the container instead — the form's container-wording item (15/18)
    records that wording, so it legitimately satisfies the check."""
    if verdict.outcome != Outcome.MISSING or not form.container_wording:
        return verdict
    retry = rematch([form.container_wording])
    if retry.outcome == Outcome.EXACT:
        retry.note = "stated on container (form: blown/branded/embossed wording)"
        return retry
    return verdict


def _escalation_reasons(result: RecordResult) -> list[str]:
    reasons = []
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
            ("ttb_id", f.ttb_id),
            ("revision", f.revision),
            ("status", f.status),
            ("brand_name", f.brand_name),
            ("fanciful_name", f.fanciful_name),
            ("product_type", f.product_type),
            ("source", f.source),
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
        print(
            f"  crop[{c.index}] {c.kind:6s} {c.caption_type:28s} "
            f'{c.width_in}"x{c.height_in}" {c.px_width}x{c.px_height}px '
            f"{c.dpi}dpi p{c.page} {c.ext}{aspect}{ocr_note}"
        )
    for v in result.verdicts:
        score = f" {v.score:.0f}" if v.score is not None else ""
        norm = " (normalized)" if v.normalized else ""
        note = f"  [{v.note}]" if v.note else ""
        print(
            f"  {v.field:18s} {v.outcome.value:10s}{score} "
            f"form={v.form_value!r} label={v.label_value!r}{norm}{note}"
        )
    if result.warning:
        print(f"  warning            {result.warning.status.value} ({result.warning.score:.0f})")
    if result.auto_status:
        print(f"  AUTO-STATUS        {result.auto_status}")
    for r in result.escalation_reasons:
        print(f"  ESCALATE: {r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="COLA Proof corpus harness")
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out/crops"))
    ap.add_argument("--no-ocr", action="store_true", help="parse/extract only")
    args = ap.parse_args(argv)

    failures = 0
    n_crops = 0
    statuses: dict[str, int] = {}
    escalations = 0
    for path in args.pdfs:
        result = process_pdf(path, run_ocr=not args.no_ocr)
        _print_record(result)
        if result.ok:
            write_crops(result, args.out)
            n_crops += len(result.crops)
            if result.auto_status:
                statuses[result.auto_status] = statuses.get(result.auto_status, 0) + 1
            if result.escalation_reasons:
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
        print(f"auto-status: {summary} · would escalate: {escalations}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
