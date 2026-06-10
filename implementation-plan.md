# COLA Proof — Implementation Plan

## Context

Build the tool described in `cola-proof-spec.md`: a local-first batch verifier that checks alcohol-label crops embedded in TTB COLA application PDFs (form TTB F 5100.31) against the form's typed fields, flagging mismatches for human review. Constraints: ~5s perceived response via progressive fill, no outbound network for inference, non-technical users, batch-native (200–300 PDFs), strict deterministic government-warning check, fuzzy matching elsewhere, session-scoped source data.

### New findings from inspecting the 30 sample PDFs (these refine the spec)

1. **Two form revisions exist.** Pre-2016 forms (≈half the samples) have typed fields `12. NET CONTENTS` (e.g. `750 MILLILITERS`) and `13. ALCOHOL CONTENT` (e.g. `42%`). The 06-2016 revision **drops both fields** — the only "net contents" text is boilerplate. → The parser needs a per-revision field map, and for 2016+ records ABV/net-contents become *label-format checks* ("present and plausible on label") rather than form-vs-label matches. The UI must say so ("not on form — format check only").
2. **Label images pair 1:1, in document order, with text-layer captions**: `Image Type: Brand (front) or keg collar / Back / Other` + `Actual Dimensions: W x H inches`. Verified caption→image pairing by aspect ratio (e.g. 1.78"×4.38" caption ↔ 415×1000px JPEG). → Label classification is **deterministic from captions**, no size/position heuristics needed. Neck/"Other" crops are identified and skipped for matching by type.
3. **Pixel dims ÷ physical inches = effective DPI** (observed 110–320 DPI) — a free OCR trust signal for escalation.
4. All label images are JPEG XObjects (`/DCTDecode`) — extract raw bytes directly, no recompression.
5. **Class/type description** (e.g. `OTHER GRAPE BRANDY (PISCO, GRAPPA) FB`) and COLA status live on the "FOR TTB USE ONLY" page text layer.
6. Page counts vary 2–4; page 0 is always Part I; label pages follow "AFFIX COMPLETE SET OF LABELS BELOW".

## Stack

- **Python 3.12 via `uv`** (system 3.14 is too new for some OCR/imaging wheels). Backend: **FastAPI** + uvicorn, **SQLite** (stdlib) for decision metadata only.
- **PyMuPDF (fitz)** for parsing: word boxes for layout-aware field extraction, image bytes + rects for crop/caption pairing. (AGPL — fine for prototype; pdfplumber+pypdf is the MIT fallback if licensing ever matters.)
- **Tier A OCR: Tesseract** via pytesseract, because it returns per-word confidences (needed to drive escalation) and multi-language packs (eng+spa+ita+fra+por+deu). Preprocess: upscale low-DPI crops 2–4× with Pillow.
- **Tier B vision: small self-hosted CPU model on llama.cpp.** A **`llama-server` sidecar** (CPU-only) serving **Qwen2.5-VL-3B-Instruct** — quantized GGUF + matching mmproj projector, pinned by exact release/hash — the strongest small VLM for dense text transcription. llama-server keeps the model resident (no idle-unload cold starts) and exposes the **OpenAI-compatible API**; the app talks to it through a client with a configurable base URL (`VISION_BASE_URL`), so the same code works against local llama-server in dev (Metal), the Railway sidecar (CPU), and whatever runtime (vLLM, Ollama) the agency stands up. Same runtime, same GGUF everywhere — strict dev/prod parity. This answers spec open-question 1 with no outbound network anywhere. Tier B returns structured JSON transcription via llama.cpp's JSON-schema-constrained output (brand, ABV, net contents, verbatim warning text); the deterministic validator always runs on the output — the model never judges compliance.
  - **Latency implication**: CPU Tier B will run ~10–30s/crop. That's acceptable *only* because escalation is the exception — the runner treats Tier B as a bounded-concurrency background queue (1–2 workers) while Tier A keeps streaming results, and progressive fill keeps the agent triaging. If 3B proves too slow on Railway vCPUs, moondream2 (~2B) is the drop-down; measure in phase 6.
