# Intent-Card Representation Pipeline (v1 Proposal)

Status: Phase 5 source-aware batch pipeline support in progress (`candidates|judge|judge-audit|canonicalize|plan-close --source` implemented; A/B quality reporting pending)
Owner: dupcanon
Date: 2026-02-15

---

## 1) Purpose

Define a safe, incremental migration from raw issue/PR prose embeddings to **LLM-extracted intent cards** as the primary retrieval representation.

This proposal is based on team discussion that:
- raw issue/PR text is noisy,
- PR title/body alone under-represents implementation intent,
- duplicate detection should compare normalized intent rather than prose quality.

This doc proposes **how** to introduce intent cards without breaking existing safety, auditability, and review gates.

---

## 2) Scope and non-scope

### In scope

1. Add an intent-card extraction pipeline for issues and PRs.
2. Persist intent cards and intent-card embeddings.
3. Support candidate retrieval from intent-card embeddings.
4. Feed intent-based candidates into existing judge/canonicalization/plan-close flows.
5. Keep rollout non-destructive and reversible.

### Out of scope (for this proposal phase)

1. Replacing v1 raw-text pipeline immediately.
2. Auto-closing directly from online/event inference.
3. Unbounded autonomous repo exploration during extraction.
4. Multi-repo orchestration.

---

## 3) Design constraints (must remain true)

The following v1 properties remain mandatory:

1. **Stateful canonicalization** (no chain chaos): accepted edge history and canonicalization logic remain source of truth.
2. **Non-destructive default**: reviewable `plan-close` before `apply-close --yes`.
3. **Guardrails stay active**: open-target requirement, min-edge, candidate-gap veto, maintainer/assignee protections.
4. **Auditability**: persisted run data + structured logs + failure artifacts.
5. **Schema compatibility**: current batch and online commands continue to function in raw mode.

---

## 4) Target pipeline

Target flow (intent mode):

1. Ingest issue/PR event (`sync` / `refresh` / `detect-new` source fetch)
2. Extract intent card
   - issue: intent from prose
   - PR: intent from diff + bounded code context
3. Embed intent card
4. Retrieve candidates by intent-card similarity
5. Judge duplicates / cluster
6. Canonicalize / plan-close with existing guardrails

### Key architectural decision

Intent cards are an **additional representation layer**, not a replacement for `items`.

- `items` continues to store source metadata/raw content.
- intent cards become the normalized retrieval substrate.

---

## 5) Intent-card schema contract

Use strict, versioned JSON contracts.

### 5.1 Shared fields (required)

- `schema_version`
- `item_type` (`issue` | `pr`)
- `problem_statement`
- `desired_outcome`
- `important_signals` (array)
- `scope_boundaries` (array)
- `unknowns_and_ambiguities` (array)
- `evidence_facts` (array of short, atomic facts)
- `fact_provenance` (array/object mapping each evidence fact to source: `title|body|diff|file_context`)
- `reported_claims` (what author claims; may be incorrect)
- `extractor_inference` (model’s normalized interpretation)
- `insufficient_context` (boolean)
- `missing_info` (array)
- `extraction_confidence` (0..1)

### 5.2 PR-specific fields

- `key_changed_components` (paths/modules)
- `behavioral_intent` (what behavior change is intended)
- `change_summary` (concise)
- `risk_notes` (kept for review/audit, usually not embedded)

### 5.3 Two renderings are mandatory

Store and treat these separately:

1. `card_json` (full structured audit record)
2. `card_text_for_embedding` (retrieval-optimized rendering)

`card_text_for_embedding` requirements:
- deterministic field ordering,
- bounded length,
- explicit section labels,
- no prompt/runtime metadata,
- include only high-signal semantic fields.

### 5.4 Embedding inclusion/exclusion policy

Default **include** in `card_text_for_embedding`:
- `problem_statement`
- `desired_outcome`
- `important_signals`
- `scope_boundaries`
- `evidence_facts`
- PR: `key_changed_components`, `behavioral_intent`, `change_summary`

Default **exclude/demote** from `card_text_for_embedding`:
- long free-form prose beyond cap
- operational metadata (provider/model/prompt version/confidence)
- `risk_notes` (keep in `card_json`, usually not embedded)

### 5.5 Phase 0 locked field limits and normalization rules

The following limits are locked for initial implementation and can only change via explicit doc update.

Common string caps (characters, post-normalization):
- `problem_statement`: 500
- `desired_outcome`: 500
- `behavioral_intent`: 500
- `change_summary`: 700
- each `important_signals` item: 220
- each `scope_boundaries` item: 220
- each `unknowns_and_ambiguities` item: 220
- each `evidence_facts` item: 260
- each `reported_claims` item: 260
- each `extractor_inference` item: 260
- each `missing_info` item: 220

