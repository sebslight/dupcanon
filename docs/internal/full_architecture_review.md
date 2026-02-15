# Full Architecture Review: dupcanon vs simili-bot

_Date: 2026-02-15_

## 1) Executive summary

This review compares:

- **Current project:** `dupcanon` (this repo)
- **Reference project:** `https://github.com/similigh/simili-bot` (cloned to `/tmp/simili-bot`)

### Bottom line

They are **related in problem domain** (duplicate detection for GitHub issues/PRs), but `dupcanon` is **not a direct code continuation** of `simili-bot`.

- `dupcanon` is a **DB-first canonicalization + safe close-planning CLI**.
- `simili-bot` is a **modular issue intelligence bot** (similarity, duplicate hints, routing, triage, action execution).

So this is not "completely new" conceptually, but it is a **new implementation with a different architecture and product center of gravity**.

---

## 2) Verification and evidence used

### Local checks run

#### dupcanon
- `uv run ruff check` ✅
- `uv run pyright` ✅
- `uv run pytest -q` ✅ (`264 passed`)

#### simili-bot
- `go test ./...` ✅ (tests passed across command/internal/integration packages)

### Repository clone step completed

- Ran: `gh repo clone similigh/simili-bot` into `/tmp/simili-bot`
- Reviewed key code/docs in:
  - `/tmp/simili-bot/README.md`
  - `/tmp/simili-bot/cmd/simili/commands/*`
  - `/tmp/simili-bot/internal/core/*`
  - `/tmp/simili-bot/internal/steps/*`
  - `/tmp/simili-bot/internal/integrations/*`

---

## 3) Current status of dupcanon

`dupcanon` is already beyond prototype stage for v1 batch flow and online shadow detection.

### Implemented command surface

- `init`
- `sync`
- `refresh`
- `embed`
- `candidates`
- `judge`
- `judge-audit`
- `report-audit`
- `detect-new`
- `canonicalize`
- `maintainers`
- `plan-close`
- `apply-close`

### Operational maturity indicators

- End-to-end batch pipeline is implemented and tested.
- Online single-item inference (`detect-new`) is implemented.
- Shadow GitHub workflow exists (`.github/workflows/detect-new-shadow.yml`).
- Migrations are explicit and versioned in `supabase/migrations/`.
- `judge_decisions` is the canonical decision/edge source-of-truth.
- Structured logs + Logfire sink are integrated.

### Remaining gap explicitly acknowledged in docs

- No first-class dedicated Phase 9 evaluation command yet (precision gate workflow is still operational/manual-heavy).

---

## 4) Tech stack and runtime model (dupcanon)

## Language and tooling

- Python 3.12
- Dependency/env management: `uv`
- CLI: Typer
- UX/progress: Rich
- Validation/models/config: Pydantic + pydantic-settings
- DB client: psycopg
- DB platform: Supabase Postgres + pgvector
- Lint/type/test: ruff + pyright + pytest

## LLM / embedding providers

- Embeddings: OpenAI or Gemini
- Judge providers: `openai-codex` (default via `pi` RPC), OpenAI, Gemini, OpenRouter
- Thinking level controls with provider validation (e.g., Gemini disallows `xhigh`)

## Observability

- stdlib logging + Rich console handler
- Logfire remote sink (`send_to_logfire="if-token-present"`)
- Artifact payload policy: Logfire-first (no local failure artifact file writes)

---

## 5) Data architecture and schema strategy

`dupcanon` is intentionally **DB-first and auditable**.

Core tables:

- `repos`, `items`
- `embeddings` (`vector(3072)`, content hash bound)
- `candidate_sets`, `candidate_set_members`
- `judge_decisions` (accepted/rejected/skipped + metadata)
- `judge_audit_runs`, `judge_audit_run_items`
- `close_runs`, `close_run_items`

## Important design implications

1. **Reproducibility:** candidate snapshots are persisted; prompt/LLM changes can be replayed on stable retrieval sets.
2. **Auditability:** every decision lane and close plan has durable records.
3. **Safety constraints in DB:** consistency triggers + accepted-edge uniqueness.
4. **Canonical graph support:** accepted edges become an operational graph for clustering/canonicalization.

---

## 6) End-to-end information flow in dupcanon

There are two coordinated flows.

## A) Batch canonicalization + close pipeline

### 1. `sync` / `refresh`

- Pulls issues/PRs from GitHub into `items`.
- Semantic content hash uses **type + title + body** only.
- `content_version` bumps only on semantic change.
- Metadata-only updates do not trigger semantic version bump.
- Changed content propagates staleness to candidate snapshots.

### 2. `embed`