- **rapidfuzz** for fuzzy scores; **reportlab** for PDF export; stdlib csv for CSV.
- **Frontend: React + Vite + TypeScript + Tailwind**, built into static files and served by FastAPI (single port, single app container). Progressive results via **SSE**.
- **Deployment: Docker image on Railway.** Multi-stage Dockerfile (node builds `web/` → python:3.12-slim with `tesseract-ocr` + language packs, app + static). Two Railway services: the app (volume mounted for SQLite + session media) and the llama.cpp sidecar (`ghcr.io/ggml-org/llama.cpp:server` image, GGUF + mmproj staged on a volume, private networking, no public ingress). A `docker-compose.yml` mirrors the same topology for on-prem/agency use — the cloud demo and the local-first story are the same artifact.
- Repo layout: build at repo root — `server/` and `web/`. Remove the empty nested `cola-proof/` scaffold (it's a bare `git init`, zero commits, zero files).

## Module map (server/)

```
server/
  app.py                 # FastAPI app, routes, SSE, static serving
  config.py              # env-driven settings (VISION_BASE_URL, DATA_DIR, thresholds)
  store.py               # SQLite schema + session media dir (temp, purgeable)
  export.py              # CSV + PDF report (batch + per-record)
  pipeline/
    runner.py            # per-record orchestration, batch worker pool, escalation logic
    parse_form.py        # layout-aware Part I parser; revision detect + per-revision field map
    extract_labels.py    # caption-paired JPEG extraction; type classify; effective DPI
    ocr.py               # Tier A tesseract wrapper → words + confidences
    vision.py            # Tier B client (OpenAI-compatible, VISION_BASE_URL)
    match.py             # normalization + three-valued matching (exact/near-miss/mismatch)
    warning.py           # deterministic GOVERNMENT WARNING validator (27 CFR 16.21 text)
```

Key design points carried from the spec:

- **Escalation triggers** (runner.py): low OCR word confidence on a crop, OR required field empty/malformed (no ABV/net-contents pattern), OR warning *almost* matches. Escalate on doubt, never reject on doubt. Tier B runs as a bounded background queue (CPU model is slow); a record awaiting Tier B shows as still-processing while the rest of the batch streams. Tier-B failures/timeouts degrade to honest "couldn't read clearly" → Needs Review.
- **Warning validator** (warning.py): pure string check — presence, exact statutory wording, all-caps `GOVERNMENT WARNING:` prefix. Whitespace/line-break normalization only. Checked across all readable crops; lives nowhere near the LLM.
- **Matching** (match.py): field-aware normalizers (unit canonicalization for net contents, numeric-% extraction for ABV, case/punct/accent folding for brand, class/type description mapping non-English aware). Outcomes: exact ≥97 token-ratio, near-miss 85–97 → review, below → mismatch (tune in phase 2 tests).
- **Status/disposition** (store.py): separate `auto_status` (Pass/Needs Review/Fail) and `disposition` (Approved/Rejected, `dispositioned_by`, timestamp, note). Pass → auto-Approved by `system`, editable. Never auto-reject. Batch complete when no record open.
- **Data lifecycle**: uploaded PDFs + extracted crops in a per-batch temp media dir; SQLite holds decisions/metadata only; purge media on batch delete (and offer purge after export).

## API surface

`POST /api/batches` (multipart PDFs) · `GET /api/batches/{id}/events` (SSE progressive results) · `GET /api/batches/{id}/records` · `GET /api/records/{id}` · `GET /api/records/{id}/crops/{n}` · `POST /api/records/{id}/disposition` · `GET /api/batches/{id}/export.{csv,pdf}?scope=` · `GET /api/records/{id}/export.pdf` · `DELETE /api/batches/{id}` (purge)

## UI (web/) — four screens per spec §5

1. **Upload** — single drop zone, drag a stack of PDFs.
2. **Progress** — records stream in via SSE as each finishes; triage can start immediately.
3. **Queue** — sorted Fail → Needs Review → Pass; summary bar ("212 processed · 7 failed · 14 need review · 21 open"); full-height rows with plain-language reasons for fail/review, quiet collapsed green rows for passes; filter chips All/Open/Failed/Needs review/Passed.
4. **Detail/review** — per-field verdicts left (form value vs extracted, "(normalized)" tag), zoomable crop right, dedicated warning block (verbatim extracted text or honest "couldn't read clearly — please verify"), Approve/Reject + note, auto-advance to next open record.

Plain language everywhere; confidence numbers route under the hood, never shown.

## Build order (spec §7, each independently demonstrable)

1. **Ingest & parse** — `parse_form.py` + `extract_labels.py` + a CLI harness (`python -m server.pipeline.runner sample-forms/*.pdf`) that prints parsed fields and writes crops for all 30 samples. Proves both form revisions and caption-pairing hold corpus-wide.
2. **Match engine** — `match.py` + `warning.py` with table-driven pytest cases (incl. spec's normalization table rows); fed known-good text, no OCR.
3. **Tier A** — `ocr.py` wired into runner; end-to-end on easy samples; record per-crop confidence + timing.
4. **Status/disposition + store** — schema, state machine, auto-approve-editable rule.
5. **UI** — all four screens against the real pipeline, SSE progressive fill.
6. **Tier B** — `vision.py` (OpenAI-compatible client → llama-server with Qwen2.5-VL-3B GGUF + mmproj), escalation triggers driven by phase-3 confidence signals, bounded background queue. Measure CPU latency; fall back to moondream2 if needed.
7. **Export** — CSV + PDF, scope picker reusing filter vocabulary, batch + per-record; purge-after-export.
8. **Package & deploy** — multi-stage Dockerfile, `docker-compose.yml` (app + llama-server), deploy both services to Railway with volumes; smoke-test the deployed instance with a sample batch.

## Setup needed

- Local dev: `brew install tesseract tesseract-lang llama.cpp`; download pinned Qwen2.5-VL-3B GGUF + mmproj (phase 6)
- `uv init` server project pinned to Python 3.12; deps: fastapi, uvicorn, pymupdf, pytesseract, pillow, rapidfuzz, reportlab, openai (client only, pointed at self-hosted endpoint), pytest, httpx
- `npm create vite` for web/; tailwind
- Deploy (phase 8): Dockerfile installs `tesseract-ocr` + `tesseract-ocr-{spa,ita,fra,por,deu}` via apt; Railway app service + llama.cpp sidecar service, both with volumes

## Verification

- **Unit**: pytest on match/warning/normalizers (table-driven; near-miss bands; warning exact/almost/missing cases).
- **Corpus run**: CLI harness over all 30 sample PDFs after phases 1, 3, 6 — assert every record parses, every label page yields classified crops, no crashes; eyeball a printed field/verdict table against the actual PDFs for a handful (incl. one pre-2016, one 2016+, one non-English import).
- **End-to-end**: upload all 30 via the UI, watch progressive fill, disposition a few records, export CSV+PDF and check the auto-status vs disposition columns; verify media purge empties the batch dir.
- **Latency**: time the 30-record batch; fast-path median target <5s/record, report Tier-B escalation rate and per-escalation cost (validates spec open-question 2's stated assumption). Re-measure Tier B on Railway vCPUs, not just the M3 Max.
- **Deployed smoke test**: upload a sample batch against the Railway URL; confirm SSE streaming, crops render, export downloads, and the llama.cpp sidecar is reachable only over private networking.