Array caps:
- `important_signals`: max 12
- `scope_boundaries`: max 10
- `unknowns_and_ambiguities`: max 10
- `evidence_facts`: max 15
- `fact_provenance`: max 15 (must map to evidence facts)
- `reported_claims`: max 10
- `extractor_inference`: max 10
- `missing_info`: max 10
- PR `key_changed_components`: max 60 paths
- `risk_notes`: max 10

Normalization rules:
- normalize line endings and trim whitespace.
- collapse repeated blank lines.
- deduplicate array items (case-insensitive after normalization).
- truncate over-limit fields deterministically (suffix `…`).
- enforce JSON schema strictly; invalid payloads are `status=failed`.

### 5.6 Phase 0 locked embedding rendering contract

`card_text_for_embedding` must be rendered with deterministic section order:

1. `TYPE`
2. `PROBLEM`
3. `DESIRED_OUTCOME`
4. `IMPORTANT_SIGNALS`
5. `SCOPE_BOUNDARIES`
6. `EVIDENCE_FACTS`
7. PR-only: `PR_KEY_CHANGED_COMPONENTS`
8. PR-only: `PR_BEHAVIORAL_INTENT`
9. PR-only: `PR_CHANGE_SUMMARY`

Rendering constraints:
- maximum total `card_text_for_embedding` length: 4000 chars.
- preserve section labels exactly for reproducibility.
- do not include `reported_claims`, `extractor_inference`, `risk_notes`, `unknowns_and_ambiguities`, `missing_info`, or operational metadata.

---

## 6) PR extraction context policy (phase-1 safe mode)

Use deterministic, bounded context first.

Phase 0 locked defaults:
- max changed files: 40
- max patch chars/file: 2000
- max total patch chars: 50000
- max file-context chars/file: 1200
- max total file-context chars: 12000
- skip binaries and huge generated files
- prefer changed-file-local context only (no autonomous repo-wide exploration in this phase)

Rationale:
- improves intent quality vs title/body-only,
- controls latency/cost,
- limits prompt-injection blast radius.

Note: “free exploration agent” behavior is explicitly deferred until baseline metrics are known.

---

## 7) Data model changes (proposed)

Add tables (or equivalent columns where justified):

### 7.1 `intent_cards`

- `id` (pk)
- `item_id` (fk items.id)
- `source_content_hash` (bind card to specific source state)
- `schema_version`
- `extractor_provider`
- `extractor_model`
- `prompt_version`
- `card_json` (jsonb)
- `card_text_for_embedding` (text)
- `embedding_render_version`
- `status` (`fresh` | `stale` | `failed`)
- `insufficient_context` (boolean)
- `error_class` / `error_message` (nullable)
- timestamps

Constraints:
- unique `(item_id, source_content_hash, schema_version, prompt_version)`
- one latest-fresh lookup index per item

### 7.2 `intent_embeddings`

- `id` (pk)
- `intent_card_id` (fk intent_cards.id)
- `model`
- `dim` (3072 in v1 constraints)
- `embedding` (`vector(3072)`)
- `embedded_card_hash`
- `created_at`

Constraints:
- unique `(intent_card_id, model)`

### 7.3 Candidate set provenance

Record representation source in candidate-set metadata:
- `representation` (`raw` | `intent`)
- optionally `representation_version`

This is required for audit/replay and A/B analysis.

---

## 8) CLI/service changes (proposed)

### New command

- `dupcanon analyze-intent --repo ... [--type ...] [--state open|closed|all] [--only-changed] [--provider ...] [--model ...] [--workers N]`
  - extracts + validates + persists cards
  - logs parse/validation failures with artifacts

### Existing command extensions

- `embed --source raw|intent`
- `candidates --source raw|intent [--source-state open|closed|all]`
- `judge --source raw|intent`
- `judge-audit --source raw|intent`
- `canonicalize --source raw|intent`
- `plan-close --source raw|intent`
- `detect-new --source raw|intent` (planned; default remains raw until cutover)

### Service modules

- new intent extraction service (card generation + validation)
- database helpers for card freshness, lookup, and embedding write/read
- retrieval path branch based on representation source

---

## 9) Safety and threat model additions

1. **Extractor isolation per item**: no cross-item write authority.
2. **Strict JSON schema validation** for card outputs.
3. **Prompt-injection resistance**:
   - treat issue/PR text and diffs as untrusted input,
   - keep system instructions immutable,
   - avoid allowing source text to redefine extraction policy.
4. **Provenance-aware trust model**:
   - separate `reported_claims` from `extractor_inference`,
   - require `fact_provenance` so extracted facts are inspectable.
