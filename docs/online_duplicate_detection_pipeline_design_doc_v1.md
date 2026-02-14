# Online Duplicate Detection Pipeline (v1) — Design Doc

Status: Draft for implementation planning
Owner: dupcanon
Date: 2026-02-14

---

## 1) Purpose

Define a **near-real-time duplicate check pipeline** for newly submitted GitHub issues/PRs.

The pipeline should take a new item, compare it to the repo corpus, and output JSON indicating whether it is likely a duplicate.

This doc is intentionally separate from the batch canonicalization design (`duplicate_triage_cli_python_spec_design_doc_v_1.md`).

---

## 2) Product decisions (confirmed)

These are locked from stakeholder Q&A:

- Output format: JSON (contains more than a boolean; UI/animation can extract `is_duplicate`).
- Match scope: same-type only:
  - issue -> issues
  - pr -> prs
- Repo scope: single repo.
- Candidate state filter: **open items only**.
- Triggers: GitHub + CLI.
- Input features:
  - issues: title + body
  - prs: title + body + changed file paths + limited patch excerpts
- Post-detection action in v1: JSON output only (no auto-close).
- Decision classes: 3-way outcome:
  - `duplicate`
  - `maybe_duplicate`
  - `not_duplicate`
- Optimization priority: **precision-first** (minimize false positives).
- Latency: async acceptable.
- Model/provider: configurable.
- Learning loop: out of scope in v1.
- Storage strategy: reuse existing tables.
- Rollout: start in **shadow mode**.

---

## 3) Goals and non-goals

### Goals

1. Given a new issue/PR, produce deterministic JSON verdict + supporting evidence.
2. Reuse existing embeddings/candidate/judge architecture where possible.
3. Avoid costly full reprocessing on each new item.
4. Maintain auditability (store enough data to explain decisions).

### Non-goals (v1)

- Auto-close or auto-comment on GitHub.
- Cross-repo duplicate detection.
- Cross-type matching (issue<->PR).
- Feedback-training loop from maintainer corrections.

---

## 4) End-state architecture (two coordinated pipelines)

The target operating model is **not** a single command. It is two coordinated pipelines:

### A) Corpus maintenance pipeline (batch/scheduled)

Purpose: keep searchable corpus fresh so online checks are accurate.

Recommended cadence:
- frequent `sync`/`refresh` (repo-specific)
- `embed --only-changed` after sync

Outputs reused by online detection:
- up-to-date `items`
- up-to-date `embeddings`

Without this maintenance pipeline, online duplicate detection quality will degrade (stale corpus risk).

### B) Online inference pipeline (event-driven or manual)

Purpose: classify a *single new* issue/PR as duplicate/maybe/not.

1. Trigger from GitHub event (`issues.opened`, `pull_request.opened`) or CLI.
2. Load source item context.
3. Build source text payload.
4. Retrieve candidates from **open same-type** corpus.
5. Judge via configured provider/model.
6. Map result to 3-way verdict.
7. Emit JSON artifact/output (shadow mode in v1).

### C) Fit assessment (current codebase)

- ✅ Current CLI architecture is the right base (DB-first + reusable services).
- ✅ Existing batch commands already support corpus maintenance.
- ✅ `detect-new` command/service is implemented for one-item online inference.
- ✅ PR diff context (file paths + bounded patch excerpts) is implemented for PR online inference.
- ✅ GitHub Actions shadow workflow is implemented (`.github/workflows/detect-new-shadow.yml`).
- ⚠️ Hosted DB access remains required for workflow execution (local DB alone is insufficient).

---

## 5) Source representation

### 5.1 Issues

Use:
- `title`
- `body`

### 5.2 PRs

Use:
- `title`
- `body`
- changed file paths
- limited patch excerpts

Recommended limits (v1 defaults):
- `max_changed_files = 30`
- `max_patch_chars_per_file = 2000`
- `max_total_patch_chars = 12000`
- skip binary files / missing patches

Rationale: captures implementation context while keeping token cost and latency bounded.

---

## 6) Candidate retrieval policy

- Scope by repo + type + open state.
- Exclude source item itself.
- Use existing embedding vector search path.
- Default retrieval params should remain configurable, with precision-oriented defaults:
  - `k = 8` (baseline)
  - `min_score = 0.75` (baseline)

