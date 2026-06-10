"""FastAPI app: batch upload, progressive results over SSE, review API,
crop serving, and the built static frontend — one port, one container.

Processing runs on a bounded thread pool (Tesseract is a subprocess, so
threads parallelize fine). The SSE endpoint tails the store and emits a
record event as each record finishes — progressive fill is what makes a
200-record batch feel fast, so the agent can start triaging failures
while the rest still process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .export import SCOPES, batch_csv, batch_pdf, filter_scope, record_pdf
from .pipeline.runner import RecordResult, escalate, process_pdf
from .pipeline.vision import VisionClient
from .store import Store

app = FastAPI(title="COLA Proof")
store = Store(settings.db_path, settings.media_dir)
_executor = ThreadPoolExecutor(max_workers=settings.ocr_workers)
# Tier B is a separate, smaller pool: the CPU vision model is slow, so
# escalations queue here while Tier A keeps streaming results.
_vision_executor = ThreadPoolExecutor(max_workers=settings.vision_workers)
_vision = (
    VisionClient(settings.vision_base_url, settings.vision_model)
    if settings.vision_base_url
    else None
)


# --- processing -------------------------------------------------------------


def _crop_meta(result: RecordResult, crop_dir: Path) -> list[dict]:
    crop_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for crop, ocr in zip(result.crops, result.ocr or [None] * len(result.crops)):
        filename = f"{crop.index}_{crop.kind}.{crop.ext}"
        (crop_dir / filename).write_bytes(crop.data)
        vision = result.vision.get(crop.index)
        out.append(
            {
                "index": crop.index,
                "kind": crop.kind,
                "caption_type": crop.caption_type,
                "width_in": crop.width_in,
                "height_in": crop.height_in,
                "px_width": crop.px_width,
                "px_height": crop.px_height,
                "dpi": crop.dpi,
                "ext": crop.ext,
                "filename": filename,
                "ocr_conf": ocr.mean_conf if ocr else None,
                # Tier B audit trail; None when the crop wasn't re-read.
                "vision_ok": vision.ok if vision else None,
                "vision_ms": vision.elapsed_ms if vision else None,
                "vision_error": vision.error if vision else None,
            }
        )
    return out


def _finish_record(record_id: str, batch_id: str, result: RecordResult) -> None:
    crops = _crop_meta(result, store.batch_media_dir(batch_id) / record_id)
    store.record_done(
        record_id,
        ttb_id=result.form.ttb_id,
        auto_status=result.auto_status or "Needs Review",
        form=dataclasses.asdict(result.form),
        crops=crops,
        verdicts=[dataclasses.asdict(v) for v in result.verdicts],
        warning=dataclasses.asdict(result.warning) if result.warning else None,
        escalation=result.escalation_reasons,
    )


def _escalate_record(record_id: str, batch_id: str, result: RecordResult) -> None:
    try:
        escalate(result, _vision)
    except Exception:
        pass  # Tier B trouble degrades to Tier A verdicts (review), never an error
    try:
        _finish_record(record_id, batch_id, result)
    except Exception as e:
        store.record_error(record_id, f"{type(e).__name__}: {e}")


def _process_record(record_id: str, batch_id: str, pdf_path: Path) -> None:
    store.record_processing(record_id)
    try:
        result = process_pdf(pdf_path, run_ocr=True)
        if not result.ok:
            store.record_error(record_id, "; ".join(result.errors))
            return
        if _vision and result.escalation_reasons:
            # Stays "processing" while it waits its turn for Tier B; the
            # rest of the batch keeps streaming.
            _vision_executor.submit(_escalate_record, record_id, batch_id, result)
            return
        _finish_record(record_id, batch_id, result)
    except Exception as e:  # never lose a record silently
        store.record_error(record_id, f"{type(e).__name__}: {e}")


# --- batches ----------------------------------------------------------------


@app.post("/api/batches")
async def create_batch(files: list[UploadFile]) -> dict:
    if not files:
        raise HTTPException(400, "no files uploaded")
    batch = store.create_batch(name=f"{len(files)} PDFs")
    media = store.batch_media_dir(batch["id"])
    record_ids = []
    for f in files:
        record_id = store.add_record(batch["id"], f.filename or "upload.pdf")
        pdf_path = media / f"{record_id}.pdf"
        pdf_path.write_bytes(await f.read())
        _executor.submit(_process_record, record_id, batch["id"], pdf_path)
        record_ids.append(record_id)
    return {"batch": batch, "record_ids": record_ids}


@app.get("/api/batches")
def list_batches() -> list[dict]:
    return store.list_batches()


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "batch not found")
    return batch | {"summary": store.batch_summary(batch_id)}


@app.delete("/api/batches/{batch_id}")
def delete_batch(batch_id: str) -> dict:
    if not store.get_batch(batch_id):
        raise HTTPException(404, "batch not found")
    store.delete_batch(batch_id)
    return {"deleted": batch_id}


@app.get("/api/batches/{batch_id}/records")
def list_records(batch_id: str) -> list[dict]:
    if not store.get_batch(batch_id):
        raise HTTPException(404, "batch not found")
    return store.list_records(batch_id)


@app.get("/api/batches/{batch_id}/events")
async def batch_events(batch_id: str) -> StreamingResponse:
    """SSE: one `record` event as each record finishes, `summary` events
    as counts move, and a final `done` when the batch finishes processing."""
    if not store.get_batch(batch_id):
        raise HTTPException(404, "batch not found")

    async def stream():
        seen: set[str] = set()
        while True:
            records = store.list_records(batch_id)
            for r in records:
                if r["state"] in ("done", "error") and r["id"] not in seen:
                    seen.add(r["id"])
                    yield f"event: record\ndata: {json.dumps(r)}\n\n"
            summary = store.batch_summary(batch_id)
            yield f"event: summary\ndata: {json.dumps(summary)}\n\n"
            if summary["total"] and summary["processed"] == summary["total"]:
                yield f"event: done\ndata: {json.dumps(summary)}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- export -----------------------------------------------------------------


def _scoped_records(batch_id: str, scope: str) -> list[dict]:
    if not store.get_batch(batch_id):
        raise HTTPException(404, "batch not found")
    if scope not in SCOPES:
        raise HTTPException(422, f"scope must be one of {', '.join(SCOPES)}")
    return filter_scope(store.list_records(batch_id), scope)


@app.get("/api/batches/{batch_id}/export.csv")
def export_csv(batch_id: str, scope: str = "all") -> Response:
    records = _scoped_records(batch_id, scope)
    return Response(
        batch_csv(records),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="cola-proof-{batch_id}-{scope}.csv"'
        },
    )


@app.get("/api/batches/{batch_id}/export.pdf")
def export_pdf(batch_id: str, scope: str = "all") -> Response:
    records = _scoped_records(batch_id, scope)
    pdf = batch_pdf(records, store.batch_summary(batch_id), store.media_root, scope)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="cola-proof-{batch_id}-{scope}.pdf"'
        },
    )


@app.get("/api/records/{record_id}/export.pdf")
def export_record_pdf(record_id: str) -> Response:
    record = store.get_record(record_id)
    if not record:
        raise HTTPException(404, "record not found")
    pdf = record_pdf(record, store.media_root)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="cola-proof-{record.get("ttb_id") or record_id}.pdf"'
        },
    )


# --- records ----------------------------------------------------------------


@app.get("/api/records/{record_id}")
def get_record(record_id: str) -> dict:
    record = store.get_record(record_id)
    if not record:
        raise HTTPException(404, "record not found")
    return record


@app.get("/api/records/{record_id}/crops/{index}")
def get_crop(record_id: str, index: int) -> FileResponse:
    record = store.get_record(record_id)
    if not record or not record.get("crops"):
        raise HTTPException(404, "record not found")
    crops = [c for c in record["crops"] if c["index"] == index]
    if not crops:
        raise HTTPException(404, "crop not found")
    path = (
        store.batch_media_dir(record["batch_id"])
        / record_id
        / crops[0]["filename"]
    )
    if not path.exists():
        raise HTTPException(410, "media purged")
    return FileResponse(path)


class DispositionBody(BaseModel):
    disposition: str  # Approved | Rejected
    by: str
    note: str | None = None


@app.post("/api/records/{record_id}/disposition")
def set_disposition(record_id: str, body: DispositionBody) -> dict:
    try:
        return store.set_disposition(
            record_id, body.disposition, by=body.by, note=body.note
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


# --- static frontend --------------------------------------------------------

_web_dist = Path(__file__).parent.parent / "web" / "dist"
if _web_dist.exists():
    app.mount("/assets", StaticFiles(directory=_web_dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # Client-side routes (e.g. /batches/abc) all serve the SPA shell.
        candidate = _web_dist / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_web_dist / "index.html")
