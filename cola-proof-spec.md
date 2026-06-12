# COLA Proof — Architecture

A standalone tool that helps TTB compliance agents verify that alcohol beverage labels match their
COLA application data. Agents import COLA PDFs — registry print views of approved applications, or
bare 04/2023 filings — and the tool checks each label image against the form fields, flagging
mismatches for human review rather than making compliance determinations itself.

This document describes the system as built. Section 7 records the build history.

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

## 2. Verified findings about the COLA corpus

Checked against the real sample corpus (30 registry + 18 application PDFs), and corrected where
building proved early assumptions wrong:

- **The form PDF has a real text layer.** Part I fields are selectable text, so field extraction is
  a *parsing* task, not OCR. The two-column layout interleaves in reading order, so parsing is
  layout-aware (word x/y coordinates, anchored on field-label boxes).
- **There are two PDF shapes, detected per record.** Registry print views of approved applications,
  and bare 04/2023 fillable filings (legal-size, data as flat text on page 1, labels affixed on
  page 1). Applications carry no TTB ID / status / class-type — nothing has been approved yet.
- **There are four form revisions, not two** (6/2006, 5/2011, 07/2012, 06-2016). Field numbering
  shifts between revisions, so the parser anchors on field *names*. Pre-2016 revisions carry typed
  NET CONTENTS and ALCOHOL CONTENT fields; 06-2016 and the 04/2023 application drop them, so for
  those records ABV/net-contents become label-format checks ("present and plausible") rather than
  form-vs-label matches — and the UI says so.
- **Label crops are identified by their captions, not by size or position.** Registry pages pair
  each label image with an "Image Type: … Actual Dimensions: …" caption block, strictly in document
  order (captions and images can straddle page boundaries, so pairing runs over the whole document).
  Applications type a one-word FRONT/BACK/NECK caption under each image. Caption inches ÷ pixel
  dimensions give an effective DPI per crop (observed ~90–506) — a free trust signal for escalation.
- **Labels arrive in three affixing styles** (all present in the application corpus): label artwork
  under captions; captioned *photographs* of the physical labels; and a single uncaptioned
  photograph of all the labels laid out together, which becomes crop kind `photo` — always escalated
  to the vision tier, never auto-Passed on local OCR alone.
- **Labels are frequently non-English and multi-image.** Imports are common (Italian, Spanish,
  etc.), and a record may carry front / back / neck images. Neck and strip crops are excluded from
  name matching (stray text could false-match a short brand) but are still read for the numeric,
  bottler, origin, and warning checks, which routinely live there.
- **Corpus quirks the code handles:** one PNG label among the JPEGs, a record with two front crops,
  and a record carrying both a source field and a net-contents field.

---

## 3. Pipeline (per record)

```
PDF in
  │
  ├─ 1. Detect shape ──────────────► registry print view | bare 04/2023 application
  │
  ├─ 2. Parse text layer ──────────► Part I form fields (deterministic, layout-aware, per-revision)
  │
  ├─ 3. Extract label crops ───────► caption-paired; kind front / back / other / photo
  │
  ├─ 4. Extract label text ────────► Tier A: local OCR (fast path)
  │                                   Tier B: vision model (escalation — see triggers below)
  │
  ├─ 5. Normalize + match ─────────► field-aware comparison (units, %, accents, casing,
  │                                   bottler, origin)
  ├─ 6. Validate warning ──────────► strict, deterministic, across all crops
  │
  └─ 7. Emit record result ────────► auto-status + per-field verdicts + crops
```

### Three-tier extraction

The two extractor tiers feed one deterministic validator. Keeping the warning check deterministic —
regardless of which tier produced the text — is essential: a rejection has to be explainable to an
agent, and an LLM can't be the thing that decides exact-match compliance.

1. **Tier A — local OCR (fast path).** Tesseract on each (upscaled) crop, keeping per-word
   confidences and word boxes. Handles the bulk at speed, no network.
2. **Tier B — vision model (escalation).** Qwen3-VL-4B (GGUF, Q4_K_M) served by a llama.cpp
   sidecar, reached through an OpenAI-compatible client at a configurable base URL. Invoked only
   when the fast path is untrustworthy; runs as a bounded background queue because the CPU model is
   slow, while the rest of the batch keeps streaming.
3. **Warning validator (deterministic).** Pure string/format check on whatever text the tiers
   produced. Verifies presence, exact wording, all-caps "GOVERNMENT WARNING:".

### Escalation triggers (Tier A → Tier B)

The escalation logic *is* the design — get it wrong and you either escalate everything (losing the
speed win) or nothing (shipping OCR errors). Escalate when:

- OCR per-word confidence falls below a threshold on a crop, **or**
- a required field comes back empty/malformed (no ABV pattern, no net-contents pattern), **or**
- the government warning is *almost* but not exactly matched — escalate before flagging, because a
  false reject is worse than a slow review, **or**
- any field would be flagged MISMATCH — flag only after the best available reader agrees, **or**
- the labels arrived as a photograph (`photo` crops always get the backup reader).

Principle: **escalate on doubt, never reject on doubt.**

### Vision corroboration rules