5. **Fallback policy (Phase 0 lock)**:
   - extraction failure => mark `intent_cards.status=failed` + artifact.
   - batch path: skip intent candidate generation for failed cards and continue raw path when `--source raw` (default) or explicit fallback mode is enabled.
   - online path (`detect-new`): if intent extraction fails, continue with raw retrieval and emit result metadata indicating fallback reason.
   - never silently drop failed items without status/metadata.
6. **No new destructive action path** introduced by intent mode.

---

## 10) Phased execution plan

This section is the implementation sequence for the intent-card extension. Follow phases in order and keep default behavior non-destructive until cutover criteria are met.

### Phase 0 — Contract lock (no behavior change)

**Goal**
- Align on schema, safety policies, and evaluation gates before code changes.

**Deliverables**
- Final card schema (`card_json` + `card_text_for_embedding`).
- Final field limits/truncation rules and embedding inclusion/exclusion policy.
- Final PR context budget defaults.
- Final fallback policy for extraction failures.
- Signed-off cutover and rollback criteria.

**Exit criteria**
- Team sign-off on this design doc.
- Explicit decision that rollout starts in shadow/flagged mode.

### Phase 1 — Storage foundation

Status: Complete (2026-02-15)

**Goal**
- Add persistence structures without changing current pipeline behavior.

**Deliverables**
- Migrations for `intent_cards`, `intent_embeddings`, and candidate representation provenance.
- Pydantic models for card contracts and DB records.
- Database methods for upsert/list/get freshness.

**Exit criteria**
- Migrations pass local reset/lint checks.
- Unit tests cover read/write paths for new tables.

Implementation notes (2026-02-15)
- Added migration `supabase/migrations/20260216091500_add_intent_cards_phase1.sql`.
- Added intent-card and intent-embedding contracts to `src/dupcanon/models.py`.
- Added DB accessors for intent-card upsert/freshness/listing in `src/dupcanon/database.py`.
- Added candidate-set representation persistence support (`raw|intent`) in DB insert path.

### Phase 2 — Intent extraction (shadow)

Status: Complete (2026-02-16)

**Goal**
- Generate and persist intent cards without changing retrieval/judging defaults.

**Deliverables**
- New service for issue + PR card extraction and schema validation.
- New CLI command: `analyze-intent`.
- Structured failure artifacts and observability fields (provider/model/prompt version/source hash).

**Exit criteria**
- High schema-valid output rate.
- Extraction failures are classified and auditable.
- Existing raw pipeline remains unchanged.

Implementation notes (2026-02-16)
- Added `analyze-intent` CLI command in `src/dupcanon/cli.py`.
- Added extraction service `src/dupcanon/intent_card_service.py` for issue/PR intent-card generation.
- Added bounded PR changed-file/patch context in extraction prompts.
- Added extractor failure persistence with `status=failed` sidecar rows + artifacts.
- Added regression tests in `tests/test_intent_card_service.py` and CLI coverage for `analyze-intent`.

### Phase 3 — Intent embedding path

Status: Complete (2026-02-16)

**Goal**
- Produce embeddings from `card_text_for_embedding`.

**Deliverables**
- `embed --source intent` path with incremental freshness handling.
- `intent_embeddings` write/read path with model/dim compatibility checks.

**Exit criteria**
- Embedding coverage for fresh cards is stable.
- No regressions in existing `embed --source raw` behavior.

Implementation notes (2026-02-16)
- Extended `embed` command with `--source raw|intent` in `src/dupcanon/cli.py`.
- Added intent embedding execution path in `src/dupcanon/embed_service.py`:
  - reads latest fresh intent cards,
  - computes incremental freshness via `embedded_card_hash` vs rendered text hash,
  - writes to `intent_embeddings`.
- Kept raw embedding path as default and behavior-compatible.
- Added regression coverage in `tests/test_embed_service.py` and `tests/test_cli.py` for intent source behavior.

### Phase 4 — Retrieval A/B (raw vs intent)

**Goal**
- Compare retrieval quality before touching judge defaults.

**Deliverables**
- `candidates --source raw|intent` support.
- Candidate-set provenance persisted (`representation`, optional version).
- Side-by-side retrieval comparison reports.

**Exit criteria**
- A/B retrieval metrics available for same time window.
- Raw path remains stable and reproducible.

Implementation notes (2026-02-18)
- Added `candidates --source raw|intent` in `src/dupcanon/cli.py` and `src/dupcanon/candidates_service.py`.
- Retrieval now selects source embeddings by representation:
  - raw: `public.embeddings`
  - intent: latest fresh `intent_cards` + `public.intent_embeddings`
