# COLA Proof — Architecture & Build Plan

A standalone tool that helps TTB compliance agents verify that alcohol beverage labels match their
COLA application data. Agents bulk-export approved applications (form TTB F 5100.31) from the COLA
Registry as PDFs, import them into COLA Proof, and the tool checks each label image against the
form fields — flagging mismatches for human review rather than making compliance determinations itself.

---

## 1. Problem & constraints

The job today is manual: an agent opens an application, reads the label artwork, and confirms the
brand name, ABV, net contents, and government warning match what the form says. Most of it is rote
matching. COLA Proof automates the matching and surfaces only the records that need a human's judgment.

These constraints come directly from the discovery interviews and drive every decision below:

- **~5 second response (perceived).** A prior scanning vendor was abandoned because it took 30–40s
  per label. The target is a fast median via progressive-fill, not a hard per-record ceiling — see
  the latency note in §6.
- **No reliable outbound network.** The agency firewall blocked the prior vendor's cloud ML
  endpoints. COLA Proof must run local-first; any model inference has to be self-hostable.
- **Non-technical users.** Half the team is 50+, with a wide range of tech comfort. The UI must be
  obvious — clean, few buttons, no hunting.
- **Batch-native.** Peak season brings 200–300 applications dumped at once. Batch is the primary
  workflow, not an add-on.
- **Strict government-warning matching.** The warning must be exact, all-caps "GOVERNMENT WARNING:".
  This is a deterministic string/format check, never an AI judgment call.
- **Fuzzy matching elsewhere.** Trivial differences (casing, punctuation, "STONE'S THROW" vs
  "Stone's Throw") should be flagged for review, not hard-rejected.
- **Prototype data posture.** No long-term storage of sensitive source data. Source PDFs and
  extracted images are session-scoped; the export is the durable artifact.

---

## 2. Verified findings about the COLA export

These were checked against real sample PDFs, not assumed:

- **The form PDF has a real text layer.** Part I application fields are selectable text (~3,500
  chars extracted cleanly), so field extraction is a *parsing* task, not OCR. Caveat: the two-column
  form layout interleaves in reading order, so field-to-value mapping needs layout-aware parsing
  (use word x/y coordinates and anchor on field-label boxes — not naive line-by-line).
- **Label crops are identifiable by size and position.** A sample page had 12 embedded images:
  nine ~20×22px checkbox glyphs, one ~193×49px signature, and two ~265×361px label crops in the
  bottom third of the page. Filter to large images in the lower page region to isolate the labels;
  discard the rest.
- **Label crops are low-resolution** (~265px wide). The dense government warning on a back-label
  crop at this size is the single hardest extraction target — this is the main justification for the
  vision-model escalation tier.
- **Labels are frequently non-English and multi-image.** Imports are common (Italian, Spanish, etc.),
  and a record may carry front / back / neck images. Neck/embossed crops are often unreadable and
  should be ignored for matching, not failed.

---

## 3. Pipeline (per record)

```
PDF in
  │
  ├─ 1. Parse text layer ──────────► Part I form fields (deterministic, layout-aware)
  │
  ├─ 2. Extract label crops ───────► large bottom-of-page images (discard chrome/signature/neck)
  │
  ├─ 3. Extract label text ────────► Tier A: local OCR (fast path)
  │                                   Tier B: vision model (escalation — see triggers below)
  │
  ├─ 4. Normalize + match ─────────► field-aware comparison (units, %, accents, casing)
  │
  ├─ 5. Validate warning ──────────► strict, deterministic, across all crops
  │
  └─ 6. Emit record result ────────► auto-status + per-field verdicts + crops
```

### Three-tier extraction

The two extractor tiers feed one deterministic validator. Keeping the warning check deterministic —
regardless of which tier produced the text — is essential: a rejection has to be explainable to an
agent, and an LLM can't be the thing that decides exact-match compliance.

1. **Tier A — local OCR (fast path).** Tesseract or PaddleOCR on each (upscaled) crop. Handles the
   bulk at speed, no network.
2. **Tier B — vision model (escalation).** Self-hosted vision-language model, invoked only when the
   fast path is untrustworthy. Better on low-res / skewed / stylized crops.
3. **Warning validator (deterministic).** Pure string/format check on whatever text the tiers
   produced. Verifies presence, exact wording, all-caps "GOVERNMENT WARNING:".

### Escalation triggers (Tier A → Tier B)

The escalation logic *is* the design — get it wrong and you either escalate everything (losing the
speed win) or nothing (shipping OCR errors). Escalate when:

- OCR per-word confidence falls below a threshold on a crop, **or**
- a required field comes back empty/malformed (no ABV pattern, no net-contents pattern), **or**
- the government warning is *almost* but not exactly matched — escalate before flagging, because a
  false reject is worse than a slow review.

Principle: **escalate on doubt, never reject on doubt.**

### Field normalization rules

The form and label express the same fact differently; normalize before comparing:

| Field | Form example | Label example | Rule |
|---|---|---|---|
| Net contents | `750 MILLILITERS` | `750 ml` | normalize unit + casing; compare magnitude+unit |
| Alcohol content | `42` or `42%` | `Alc. 42% by Vol` | extract numeric %, tolerate surrounding text |
| Brand name | `VIJO TONEL` | `Viejo Tonel` | case/punct/accent-insensitive; near-miss → review |
| Type/class | `OTHER GRAPE BRANDY` | `PISCO` | map against class/type description; non-English aware |