A vision-language model can fabricate memorized-boilerplate-shaped text wholesale — the statutory
warning, "BOTTLED BY …" lines, "PRODUCT OF …" statements. Vision-only readings of those count only
when Tier A independently saw something similar enough; an uncorroborated vision-only "exact"
warning demotes to review rather than auto-passing, and vision-only mismatches/presence claims
demote the same way. The vision tier can rescue records, never quietly decide them.

### Field normalization rules

The form and label express the same fact differently; normalize before comparing:

| Field | Form example | Label example | Rule |
|---|---|---|---|
| Net contents | `750 MILLILITERS` | `750 ml` | normalize unit + casing; compare magnitude+unit |
| Alcohol content | `42` or `42%` | `Alc. 42% by Vol` | extract numeric %, tolerate surrounding text |
| Brand name | `VIJO TONEL` | `Viejo Tonel` | case/punct/accent-insensitive; near-miss → review |
| Type/class | `OTHER GRAPE BRANDY` | `PISCO` | map against class/type description; non-English aware |
| Bottler | applicant name + address block | `BOTTLED BY NORTH HARBOR DISTILLING CO.` | company-name match near a bottled/produced-by statement; corporate suffixes stripped; city/state validated |
| Country of origin | `Source: Imported` | `PRODUCT OF ITALY` | presence/format check; ambiguous country names (TURKEY, GEORGIA, …) require a product-of / imported-from anchor |

Match outcomes are three-valued: **exact match**, **near-miss (review)**, **mismatch (fail)**.
The bottler and origin checks never produce a mismatch — at worst they hold a record in review.
Where the form has no typed value (06-2016 and applications), net contents and ABV are checked for
presence and plausibility on the label instead.

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

Three screens. The whole UX thesis: *stop making the agent eyeball the easy 80%.*

**Upload.** One obvious drop zone; drag a stack of PDFs. One action, impossible to miss.

**Batch.** Progress and queue are one screen: records stream results over SSE as they finish rather
than blocking on a spinner — this is how batch *feels* fast even when total wall-clock is minutes,
and the agent can start triaging early flags while the rest process. A summary bar up top doubles
as the progress meter. Rows sort worst-first (`Fail → error → Needs Review → Pass`), open records
before decided ones. Filters come in two families sharing one bar: **To review** is workflow state
(no decision yet); **Flagged / Failed / Passed** are what the checks concluded, regardless of any
decision since. Flagged and failed rows are full-height with a plain-language reason; passes
collapse to quiet green rows (present, so nothing looks hidden, but visually receding).

**Detail / review.** The core screen. Side-by-side: per-field verdicts on the left (form value vs
extracted value, plain-language result, "(normalized)" tag where relevant), the actual label crop on
the right with zoom and a highlight box on the words the value was read from. The agent verifies the
claim against the pixels in one glance — never trusts OCR blindly. The government warning gets its
own block showing the extracted text; if it couldn't be read, it says so honestly ("couldn't read
clearly — please verify") rather than guessing. Approve / Reject + optional note at the bottom;
acting advances to the next open record.

Design principles throughout: plain language over confidence scores (numbers drive routing under the
hood, not the agent's screen); three status states only; always show the crop next to the claim.

---

## 6. Data lifecycle & export

**Lifecycle.** Source PDFs and extracted images are held only for the working session, in a
per-batch media directory purged on batch delete. SQLite holds decision metadata only. The durable
artifact is the **export**: the agent's dispositions, reasons, and audit trail.

**Export.** The UI exposes one export: **CSV** ("Save Results"), scoped by the active queue filter —
one row per record with TTB ID, brand, type, source, auto-status, disposition, `dispositioned_by`,
per-field results, timestamp, and note. The auto-status vs disposition split stays as separate
columns so disagreements are filterable. PDF report endpoints (batch-level and per-record, with the
label crop embedded next to the verdict) exist in the API but were dropped from the UI to keep it
to one obvious action.

### Latency posture (the two questions the design left open, since resolved)

1. **Vision runtime:** a self-hosted llama.cpp `llama-server` sidecar running Qwen3-VL-4B — Docker
   compose locally/on-prem, a private-network sidecar service in production. No outbound inference
   anywhere; the app reaches whatever endpoint `VISION_BASE_URL` names.
2. **"5 seconds" is the fast-path median, not a per-record ceiling.** Tier A runs ~4–5s per record;
   an escalated record waits on a ~6–12s-per-crop CPU vision read in a bounded background queue.
   Progressive fill keeps the agent triaging other rows while escalations drain, which is what the
   constraint actually required.

---

## 7. Build history

Built in the phased order the original plan suggested, each phase demonstrable on the real corpus:
ingest & parse → match engine (on known-good text, before any model work) → Tier A OCR → status +
disposition store → UI → Tier B escalation (last, because its triggers depend on the fast path's
trust signals) → export → Docker packaging and deployment (multi-stage Dockerfile; compose for
on-prem; Railway app + vision sidecar in production).

Notable decisions along the way: Tesseract won the Tier A slot (per-word confidences are the
escalation signal); the Tier B model moved from Qwen2.5-VL to Qwen3-VL-4B after an A/B over the full
corpus (fixed a false Fail and a false Pass); the vision corroboration rules (§3) were added after
observing the model parrot prompt examples and fabricate memorized boilerplate; bottler and
country-of-origin checks were added once the core five fields were stable.
