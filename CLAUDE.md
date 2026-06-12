# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

COLA Proof: a local-first batch tool for TTB compliance agents. It ingests COLA application PDFs (form TTB F 5100.31) — registry print views of approved applications, or bare 04/2023 filings — checks the embedded label images against the form's typed fields (brand, ABV, net contents, class/type, government warning), and flags mismatches for human review. It never makes compliance determinations itself.

## Repo state

**Implemented and deployed.** All build phases from the original plan are shipped: pipeline, match engine, both extraction tiers, store, UI, export, Docker packaging, and a Railway deployment (app + vision sidecar over private networking). `cola-proof-spec.md` documents the architecture as built. Layout:

- `server/` — FastAPI app (`app.py`), SQLite store (`store.py`), export (`export.py`), and the per-record pipeline under `server/pipeline/`
- `web/` — React + Vite + TypeScript + Tailwind SPA, built to static files served by FastAPI on a single port; progressive results via SSE
- `tests/` — pytest suite over the real sample corpus plus unit tests
- `sample-forms/registry/` — 30 real COLA PDFs from the COLA Public Registry print view; the test corpus for every phase. Covers both form revisions and non-English imports.
- `sample-forms/applications/` — 18 filled TTB F 5100.31 (04/2023) application PDFs, a different shape from the registry print view: legal-size, 5 pages (last 4 are static instructions), data as flat text on page 1 (AcroForm widgets hold only form furniture), label images affixed on page 1. Three groups by numbering: 01–08 affix label artwork under typed FRONT/BACK/NECK captions; 11–17 affix individually captioned *photographs* of the physical labels (11–14 are four irongate variants; 16_pinot also carries its neck strip as an extra uncaptioned photo); 21–24 (`-single` suffix) affix one uncaptioned photograph of the labels laid out together. 06_graniteharbor is the two-spellings trap: brand display says HARBOUR, form and back-label boilerplate say HARBOR.

## Stack & commands

- Python 3.12 via `uv` (system 3.14 is too new for some OCR/imaging wheels). Backend: FastAPI + uvicorn + SQLite. Tests: `uv run pytest`.
- Run locally: `DATA_DIR=data uv run uvicorn server.app:app --port 8000` (add `VISION_BASE_URL=http://127.0.0.1:8090/v1` with a local llama-server for Tier B); or `docker compose up --build` for the full two-service topology.
- CLI corpus harness: `uv run python -m server.pipeline.runner sample-forms/registry/*.pdf [--vision URL] [--no-ocr]` — prints parsed fields and verdicts, writes crops; must hold across the whole corpus.
- Frontend build: `cd web && npm install && npm run build` (FastAPI serves `web/dist`); `npm run dev` only for frontend work.
- Local dev deps: `brew install tesseract tesseract-lang llama.cpp`.
- Deploy: multi-stage Dockerfile → Railway (app service + llama.cpp sidecar, private networking only), with a mirroring `docker-compose.yml` for on-prem.

## Architecture

Per-record pipeline (`server/pipeline/`): parse Part I form fields from the PDF text layer (layout-aware, word x/y coordinates — `parse_form.py`) → extract label crops, classified deterministically by their text-layer captions, not size heuristics (`extract_labels.py`) → Tier A local OCR with Tesseract, keeping per-word confidences (`ocr.py`) → Tier B escalation to a self-hosted vision model (Qwen3-VL-4B GGUF on a llama-server sidecar, OpenAI-compatible client, `VISION_BASE_URL` env — `vision.py`) → field-aware normalization and three-valued matching: exact / near-miss (review) / mismatch (`match.py`), including bottler/applicant matching and a country-of-origin format check for imports → deterministic government-warning validation (`warning.py`). `runner.py` orchestrates and owns the escalation logic; Tier B runs as a bounded background queue because the CPU model is slow.

Key facts that shape the code:

- **Two form revisions.** Pre-2016 forms carry typed NET CONTENTS and ALCOHOL CONTENT fields; the 06-2016 revision drops both. The parser needs a per-revision field map, and for 2016+ records ABV/net-contents become label-format checks ("present and plausible"), not form-vs-label matches — and the UI must say so.
- **Two PDF shapes.** `detect_shape` splits registry print views from bare 04/2023 fillable applications (same form, different rendering). Applications have no TTB ID/status/class-type — nothing has been approved yet — and follow the 06-2016 label-format-check policy. Labels affixed as a single uncaptioned photograph of the containers become crop kind `photo`: always escalated to Tier B, never auto-Passed on Tier A alone.
- **Auto-status vs disposition are separate fields.** Pipeline sets `auto_status` (Pass / Needs Review / Fail); the agent (or system default) sets `disposition` (Approved / Rejected + `dispositioned_by`, timestamp, note). Their disagreement is the audit signal — never merge them.
- **Escalation triggers** (the design's core): low OCR word confidence, required field empty/malformed, warning *almost* matching, any photo crop, or a would-be MISMATCH (flag only after the best reader agrees). Effective DPI (pixel dims ÷ caption inches) is a free trust signal.
- **Vision corroboration rules.** A vision model can fabricate memorized-boilerplate-shaped text (the statutory warning, bottler lines, origin statements). Vision-only readings of those count only when Tier A independently saw something similar enough; an uncorroborated vision-only "exact" warning demotes to review, never to Pass.
- **Data lifecycle**: uploaded PDFs and crops live in a per-batch temp media dir, purged on batch delete; SQLite stores decision metadata only; the CSV/PDF export is the durable artifact.

## Non-negotiable constraints

- The GOVERNMENT WARNING check is a pure deterministic string/format check (exact statutory wording, all-caps `GOVERNMENT WARNING:` prefix) — never an LLM or fuzzy judgment, regardless of which tier produced the text.
- Escalate on doubt, never reject on doubt. Never auto-reject: a `Fail` is a recommendation the agent confirms. Passes auto-approve but stay editable.
- No outbound network for inference — all model inference is self-hostable and reached via configurable base URL.
- UI uses plain language only; confidence scores drive routing under the hood and are never shown to the agent. Three status states only; always show the label crop next to the claim.

## Commit Conventions

- Use Conventional Commits: `<type>(<scope>): <subject>`
- Types: `feat`, `fix`, `chore`, `refactor`, `docs`, `test`, `perf`, `style`, `build`, `ci`
- Scope is the package or area touched (e.g. `api`, `web`, `shared`)
- Subject: imperative mood ("add", not "added"), ≤50 chars, no trailing period
- Body (when needed): blank line after subject, wrap at 72 chars, explain *why* not *what*
- Breaking changes: add `!` after type/scope and a `BREAKING CHANGE:` footer
- Skip the body for small, obvious changes; reserve prose for non-obvious decisions

## Branch & PR Practices

- Keep PRs small and reviewable; split mechanical changes (renames, moves) from logic changes
- Branch naming mirrors commit types: `feat/oauth-consent-screen`, `fix/token-double-submit`
- Keep branches short-lived; sync with main frequently to avoid drift
- Break large features into incrementally-mergeable pieces rather than one long-lived branch
