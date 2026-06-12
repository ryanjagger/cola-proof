# COLA Proof — Approach, Tools, and Assumptions

Companion to `README.md` (how to run it) and `cola-proof-spec.md` (full architecture).

## Assumptions made

- **The corpus is representative.** Some corpus items were derived from real forms data from the
  [COLA Registry public search](https://www.ttbonline.gov/colasonline/publicSearchColasBasic.do);
  the rest are synthetic applications with generated label artwork and photographs, as the brief
  suggests. Four form revisions (6/2006, 5/2011, 07/2012, 06-2016) and two PDF shapes (registry
  print view, bare 04/2023 application) were observed and are handled; an unseen revision would
  surface as a parse failure routed to review, not a silent wrong answer.
- **Part I is real text.** Part I — the form's typed application-data section (brand name,
  applicant, serial number, etc.) — arrives as a selectable text layer, never as a scan. Only
  the *label images* need OCR. Image-only/scanned forms are out of scope.
- **Captions are trustworthy.** Registry "Image Type" blocks and typed FRONT/BACK/NECK captions
  correctly describe their images; classification rides on them. The one uncaptioned case (a
  photograph of the physical labels) is recognized and always escalated, never trusted to OCR alone.
- **The statutory warning is the English wording of 27 CFR part 16,** compared after
  whitespace/line-break normalization only, with an all-caps `GOVERNMENT WARNING:` prefix required.
- **Boldness can't be machine-verified.** The regulation also wants the warning prefix bold, but
  extracted text (OCR or vision) carries no typography — wording and all-caps are checked
  automatically; bold is left to the agent, who always has the label crop on screen.
- **Standalone by design.** No integration with the COLA system itself — per the IT constraints
  from discovery, this is a proof-of-concept that could inform future procurement, not a system
  that touches existing infrastructure or its authorization requirements.
- **"~5 seconds" means the fast-path median, not a per-record ceiling.** Escalated records may take
  ~6–12s per crop on CPU; progressive fill keeps the agent working while the bounded Tier B queue
  drains.
- **No outbound inference is acceptable infrastructure-wise:** a CPU-only, self-hosted 4B vision
  model is good enough for the escalation tier (verified by A/B over the corpus), so the firewall
  constraint never has to be traded away.
- **Single-agent prototype.** No authentication or multi-user concurrency model; the
  deployment is for one reviewer or a trusted team network.
- **Session-scoped data is sufficient.** Uploaded PDFs and crops are purged with their batch; the
  CSV export is the durable audit artifact. No long-term storage of source documents is wanted.
- **Tesseract word confidence is a usable trust signal** for routing between tiers — imperfect, but
  failures skew toward over-escalation (slower, safe) rather than under-escalation (wrong, unsafe).
- **Bottler and origin checks should never hard-fail a record** — company-name and origin phrasing
  vary too much; at worst they hold a record for review.

## Approach

**Corpus-driven, deterministic-first.** Every design decision was tested against a real sample
corpus (30 COLA Registry print views + 18 filled 04/2023 applications) before being trusted. The
pipeline prefers deterministic extraction wherever the PDF allows it — Part I form fields are
*parsed* from the text layer (layout-aware, anchored on field names), and label crops are
classified by their typed captions, not by size heuristics or model judgment. AI only enters
where determinism runs out: reading the label pixels.

**Two-tier reading, escalate on doubt.** Tier A is fast local OCR (Tesseract) with per-word
confidences; those confidences, missing/malformed required fields, near-miss warnings, would-be
mismatches, and photographed labels are the triggers that escalate a crop to Tier B — a
self-hosted vision model that re-reads only the doubtful crops. The principle throughout:
escalate on doubt, never reject on doubt. A "Fail" is always a recommendation a human confirms;
nothing is auto-rejected.

**The checks stay explainable.** The GOVERNMENT WARNING check is a pure string/format comparison
regardless of which tier produced the text — an LLM is never the thing deciding exact-match
compliance. Everywhere else, matching is field-aware and case/punctuation/accent-insensitive
("STONE'S THROW" vs "Stone's Throw" is a match), and a near-miss is a review, never an automatic
rejection. Vision output is treated as a witness, not an oracle: boilerplate-shaped text
(warning, bottler, origin) read *only* by the vision model is demoted unless Tier A independently
corroborates it, because vision-language models can fabricate memorized boilerplate.

**Batch-native, human-centered.** Peak season dumps 200–300 applications at once, so batch is the
primary workflow: records stream into the queue as they finish and the agent starts triaging in
seconds while the rest drain. The pipeline's verdict (`auto_status`) and the agent's decision
(`disposition`) are separate fields; their disagreement is the audit signal. Passes auto-approve
but stay editable. The UI shows plain language and always puts the label crop next to the claim —
confidence scores route work under the hood and are never shown, built for a team with a wide
range of tech comfort.

**Built in phases, each demonstrable.** Parse → match engine (on known-good text, before any model
work) → Tier A → store/state machine → UI → Tier B escalation → export → packaging/deploy. A CLI
harness runs the whole corpus at every phase so regressions surface immediately.

## Tools used

- Language / runtime: Python 3.12, managed by `uv`
- Backend: FastAPI + uvicorn, SQLite, SSE for progressive batch results
- PDF parsing: PyMuPDF (text layer with word coordinates, embedded-image extraction)
- Tier A OCR: Tesseract via pytesseract (+ language packs), Pillow for preprocessing
- Tier B vision: Qwen3-VL-4B Instruct (GGUF Q4_K_M) on a llama.cpp `llama-server` sidecar,
  reached through an OpenAI-compatible client
- Matching: rapidfuzz (fuzzy scoring) under field-specific normalizers; difflib for warning diffs
- Export: CSV (stdlib) and PDF reports (reportlab)
- Frontend: React + Vite + TypeScript + Tailwind, built to static files served by FastAPI
- Tests: pytest, run over the real corpus
- Packaging / deploy: multi-stage Dockerfile; docker compose (app + vision sidecar, private
  network); Railway in production
- Development: Claude Code for AI-assisted implementation; the corpus harness as the feedback loop

## Next steps

- **Demo with users for feedback** — put the prototype in front of compliance agents and let real
  triage sessions drive the next iteration.
- **Extend the vision sidecar to multithreaded Qwen processing** — the llama-server currently runs
  a single slot (`--parallel 1`), so escalated crops are read one at a time; parallel slots would
  cut the Tier B tail on large batches.
- **Extend the vision model to account for bold GOVERNMENT WARNING text** — close the boldness gap
  noted under assumptions by asking the vision tier whether the prefix is rendered bold. Its answer
  would route a record to review, never decide compliance — the wording check stays deterministic.
- **English-first OCR with a multi-language rescue** — Tier A loads all six language packs on
  every pass; `eng` alone measured ~3x faster per crop. A prototype that read domestic crops
  English-first kept every corpus decision unchanged only with a conservative rescue — any
  escalation-worthy doubt re-reads the record's crops with the full pack, so nothing is flagged
  on a confident English misread (one-character warning misreads and "IRON GATE"→"tron gate"
  both occurred). That trade makes clean-Pass records ~3x faster but review-heavy batches ~15%
  slower, so it was shelved; worth revisiting against real production traffic, where routine
  clean records should dominate.
