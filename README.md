# COLA Proof

A local-first batch tool for TTB compliance agents. It ingests approved COLA
application PDFs (form TTB F 5100.31), checks the embedded label images
against the form's typed fields (brand, ABV, net contents, class/type,
government warning), and flags mismatches for human review. It never makes
compliance determinations itself — a "Fail" is a recommendation the agent
confirms.

Design and architecture: see `cola-proof-spec.md` and `implementation-plan.md`.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python 3.12 and dependencies)
- Node 20+ / npm (frontend build)
- Tesseract with language packs, and llama.cpp for the optional vision tier:

```bash
brew install tesseract tesseract-lang llama.cpp
```

## Setup (one time)

```bash
uv sync                                  # python deps into .venv
cd web && npm install && npm run build   # build the SPA into web/dist
```

## Running locally

### 1. Python server (serves the API and the built frontend)

```bash
DATA_DIR=data uv run uvicorn server.app:app --port 8000
```

Open <http://localhost:8000> and drop PDFs (try `sample-forms/registry/`). This runs
**Tier A only** (Tesseract OCR): hard-to-read labels stay in "Needs review"
instead of getting a second read.

### 2. Vision tier (optional, recommended)

Tier B re-reads doubtful labels with a self-hosted vision model. Start the
sidecar (first run downloads ~2 GB of model weights, then they're cached):

```bash
llama-server -hf ggml-org/Qwen2.5-VL-3B-Instruct-GGUF:Q4_K_M --port 8090
```

Then point the app at it:

```bash
DATA_DIR=data VISION_BASE_URL=http://127.0.0.1:8090/v1 \
  uv run uvicorn server.app:app --port 8000
```

### 3. Web dev server (only for frontend work)

For hot reload instead of the static build:

```bash
cd web && npm run dev
```

Vite serves on <http://localhost:5173> and proxies `/api` to the Python
server on :8000 (which must be running). For everything else, the built SPA
served by FastAPI on :8000 is all you need — rebuild with `npm run build`
after frontend changes.

## Environment variables

| Variable          | Default        | Purpose                                            |
| ----------------- | -------------- | -------------------------------------------------- |
| `DATA_DIR`        | `data`         | SQLite DB + per-batch media (PDFs, label crops)    |
| `VISION_BASE_URL` | *(empty)*      | OpenAI-compatible Tier B endpoint; empty = Tier A only |
| `VISION_MODEL`    | `qwen2.5-vl-3b`| Model name sent to the vision endpoint             |
| `OCR_WORKERS`     | `4`            | Tier A worker pool size                            |
| `VISION_WORKERS`  | `2`            | Tier B worker pool size (the CPU model is slow)    |

Uploaded PDFs and crops are session-scoped: deleting a batch purges its
files. The CSV/PDF export is the durable artifact.

## Tests and the CLI harness

```bash
uv run pytest                                            # full suite
uv run python -m server.pipeline.runner sample-forms/registry/*.pdf   # corpus run, Tier A
uv run python -m server.pipeline.runner sample-forms/applications/*.pdf  # 04/2023 fillable-form corpus
uv run python -m server.pipeline.runner sample-forms/registry/*.pdf \
    --vision http://127.0.0.1:8090/v1                    # with Tier B
uv run python -m server.pipeline.runner sample-forms/registry/*.pdf --no-ocr  # parse/extract only
```

## Docker

`docker compose up` runs the same two-service topology used in production
(app + llama.cpp vision sidecar on a private network, model weights cached
on a named volume). App on <http://localhost:8000>; the vision service has
no published port on purpose.