If zero candidates pass threshold:
- return `not_duplicate` with reason `no_candidates`.

---

## 7) Judging policy and outcome mapping

The LLM judge still returns duplicate decision + confidence for one target candidate (or none).

Pipeline maps to 3-way verdict:

- `duplicate`
  - model returns duplicate target
  - confidence >= `duplicate_threshold`
  - strict duplicate guardrails pass (`same_instance`, `root_cause_match=same`, `scope_relation=same_scope`, `certainty=sure`)
  - strongest retrieval support is also high (current code floor: `top_matches[0].score >= 0.90`)
- `maybe_duplicate`
  - duplicate decision with confidence in [`maybe_threshold`, `duplicate_threshold`)
  - OR duplicate decision fails strict guardrails (`reason=online_strict_guardrail:*`)
  - OR duplicate decision has high confidence but weak retrieval support (`reason=duplicate_low_retrieval_support`)
  - OR invalid/ambiguous response with strong retrieval clues
- `not_duplicate`
  - no duplicate selected
  - OR confidence < `maybe_threshold`
  - OR no candidates

Recommended precision-first starting thresholds:
- `maybe_threshold = 0.85`
- `duplicate_threshold = 0.92`

(These are intentionally stricter than batch edge acceptance.)

---

## 8) JSON output contract (v1)

```json
{
  "schema_version": "v1",
  "repo": "org/name",
  "type": "issue|pr",
  "source": {
    "number": 123,
    "title": "..."
  },
  "verdict": "duplicate|maybe_duplicate|not_duplicate",
  "is_duplicate": true,
  "confidence": 0.94,
  "duplicate_of": 98,
  "reasoning": "short explanation",
  "top_matches": [
    {
      "number": 98,
      "score": 0.91,
      "state": "open",
      "title": "..."
    }
  ],
  "provider": "gemini|openai|openrouter|openai-codex",
  "model": "...",
  "run_id": "...",
  "timestamp": "2026-02-14T...Z"
}
```

Rules:
- `schema_version` is required and currently fixed to `v1`.
- `is_duplicate == true` only when `verdict == "duplicate"`.
- `duplicate_of` is required for `duplicate`, optional for `maybe_duplicate`, null for `not_duplicate`.
- `top_matches` should include retrieved candidates (e.g., top 3-5).

---

## 9) Persistence and reuse of existing tables

Online detection reuses existing core tables for source/corpus state, but v1 `detect-new` is intentionally non-persistent for judge outcomes.

Current v1 reuse:
- `items`: source and corpus items (source is fetched and upserted)
- `embeddings`: source/corpus vectors (source embedding is upserted on change)
- retrieval is executed directly via vector search (no persisted candidate_set snapshot in v1 detect-new)

Current v1 non-goal:
- `detect-new` does **not** persist online judge outcomes into `judge_decisions`.
- `detect-new` remains a shadow-mode JSON output path.

Batch accepted-edge consumers continue to read from `judge_decisions.final_status='accepted'`.

---

## 10) Trigger integration details

### 10.1 CLI

Implemented command:

```bash
uv run dupcanon detect-new --repo org/name --type issue --number 123
```

Current supported options:
- `--provider`
- `--model`
- `--thinking` (`off|minimal|low|medium|high|xhigh`)
- `--k`
- `--min-score`
- `--maybe-threshold`
- `--duplicate-threshold`
- `--json-out <path>`

Note:
- Ad-hoc text-only source mode (`--title` / `--body` / `--pr-files-json`) is not implemented in v1.

Implementation note:
- Core logic lives in a service module; CLI is a thin wrapper so GitHub workflow and local CLI share the same inference path.

### 10.2 GitHub

Initial v1 recommendation:
- Trigger via GitHub Actions on `issues.opened` and `pull_request.opened`.
- Execute online detection CLI command for the single incoming item.
- Save JSON as workflow artifact/log (shadow mode).

Required runtime assumptions:
- Workflow must connect to **hosted Supabase/Postgres** (`SUPABASE_DB_URL` secret).
- Provider API keys must be available as workflow secrets.
- Corpus maintenance pipeline must already be running (scheduled sync/embed).