- Embeds changed/missing items.
- Deterministic truncation policy:
  - title ≤ 300 chars
  - body ≤ 7700 chars
  - combined ≤ 8000 chars
- Writes/upserts embeddings with `embedded_content_hash`.

### 3. `candidates`

- Performs same-repo, same-type retrieval from pgvector.
- Stores ranked candidate members per source item.
- Rotates fresh/stale candidate sets for reproducible judging.

### 4. `judge`

- Calls LLM on each source + candidate set.
- Parses strict JSON contract.
- Applies deterministic veto and confidence gates.
- Persists `judge_decisions` row as accepted/rejected/skipped.

### 5. `canonicalize`

- Builds connected components from accepted edges.
- Chooses one canonical per cluster deterministically.

### 6. `plan-close`

- Creates auditable plan rows with action=close/skip and skip reasons.
- Enforces maintainer/assignee and confidence guardrails.

### 7. `apply-close`

- Requires reviewed plan run + `--yes`.
- Creates `mode=apply` run, copies planned rows, performs GitHub close actions.
- Stores per-item API results.

## B) Online single-item detection (`detect-new`)

- Fetch source issue/PR
- Upsert source item
- Embed source if stale/missing
- Retrieve open same-type neighbors
- Judge and map into 3-way verdict
- Emit JSON (`duplicate`, `maybe_duplicate`, `not_duplicate`)

PR path enriches source context with bounded changed-file + patch excerpts.

---

## 7) What each dupcanon command does (operator-facing)

- **`init`**: runtime checks (DB DSN, keys, `pi` availability, artifacts dir)
- **`maintainers`**: lists maintainer logins from collaborators (`admin|maintain|push`)
- **`sync`**: full ingest/upsert pass for repo issues/PRs
- **`refresh`**: incremental discovery; optionally refresh known metadata
- **`embed`**: compute/update embeddings (`--only-changed` supported)
- **`candidates`**: persist nearest-neighbor candidate snapshots
- **`judge`**: LLM duplicate adjudication + decision persistence
- **`judge-audit`**: sampled cheap-vs-strong lane comparison
- **`report-audit`**: report persisted audit run + simulate deterministic gates
- **`detect-new`**: online single-item duplicate verdict JSON
- **`canonicalize`**: compute canonical stats from accepted-edge graph
- **`plan-close`**: create reviewable close plan with guardrails
- **`apply-close`**: execute reviewed plan with explicit confirmation

---

## 8) Duplicate definition and acceptance logic in dupcanon

`dupcanon` uses a strict duplicate definition:

> Same underlying issue/request instance; related topical overlap is insufficient.

A duplicate claim is accepted only if all of the following pass:

1. valid structured response (strict JSON)
2. `duplicate_of` points to a candidate in the set
3. confidence ≥ `min_edge` (default 0.85)
4. relation/root-cause/scope/path/certainty vetoes do not trip
5. bug-vs-feature mismatch veto does not trip
6. target candidate is open
7. selected candidate score gap vs best alternative ≥ 0.015
8. accepted-edge uniqueness constraints are satisfied

This is intentionally precision-first.

---

## 9) Deterministic gates + LLM combination

The system is hybrid by design.

## LLM handles

- semantic duplicate judgment
- structured relation/causality/scope signals
- natural-language reasoning

## Deterministic policy handles

- schema and candidate-bound checks
- thresholds (`min_edge`, `min_close`, online thresholds)
- veto policy (certainty/mismatch/open-target/gap)
- accepted edge cardinality and rejudge semantics
- maintainers/assignees protections for closing
- apply mutation gate (`mode=plan` + `--yes`)

Result: LLM is an important input, but **not autonomous final authority**.

---

## 10) Canonicalization and close guardrails in dupcanon

### Cluster + canonical rules

- Clusters are connected components on accepted-edge graph (treated undirected for clustering)
- Canonical preference order:
  1. open-first (if any open exists)
  2. English-language preference
  3. maintainer-authored preference
  4. highest discussion activity
  5. oldest created time
  6. lowest issue/PR number

### Close planning safety

A non-canonical item is eligible for close only if:

- it is open
- not maintainer-authored
- not maintainer-assigned
- maintainer identity is not uncertain
- it has a **direct** accepted edge to canonical
- direct edge confidence ≥ `min_close` (default 0.90)

This direct-edge requirement blocks weak transitive closes.

---

## 11) Online detection specifics (`detect-new`)

- Same provider/model/thinking control pattern as `judge`
- Defaults: `k=8`, `min_score=0.75`, maybe=0.85, duplicate=0.92
- Extra precision logic:
  - strict guardrail downgrade from `duplicate` to `maybe_duplicate`
  - retrieval support floor for high-confidence duplicate class
  - invalid judge response fallback strategy using retrieval evidence
