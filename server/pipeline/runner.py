"""Per-record pipeline orchestration.

Phase 1 scope: CLI corpus harness. Parses Part I fields and extracts
caption-classified label crops for each PDF, prints a summary, writes
crops to disk, and exits non-zero if any record fails. Later phases add
OCR (Tier A), vision escalation (Tier B), matching, and the batch worker
pool here.

Usage:
    python -m server.pipeline.runner sample-forms/*.pdf [--out OUT_DIR]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from .extract_labels import LabelCrop, extract_labels
from .parse_form import ParsedForm, parse_form


@dataclass
class RecordResult:
    path: Path
    form: ParsedForm | None = None
    crops: list[LabelCrop] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def process_pdf(path: Path) -> RecordResult:
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
    return result


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
    if result.errors:
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
    for c in result.crops:
        aspect = "" if c.aspect_ok else "  ASPECT MISMATCH"
        print(
            f"  crop[{c.index}] {c.kind:6s} {c.caption_type:28s} "
            f'{c.width_in}"x{c.height_in}" {c.px_width}x{c.px_height}px '
            f"{c.dpi}dpi p{c.page} {c.ext}{aspect}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="COLA Proof corpus harness")
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out/crops"))
    args = ap.parse_args(argv)

    failures = 0
    n_crops = 0
    for path in args.pdfs:
        result = process_pdf(path)
        _print_record(result)
        if result.ok:
            write_crops(result, args.out)
            n_crops += len(result.crops)
        else:
            failures += 1

    print("=" * 72)
    print(
        f"{len(args.pdfs)} PDFs, {failures} failures, "
        f"{n_crops} crops written to {args.out}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
