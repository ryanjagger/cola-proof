# COLA Proof

A local-first batch tool for TTB compliance agents. It ingests COLA
application PDFs (form TTB F 5100.31), checks the embedded label images
against the form's typed fields (brand, ABV, net contents, class/type,
government warning), and flags mismatches for human review.

Approach, tools used, and assumptions in brief: [APPROACH.md](APPROACH.md).

## Quick start (Docker)

The only thing you need installed is
[Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
git clone https://github.com/ryanjagger/cola-proof
cd cola-proof
docker compose up --build
```

Then open <http://localhost:8000> and drag in some PDFs — the repo ships
ready-made test files in [`sample-forms/registry/`](sample-forms/registry/) and
[`sample-forms/applications/`](sample-forms/applications/).

A few things to expect:

- **The first start downloads ~3 GB of AI model weights** (one time; they're
  cached afterwards). The app itself is up within seconds — until the
  download finishes, hard-to-read labels simply wait in "Needs review"
  instead of getting their AI re-read.
- **Everything runs on your machine.** The AI model is served from a second
  container that is reachable only from the app — it has no published port,
  and nothing sends label data to an outside service.
- Stop with `Ctrl-C` (or `docker compose down`). Uploaded batches persist in
  a Docker volume between runs; `docker compose down -v` wipes them. The CSV
  export is the durable record of a review session.

## Running without Docker

The secondary path: install the toolchain and run the pieces yourself.

**Prerequisites**

- [uv](https://docs.astral.sh/uv/) (manages Python 3.12 and dependencies)
- Node 20+ / npm (frontend build)
- Tesseract with language packs, and llama.cpp for the optional vision tier:

```bash
brew install tesseract tesseract-lang llama.cpp
```

**One-time setup**

```bash
uv sync                                  # python deps into .venv
cd web && npm install && npm run build   # build the SPA into web/dist
```

**Run the server**

```bash
DATA_DIR=data uv run uvicorn server.app:app --port 8000
```

Open <http://localhost:8000>. This runs the local OCR tier only: hard-to-read
labels stay in "Needs review" instead of getting a second read.

**Vision tier (optional, recommended)**

The vision tier re-reads doubtful labels with a self-hosted model. Start the
sidecar (first run downloads ~3 GB of model weights, then they're cached):

```bash
llama-server -hf Qwen/Qwen3-VL-4B-Instruct-GGUF:Q4_K_M --port 8090 --ctx-size 8192
```

Then point the app at it:

```bash
DATA_DIR=data VISION_BASE_URL=http://127.0.0.1:8090/v1 \
  uv run uvicorn server.app:app --port 8000
```

## Developing

Design and architecture: see `cola-proof-spec.md`.

### Web dev server (only for frontend work)

For hot reload instead of the static build:

```bash
cd web && npm run dev
```

Vite serves on <http://localhost:5173> and proxies `/api` to the Python
server on :8000 (which must be running). For everything else, the built SPA
served by FastAPI on :8000 is all you need — rebuild with `npm run build`
after frontend changes.

### Tests and the CLI harness

```bash
uv run pytest                                            # full suite
uv run python -m server.pipeline.runner sample-forms/registry/*.pdf   # corpus run, Tier A
uv run python -m server.pipeline.runner sample-forms/applications/*.pdf  # 04/2023 fillable-form corpus
uv run python -m server.pipeline.runner sample-forms/registry/*.pdf \
    --vision http://127.0.0.1:8090/v1                    # with Tier B
uv run python -m server.pipeline.runner sample-forms/registry/*.pdf --no-ocr  # parse/extract only
```

### Environment variables

| Variable          | Default        | Purpose                                            |
| ----------------- | -------------- | -------------------------------------------------- |
| `DATA_DIR`        | `data`         | SQLite DB + per-batch media (PDFs, label crops)    |
| `VISION_BASE_URL` | *(empty)*      | OpenAI-compatible Tier B endpoint; empty = Tier A only |
| `VISION_MODEL`    | `qwen3-vl-4b`  | Model name sent to the vision endpoint             |
| `OCR_WORKERS`     | `4`            | Tier A worker pool size                            |
| `VISION_WORKERS`  | `2`            | Tier B worker pool size (the CPU model is slow)    |

Uploaded PDFs and crops are session-scoped: deleting a batch purges its
files. The CSV/PDF export is the durable artifact.

`docker compose up` mirrors the production topology: app + llama.cpp vision
sidecar on a private network, model weights cached on a named volume, no
outbound inference anywhere.