- Output is strict JSON contract with schema version + run metadata.

---

## 12) Comparison: dupcanon vs simili-bot

| Dimension | dupcanon | simili-bot |
|---|---|---|
| Primary goal | Canonical duplicate graph + safe close planning/apply | General issue intelligence (triage/routing/similarity/actions) |
| Core data model | Relational Postgres-first with audit tables | Pipeline state + Qdrant-centered vector workflow |
| Duplicate persistence | `judge_decisions` as source-of-truth | Duplicate output as step result (no equivalent canonical edge graph system) |
| Canonicalization | Explicit connected-component canonical selection | Not primary architectural primitive |
| Close governance | Plan/apply audited runs + hard guardrails | Action executor model, broader but less canonical-close specific |
| Scope default | Single repo per run (v1) | Multi-repo and org workflows emphasized |
| Language/tooling | Python + Typer + Pydantic + Supabase | Go + Cobra + modular step registry + Qdrant |

## Shared features

- embeddings + semantic retrieval
- LLM judgment for triage/duplication
- GitHub integration and workflow execution

## Key divergence

`dupcanon` is deeply optimized for **governed duplicate canonicalization and closure safety**, while `simili-bot` is optimized for **modular, broader triage/routing automation**.

---

## 13) Is dupcanon “based on” simili-bot?

Most accurate answer:

- **Conceptually related:** yes (same operational pain class: duplicate-heavy issue triage)
- **Implementation lineage:** no direct codebase continuation/fork detected
- **Architecture/product shape:** substantially different

So: **related, but architecturally a separate project**.

---

## 14) Strengths and risks (dupcanon)

## Strengths

1. Strong end-to-end auditability and reproducibility
2. Clear split between retrieval snapshots and LLM adjudication
3. High-signal deterministic guardrails on top of LLM output
4. Useful evaluation tooling (`judge-audit`, `report-audit`, gate simulation/sweep)
5. Safe mutation boundary (`plan-close` review + `apply-close --yes`)

## Risks / next hardening priorities

1. Add first-class evaluation command for precision gate management
2. Monitor 3072-dim retrieval performance (ANN limits at this dimension in current host setup)
3. Formalize drift/remediation strategy for canonical changes when clusters merge
4. Continue de-duplication of runtime logic where helper overlap remains
5. Define an explicit programmatic orchestration layer for unattended DB refresh/update cycles
6. Expand post-detection action surface beyond close/no-close with richer label/taxonomy workflows

---

## 15) Missing pieces and forward plan (requested additions)

The following gaps are important to make the system fully operational at scale:

### A) Current mode is still shadow-oriented in external operations

- Today, online flow is strong for **detection + JSON output**, but mutation in external runtime contexts is still effectively conservative/shadow-first.
- This is good for precision safety, but limits autonomous impact.
- Needed next step: a controlled promotion path from shadow -> assist -> gated action, with clear precision thresholds and rollback controls.

### B) Full programmatic pipeline orchestration for DB freshness

A first-class, scriptable pipeline should be defined so DB state can be updated reliably without manual command chaining.

Recommended orchestration profile:

1. `sync` / `refresh --refresh-known`
2. `embed --only-changed`
3. `candidates --include open`
4. `judge` (or dual-lane `judge-audit` in calibration windows)
5. `canonicalize`
6. `plan-close` (optional auto-generated review bundle)
7. `apply-close --yes` only under policy gate

Needed enablers:

- stable machine-readable run summaries per stage
- idempotent retries/restarts at stage boundaries
- schedule-friendly entrypoint (single orchestrator command or workflow contract)
- explicit freshness SLOs (e.g., max age for items/embeddings/candidates)

### C) Future capability: tree-editing labels / richer triage actions

Beyond duplicate closing, future value can come from structured label operations and hierarchy-aware triage:

- label tree editing (add/remove/migrate labels across taxonomy levels)
- confidence-gated label actions separate from close actions
- policy bundles per repo (duplicate policy, label policy, routing policy)
- auditable label/action plans similar to close-run/apply-run semantics

This would extend dupcanon from duplicate canonicalization into broader governance-grade issue operations while preserving the same audit-and-gate philosophy.

---

## 16) Final conclusion

`dupcanon` is a production-oriented, precision-first duplicate canonicalization CLI with a strong governance model for close actions.

It is **related to simili-bot in mission area**, but it is **not just simili-bot rewritten**. The architecture, persistence model, safety gates, and command flow indicate an independently shaped system focused on durable decision records, canonical graph reasoning, and operator-reviewed mutation control.