Match outcomes are three-valued: **exact match**, **near-miss (review)**, **mismatch (fail)**.

---

## 4. Status & disposition model

Two separate concepts, kept as separate fields. This separation is the source of the tool's audit
value — a record that was auto-classified one way but dispositioned another is exactly what's worth
a second look.

- **Auto-status** (set by the pipeline): `Pass` | `Needs Review` | `Fail`
- **Disposition** (the agent's call, or the system's default): `Approved` | `Rejected`, plus
  `dispositioned_by: system | <agent>`, a timestamp, and an optional note.

### State machine

```
Pending
  ├─ [auto] Pass         → Approved (by system, editable)        ─┐
  ├─ [auto] Needs Review → open → Approved | Rejected (by agent) ─┼─► done
  └─ [auto] Fail         → open → Approved | Rejected (by agent) ─┘
```

Rules:
- **Never auto-reject.** A `Fail` is a recommendation; the agent confirms it.
- **Passes auto-approve but stay editable** (option C). The agent doesn't have to touch greens, but
  can open and override one. This delivers the time savings while keeping "human always has final
  say, nothing locked."
- A batch is **complete** when no record is still open.

---

## 5. UI

Four screens. The whole UX thesis: *stop making the agent eyeball the easy 80%.*

**Upload.** One obvious drop zone; drag a stack of PDFs. One action, impossible to miss.

**Progress.** Records stream results as they finish rather than blocking on a spinner — this is how
batch *feels* fast even when total wall-clock is minutes. The agent can start triaging early failures
while the rest process.

**Queue.** Show-all, sorted `Fail → Needs Review → Pass`. Summary bar up top
("212 processed · 7 failed · 14 need review · 21 open") doubling as a progress meter. Fail/review
rows are full-height with a plain-language reason; passes collapse to quiet green rows (present, so
nothing looks hidden, but visually receding). Filter chips reuse one vocabulary: All / Open / Failed
/ Needs review / Passed.

**Detail / review.** The core screen. Side-by-side: per-field verdicts on the left (form value vs
extracted value, plain-language result, "(normalized)" tag where relevant), the actual label crop on
the right with zoom. The agent verifies the claim against the pixels in one glance — never trusts OCR
blindly. The government warning gets its own block showing the extracted text; if OCR couldn't read
it, it says so honestly ("couldn't read clearly — please verify") rather than guessing. Approve /
Reject + optional note at the bottom; acting advances to the next open record.

Design principles throughout: plain language over confidence scores (numbers drive routing under the
hood, not the agent's screen); three status states only; always show the crop next to the claim.

---

## 6. Data lifecycle & the two open questions

**Lifecycle.** Source PDFs and extracted images are held only for the working session. The durable
artifact is the **export**: the agent's dispositions, reasons, and audit trail. After export, source
data can be purged. This satisfies the prototype's "don't store anything sensitive" posture and gives
a clean story: sensitive data is session-scoped; the audit trail is what persists, by being exported.
A lightweight store (SQLite or per-batch JSON) holds decision metadata only.

**Export.** Both formats, agent picks scope (reusing the queue filter vocabulary), two separate
downloads (not bundled):
- **CSV** — one row per record, text-only. TTB ID, brand, type, source, auto-status, disposition,
  `dispositioned_by`, per-field results, timestamp, note. Preserves the auto-status vs disposition
  split as separate columns so disagreements are filterable.
- **PDF** — human-readable report. Batch summary, then per-record sections; for flagged records,
  embeds the crop next to the verdict (the images' last legitimate use before purge).
- Available **batch-level** (primary) and **per-record** (a single-record PDF button on the detail
  view, for "send this one back to the importer with evidence").

### Two open questions (the only things left to decide)

1. **What is the actual self-hosted vision-model runtime in the build/deploy environment?** This sets
   the real latency budget for Tier B and confirms the no-network posture holds. It's the one
   undecided choice that could change the escalation design.
2. **Confirm "5 seconds" means fast-path median, not a hard per-record ceiling.** A record that
   escalates to Tier B may exceed 5s; that's acceptable because progressive-fill keeps the agent
   working other rows. Naming this explicitly so it's a stated assumption, not a silent one.

Everything else (language/framework, OCR engine choice, storage backend) is a mechanical pick, not an
architectural decision — any reasonable choice works and is best settled by building and measuring.

---

## 7. Suggested build phases

Incremental, each phase independently demonstrable:

1. **Ingest & parse.** PDF in → Part I fields parsed (layout-aware) + label crops extracted by
   size/position. Prove the verified findings hold across more samples.
2. **Match engine.** Field normalization + three-valued matching + deterministic warning validator,
   running on the parsed fields. No OCR yet — feed it known-good text to test the matching logic in
   isolation.
3. **Tier A extraction.** Local OCR on crops feeding the match engine. End-to-end on easy labels.
4. **Status + disposition model.** The state machine, the separate auto-status/disposition fields,
   the lightweight store.
5. **UI.** Upload → progress (progressive-fill) → queue → detail/review, wired to the pipeline.
6. **Tier B escalation.** Add the vision model and the escalation triggers once the fast path and
   the trust signals (confidence, missing fields) are in place to drive it.
7. **Export.** CSV + PDF, scope picker, batch + per-record.

Phases 1–2 de-risk the core (parsing + matching) before any model work; phase 6 is deliberately late
because the escalation triggers depend on having the fast path's confidence signals first.