Recommended workflow guardrails:
- Use workflow `concurrency` keyed by `repo + item_number` to avoid duplicate runs on retries.
- Fail fast on missing DB/API secrets.
- Upload JSON output even on soft-failure paths when possible.

Why this path first:
- minimal infrastructure
- async by design
- easy iteration on thresholds/prompts
- clean migration path to later label/comment automation

---

## 11) Config

Use configurable provider/model/thinking with existing pattern:
- provider in {`gemini`, `openai`, `openrouter`, `openai-codex`}
- model string provider-specific
- thinking in {`off`, `minimal`, `low`, `medium`, `high`, `xhigh`}

Online detect-new inherits judge defaults when flags are omitted:
- `DUPCANON_JUDGE_PROVIDER`
- `DUPCANON_JUDGE_MODEL`
- `DUPCANON_JUDGE_THINKING`

Model resolution behavior for detect-new:
- if `--model` is provided, it wins
- else if selected provider matches configured provider, `DUPCANON_JUDGE_MODEL` is used
- else provider defaults are used (`gemini-3-flash-preview`, `gpt-5-mini`, `minimax/minimax-m2.5`, `gpt-5.1-codex-mini`)

GitHub workflow-specific tuning vars (optional, consumed by `.github/workflows/detect-new-shadow.yml`):
- `DUPCANON_ONLINE_PROVIDER` (default in workflow: `gemini`)
- `DUPCANON_ONLINE_MODEL` (default: empty -> command/provider default resolution)
- `DUPCANON_ONLINE_THINKING` (default: empty -> no explicit `--thinking` flag)
- `DUPCANON_ONLINE_K` (default: `8`)
- `DUPCANON_ONLINE_MIN_SCORE` (default: `0.75`)
- `DUPCANON_ONLINE_MAYBE_THRESHOLD` (default: `0.85`)
- `DUPCANON_ONLINE_DUPLICATE_THRESHOLD` (default: `0.92`)

For `DUPCANON_ONLINE_THINKING`, allowed values are `off|minimal|low|medium|high|xhigh`.
Gemini provider paths reject `xhigh`.

Current hardcoded precision guardrail (non-configurable in v1 code):
- duplicate verdict additionally requires top retrieval score >= `0.90`

Future (Stage 3) auto-close controls:
- `DUPCANON_ONLINE_AUTO_CLOSE_ENABLED`
- `DUPCANON_ONLINE_AUTO_CLOSE_THRESHOLD`

---

## 12) Safety/guardrails and approval model

### 12.1 Online pipeline (v1)

- No mutation on GitHub (shadow mode): no close, no label, no comment.
- Keep strict same-type + single-repo scoping.
- Precision-first thresholds plus extra duplicate guardrails.
- Persist failure artifacts under `.local/artifacts/`.
- Return structured `error_class` and `reason` fields when inference is inconclusive.
- Operational `reason` examples:
  - `judge_duplicate`
  - `low_confidence_duplicate`
  - `duplicate_low_retrieval_support`
  - `online_strict_guardrail:*`
  - `model_not_duplicate`
  - `no_candidates`
  - `invalid_judge_response`

### 12.2 Approval model split (important)

- The existing batch close workflow (`plan-close` -> `apply-close`) remains the **manual-review path** for bulk changes.
- That batch path continues to require reviewed `close_run` + `--yes` before mutation.
- The online detection workflow is a separate path and, in v1, does not require manual approval because it performs no mutation.

### 12.3 Future auto-close policy (not in v1)

If/when Stage 3 auto-close is enabled, it should use a dedicated online close path with strict gates (not a direct reuse of bulk apply semantics):

Required gates:
1. `verdict == "duplicate"` (never auto-close on `maybe_duplicate`).
2. `confidence >= auto_close_threshold` (recommended starting point: `0.97`).
3. Canonical target is open and in same repo/type.
4. Maintainer and assignee protections pass.
5. Idempotency checks pass (no duplicate close action already applied for same source).
6. Explicit feature flag is enabled (e.g., `DUPCANON_ONLINE_AUTO_CLOSE_ENABLED=true`).
7. Full audit row + artifact capture for every attempted mutation.

---

## 13) Rollout plan

### Stage 0 — Shadow mode (v1)
- Produce JSON only.
- Measure precision/recall manually on sampled items.

