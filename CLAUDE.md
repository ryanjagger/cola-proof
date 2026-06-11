# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

COLA Proof: a local-first batch tool for TTB compliance agents. It ingests approved COLA application PDFs (form TTB F 5100.31), checks the embedded label images against the form's typed fields (brand, ABV, net contents, class/type, government warning), and flags mismatches for human review. It never makes compliance determinations itself.

## Repo state

**Pre-implementation.** No code exists yet. Two documents are authoritative — read them before building anything:

- `cola-proof-spec.md` — architecture, pipeline design, status/disposition model, UI, build phases
- `implementation-plan.md` — concrete stack, module map, API surface, build order; refines the spec with verified findings from the sample corpus

Other contents:

- `sample-forms/registry/` — 30 real COLA PDFs from the COLA Public Registry print view; the test corpus for every phase. Covers both form revisions and non-English imports.
- `sample-forms/applications/` — 10 filled TTB F 5100.31 (04/2023) application PDFs, a different shape from the registry print view: legal-size, 5 pages (last 4 are static instructions), data as flat text on page 1 (AcroForm widgets hold only form furniture), label images affixed on page 1. Six have individual label images with typed FRONT/BACK/NECK captions; four embed a single uncaptioned photo of physical bottles.
- `cola-proof/` — empty accidental `git init` scaffold (zero commits). The plan says to remove it; build at repo root under `server/` and `web/`. The repo root is already a git repo — do not nest another.

## Planned stack & commands (from implementation-plan.md)

- Python 3.12 via `uv` (system 3.14 is too new for some OCR/imaging wheels). Backend: FastAPI + uvicorn + SQLite. Tests: `uv run pytest`.
- CLI corpus harness (phase 1): `python -m server.pipeline.runner sample-forms/registry/*.pdf` — prints parsed fields, writes crops, must hold across all 30 samples.
- `web/`: React + Vite + TypeScript + Tailwind (npm), built to static files served by FastAPI on a single port. Progressive results via SSE.
- Local dev deps: `brew install tesseract tesseract-lang llama.cpp`.
- Deploy: multi-stage Dockerfile → Railway (app service + llama.cpp sidecar, private networking only), with a mirroring `docker-compose.yml` for on-prem.

## Architecture

Per-record pipeline (`server/pipeline/`): parse Part I form fields from the PDF text layer (layout-aware, word x/y coordinates — `parse_form.py`) → extract label crops, classified deterministically by their text-layer captions, not size heuristics (`extract_labels.py`) → Tier A local OCR with Tesseract, keeping per-word confidences (`ocr.py`) → Tier B escalation to a self-hosted vision model (Qwen2.5-VL-3B GGUF on a llama-server sidecar, OpenAI-compatible client, `VISION_BASE_URL` env — `vision.py`) → field-aware normalization and three-valued matching: exact / near-miss (review) / mismatch (`match.py`) → deterministic government-warning validation (`warning.py`). `runner.py` orchestrates and owns the escalation logic; Tier B runs as a bounded background queue because the CPU model is slow.

Key facts that shape the code:

- **Two form revisions.** Pre-2016 forms carry typed NET CONTENTS and ALCOHOL CONTENT fields; the 06-2016 revision drops both. The parser needs a per-revision field map, and for 2016+ records ABV/net-contents become label-format checks ("present and plausible"), not form-vs-label matches — and the UI must say so.
- **Two PDF shapes.** `detect_shape` splits registry print views from bare 04/2023 fillable applications (same form, different rendering). Applications have no TTB ID/status/class-type — nothing has been approved yet — and follow the 06-2016 label-format-check policy. Labels affixed as a single uncaptioned photograph of the containers become crop kind `photo`: always escalated to Tier B, never auto-Passed on Tier A alone.
- **Auto-status vs disposition are separate fields.** Pipeline sets `auto_status` (Pass / Needs Review / Fail); the agent (or system default) sets `disposition` (Approved / Rejected + `dispositioned_by`, timestamp, note). Their disagreement is the audit signal — never merge them.
- **Escalation triggers** (the design's core): low OCR word confidence, required field empty/malformed, or warning *almost* matching. Effective DPI (pixel dims ÷ caption inches) is a free trust signal.
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