- Candidate-set writes now persist explicit representation provenance for both modes.
- Retrieval artifacts now persist enough provenance (`representation`, optional `representation_version`) to feed source-aware judge/canonicalize/plan-close paths.

### Phase 5 — Source-aware downstream pipeline (implemented foundation)

**Goal**
- Evaluate end-to-end duplicate decisions using intent retrieval while preserving current guardrails.

**Deliverables**
- `judge --source raw|intent` support with representation-scoped accepted-edge lifecycle.
- `judge-audit --source raw|intent` support for sampled cheap-vs-strong comparisons.
- `canonicalize --source raw|intent` and `plan-close --source raw|intent` support for source-consistent downstream planning.
- Representation provenance persisted on `judge_decisions`, `judge_audit_runs`, and `close_runs`.

**Exit criteria**
- No safety-policy regressions.
- Measured precision profile is acceptable relative to raw baseline.

### Phase 6 — Controlled cutover decision

**Goal**
- Decide whether intent becomes default retrieval source.

**Deliverables**
- Formal cutover review with metrics + maintainer feedback.
- Default switch plan (if approved) and explicit rollback plan.

**Exit criteria**
- Meets precision gate policy (project baseline: >= 0.90 on >= 100 labeled proposed closes).
- No regressions in critical veto/safety behavior.
- `--source raw` rollback path remains available.

### Phase 7 — Online path promotion

**Goal**
- Promote online detection (`detect-new`) to intent mode only after batch path validation.

**Deliverables**
- `detect-new` default source update (optional, explicit decision).
- Continued non-destructive online behavior with JSON-first outputs.

**Exit criteria**
- Online precision/latency acceptable in shadow/assist usage.
- No destructive auto-actions introduced.

---

## 11) Evaluation and cutover criteria

Evaluate raw vs intent side-by-side on the same windows.

Minimum evaluation package:
1. extraction quality checks (schema validity rate, insufficient-context rate)
2. retrieval quality deltas (e.g., match coverage in top-k)
3. cheap-vs-strong disagreement profile on both modes
4. precision-focused sampled labeling on proposed close actions
5. false-positive risk review with maintainers

Phase 0 locked cutover gate (must all pass):
- precision gate: meet or exceed project policy baseline (`>= 0.90` precision on `>= 100` labeled proposed closes).
- no regression in deterministic safety veto behavior (`target_not_open`, `candidate_gap_too_small`, structural vetoes).
- retrieval quality is non-inferior in A/B evaluation windows (intent vs raw) for target operational cohorts.
- operator review confirms intent cards are materially more informative than raw text for triage decisions.
- rollback plan is validated and documented (`--source raw` path remains functional).

---

## 12) Phase 0 decision record (locked)

The following decisions are locked for implementation start:

1. Card schema fields and strict validation contract are fixed as documented in Section 5.
2. Embedding rendering contract (`card_json` vs `card_text_for_embedding`) is fixed as documented in Sections 5.3–5.6.
3. PR context budget defaults are fixed as documented in Section 6.
4. Extraction failure fallback behavior is fixed as documented in Section 9.
5. Rollout starts in shadow/flagged mode with raw path as default until cutover criteria are met.

Non-blocking future decisions (post-Phase-0):
- long-term deprecation plan for raw embeddings (if any),
- timing for making intent source default in online detection after batch validation.

---

## 13) Implementation checklist (initial)

1. Migration: add `intent_cards`, `intent_embeddings`, candidate provenance.
2. Models: card contracts + DB record models.
3. DB methods: upsert/list/get freshness + embedding methods.
4. Service: extraction with strict parsing and artifact logging.
5. CLI: `analyze-intent` + `--source` extensions.
6. Retrieval integration: intent embeddings path.
7. Judge integration: representation-aware input source.
8. Tests: unit + integration + CLI regression for both modes.
9. Docs/runbook updates after each phase.

---

## 14) Phase 0 completion checklist

Status: Complete (2026-02-15)

- [x] Card schema contract finalized.
- [x] Field limits and normalization rules finalized.
- [x] Embedding rendering contract finalized.
- [x] PR context budgets finalized.
- [x] Extraction fallback policy finalized.
- [x] Cutover/rollback criteria finalized.
- [x] Rollout mode set to shadow/flagged with raw default.

Phase 1 is now authorized to start (storage foundation only; no behavior switch).

---

## 15) Relationship to current v1 spec

This document proposes a staged extension and does **not** immediately replace v1 locked decisions.

Current v1 defaults remain the operational baseline until a documented cutover decision is approved.

---

## 16) Journaling location

All implementation journaling for this phase is maintained in:
- `docs/internal/journal.md`

Do not append future implementation chronology in this design doc or the v1 spec doc; use the central journal file.