### Stage 1 — Assist mode (future)
- Optional maintainer-facing signal (non-blocking).

### Stage 2 — Label/comment mode (future)
- Optional labels/comments driven by thresholds.

### Stage 3 — Auto-action mode (future)
- Optional auto-close using strict automated gates (Section 12.3).
- This is separate from bulk `plan-close` / `apply-close` manual review flow.

---

## 14) Evaluation plan (v1)

Track per-trigger run stats:
- total processed
- verdict distribution (`duplicate`, `maybe_duplicate`, `not_duplicate`)
- invalid response count
- median/p95 latency
- manual precision over reviewed duplicate predictions
- corpus freshness indicators (time since last sync/embed)

Primary quality target for go/no-go beyond shadow mode:
- very low false positives (precision-first gate)

---

## 15) Implementation sequence (updated)

1. Confirm hosted DB readiness for workflow runs.
   - schema up to date
   - corpus data copied/seeded from local as needed
2. Lock corpus-maintenance operations for each repo.
   - scheduled `sync`/`refresh`
   - scheduled `embed --only-changed`
3. Add `detect-new` service + CLI command with JSON output.
4. Implement PR file-path + bounded patch excerpt preprocessing.
5. Reuse candidate retrieval constrained to open same-type corpus.
6. Reuse judge prompt path and add 3-way mapping layer.
7. Add GitHub Action for issue/pr opened events (shadow mode).
8. Add run metrics + artifacts + concurrency/idempotency guardrails.
9. Keep workflow in shadow mode until precision targets are met; do not enable mutation in v1.

---

## 16) Open questions (for implementation kickoff)

1. For ad-hoc CLI payloads, should we persist ephemeral source rows in DB or keep fully transient?
2. Should `maybe_duplicate` include one suggested target by default, or only ranked `top_matches`?
3. Do we want separate thresholds for issue vs PR in v1, or a shared global default?

---

## 17) Summary

This v1 online pipeline is a precision-first, async-friendly, JSON-first duplicate detector for newly opened issues/PRs. It uses a two-pipeline model: scheduled corpus maintenance plus event-driven single-item inference. It deliberately starts in shadow mode, reuses existing dupcanon data structures, and adds PR diff context in a bounded way (file paths + limited patch excerpts) to improve duplicate discrimination without exploding latency/cost. Bulk manual-review closing (`plan-close` / `apply-close`) remains a separate path; online auto-close, if added later, requires dedicated strict automated gates.

---

## 18) Implementation journal (ongoing)

Note
- Entries are chronological snapshots and may describe intermediate states that were later completed.
- Current behavior is defined by Sections 1–17 above.

### 2026-02-14 — Step 1: Online inference service foundation

Done
- Added online detection result models:
  - `DetectVerdict`, `DetectSource`, `DetectTopMatch`, `DetectNewResult`
  - `CandidateItemContext` model for candidate metadata joins
- Added DB helper methods needed for single-item inference:
  - `get_embedding_item_by_number(...)`
  - `list_item_context_by_ids(...)`
- Added new service:
  - `src/dupcanon/detect_new_service.py`
  - fetches source item from GitHub
  - upserts source item into DB
  - embeds source on-demand when content changed
  - retrieves open same-type candidates
  - runs judge with configurable provider/model
  - maps to 3-way verdict (`duplicate` / `maybe_duplicate` / `not_duplicate`)
  - returns JSON-ready structured result

Notes
- PR diff context is not implemented in this step yet; service logs a warning and currently evaluates PRs using title/body only.

Next
1. Add `detect-new` CLI command with JSON output and `--json-out`.
2. Add regression tests for service and CLI.
3. Run full quality gates and append next journal entry.

### 2026-02-14 — Step 2: CLI command surface for online detection

Done
- Added `detect-new` command to CLI:
  - `dupcanon detect-new --repo ... --type issue|pr --number N`
- Added CLI options for online inference tuning:
  - `--provider`, `--model`, `--k`, `--min-score`, `--maybe-threshold`, `--duplicate-threshold`
  - `--json-out` for workflow-friendly file output
- Added provider default-model helper and reused it for both:
  - `judge`
  - `detect-new`

Notes
- Command currently outputs JSON result and optionally writes JSON to file.
- Service logic is shared via `run_detect_new(...)` to keep CLI thin and workflow-compatible.

