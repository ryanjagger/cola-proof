"""CSV + PDF export — the durable artifact.

Source PDFs and crops are session-scoped and purged; what persists is
this export: the agent's dispositions, the pipeline's auto-statuses, and
the audit trail between them. auto_status and disposition stay separate
columns so disagreements are filterable in the CSV.

Scope names reuse the queue filter vocabulary exactly:
all | open | failed | review | passed.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SCOPES = ("all", "open", "failed", "review", "passed")

_FIELD_LABELS = {
    "brand_name": "Brand name",
    "net_contents": "Net contents",
    "alcohol_content": "Alcohol content",
    "class_type": "Class/type",
}

_OUTCOME_TEXT = {
    "exact": "matches",
    "near_miss": "close, not identical",
    "mismatch": "does not match",
    "missing": "not found on label",
}

_WARNING_TEXT = {
    "exact": "exact required wording",
    "prefix_not_caps": "prefix not all capitals",
    "near": "almost matches",
    "mismatch": "wording differs",
    "missing": "not found",
}


def filter_scope(records: list[dict], scope: str) -> list[dict]:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if scope == "all":
        return records
    if scope == "open":
        return [
            r for r in records if r["state"] == "done" and r["disposition"] is None
        ]
    if scope == "failed":
        return [
            r
            for r in records
            if r["auto_status"] == "Fail" or r["state"] == "error"
        ]
    if scope == "review":
        return [r for r in records if r["auto_status"] == "Needs Review"]
    return [r for r in records if r["auto_status"] == "Pass"]


# --- CSV ---------------------------------------------------------------------


def batch_csv(records: list[dict]) -> str:
    out = io.StringIO()
    fields = list(_FIELD_LABELS)
    writer = csv.writer(out)
    writer.writerow(
        [
            "ttb_id", "filename", "brand_name", "fanciful_name",
            "class_type_description", "product_type", "source", "revision",
            "auto_status", "disposition", "dispositioned_by",
            "dispositioned_at", "note",
            *(f"{f}_result" for f in fields),
            "warning_result", "escalated", "error",
        ]
    )
    for r in records:
        form = r.get("form") or {}
        verdicts = {v["field"]: v for v in (r.get("verdicts") or [])}
        warning = r.get("warning") or {}
        writer.writerow(
            [
                r.get("ttb_id") or "",
                r["filename"],
                form.get("brand_name") or "",
                form.get("fanciful_name") or "",
                form.get("class_type_description") or "",
                form.get("product_type") or "",
                form.get("source") or "",
                form.get("revision") or "",
                r.get("auto_status") or "",
                r.get("disposition") or "",
                r.get("dispositioned_by") or "",
                r.get("dispositioned_at") or "",
                r.get("note") or "",
                *(
                    _OUTCOME_TEXT.get(verdicts[f]["outcome"], "")
                    if f in verdicts
                    else ""
                    for f in fields
                ),
                _WARNING_TEXT.get(warning.get("status"), ""),
                "yes" if r.get("escalation") else "no",
                r.get("error") or "",
            ]
        )
    return out.getvalue()


# --- PDF ---------------------------------------------------------------------

_styles = getSampleStyleSheet()
_H1 = _styles["Heading1"]
_H2 = _styles["Heading2"]
_BODY = _styles["BodyText"]
_SMALL = ParagraphStyle("small", parent=_BODY, fontSize=8, textColor=colors.grey)
_MONO = ParagraphStyle("mono", parent=_BODY, fontName="Courier", fontSize=8)


def _crop_path(record: dict, media_root: Path) -> Path | None:
    """Front crop path for embedding, if the media still exists."""
    for crop in record.get("crops") or []:
        if crop["kind"] == "front":
            p = media_root / record["batch_id"] / record["id"] / crop["filename"]
            if p.exists():
                return p
    return None


def _record_section(record: dict, media_root: Path, flagged_only_crops: bool) -> list:
    form = record.get("form") or {}
    flow: list = []
    title = form.get("brand_name") or record["filename"]
    flow.append(Paragraph(f"{title} — {record.get('ttb_id') or ''}", _H2))
    disposition = record.get("disposition") or "OPEN"
    by = record.get("dispositioned_by")
    flow.append(
        Paragraph(
            f"Automatic check: <b>{record.get('auto_status') or record.get('state')}</b>"
            f" &nbsp;·&nbsp; Disposition: <b>{disposition}</b>"
            + (f" by {by} ({record.get('dispositioned_at') or ''})" if by else ""),
            _BODY,
        )
    )
    if record.get("note"):
        flow.append(Paragraph(f"Note: {record['note']}", _BODY))
    if record.get("error"):
        flow.append(Paragraph(f"Could not process: {record['error']}", _BODY))

    rows = [["Field", "On the form", "Read from label", "Result"]]
    for v in record.get("verdicts") or []:
        rows.append(
            [
                _FIELD_LABELS.get(v["field"], v["field"]),
                Paragraph(v.get("form_value") or "—", _BODY),
                Paragraph(v.get("label_value") or "—", _BODY),
                _OUTCOME_TEXT.get(v["outcome"], v["outcome"]),
            ]
        )
    warning = record.get("warning")
    if warning:
        rows.append(
            [
                "Gov. warning",
                "27 CFR 16.21 wording",
                Paragraph((warning.get("found_text") or "—")[:300], _MONO),
                _WARNING_TEXT.get(warning.get("status"), ""),
            ]
        )
    if len(rows) > 1:
        table = Table(rows, colWidths=[1.0 * inch, 1.7 * inch, 2.6 * inch, 1.2 * inch])
        table.setStyle(
            TableStyle(
                [
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        flow.append(table)

    flagged = record.get("auto_status") in ("Fail", "Needs Review")
    if (flagged or not flagged_only_crops) and (
        path := _crop_path(record, media_root)
    ):
        crop = next(c for c in record["crops"] if c["kind"] == "front")
        w, h = crop["px_width"], crop["px_height"]
        max_w, max_h = 3.0 * inch, 3.0 * inch
        scale = min(max_w / w, max_h / h)
        flow.append(Spacer(1, 6))
        flow.append(Image(str(path), width=w * scale, height=h * scale))
        flow.append(Paragraph("Front label as submitted", _SMALL))
    flow.append(Spacer(1, 10))
    flow.append(HRFlowable(width="100%", color=colors.lightgrey))
    flow.append(Spacer(1, 10))
    return flow


def batch_pdf(
    records: list[dict], summary: dict, media_root: Path, scope: str
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter, title="COLA Proof batch report",
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )
    flow: list = [Paragraph("COLA Proof — batch report", _H1)]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    flow.append(
        Paragraph(
            f"Generated {generated} · scope: {scope} · "
            f"{summary['processed']} processed · {summary['failed']} failed · "
            f"{summary['needs_review']} need review · {summary['passed']} passed · "
            f"{summary['open']} open",
            _BODY,
        )
    )
    flow.append(Spacer(1, 14))
    for record in records:
        flow.extend(_record_section(record, media_root, flagged_only_crops=True))
    doc.build(flow)
    return buf.getvalue()


def record_pdf(record: dict, media_root: Path) -> bytes:
    """Single-record report — 'send this one back with evidence'."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter, title=f"COLA Proof record {record.get('ttb_id')}",
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )
    flow: list = [Paragraph("COLA Proof — record report", _H1)]
    flow.extend(_record_section(record, media_root, flagged_only_crops=False))
    doc.build(flow)
    return buf.getvalue()