Next
1. Add regression tests for detect-new service behavior (duplicate/maybe/not-duplicate/fallback).
2. Add CLI tests for detect-new defaults and json-out behavior.
3. Run full quality gates and append next journal entry.

### 2026-02-14 — Step 3: Regression tests + quality gates

Done
- Added service regression test suite:
  - `tests/test_detect_new_service.py`
  - covers duplicate, maybe-duplicate, not-duplicate/no-candidates, invalid-judge fallback, missing-provider-key guardrail
- Extended CLI regression tests in `tests/test_cli.py`:
  - `detect-new` appears in top-level help
  - `detect-new --help` exposes core options
  - openrouter default-model behavior for `detect-new`
  - `--json-out` writes JSON output file
- Ran full quality gates successfully:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (105 passed)

Notes
- Initial implementation intentionally keeps online workflow in shadow mode (JSON only, no GitHub mutations).
- Online PR diff enrichment remains pending (currently title/body only with warning log for PRs).

Next
1. Implement PR changed-file + bounded patch-excerpt ingestion for `detect-new` PR path.
2. Add GitHub Actions workflow for `issues.opened` / `pull_request.opened` running `detect-new` in shadow mode.
3. Add freshness/idempotency telemetry fields to online JSON output if needed by workflow consumers.

### 2026-02-14 — Step 4: PR diff context integration (bounded)

Done
- Added GitHub client support for PR file retrieval:
  - `GitHubClient.fetch_pull_request_files(...)`
  - uses paginated GitHub endpoint `repos/{repo}/pulls/{number}/files`
- Added PR diff context model:
  - `PullRequestFileChange`
- Integrated bounded PR diff context into `detect-new` judging path:
  - includes changed file paths
  - includes bounded patch excerpts with caps:
    - max files: 30
    - max chars per file: 2000
    - max total patch chars: 12000
- `detect-new` PR runs now augment source prompt body with this context before judging.

Regression coverage
- Added/updated tests:
  - `tests/test_detect_new_service.py`
    - verifies PR prompt includes changed files and patch excerpt context
  - `tests/test_github_client.py`
    - verifies PR files endpoint wiring and mapping behavior

Validation
- `uv run ruff check`
- `uv run pytest tests/test_detect_new_service.py tests/test_github_client.py`

Next
1. Add GitHub Actions workflow for `issues.opened` / `pull_request.opened` running `detect-new` in shadow mode.
2. Run full quality gates after workflow integration and append next journal entry.

### 2026-02-14 — Step 5: GitHub Actions shadow workflow + docs alignment

Done
- Added workflow file:
  - `.github/workflows/detect-new-shadow.yml`
- Workflow behavior:
  - triggers on `issues.opened` and `pull_request.opened`
  - supports `workflow_dispatch` for manual runs
  - resolves source repo/type/number from event payload
  - runs `uv run dupcanon detect-new ... --json-out ...`
  - uploads JSON result artifact
- Added workflow safety controls:
  - per-item `concurrency` key (repo + item number)
  - preflight secret/provider checks
  - shadow-mode only (no mutation step)
- Updated operator docs:
  - `docs/operator_runbook_v1.md` now includes online `detect-new` usage and workflow secrets/vars
- Updated README command surface/docs links for `detect-new`

Validation
- Full quality gates run after integration:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (109 passed)

Next
1. (Optional) Add workflow output summaries (verdict/confidence) to GitHub job summary.
2. (Optional) Add schema_version field to detect-new JSON for downstream stability.
3. Keep shadow mode while collecting precision metrics before any automation stage.

### 2026-02-14 — Step 6: Output stability + workflow summary polish

Done
- Added `schema_version` to online detection JSON contract:
  - `DetectNewResult.schema_version` defaults to `"v1"`
- Updated workflow to publish a concise job summary table from detect-new JSON output:
  - verdict
  - confidence
  - duplicate target
  - provider/model
  - run id
- Updated design doc JSON contract section to include and document `schema_version`.
- Added model regression assertion for `schema_version` default in `tests/test_models.py`.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest` (109 passed)

Next
1. Keep shadow mode while collecting precision metrics before any automation stage.
2. Decide when to promote from shadow to assist mode based on sampled precision.
