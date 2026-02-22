# dupcanon Development Journal

Status: Active

This file is the single location for chronological implementation journal entries.

Moved here from `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`:
- all historical entries from the original `## Development journal` section
- ongoing and future entries, including intent-card phase work

---

## Development journal

Note
- Entries are chronological snapshots and may describe workflows that were later superseded.
- Current behavior is defined by the sections above (CLI interface, safety/apply gate, runtime defaults, and locked v1 decisions).


### 2026-02-13 — Entry 1 (Phase 0 + Phase 1 foundations)

Today we established the project foundation and database baseline.

What we did
- Initialized Supabase locally and created the initial migration.
- Validated migrations locally with `supabase db reset` and `supabase db lint`.
- Linked hosted Supabase and pushed the migration remotely.
- Bootstrapped the Python project with `uv`, Typer, Rich logger, and Pydantic settings.
- Added baseline tests and passed `ruff`, `pyright`, and `pytest`.
- Added project operating guidance in `AGENTS.md`.
- Published the initial code to: `https://github.com/sebslight/dupcanon`.

What comes next
- Begin Phase 2 (`sync` + `refresh --refresh-known`) with DB + GitHub integration.

### 2026-02-13 — Entry 2 (Phase 2 first pass)

Today we delivered the first working implementation of sync/refresh behavior.

What we did
- Added a GitHub API client using `gh api` with retry/backoff.
- Added DB access layer operations for `repos` and `items`.
- Implemented semantic content hash/version rules (`type`, `title`, `body` only).
- Implemented `sync` flow (fetch, upsert, content-version behavior, Rich progress + summary).
- Implemented `refresh` known-item metadata flow (no discovery path yet).
- Added Pydantic domain models for refs, payloads, enums, and run stats.
- Added tests for repo parsing, semantic hashing, and `--since` parsing.

What comes next
- Clarify and document refresh semantics (`default` vs `--refresh-known`).
- Add integration tests for content-version and metadata-only updates.
- Add richer structured timing logs and artifact outputs.

### 2026-02-13 — Entry 3 (dry-run enhancement)

Today we added optional dry-run behavior for Phase 2 commands.

What we did
- Added `--dry-run` to `sync` and `refresh`.
- `sync --dry-run` now computes change stats without DB writes.
- `refresh --dry-run` now computes refresh stats without DB writes.
- Added DB inspection helper for sync dry-run (`inspect_item_change`).
- Updated CLI summaries to display `dry_run` mode.
- Added tests ensuring dry-run flags are exposed in CLI help.
- Updated command signatures in this spec to reflect dry-run options.

What comes next
1. Add integration tests against local Supabase for insert/update/content-version behavior.
2. Add structured timing fields (`duration_ms`) to command/stage logs.
3. Persist sync/refresh failure artifacts to `.local/artifacts`.
4. Run real sync/refresh with a valid Postgres DSN and capture baseline metrics.

### 2026-02-13 — Entry 4 (test hardening pass)

Today we hardened the test suite with lightweight, idiomatic coverage for critical behavior.

What we did
- Expanded `sync_service` tests with focused dry-run behavior checks:
  - dry-run sync when repo is not yet in DB
  - dry-run sync when repo exists and inspection path is used
  - dry-run refresh does not write metadata
  - DSN validation failure path (`None` and non-Postgres URL)
- Added targeted unit tests for GitHub client helper behavior:
  - HTTP status parsing
  - retry eligibility rules
  - label extraction
  - datetime parsing
- Expanded model tests:
  - blank `--since` handling
  - semantic hash changes across item types
- Added CLI guardrail test:
  - `sync` fails fast with clear message when `SUPABASE_DB_URL` is not a Postgres DSN
- Re-ran and passed all quality gates (`ruff`, `pyright`, `pytest`).

What comes next
1. Add DB integration tests against local Supabase for `content_version` transitions.
2. Add structured `duration_ms` timing fields at command/stage boundaries.
3. Persist sync/refresh error artifacts under `.local/artifacts`.
4. Run and snapshot one real sync/refresh baseline using a valid Postgres DSN.

### 2026-02-13 — Entry 5 (pagination resilience + fetch progress UX)

Today we improved sync behavior for large repositories and made fetch progress more transparent.

What we did
- Reworked GitHub list retrieval to use `gh api --paginate` stream parsing instead of page-number looping.
  - This avoids the REST `page` parameter failure on large datasets (`HTTP 422`).
- Implemented incremental paginated JSON parsing for concatenated page payloads.
- Added fetch-stage progress updates and page-style aggregation logs during sync.
  - Progress now reports ongoing fetched increments (issues/PRs) and aggregate totals.
- Added a friendlier network error hint for `No route to host` to guide users toward reachable DSNs.
- Expanded tests for:
  - paginated stream parsing
  - DSN validation (including IPv6 DSN format acceptance)
  - CLI error hint behavior

What comes next
1. Add DB integration tests against local Supabase for versioning transitions.
2. Add `duration_ms` timing fields to command/stage logs.
3. Persist sync/refresh failure artifacts for easier debugging.
4. Run a full baseline sync/refresh and capture metrics artifacts.

### 2026-02-13 — Entry 6 (server-side `--since` + incremental fetch progress)

Today we aligned `--since` semantics with created-at filtering and improved fetch progress visibility.

What we did
- Updated `--since` behavior to use **server-side created-at filtering** for both issues and PRs.
  - When `--since` is provided, sync now queries GitHub search with qualifiers like:
    - `is:issue created:>=YYYY-MM-DD`
    - `is:pr created:>=YYYY-MM-DD`
- Removed the large-dataset `page=` pagination path that caused `HTTP 422` failures.
- Switched paginated streaming to object-by-object (`--jq '.[]'` / `--jq '.items[]'`) processing.
- Improved fetch progress UX:
  - live aggregated counts for issues/PRs/total
  - incremental fetched counter updates while pages are streamed
- Added tests for:
  - server-side created filter query wiring
  - paginated batch flush behavior

What comes next
1. Add DB integration tests for `content_version` transitions.
2. Add structured `duration_ms` timings per command stage.
3. Persist sync/refresh failure artifacts under `.local/artifacts`.
4. Run full real-repo baselines and capture metrics snapshots.

### 2026-02-13 — Entry 7 (progress UX follow-up + `--since` confirmation)

Today we followed up on sync UX behavior on large repositories and confirmed filtering semantics.

What we did
- Verified and documented that `--since` is now applied server-side using created-date qualifiers in GitHub search mode.
- Investigated progress behavior during long fetch runs and updated fetch progress updates to:
  - increment task completion as batches are processed
  - show aggregate fetched counters while fetching is in progress
- Ran real dry-run sync checks against a large repo to validate behavior and catch regressions.

What comes next
1. Fine-tune fetch progress rendering to ensure increments are clearly visible in all terminal environments.
2. Add optional periodic progress log checkpoints (non-TUI fallback) for very long runs.
3. Add DB integration tests for versioning and metadata-only update behavior.
4. Add timing + artifact outputs to improve long-run observability and debugging.

### 2026-02-13 — Entry 8 (server-side type/state filtering without `--since`)

Today we enforced server-side type/state filtering even when `--since` is not provided.

What we did
- Updated sync fetch logic so non-`--since` paths also use server-side filtering:
  - issues fetched via GraphQL `repository.issues(states: ...)`
  - PRs fetched via GraphQL `repository.pullRequests(states: ...)`
- Kept `--since` behavior server-side via created-date search qualifiers.
- Added GraphQL paginated streaming collector using `gh api graphql --paginate` and object streaming.
- Added tests to verify routing/queries for:
  - issues without since -> GraphQL issues path
  - PRs without since -> GraphQL pullRequests path
  - existing created-date search path with since remains intact

What comes next
1. Validate large-repo sync runtime and progress readability in real terminal sessions.
2. Add DB integration tests for content versioning and metadata-only updates.
3. Add timing and artifact outputs to improve observability on long runs.

### 2026-02-13 — Entry 9 (Supabase pooler compatibility)

Today we fixed DB connection compatibility for the Supabase IPv4 pooler.

What we did
- Updated DB connection creation to disable psycopg auto-prepared statements:
  - `connect(..., prepare_threshold=None)`
- Added a focused unit test to ensure `prepare_threshold=None` is always passed.
- Re-ran and passed quality gates (`ruff`, `pyright`, `pytest`).

What comes next
1. Verify sync/refresh end-to-end using the IPv4 pooler DSN.
2. Add a short troubleshooting note in CLI output/docs for pooler connection mode and DSN expectations.
3. Continue progress UX refinement for long-running fetch stages.

### 2026-02-13 — Entry 10 (Phase 2 cleanup: DSN guidance + progress checkpoints)

Today we completed the planned Phase 2 cleanup follow-ups.

What we did
- Added explicit DSN troubleshooting guidance in CLI output (`init`) and error paths.
- Updated `.env.example` comments to clarify DSN expectations and pooler guidance.
- Refined sync fetch progress UX for long runs:
  - richer live counters (issues/PRs/total)
  - periodic structured checkpoint logs every 500 fetched items
- Added command/stage timing (`duration_ms`) to sync/refresh structured logs.
- Expanded test coverage for new CLI/help/config/database behavior.

What comes next
1. Run end-to-end sync/refresh verification against a reachable IPv4 pooler DSN and capture baseline metrics.
2. Persist sync/refresh failure artifacts under `.local/artifacts`.
3. Start Phase 3 embedding pipeline implementation.

### 2026-02-13 — Entry 11 (Phase 3 first pass: embedding command + service)

Today we implemented the first working pass of the embedding pipeline.

What we did
- Implemented `embed` command wiring in CLI (`--type`, `--only-changed`).
- Added deterministic embedding text construction with v1 truncation limits:
  - title max 300
  - body max 7,700
  - combined max 8,000
- Added Gemini embeddings client for `gemini-embedding-001` with retry/backoff and strict dimension validation.
- Added DB methods for listing embedding candidates and upserting embeddings.
- Implemented embedding service flow with:
  - repo/type candidate selection
  - unchanged-skip mode (`--only-changed`)
  - batch embedding with per-item fallback on batch failures
  - Rich progress and Rich logging
- Added embedding-related config defaults/validation (model, dim lock, batch size, concurrency).
- Added tests for embedding text limits, embedding flow behavior, batch fallback behavior, and Gemini response parsing.

What comes next
1. Verify embedding end-to-end on a real repo with Supabase + Gemini credentials.
2. Add embedding failure artifact persistence under `.local/artifacts`.
3. Begin Phase 4 candidate retrieval implementation.

### 2026-02-13 — Entry 12 (live verification: Phase 2 cleanup + Phase 3 embed)

Today we executed real end-to-end verification runs against the configured Supabase pooler DSN.

What we did
- Confirmed configured DSN host resolves to a Supabase pooler endpoint.
- Ran `dupcanon init` and verified runtime checks passed.
- Ran `sync` on `psf/requests` (issues, open):
  - dry-run: fetched 185, failed 0
  - write run: fetched 185, inserted 185, failed 0
- Ran `refresh --refresh-known` on the same repo:
  - dry-run: known 185, refreshed 185, failed 0
  - write run: known 185, refreshed 185, failed 0
- Ran `embed --only-changed` (issues):
  - first run: discovered 185, queued 185, embedded 185, failed 0
  - second run: discovered 185, queued 0, skipped_unchanged 185, failed 0

What comes next
1. Persist sync/refresh/embed failure artifacts under `.local/artifacts` for easier post-mortems.
2. Start Phase 4 candidate retrieval implementation (`candidates` command + DB persistence).

### 2026-02-13 — Entry 13 (Phase 4 first pass: candidate retrieval)

Today we implemented the first working candidate retrieval pipeline.

What we did
- Implemented `candidates` command wiring with spec-aligned options:
  - `--type issue|pr`
  - `--k` (default 8)
  - `--min-score` (default 0.75)
  - `--include open|closed|all`
- Added candidate retrieval service using pgvector cosine similarity, constrained to same repo + same type.
- Added DB operations for:
  - candidate source item discovery
  - stale marking for prior fresh candidate sets
  - candidate set creation
  - candidate member persistence with rank + score
- Added sync-time staleness propagation:
  - when an item’s semantic content changes, existing fresh candidate sets for that item are marked stale.
- Added candidate service and CLI tests.

Live verification
- Local run against `openclaw/openclaw` after sync + embed:
  - issues: 1000 source items processed, 1000 candidate sets created
  - PRs: 1000 source items processed, 1000 candidate sets created
  - rerun confirmed stale rotation behavior (`stale_marked` increments and new fresh sets are created)

What comes next
1. Add failure artifact persistence for candidates/sync/refresh/embed under `.local/artifacts`.
2. Implement Phase 5 judge (`judge` command + strict JSON contract + edge policy).

### 2026-02-13 — Entry 14 (artifact persistence hardening, pre-Phase 5)

Today we added consistent failure artifact persistence across implemented commands.

What we did
- Added shared artifact writer utility for JSON debug artifacts under `.local/artifacts`.
- Wired command-level failure artifacts in CLI for:
  - `sync`
  - `refresh`
  - `embed`
  - `candidates`
- Wired per-item/per-batch failure artifacts in services where runs continue on partial failure:
  - sync item write failures
  - refresh item fetch/update failures
  - embed batch fallback failures and item failures
  - candidates item failures
- Added artifact utility tests and re-ran quality gates.

Validation
- Verified normal candidates run still succeeds locally.
- Verified invalid candidates input (`--min-score 1.5`) produces a command-failure artifact and prints its path.

What comes next
1. Continue Phase 4 refinement if needed (sampling/inspection UX, optional dry-run semantics discussion).
2. Stop before Phase 5 until explicitly requested.

### 2026-02-13 — Entry 15 (Phase 4 refinement: candidates dry-run)

Today we added `candidates --dry-run` semantics.

What we did
- Added `--dry-run` option to `dupcanon candidates`.
- Implemented dry-run behavior in candidate retrieval service:
  - computes retrieval stats and neighbor counts
  - does not mutate DB (`candidate_sets` / `candidate_set_members`)
  - reports would-be stale rotations via fresh-set counting
- Added DB helper to count fresh candidate sets per item for accurate dry-run stale estimates.
- Added tests covering dry-run non-mutation behavior and CLI help surface.

What comes next
1. Keep Phase 4 stable and gather operator feedback on summary outputs.
2. Wait for explicit go-ahead before starting Phase 5.

### 2026-02-13 — Entry 16 (logging field normalization + Rich logger cleanup)

Today we standardized logging field naming and completed the Rich logger migration cleanup.

What we did
- Standardized naming conventions across logs/artifacts:
  - `type` for command/run-level type context
  - `item_type` for per-item failure events
  - `item_id` for item identifier in per-item logs
- Filled a gap in candidates per-item error logs by adding `item_type` consistently.
- Verified all remaining logging callsites now use the Rich logger wrapper and consistent key-value output.

Validation
- Re-ran quality gates: `ruff`, `pyright`, `pytest` all pass.
- Spot-checked failure output formatting and artifact linkage in CLI error paths.

What comes next
1. Keep Phase 4 stable (sync/embed/candidates) and continue operator usability polish if needed.
2. Hold before Phase 5 until explicitly requested.

### 2026-02-13 — Entry 17 (Phase 5 implementation: judge + edge recording)

Today we implemented Phase 5 (`judge`) end to end.

What we did
- Added `judge` command implementation and CLI options:
  - `--repo`
  - `--type issue|pr`
  - `--provider` (v1: gemini)
  - `--model`
  - `--min-edge` (default 0.85)
  - `--allow-stale`
  - `--rejudge`
- Implemented judge service pipeline:
  - reads latest candidate sets per source item (fresh by default; stale optionally)
  - builds strict duplicate-judge prompt from title/body + candidate context
  - parses strict JSON decision contract
  - validates candidate-bounded `duplicate_of` targets
- Implemented edge writing policy in DB:
  - first accepted edge wins by default
  - below-threshold decisions are recorded as `rejected`
  - `--rejudge` replaces prior accepted edges (old accepted -> rejected, new accepted inserted)
- Added DB methods for judge work retrieval and duplicate edge persistence.
- Added failure/invalid-response artifact persistence for judge under `.local/artifacts`.

Gemini integration
- Added a dedicated judge client using the official `google-genai` SDK.
- Configured JSON-mode responses (`response_mime_type=application/json`) and retry/backoff for transient/API failures.

Validation
- Added unit tests for:
  - judge service edge policy paths
  - invalid response handling
  - judge client retry/response parsing helpers
  - JudgeDecision schema validation rules
  - judge CLI help/options surface
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (63 passed)

What comes next
1. Phase 6 canonicalization (cluster + canonical selection rules).
2. Then Phase 7 plan-close guardrails.

### 2026-02-13 — Entry 18 (Phase 6 implementation: canonicalize + canonical preference)

Today we implemented Phase 6 (`canonicalize`) and introduced deterministic canonical preference rules.

What we did
- Implemented `dupcanon canonicalize --repo ... --type issue|pr`.
- Added canonicalization service:
  - reads accepted duplicate edges
  - builds connected components (undirected clustering)
  - selects one canonical per cluster deterministically
- Implemented canonical selection order as:
  1) open if any open exists
  2) English-language item if any eligible English item exists
  3) maintainer-authored if any eligible maintainer-authored item exists
  4) highest discussion activity
  5) oldest `created_at_gh`
  6) lowest item number
- Added maintainer resolution from GitHub collaborators (`admin|maintain|push`).
- Added DB methods for accepted-edge and canonical-node reads.
- Added service/CLI/GitHub client tests for canonicalization and maintainer filtering.

Validation
- Ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (68 passed)
- Ran `canonicalize` locally against `openclaw/openclaw` to verify command execution and summaries.

What comes next
1. Phase 7 `plan-close` with guardrails and approval workflow.
2. Phase 8 `apply-close` (after planning/guardrail gates are in place).

### 2026-02-13 — Entry 19 (judge hardening: response validity + concurrency safety)

Today we hardened judge behavior based on live runs against `openclaw/openclaw`.

What we did
- Reduced invalid judge responses by tightening prompt constraints:
  - explicitly includes `ALLOWED_CANDIDATE_NUMBERS`
  - numbered candidate formatting
- Softened strict reasoning handling:
  - long `reasoning` strings are truncated to 240 chars instead of rejecting the full decision.
- Added judge concurrency override:
  - `dupcanon judge ... --workers N`
  - default remains `DUPCANON_JUDGE_WORKER_CONCURRENCY`.
- Implemented concurrent judging with race-safe behavior:
  - progress bar updates only on main thread
  - per-item work runs in worker threads
  - accepted-edge uniqueness conflicts are handled as skip (`judge.edge_conflict`) rather than hard failure.
- Removed unsupported Gemini response schema payload that produced `400 INVALID_ARGUMENT` errors in live runs.

Validation
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (70 passed)
- Verified local DB writes continue during judge runs and inspected live accepted edges/canonical clusters.

What comes next
1. Continue iterative judge tuning (invalid-rate and throughput tradeoffs) while collecting more edges.
2. Start Phase 7 planning/guardrail implementation once enough judged coverage is available.

### 2026-02-13 — Entry 20 (Phase 7 implementation: plan-close + maintainer listing command)

Today we implemented the first full Phase 7 close-planning pass and added a dedicated maintainer listing command.

What we did
- Implemented `dupcanon plan-close` with spec-aligned options:
  - `--repo`
  - `--type issue|pr`
  - `--min-close` (default 0.90)
  - `--maintainers-source collaborators`
  - `--dry-run`
- Added close-planning service with guardrails:
  - clusters are formed from accepted duplicate edges
  - canonical is selected using Phase 6 rules
  - plan only targets non-canonical items
  - skip rules include:
    - `not_open`
    - `maintainer_author`
    - `maintainer_assignee`
    - `uncertain_maintainer_identity`
    - `low_confidence`
    - `missing_accepted_edge`
- Added close plan persistence in DB:
  - create `close_runs` (mode=`plan`)
  - create `close_run_items` with `action=close|skip` and `skip_reason`
- Implemented `dupcanon maintainers --repo org/name`:
  - outputs collaborator-derived maintainer logins (`admin|maintain|push`), sorted and counted.

Validation
- Added tests for:
  - plan-close guardrail behavior and persistence wiring
  - maintainers service sorting/output behavior
  - CLI help surface for `plan-close` and `maintainers`
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (76 passed)
- Ran local command verification against `openclaw/openclaw`:
  - `maintainers` command output looked correct
  - `plan-close --dry-run` produced expected close/skip mix
  - `plan-close` persisted a `close_run` and `close_run_items` in local DB.

What comes next
1. Implement approval checkpoint file generation/verification for reviewed plans.
2. Implement Phase 8 `apply-close` gating (`--approval-file` + `--yes`) before any GitHub mutation.

### 2026-02-13 — Entry 21 (candidates concurrency controls)

Today we added worker-concurrency support to `candidates` for throughput scaling.

What we did
- Added candidates runtime setting:
  - `DUPCANON_CANDIDATE_WORKER_CONCURRENCY` (default `4`)
- Added CLI override:
  - `dupcanon candidates ... --workers N`
- Implemented concurrent candidate processing with safety constraints:
  - progress bar updates only on main thread
  - worker threads process source items independently
  - per-item failures remain isolated and artifacted
- Kept dry-run semantics unchanged (no candidate table writes).

Validation
- Updated config/CLI/service tests and re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (76 passed)

What comes next
1. Keep tuning worker defaults against API/DB behavior on larger repos.
2. Continue approval checkpoint + apply-close gating implementation.

### 2026-02-13 — Entry 22 (approval checkpoint + apply-close gating and execution) [historical, superseded]

Today we completed the review gate path from `plan-close` into `apply-close`.

What we did
- Added approval checkpoint generation to `plan-close` (non-dry-run):
  - deterministic `plan_hash`
  - `close_run_id`, `repo`, `type`, `min_close`
  - `approved_by`, `approved_at` fields (initially null placeholders for human review)
  - default output under `.local/artifacts/` or explicit `--approval-file-out <path>`.
- Added deterministic plan hash utilities and checkpoint read/write helpers.
- Implemented `apply-close` service and CLI wiring:
  - requires `--close-run`, `--approval-file`, and `--yes`
  - validates checkpoint metadata against `close_runs`
  - recomputes current plan hash from persisted plan items and blocks on mismatch
  - refuses apply when approval metadata is incomplete (`approved_by` / `approved_at`)
- Added apply execution persistence:
  - creates a new `close_runs` row with `mode=apply`
  - copies planned rows into `close_run_items` for apply audit trail
  - executes GitHub close+comment operations only for `action=close`
  - stores per-item `gh_result` and `applied_at`.

Validation
- Added tests for:
  - approval hash/checkpoint roundtrip
  - apply-close gate + success path
  - plan-close checkpoint output
  - CLI help/options for `plan-close` and `apply-close`
  - GitHub close command wiring.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (83 passed)

What comes next
1. Add an explicit reviewed-approval authoring flow (e.g. helper command or template guidance) to reduce manual JSON edits.
2. Run controlled dry-run/limited apply rehearsals before broader usage.

### 2026-02-13 — Entry 23 (approve-plan helper command) [historical, superseded]

Today we added a dedicated CLI helper so operators no longer need to hand-edit approval JSON.

What we did
- Added `dupcanon approve-plan`:
  - `--approval-file <path>` (required)
  - `--approved-by <identity>` (required)
  - `--approved-at <ISO8601>` (optional, defaults to now UTC)
  - `--force` (optional overwrite of existing approval metadata)
- Implemented approval update service with validation:
  - rejects blank approver identities
  - validates timestamp format
  - refuses accidental overwrite unless `--force`
- Added CLI summary output for approved checkpoint metadata.

Validation
- Added service + CLI tests for approve flow and help surface.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (88 passed)

What comes next
1. Use `approve-plan` in operator workflow before `apply-close`.
2. Add optional helper docs/example for staged rollouts (small first apply sets).

### 2026-02-13 — Entry 24 (plan-close safety fix: require direct edge to canonical)

Today we addressed an overly permissive close-planning behavior discovered in live validation.

Problem
- `plan-close` previously allowed close actions when an item had *any* accepted outgoing edge
  above threshold, even if the selected canonical was only connected transitively.
- This could produce "close as duplicate of" pairs without direct evidence between source and canonical.

Fix
- `plan-close` now requires a **direct accepted edge** `(from_item_id -> canonical_item_id)`
  with confidence `>= min_close` before a close action is planned.
- If no direct edge exists, the item is skipped with `missing_accepted_edge`.

Validation
- Added regression test covering transitive-only chain behavior.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (89 passed)
- Re-verified the previously problematic pair now plans as skip (missing direct edge).

What comes next
1. Consider an additional apply-time safety recheck for direct-edge eligibility.
2. Continue precision-focused evaluation before larger apply batches.

### 2026-02-13 — Entry 25 (judge prompt hardening: stricter duplicate rubric)

Today we tightened the LLM judging rubric to reduce false positives.

What we changed
- Rewrote the judge system prompt with explicit conservative criteria:
  - duplicate requires at least two concrete matching facts
  - broad topical similarity is explicitly insufficient
  - vague/generic source reports default to non-duplicate
  - conflicting root-cause/subsystem details force non-duplicate
- Added a stricter confidence rubric in prompt guidance:
  - high confidence reserved for strong/near-exact evidence
  - avoid high confidence for weak or generic matches
- Kept output schema unchanged (`is_duplicate`, `duplicate_of`, `confidence`, `reasoning`) for compatibility.

Validation
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (89 passed)

What comes next
1. Evaluate acceptance precision after prompt hardening.
2. If needed, add second-pass/consensus gating for accepted edges.

### 2026-02-13 — Entry 26 (judge hardening: skip vague source reports)

Today we added a source-quality guard to reduce low-information false positives.

What we changed
- Added a pre-judge heuristic that skips LLM judging when SOURCE content is too vague.
- Vague criteria include very short/low-wording reports and generic low-signal phrasing.
- On vague-source skip:
  - no LLM call is made
  - no duplicate edge is written
  - run stats increment skip counters (`skipped_not_duplicate`).

Validation
- Added regression test ensuring vague SOURCE items are skipped without invoking the judge model.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (90 passed)

Operational note
- For local re-baselining after this hardening pass, derived tables were cleared while preserving
  `repos` and `items`:
  - cleared: `embeddings`, `candidate_sets`, `candidate_set_members`, `judge_decisions`,
    `close_runs`, `close_run_items`
  - preserved: `repos`, `items`.

### 2026-02-13 — Entry 27 (judge runtime defaults update)

Today we updated judge runtime defaults for further experimentation.

What we changed
- Changed default judge model to `gemini-3-flash-preview`.
- Changed judge generation temperature from `0` to `1`.
- Updated config and env defaults accordingly (`DUPCANON_JUDGE_MODEL`).

Validation
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (90 passed)

### 2026-02-14 — Entry 28 (OpenAI judge provider support)

Today we added optional OpenAI support for judging duplicate candidates.

What we changed
- Added `openai` as a supported `dupcanon judge --provider` value.
- Added OpenAI judge client implementation with retries/backoff.
- Added `OPENAI_API_KEY` settings support.
- Added provider-specific default model behavior:
  - `--provider openai` defaults to `gpt-5-mini` when `--model` is omitted.
- Kept Gemini as the default provider/model path.

Validation
- Added tests for:
  - OpenAI judge client behavior
  - openai provider path in judge service
  - missing `OPENAI_API_KEY` guardrails
  - settings/env loading for OpenAI key
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (96 passed)

### 2026-02-14 — Entry 29 (approval workflow removal + apply-close initialization speedups)

Today we simplified the apply path by removing approval-file requirements and reducing apply startup latency.

What we changed
- Removed file-based approval workflow end-to-end:
  - removed `approval.py` and `approve_plan_service.py`
  - removed `approve-plan` command
  - removed `plan-close --approval-file-out`
  - removed `apply-close --approval-file`
- Updated apply gate semantics:
  - `apply-close` now requires reviewed `--close-run` + `--yes`
  - still enforces `close_run.mode = plan` before mutation
- Reduced apply initialization overhead:
  - removed unnecessary maintainer fetch in `apply-close`
  - replaced row-by-row apply-run copy with bulk `INSERT ... SELECT`
  - added explicit initialization progress stage so startup work is visible
- Added DB helper `copy_close_run_items(...)` for efficient apply-run audit copying.

Validation
- Updated apply/plan/CLI tests for the new workflow.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (89 passed at this step)

### 2026-02-14 — Entry 30 (canonical preference update: prefer English canonical when available)

Today we updated canonical selection policy so representative issues are more operator-friendly for English-speaking triage flows.

What we changed
- Updated canonical selection order to:
  1) open if any open exists
  2) English-language item if any eligible English item exists
  3) maintainer-authored if any eligible maintainer-authored item exists
  4) highest discussion activity
  5) oldest created date
  6) lowest item number
- Added title/body into canonicalization planning models and DB reads so language preference has the required content context.
- Added a lightweight English heuristic in canonicalization and tracked usage via `english_preferred_clusters` in stats.
- Updated docs and tests to reflect the new canonical policy.

Validation
- Added canonicalization regression coverage for English preference behavior.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (90 passed)

### 2026-02-14 — Entry 31 (documentation realignment + operator runbook baseline)

Today we aligned docs with current code behavior and added a dedicated operator runbook.

What we changed
- Updated command documentation to match current CLI:
  - `apply-close` uses `--close-run` + `--yes`
  - no approval-file/approve-plan flow
  - `init` now documented as runtime/env validation (not DB schema probing)
- Updated architecture/operations notes:
  - canonicalize is on-the-fly (no canonical table materialization in v1)
  - migration guidance now reflects Supabase SQL workflow (`supabase/migrations`, `db reset/lint/push`)
  - close comment template documented as `Closing as duplicate of #{}...`
- Marked superseded approval-flow journal entries as historical and added a journal note clarifying that current behavior is defined by top-level sections.
- Added `README.md` for quickstart and current command surface.
- Added `docs/internal/operator_runbook_v1.md` as the baseline end-to-end operator procedure.

Validation
- Verified docs against current command/service behavior.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (90 passed)

### 2026-02-14 — Entry 32 (OpenRouter judge provider + default model update)

Today we added OpenRouter as a supported judge provider and set its default model.

What we changed
- Added `openrouter` as a supported `dupcanon judge --provider` value.
- Added OpenRouter judge client implementation using the OpenRouter Python SDK (`openrouter`).
- Added `OPENROUTER_API_KEY` settings support and init checks.
- Added provider-specific OpenRouter default model behavior:
  - `--provider openrouter` defaults to `minimax/minimax-m2.5` when `--model` is omitted.
- Updated README, `.env.example`, and operator/spec docs for OpenRouter usage.

Validation
- Added tests for:
  - OpenRouter judge client behavior
  - openrouter provider path and API-key guardrail in judge service
  - openrouter default-model behavior in CLI and judge service
  - settings/env loading for `OPENROUTER_API_KEY`
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest` (97 passed)

### 2026-02-14 — Entry 33 (judge hardening: structured overlap/uncertainty vetoes + counters)

Today we hardened judge acceptance behavior to reduce false-positive accepted edges.

What we changed
- Updated judge prompt contract to request additional structured fields:
  - `relation`, `root_cause_match`, `scope_relation`, `path_match`, `certainty`.
- Added acceptance vetoes for high-risk mismatch classes:
  - `certainty="unsure"`
  - `relation` in `related_followup|partial_overlap|different`
  - root-cause mismatch (`adjacent|different`) for duplicate claims
  - bug-vs-feature mismatches
- Removed similarity-score anchoring in prompt context (kept retrieval rank for context ordering).
- Added run-level decision counters to judge logs:
  - relation/scope/path/certainty distributions
  - final status counts (accepted/rejected/skipped)
  - veto-reason counts.

Validation
- Added/updated judge service tests for overlap vetoes and uncertainty handling.
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest`

### 2026-02-14 — Entry 34 (schema simplification: `judge_decisions` as single source of truth)

Today we simplified persistence by removing `duplicate_edges` and using `judge_decisions` for both audit and accepted-edge graph derivation.

What we changed
- Added migration to backfill legacy `duplicate_edges` into `judge_decisions`.
- Added accepted-edge uniqueness enforcement on `judge_decisions`:
  - unique `(repo_id, type, from_item_id)` where `final_status='accepted'`.
- Added `judge_decisions` consistency trigger for item repo/type correctness.
- Dropped `duplicate_edges` table and its trigger/function.
- Updated DB read paths for canonicalization and close-planning to read accepted edges from `judge_decisions`.
- Added persistence for invalid judge responses as `judge_decisions` rows with `final_status='skipped'` and `veto_reason='invalid_response:*'`.

Validation
- Ran local migration lifecycle checks:
  - `supabase db reset`
  - `supabase db lint`
- Re-ran quality gates:
  - `uv run ruff check`
  - `uv run pyright`
  - `uv run pytest`

### 2026-02-15 — Entry 35 (journal centralization + intent-card phase planning)

Today we centralized implementation journaling and documented the phased intent-card rollout plan.

What we changed
- Created a central journal file: `docs/internal/journal.md`.
- Moved all historical entries from `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md` into this file.
- Replaced the spec’s inline journal section with a pointer to the new central journal location.
- Expanded intent-card planning in `docs/internal/intent_card_pipeline_design_doc_v1.md` with:
  - enhanced card schema fields (`evidence_facts`, `fact_provenance`, `reported_claims`, `extractor_inference`, `insufficient_context`, `missing_info`),
  - explicit `card_json` vs `card_text_for_embedding` split,
  - embedding inclusion/exclusion guidance,
  - phased execution plan (Phase 0–7) with goals, deliverables, and exit criteria.
- Added documentation pointers so future journaling for intent-card implementation is recorded in this central journal.

What comes next
1. Start Phase 0 sign-off decisions (schema lock, context budgets, fallback policy, cutover gates).
2. Begin Phase 1 migrations once sign-off is complete.

### 2026-02-15 — Entry 36 (intent-card Phase 0 contract lock complete)

Today we completed Phase 0 for the intent-card sidecar initiative by locking implementation contracts in documentation.

What we changed
- Updated `docs/internal/intent_card_pipeline_design_doc_v1.md` status to Phase 0 complete and Phase 1-ready.
- Locked schema semantics by renaming confidence to `extraction_confidence` and finalizing required fields.
- Added explicit field limits and normalization/truncation rules for card generation.
- Locked deterministic embedding rendering contract (`card_text_for_embedding`) including section ordering and max length.
- Locked PR extraction context budgets and explicitly deferred autonomous repo-wide exploration.
- Locked fallback policy for extraction failures across batch and online paths.
- Finalized cutover gate criteria and rollback expectations.
- Replaced open Phase 0 decisions with a locked decision record and added a completion checklist.

Validation
- Reviewed updated design doc sections for internal consistency (schema, safety, rollout, and gates).
- Confirmed journaling location remains centralized in `docs/internal/journal.md`.

What comes next
1. Start Phase 1 implementation: migrations for `intent_cards`, `intent_embeddings`, and candidate representation provenance.
2. Add Pydantic/DB contracts for new tables with tests before any behavior-switch work.

### 2026-02-15 — Entry 37 (intent-card Phase 1 storage foundation complete)

Today we completed Phase 1 for the intent-card sidecar initiative (storage foundation only, no behavior switch).

What we changed
- Added migration: `supabase/migrations/20260216091500_add_intent_cards_phase1.sql`.
  - added `intent_cards` table
  - added `intent_embeddings` table
  - added `candidate_sets.representation` (`raw|intent`) and `representation_version`
- Extended domain models in `src/dupcanon/models.py`:
  - new intent-card schema contracts (`IntentCard`, provenance/status/source enums)
  - DB record/helper models for intent-card workflows
  - deterministic embedding render helper (`render_intent_card_text_for_embedding`)
- Extended DB layer in `src/dupcanon/database.py`:
  - `upsert_intent_card`
  - `get_latest_intent_card`
  - `list_items_for_intent_card_extraction`
  - `list_intent_cards_for_embedding`
  - `upsert_intent_embedding`
  - candidate-set insert path now persists representation provenance
- Added/updated regression tests:
  - `tests/test_models.py` for intent-card validation + rendering behavior
  - `tests/test_database.py` for new DB methods and candidate representation insert path
  - adjusted one CLI test to avoid `.env` precedence interference by using a temp working directory
- Updated intent-card design doc status to Phase 1 complete and marked Phase 2 as next.

Validation
- `supabase db reset`
- `supabase db lint`
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest` (272 passed)

What comes next
1. Start Phase 2 (shadow): implement `analyze-intent` service/command for issue + PR card extraction.
2. Persist extraction failures/status cleanly and keep raw path as default.
3. Keep all behavior switches behind explicit flags until Phase 4/5 evaluation.

### 2026-02-16 — Entry 38 (intent-card Phase 2 extraction foundation complete)

Today we completed Phase 2 for the intent-card sidecar initiative by implementing extraction in shadow mode.

What we changed
- Added `analyze-intent` CLI command (`src/dupcanon/cli.py`) with provider/model/thinking and `--only-changed` controls.
- Added extraction service `src/dupcanon/intent_card_service.py`:
  - issue + PR intent-card extraction via LLM
  - bounded PR changed-file + patch-excerpt prompt context
  - strict schema parsing into `IntentCard`
  - deterministic rendering to `card_text_for_embedding`
  - failure handling with fallback `status=failed` sidecar rows and artifact payloads
- Extended DB access usage for extraction flow (`list_items_for_intent_card_extraction`, `upsert_intent_card`).
- Added test coverage:
  - `tests/test_intent_card_service.py`
  - CLI tests for `analyze-intent` command/help/default model resolution

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest` (277 passed)

What comes next
1. Start Phase 3: intent embedding path (`embed --source intent`) using `intent_embeddings`.
2. Keep raw retrieval as default; no behavior switch before A/B retrieval in Phase 4.
3. Add representation-aware retrieval plumbing needed for raw vs intent comparison.

### 2026-02-16 — Entry 39 (intent-card Phase 3 embedding foundation complete)

Today we completed Phase 3 for the intent-card sidecar initiative by enabling intent-source embedding while preserving raw defaults.

What we changed
- Extended `embed` command with `--source raw|intent` in `src/dupcanon/cli.py`.
- Added intent embedding execution path in `src/dupcanon/embed_service.py`:
  - reads latest fresh cards from `intent_cards`
  - computes incremental skip via `embedded_card_hash` vs current rendered text hash
  - writes vectors to `intent_embeddings`
- Kept existing raw embedding path behavior-compatible and default (`--source raw`).
- Added tests:
  - `tests/test_embed_service.py` (intent-source embedding + only-changed skip behavior)
  - `tests/test_cli.py` (`embed --source intent` propagation + help surface)
- Updated docs for phase status and command behavior (`README.md`, intent-card design doc).

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest` (279 passed)

What comes next
1. Start Phase 4: retrieval A/B support (`candidates --source raw|intent`).
2. Ensure candidate-set provenance is used consistently for representation-aware comparisons.
3. Add comparison/report workflow to quantify raw vs intent retrieval deltas before judge-path changes.

### 2026-02-16 — Entry 40 (intent PR diff cap increase to 50k chars)

Today we increased the PR patch context budget used by `analyze-intent` to provide richer implementation evidence in intent extraction.

What we changed
- Updated `src/dupcanon/intent_card_service.py`:
  - `_PR_MAX_TOTAL_PATCH_CHARS` changed from `20000` to `50000`.
- Updated design doc lock values in `docs/internal/intent_card_pipeline_design_doc_v1.md`:
  - Phase-0 locked default `max total patch chars` changed to `50000`.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Continue Phase 4 retrieval A/B work (`candidates --source raw|intent`).
2. Monitor extraction latency/cost impact from larger PR patch context windows.

### 2026-02-17 — Entry 41 (fix Logfire token wiring from settings/.env)

Today we fixed a logging configuration gap where Logfire token values loaded via Pydantic settings were not being passed to `logfire.configure`, which could result in no remote logs when `LOGFIRE_TOKEN` existed only in `.env`.

What we changed
- Added `logfire_token` to `Settings` in `src/dupcanon/config.py` (`validation_alias="LOGFIRE_TOKEN"`).
- Updated logging setup in `src/dupcanon/logging_config.py`:
  - `configure_logging` now accepts `logfire_token`.
  - `_configure_logfire_once` now forwards `token` into `logfire.configure(...)`.
- Updated CLI bootstrap in `src/dupcanon/cli.py` to pass `settings.logfire_token` into `configure_logging`.
- Added init visibility in `src/dupcanon/cli.py` for optional remote logging readiness:
  - `LOGFIRE_TOKEN (optional for remote logs)` check row.
- Updated tests:
  - `tests/test_config.py` for `LOGFIRE_TOKEN` load/default behavior.
  - `tests/test_logging_config.py` for configure kwargs and explicit token forwarding.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Verify remote Logfire ingestion in a real run using `dupcanon init` + one command (`sync`/`analyze-intent`) and confirm events appear in project logs.

### 2026-02-17 — Entry 42 (intent-card structured output hardening for provenance schema errors)

Today we hardened `analyze-intent` extraction against common structured-output drift that was causing `IntentCard` validation failures on `fact_provenance`.

What we changed
- Updated extraction prompt contract in `src/dupcanon/intent_card_service.py` to explicitly require:
  - `fact_provenance.fact` must exactly match `evidence_facts` entries,
  - `fact_provenance.source` must be one of `title|body|diff|file_context`,
  - never emit file paths/labels (for example `PR_CHANGED_FILES`) as `source`.
- Added payload normalization before `IntentCard.model_validate(...)` in `src/dupcanon/intent_card_service.py`:
  - normalizes/coerces non-enum provenance sources into allowed enum values,
  - canonicalizes provenance facts to normalized evidence facts,
  - drops provenance rows that cannot be mapped to `evidence_facts`.
- Kept strict Pydantic validation as final gate; normalization is pre-validation repair only.
- Added regression test in `tests/test_intent_card_service.py` verifying malformed provenance (`source` file paths and unmapped facts) is normalized into schema-compliant output and persists as `status=fresh`.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Re-run `analyze-intent` on the previously failing PR sample and confirm conversion rate from `failed` to `fresh` improves.
2. Continue Phase 4 retrieval A/B implementation (`candidates --source raw|intent`).

### 2026-02-17 — Entry 43 (analyze-intent defaults to open items)

Today we updated `analyze-intent` to focus on active work by default, filtering source items to open issues/PRs unless explicitly overridden.

What we changed
- Added `--state` to `dupcanon analyze-intent` in `src/dupcanon/cli.py` with default `open` (`open|closed|all`).
- Threaded the selected state filter through `run_analyze_intent(...)` in `src/dupcanon/intent_card_service.py`.
- Updated DB source-item query in `src/dupcanon/database.py`:
  - `list_items_for_intent_card_extraction(...)` now accepts `state_filter` (default `open`),
  - applies `i.state = %s` when filter is not `all`.
- Updated tests:
  - `tests/test_cli.py` now checks `analyze-intent --help` includes `--state` and verifies default state passed is `open`.
  - `tests/test_intent_card_service.py` updated `run_analyze_intent(...)` calls for the new `state_filter` parameter.
  - `tests/test_database.py` verifies the intent extraction source query includes the open-state filter by default.
- Updated docs:
  - `README.md` notes `analyze-intent` default `--state open` behavior.
  - `docs/internal/intent_card_pipeline_design_doc_v1.md` command signature includes `--state`.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Run `analyze-intent` in a repo with mixed open/closed history and confirm reduced extraction volume aligns with expected open-item counts.
2. Decide whether `embed --source intent` should mirror open-only defaults or remain all-items for offline analysis.

### 2026-02-17 — Entry 44 (add `--workers` to analyze-intent)

Today we added explicit worker concurrency control for `analyze-intent` so operators can tune extraction throughput the same way as other batch commands.

What we changed
- Added CLI flag `--workers` to `dupcanon analyze-intent` in `src/dupcanon/cli.py`.
  - Passed through to service as `worker_concurrency`.
  - Included in failure artifact context and command summary table output.
- Updated `run_analyze_intent(...)` in `src/dupcanon/intent_card_service.py`:
  - accepts `worker_concurrency` override,
  - defaults to `settings.judge_worker_concurrency` when unset,
  - validates effective concurrency is `> 0`.
- Implemented concurrent extraction execution path using `ThreadPoolExecutor` + `as_completed`:
  - sequential path remains for worker count `1`,
  - multi-worker path processes items in parallel and preserves per-item failure logging/artifacts/fallback upserts.
- Added internal helper `_process_intent_source_item(...)` for single-item extraction + persistence + fallback handling.
- Updated tests:
  - `tests/test_cli.py` now checks analyze-intent help includes `--workers` and validates worker override propagation.
  - `tests/test_intent_card_service.py` updated for new required service argument.
- Updated docs:
  - `README.md` notes `analyze-intent` supports `--workers N`.
  - `docs/internal/intent_card_pipeline_design_doc_v1.md` command signature includes `--workers N`.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Benchmark `analyze-intent --workers {1,2,4,8}` on a representative repo and document latency/cost tradeoffs.
2. Consider introducing a dedicated `DUPCANON_ANALYZE_INTENT_WORKER_CONCURRENCY` setting if operators want independent defaults from `judge`.

### 2026-02-17 — Entry 45 (regression test coverage for analyze-intent state/workers changes)

Today we added targeted regression tests to cover the full behavior changed in recent `analyze-intent` updates (`--state` defaulting and `--workers` concurrency).

What we changed
- Expanded CLI regression coverage in `tests/test_cli.py`:
  - verify default `state_filter=open` still passes to service,
  - verify explicit `--state closed` override passes through,
  - verify default `worker_concurrency` remains `None` when unset,
  - keep explicit `--workers` pass-through assertion.
- Expanded service regression coverage in `tests/test_intent_card_service.py`:
  - verify `state_filter` is forwarded from service to DB source-item query call,
  - verify non-positive worker concurrency raises `ValueError`,
  - verify parallel extraction path (`worker_concurrency=2`) processes multiple items and persists all results.
- Expanded DB regression coverage in `tests/test_database.py`:
  - verify `state_filter=all` does not inject `i.state = %s` clause in query,
  - preserving existing assertion that default behavior includes open-state filtering.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Add a small stress/integration test fixture for mixed issue+PR intent extraction with `--workers > 1` to catch thread-safety regressions earlier.
2. Evaluate whether a separate analyze-intent default worker setting should be introduced (vs reusing judge worker default).

### 2026-02-17 — Entry 46 (switch OpenAI judge path from Chat Completions to Responses API)

Today we corrected the OpenAI judge/analyze-intent transport to use the Responses API, removing the Chat Completions dependency that was incompatible with non-chat model endpoints and causing 404 endpoint errors in operator runs.

What we changed
- Updated `src/dupcanon/openai_judge.py`:
  - replaced `client.chat.completions.create(...)` calls with `client.responses.create(...)`,
  - removed the temporary legacy `/v1/completions` fallback path,
  - mapped thinking controls to Responses API format (`reasoning={"effort": ...}`),
  - moved JSON-output constraint to Responses API text format (`text={"format": {"type": "json_object"}}`),
  - added Responses-input builder with explicit `system` + `user` roles,
  - updated response parsing to prefer `response.output_text` and fallback to `response.output[*].content[*].text`.
- Reworked OpenAI judge tests in `tests/test_openai_judge.py`:
  - validates request shape for Responses API,
  - validates reasoning mapping,
  - validates extraction from `output_text` and structured `output` parts,
  - validates status-code propagation on API status errors.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Add a small runtime sanity command/example in docs showing a known-good OpenAI Responses model for `analyze-intent`.
2. Consider fail-fast model capability checks for provider/model mismatches before batch processing begins.

### 2026-02-17 — Entry 47 (normalize blank PR behavior fields in intent extraction)

Today we addressed a recurring `IntentCard` validation failure where extractor outputs included empty strings for PR-required fields (`behavioral_intent`, `change_summary`).

What we changed
- Updated payload normalization in `src/dupcanon/intent_card_service.py`:
  - trims/coerces blank `behavioral_intent` / `change_summary` strings to missing values,
  - for PR cards, deterministically backfills missing values from existing extracted fields:
    - `behavioral_intent`: preferred from `desired_outcome`, else `problem_statement`, else a fixed fallback,
    - `change_summary`: preferred from `key_changed_components`, else first evidence fact, else a fixed fallback.
- Added regression test in `tests/test_intent_card_service.py`:
  - `test_run_analyze_intent_normalizes_blank_pr_behavior_fields`
  - verifies PR cards with blank behavior fields now persist as `status=fresh` with non-empty normalized values.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Re-run the previously failing `analyze-intent` batch (`openclaw/openclaw`) and confirm failure rate drops for PR items.
2. Consider logging concise per-field normalization counters to better quantify model-output drift over time.

### 2026-02-17 — Entry 48 (OpenAI structured outputs for intent extraction via Responses JSON schema)

Today we implemented true OpenAI Structured Outputs for `analyze-intent` by switching the OpenAI extraction path from generic JSON-object mode to strict JSON-schema output on the Responses API.

What we changed
- Updated `src/dupcanon/openai_judge.py`:
  - added `judge_with_json_schema(...)` helper that sends:
    - `text.format.type = "json_schema"`
    - `text.format.name = ...`
    - `text.format.schema = ...`
    - `text.format.strict = true`
  - refactored `judge(...)` to share the same Responses transport path via internal helper.
- Updated `src/dupcanon/intent_card_service.py`:
  - added `_OPENAI_INTENT_CARD_JSON_SCHEMA` covering the full intent-card payload shape,
  - when provider is `openai`, extraction now calls `judge_with_json_schema(...)` with strict mode,
  - non-openai providers continue using the existing `judge(...)` flow.
- Added/updated tests:
  - `tests/test_openai_judge.py`
    - regression coverage for structured-output request shape and JSON-schema config,
  - `tests/test_intent_card_service.py`
    - verifies `analyze-intent` openai path uses schema-based structured output method.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Re-run `analyze-intent` on `openclaw/openclaw` with OpenAI provider and compare fresh/failed rates before/after schema enforcement.
2. If needed, tighten schema constraints further (for example per-field max lengths) to better align with `IntentCard` post-validators.

### 2026-02-18 — Entry 49 (Phase 4 foundation: `candidates --source raw|intent`)

Today we implemented source-aware candidate retrieval so operators can run retrieval A/B between raw and intent representations.

What we changed
- Extended `dupcanon candidates` in `src/dupcanon/cli.py` with:
  - `--source raw|intent` (default `raw`),
  - source propagation into service call, failure artifact context, and summary output.
- Updated retrieval service in `src/dupcanon/candidates_service.py`:
  - added `source: RepresentationSource` to `run_candidates(...)`,
  - added intent representation constants (`v1`, `intent-card-v1`),
  - source-aware source-item discovery and neighbor lookup,
  - candidate-set writes now persist `representation` + `representation_version` for both modes,
  - stale-marking/count now applies per representation to avoid cross-source interference.
- Updated DB layer in `src/dupcanon/database.py`:
  - `list_candidate_source_items(...)` now supports raw vs intent source selection,
  - `find_candidate_neighbors(...)` now supports intent-neighbor retrieval via latest fresh `intent_cards` + `intent_embeddings`,
  - `count_fresh_candidate_sets_for_item(...)` / `mark_candidate_sets_stale_for_item(...)` now accept optional representation filters,
  - constrained judge and judge-audit candidate-set selection to `representation='raw'` until Phase 5 source-aware judge integration lands.
- Added/updated tests:
  - `tests/test_cli.py` for candidates `--source` help/default/propagation,
  - `tests/test_candidates_service.py` for raw behavior and intent-source propagation,
  - `tests/test_database.py` for intent-source SQL paths (`list_candidate_source_items`, `find_candidate_neighbors`).
- Updated docs:
  - `README.md` key behavior section,
  - `docs/internal/intent_card_pipeline_design_doc_v1.md` status and Phase 4 implementation notes.

Validation
- `uv run ruff check src/dupcanon/candidates_service.py src/dupcanon/database.py src/dupcanon/cli.py tests/test_candidates_service.py tests/test_cli.py tests/test_database.py`

What comes next
1. Run side-by-side retrieval windows (`candidates --source raw` vs `--source intent`) and produce comparison metrics/artifacts for Phase 4 exit criteria.
2. Implement representation-aware judging (`judge --source raw|intent`) in Phase 5.

### 2026-02-18 — Entry 50 (candidates source-state filter + intent skip-cause split)

Today we reduced operator confusion in intent retrieval runs by adding explicit source-item state filtering and more precise skip metrics for intent coverage gaps.

What we changed
- Extended `dupcanon candidates` in `src/dupcanon/cli.py`:
  - added `--source-state open|closed|all` (default `open`),
  - passed through to service as `source_state_filter`,
  - surfaced in failure artifact context and summary table output.
- Updated `run_candidates(...)` in `src/dupcanon/candidates_service.py`:
  - added `source_state_filter` input and logger fields,
  - applied source-state filtering at source-item discovery,
  - split intent skip outcomes into distinct counters:
    - `skipped_missing_fresh_intent_card`
    - `skipped_missing_intent_embedding`
  - preserved `skipped_missing_embedding` for raw-embedding skips.
- Updated candidate stats/model contracts in `src/dupcanon/models.py`:
  - `CandidateSourceItem` now carries `has_intent_card` metadata,
  - `CandidateStats` now includes split intent skip counters.
- Updated DB source-item discovery in `src/dupcanon/database.py`:
  - `list_candidate_source_items(...)` now accepts `state_filter`,
  - intent path now returns both `has_intent_card` and `has_embedding` to support skip-cause disambiguation.
- Added/updated tests:
  - `tests/test_cli.py` for `--source-state` help/default/override propagation,
  - `tests/test_candidates_service.py` for source-state forwarding and split skip counters,
  - `tests/test_database.py` for source-state SQL filtering and intent-card presence metadata.
- Updated docs:
  - `README.md` behavior notes,
  - `docs/internal/intent_card_pipeline_design_doc_v1.md` command signature update.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Consider extending similar source-state controls to future `judge --source intent` work in Phase 5.
2. Add optional operator report output that summarizes intent coverage gaps by state (`missing card` vs `missing embedding`) before large runs.

### 2026-02-18 — Entry 51 (source-aware judge/canonicalize/plan-close pipeline)

Today we extended source-selection beyond retrieval so operators can run source-consistent batch pipelines (`raw` vs `intent`) through judging, canonicalization, and close planning.

What we changed
- Extended CLI command surface in `src/dupcanon/cli.py`:
  - `judge --source raw|intent`
  - `judge-audit --source raw|intent`
  - `canonicalize --source raw|intent`
  - `plan-close --source raw|intent`
  - propagated source into service calls, failure-artifact context, and summary output.
- Updated judge execution in `src/dupcanon/judge_service.py`:
  - `run_judge(..., source=...)` now selects candidate sets by representation,
  - existing-edge checks, rejudge demotion, and decision persistence are now source-aware,
  - decision writes now persist representation source metadata.
- Updated downstream services:
  - `src/dupcanon/canonicalize_service.py`: canonicalization now filters accepted edges/nodes by source.
  - `src/dupcanon/plan_close_service.py`: planning now reads source-filtered accepted edges/items and persists plan close runs with representation provenance.
  - `src/dupcanon/apply_close_service.py`: apply runs now inherit representation from the reviewed plan run.
- Updated judge-audit source support:
  - `src/dupcanon/judge_audit_service.py` + `src/dupcanon/database.py` now support source-filtered candidate-set sampling and source provenance on audit runs.
- Updated DB contracts in `src/dupcanon/database.py`:
  - source-aware filters for judge candidate-set selection, accepted-edge reads, canonicalization node reads, and close-planning item reads,
  - source-aware accepted-edge lifecycle checks/replacement,
  - close-run and judge-audit-run create/read paths now carry representation metadata.
- Added migration:
  - `supabase/migrations/20260218102000_add_representation_source_to_judge_and_close_runs.sql`
  - adds `representation` columns/checks/indexing for `judge_decisions`, `judge_audit_runs`, and `close_runs`,
  - updates accepted-edge uniqueness to be representation-scoped.
- Added/updated tests:
  - `tests/test_judge_service.py` source propagation coverage,
  - `tests/test_judge_audit_service.py` source propagation coverage,
  - `tests/test_canonicalize_service.py` + `tests/test_plan_close_service.py` source propagation coverage,
  - `tests/test_cli.py` source help/override coverage for judge/audit/canonicalize/plan-close,
  - `tests/test_database.py` source filter + representation parsing coverage.
- Updated docs:
  - `README.md`
  - `docs/internal/intent_card_pipeline_design_doc_v1.md`
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Run side-by-side end-to-end windows (`judge` → `canonicalize` → `plan-close`) for `--source raw` and `--source intent` and compare close-plan precision proxies.
2. Add reporting helpers that summarize representation-specific accepted-edge overlap and plan-close deltas for operator review.

### 2026-02-18 — Entry 52 (intent-aware judge prompt for `judge --source intent`)

Today we added a structured intent-card prompt path for judge runs when `--source intent` is selected.

What we changed
- Updated `src/dupcanon/judge_service.py`:
  - added an intent-specific system prompt tailored to `IntentCard` fields,
  - added structured user-prompt assembly from source/candidate intent cards,
  - kept automatic fallback to the existing raw-text prompt when fresh intent cards are unavailable,
  - added prompt-mode context to invalid-response artifacts for troubleshooting.
- Updated DB access in `src/dupcanon/database.py`:
  - added `list_latest_fresh_intent_cards_for_items(...)` for batched card lookup by item ids.
- Added/updated tests:
  - `tests/test_judge_service.py` now verifies the intent prompt path is used when fresh cards are present.
- Updated docs:
  - `README.md`
  - `docs/internal/intent_card_pipeline_design_doc_v1.md`
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Extend the same intent-prompt behavior to `judge-audit --source intent` so audit runs evaluate the same prompt path as production judge runs.
2. Add explicit prompt-version provenance for judge decisions (raw vs intent prompt versions) for replay comparisons.

### 2026-02-18 — Entry 53 (judge-audit parity: intent prompt path)

Today we aligned `judge-audit --source intent` with the same intent-card prompt path used by `judge --source intent`.

What we changed
- Updated `src/dupcanon/judge_audit_service.py`:
  - audit item processing now attempts intent-card prompt construction when `source=intent`,
  - cheap and strong lanes now consume the same prompt payload per work item,
  - falls back to the existing raw prompt path when intent cards are unavailable,
  - preserves existing decision parsing and veto/guardrail logic.
- Reused judge prompt helpers from `src/dupcanon/judge_service.py` to keep prompt shape and behavior consistent.
- Added/updated tests:
  - `tests/test_judge_audit_service.py` now verifies structured intent prompts are used for both cheap/strong lanes when cards are available.

Docs updated
- `README.md`
- `docs/internal/intent_card_pipeline_design_doc_v1.md`
- `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Persist explicit judge prompt provenance fields (prompt family/version) on `judge_decisions` and optionally `judge_audit_run_items` for replay-grade analysis.
2. Add report-level breakdowns comparing raw vs intent prompt-mode outcomes for sampled audits.

### 2026-02-18 — Entry 54 (doc/code alignment pass + raw-fallback regressions)

Today we ran a documentation alignment pass against the live codebase after intent-prompt rollout, and added regressions to lock fallback behavior.

What we changed
- Documentation alignment updates:
  - `README.md`: clarified that judge runtime primitives remain in `judge_runtime.py`, while source-aware prompt orchestration now lives in `judge_service.py` / `judge_audit_service.py`.
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`: updated architecture snapshot text to match the current split.
  - `docs/internal/intent_card_pipeline_design_doc_v1.md`: documented regression-covered raw fallback when fresh intent cards are missing.
- Regression tests added:
  - `tests/test_judge_service.py`
    - verifies `judge --source intent` falls back to raw prompt when intent cards are unavailable.
  - `tests/test_judge_audit_service.py`
    - verifies `judge-audit --source intent` falls back to raw prompt for both cheap/strong lanes when intent cards are unavailable.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Persist prompt provenance (`prompt_mode`, prompt version) to DB rows, not only failure artifacts/logs.
2. Extend report tooling to summarize prompt-mode usage/fallback rates in judge and audit runs.

### 2026-02-18 — Entry 55 (plan-close target policy flag: canonical-only default + direct-fallback option)

Today we added an explicit close-target policy control to `plan-close` to recover transitive-only duplicate cases without changing the default precision-first behavior.

What we changed
- Added new enum `PlanCloseTargetPolicy` (`canonical-only`, `direct-fallback`) in `src/dupcanon/models.py`.
- Extended `dupcanon plan-close` CLI with `--target-policy`:
  - default: `canonical-only`
  - optional: `direct-fallback`
- Threaded target-policy handling through `src/dupcanon/cli.py` and `src/dupcanon/plan_close_service.py`:
  - default behavior unchanged (`canonical-only` still requires direct source->canonical accepted edge)
  - under `direct-fallback`, when source->canonical is missing, close planning can use the source item’s direct accepted target edge if confidence meets `--min-close`
  - persisted close-plan rows now store the selected close target (canonical or fallback target)
  - added `close_actions_direct_fallback` summary metric for operator visibility.
- Test coverage updates:
  - `tests/test_plan_close_service.py`: new regression validating transitive-chain recovery with `direct-fallback`.
  - `tests/test_cli.py`: verifies `--target-policy` override propagation and help surface.
- Documentation updates:
  - `README.md`
  - `docs/evaluation.mdx`
  - `docs/architecture.mdx`
  - `docs/index.mdx`
  - `docs/internal/operator_runbook_v1.md`
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`
  - `docs/internal/intent_card_pipeline_design_doc_v1.md`

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest` (315 passed)

What comes next
1. Add operator reporting to break down close-plan actions by target mode (`canonical` vs `direct-fallback`) and confidence buckets.
2. Evaluate whether `direct-fallback` should enforce additional target-state constraints (for example, open-only fallback target) in future hardening.

### 2026-02-18 — Entry 56 (online intent source path: `detect-new --source raw|intent`)

Today we implemented the planned online source-selection extension so `detect-new` can run with intent-card retrieval (`--source intent`) while preserving safe fallback behavior.

What we changed
- Extended CLI in `src/dupcanon/cli.py`:
  - added `detect-new --source raw|intent` (default `raw`),
  - threaded source into `run_detect_new(...)` call and failure-artifact context.
- Updated online service in `src/dupcanon/detect_new_service.py`:
  - added `source: RepresentationSource` to `run_detect_new(...)` with default raw,
  - added online intent path for source item:
    - reuses latest fresh intent card when source hash matches,
    - otherwise attempts extraction and persists a fresh intent card,
    - writes a failed fallback intent card on extraction failure,
    - embeds source intent card when hash changed,
  - candidate retrieval now runs against the selected source representation (`raw` or `intent`) using existing DB representation-aware neighbor lookup,
  - when `source=intent`, detect-new attempts the structured intent-card judge prompt path and falls back to raw prompt with explicit fallback metadata when intent prompt prerequisites are missing,
  - result payload now includes source provenance fields:
    - `requested_source`,
    - `effective_source`,
    - `source_fallback_reason`.
- Added DB helper in `src/dupcanon/database.py`:
  - `get_intent_embedding_hash(intent_card_id, model)` for cheap source intent-embedding freshness checks.
- Added/updated tests:
  - `tests/test_cli.py`:
    - detect-new help includes `--source`,
    - detect-new source default and override propagation.
  - `tests/test_detect_new_service.py`:
    - intent source path uses intent neighbor retrieval + structured intent prompt,
    - intent extraction failure falls back to raw retrieval and reports fallback metadata.
- Updated docs:
  - `README.md` detect-new behavior notes,
  - `docs/internal/intent_card_pipeline_design_doc_v1.md` (`detect-new --source` status),
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md` command signature.

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest` (319 passed)

What comes next
1. Add reporting helpers that summarize online fallback rates (`requested_source=intent` but `effective_source=raw`) by reason.
2. Run side-by-side online shadow samples (`detect-new --source raw` vs `--source intent`) and compare precision-oriented outcomes before any default-source decision.

### 2026-02-18 — Entry 57 (intent defaults across batch + online commands)

Today we promoted intent to the default representation for all source-aware commands (batch + online), while keeping `--source raw` available for rollback/A-B.

What we changed
- Updated CLI defaults in `src/dupcanon/cli.py`:
  - `embed`, `candidates`, `judge`, `judge-audit`, `canonicalize`, `plan-close`, `detect-new` now default to `--source intent`.
- Updated service defaults:
  - `run_embed`, `run_candidates`, `run_judge`, `run_judge_audit`, `run_canonicalize`, `run_plan_close`, and `run_detect_new` now default to `RepresentationSource.INTENT`.
  - model/record defaults (`DetectNewResult`, `JudgeAuditRunReport`, `CloseRunRecord`) now default to intent representations.
- Updated tests to keep raw-path coverage explicit where needed and to assert new CLI defaults.
- Documentation updates for intent-as-default:
  - `README.md` behavior notes + examples
  - `docs/architecture.mdx`, `docs/evaluation.mdx`
  - `docs/internal/intent_card_pipeline_design_doc_v1.md`
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`
  - `docs/internal/operator_runbook_v1.md`

Validation
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

What comes next
1. Add online fallback reporting to quantify intent fallback rates by reason.
2. Schedule an explicit cutover review once A/B metrics are collected (keep `--source raw` rollback ready).

### 2026-02-18 — Entry 58 (semantic query CLI spec + architecture design)

Today we drafted a full implementation-oriented design doc for a new CLI semantic search surface that is aligned with the current intent-first codebase.

What we changed
- Added new design/spec doc:
  - `docs/internal/semantic_search_cli_design_doc_v1.md`
- Updated the main CLI design spec cross-reference list:
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`
    now links the semantic query doc.

Doc highlights
- New command proposal: `dupcanon search`.
- Intent-first retrieval default with explicit `--source raw` rollback.
- Read-only safety model (no mutation paths from query mode).
- Optional chat-style grounded answer mode (`--answer`) with required citations.
- Proposed typed contracts (`SearchHit`, `SearchAnswer`, `SearchResult`) and CLI/service/database changes.
- Production hardening plan: observability fields, evaluation metrics (Recall@K/MRR/nDCG), and phased rollout.

Validation
- Documentation-only changes; no runtime behavior changed.

What comes next
1. Resolve open product defaults (`--state`, `--answer`, persistence toggle, interactive mode scope).
2. Convert doc decisions into implementation tasks: migration, database methods, `search_service`, CLI wiring, tests.

### 2026-02-18 — Entry 59 (semantic query defaults locked)

We finalized product defaults for `dupcanon search` v1 and updated the semantic query design doc accordingly.

Locked defaults
- `--state open` by default.
- `--answer` is opt-in only; default output is ranked issue/PR results.
- `--json` is optional for machine-readable output; default remains human-readable table output.
- Search run persistence in DB is out of scope for v1.
- Interaction model is one-shot only for v1 (no interactive chat loop).

Doc updates
- Updated `docs/internal/semantic_search_cli_design_doc_v1.md` to reflect the locked defaults:
  - product decisions,
  - output flag semantics,
  - no-persistence decision,
  - resolved defaults section,
  - implementation/test notes.

Validation
- Documentation-only updates; no runtime behavior changed.

What comes next
1. Break implementation into concrete tasks (`models`, `database retrieval methods`, `search_service`, `cli`, `tests`).
2. Implement retrieval-only `search` first, then optional grounded `--answer` mode.

### 2026-02-18 — Entry 60 (`search` retrieval implementation, CLI-first v1)

Implemented retrieval-only semantic query support as `dupcanon search`, aligned with locked v1 defaults.

What we changed
- Added new service: `src/dupcanon/search_service.py`
  - one-shot read-only search orchestration (`run_search(...)`),
  - query embedding via configured embedding provider/model,
  - intent-first retrieval with explicit raw fallback when intent corpus is unavailable,
  - typed `SearchResult` output with `requested_source`, `effective_source`, and fallback reason metadata.
- Added data contracts in `src/dupcanon/models.py`:
  - `SearchMatch`, `SearchHit`, `SearchAnswer`, `SearchResult`.
- Extended database retrieval APIs in `src/dupcanon/database.py`:
  - `count_searchable_items(...)`,
  - `search_similar_items_raw(...)`,
  - `search_similar_items_intent(...)`.
- Added CLI command in `src/dupcanon/cli.py`:
  - `dupcanon search --repo ... --query ...`
  - defaults: `--type all`, `--state open`, `--source intent`
  - output modes: human-readable table (default) or `--json` to stdout
  - optional `--show-body-snippet`.
- Tests added/updated:
  - `tests/test_search_service.py`
  - `tests/test_database.py` (new search/count query coverage)
  - `tests/test_models.py` (search result validation)
  - `tests/test_cli.py` (search command help/defaults/option propagation)
- Docs updated:
  - `README.md` command surface + behavior + examples
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md` command interface section

Validation
- `uv run ruff check src tests`
- `uv run pyright`
- `uv run pytest` (330 passed)

What comes next
1. Optional v1.1: add grounded `--answer` mode with strict citation validation.
2. Build a small labeled query relevance set for Recall@K/MRR/nDCG baseline tracking.

### 2026-02-18 — Entry 61 (`search` default min-score tuned to 0.30)

Adjusted the default `dupcanon search` retrieval threshold from `0.60` to `0.30` after observing empty result sets for short natural queries (for example, `"cron"`) under intent retrieval.

What we changed
- Code:
  - `src/dupcanon/cli.py`
    - `SEARCH_MIN_SCORE_OPTION` default changed `0.60 -> 0.30`.
- Tests:
  - `tests/test_cli.py`
    - updated search-default assertion to expect `min_score=0.3`.
- Docs:
  - `docs/internal/semantic_search_cli_design_doc_v1.md`
  - `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`

Rationale
- `0.60` worked as a precision-oriented threshold for duplicate decisioning contexts, but is too strict for broad semantic search discovery.
- `0.30` yields useful ranked retrieval for short query intents while preserving caller control via explicit `--min-score` overrides.

Validation
- `uv run ruff check src tests`
- `uv run pyright`
- `uv run pytest`

### 2026-02-18 — Entry 62 (`search` adds `--similar-to`, `--include`, `--exclude`)

Extended the retrieval-only semantic search command so agents/operators can express anchor-based and constrained search requests without introducing a fixed topic taxonomy.

What we changed
- CLI (`src/dupcanon/cli.py`)
  - `search` now accepts:
    - `--query <text>` (optional),
    - `--similar-to <number>` (optional),
    - repeatable `--include <term>`,
    - repeatable `--exclude <term>`.
  - Validation contract: exactly one base signal (`--query` xor `--similar-to`).
- Search service (`src/dupcanon/search_service.py`)
  - supports anchor-derived search text from `--similar-to` item,
  - intent-anchor fallback to raw when fresh anchor intent card is unavailable,
  - applies semantic include/exclude filtering over retrieved candidates,
  - excludes anchor item itself from anchor-mode results.
- Database layer (`src/dupcanon/database.py`)
  - added `get_search_anchor_item(...)`,
  - added per-item scoring helpers used by include/exclude filtering:
    - `score_search_items_raw(...)`,
    - `score_search_items_intent(...)`.
- Models (`src/dupcanon/models.py`)
  - added `SearchAnchorItem` and enriched `SearchResult` fields:
    - `similar_to_number`, `include_terms`, `exclude_terms`.
- Tests
  - updated `tests/test_cli.py`, `tests/test_search_service.py`, `tests/test_database.py`, `tests/test_models.py` for new flags/behavior.
- Docs
  - updated `README.md`,
  - updated `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`,
  - updated `docs/internal/semantic_search_cli_design_doc_v1.md`.

Validation
- `uv run ruff check src tests`
- `uv run pyright`
- `uv run pytest` (336 passed)

### 2026-02-18 — Entry 63 (`search` adds include/exclude threshold flags)

Added explicit tuning controls for semantic constraint strictness in the new `search` command.

What we changed
- CLI (`src/dupcanon/cli.py`)
  - added `--include-threshold` (default `0.25`),
  - added `--exclude-threshold` (default `0.30`),
  - wired both flags into `run_search(...)` and failure artifact context.
- Search service (`src/dupcanon/search_service.py`)
  - thresholds are validated (`0..1`),
  - include/exclude filtering now uses run-provided thresholds instead of fixed constants,
  - thresholds are logged in start/complete events.
- Models (`src/dupcanon/models.py`)
  - `SearchResult` now persists `include_threshold` and `exclude_threshold` for JSON/audit transparency.
- Tests
  - updated CLI tests for help/default/override propagation,
  - updated service tests for threshold validation,
  - updated model tests for threshold fields + validation.
- Docs
  - updated `README.md`,
  - updated `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`,
  - updated `docs/internal/semantic_search_cli_design_doc_v1.md`.

Validation
- `uv run ruff check src tests`
- `uv run pyright`
- `uv run pytest` (336 passed)

### 2026-02-18 — Entry 64 (`search` adds `--include-mode` with default boost)

Implemented configurable include semantics for search constraints, with **boost mode as default**.

What we changed
- CLI (`src/dupcanon/cli.py`)
  - added `--include-mode boost|filter` (default `boost`),
  - added `--include-weight` (default `0.15`),
  - wired both into `run_search(...)`, artifacts, and operator output.
- Models (`src/dupcanon/models.py`)
  - added `SearchIncludeMode` enum,
  - `SearchResult` now includes:
    - `include_mode`,
    - `include_weight`.
- Search service (`src/dupcanon/search_service.py`)
  - include constraints now support two modes:
    - `filter`: hard include gate (`score >= include_threshold`),
    - `boost`: soft rerank by `base_score + include_weight * include_score` when include score passes threshold.
  - excludes remain hard filters.
- Tests
  - updated CLI/help/default/override coverage,
  - added boost-mode rerank coverage in search service tests,
  - updated model validation coverage for include mode/weight fields.
- Docs
  - updated `README.md`,
  - updated `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`,
  - updated `docs/internal/semantic_search_cli_design_doc_v1.md`.

Validation
- `uv run ruff check src tests`
- `uv run pyright`
- `uv run pytest` (338 passed)

### 2026-02-18 — Entry 65 (`search` default thresholds lowered + debug constraints)

Adjusted default semantic constraint thresholds and added explicit per-hit constraint diagnostics for operator tuning.

What we changed
- Defaults adjusted:
  - `--include-threshold` default `0.25 -> 0.20`
  - `--exclude-threshold` default `0.30 -> 0.20`
  - reflected in CLI option defaults, service defaults, and `SearchResult` defaults.
- Added CLI diagnostics switch:
  - `--debug-constraints/--no-debug-constraints` (default off).
- Search service (`src/dupcanon/search_service.py`)
  - computes per-hit include/exclude term scores when debug mode is enabled,
  - emits `constraint_debug` payload on each hit (JSON),
  - table output shows `IncMax` / `ExcMax` columns when debug mode is active and constraints are present.
- Models (`src/dupcanon/models.py`)
  - added `SearchConstraintDebug`,
  - `SearchHit` now has optional `constraint_debug`.
- Docs
  - updated `README.md`,
  - updated `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`,
  - updated `docs/internal/semantic_search_cli_design_doc_v1.md`.

Validation
- `uv run ruff check src tests`
- `uv run pyright`
- `uv run pytest` (338 passed)

### 2026-02-19 — Entry 66 (LLM client refactors)

Refactored LLM/embedding clients to reduce duplication and improve consistency.

What we did
- Added `retry_with_backoff` helper in `src/dupcanon/llm_retry.py` and migrated OpenAI/Gemini/OpenRouter judge + embeddings + Codex RPC judge to use it.
- Centralized reasoning-effort validation in `src/dupcanon/thinking.py` (`normalize_reasoning_effort`) and reused it in OpenAI/OpenRouter judges; normalized Codex thinking validation via `normalize_thinking_level`.
- Added `src/dupcanon/llm_text.py` and shared response text extraction between OpenAI and OpenRouter judges.
- Simplified judge runtime client caching with a `JudgeClientCache` dataclass and `_build_judge_client` helper.

Validation
- Not run (not requested).

What comes next
- Run `uv run ruff check src tests`, `uv run pyright`, `uv run pytest`.

### 2026-02-19 — Entry 67 (sync fetch counts on retries)

Fixed inflated fetch counts in `sync` when GitHub pagination retries.

What we did
- Roll back streamed batch counts in `GitHubClient` when a paginated `gh api` attempt fails and is retried.
- Updated sync fetch progress to use absolute totals and ignore negative batch deltas.
- Reconciled final issue/PR counts with the lengths returned by GitHub before logging/completing the fetch stage.

Validation
- Not run (not requested).

### 2026-02-19 — Entry 68 (Mintlify docs refresh)

Aligned the public docs in `docs/` with the latest intent-first and search updates.

What we did
- Added a new Mintlify page for semantic search (`docs/search.mdx`) and wired it into navigation.
- Updated the overview, get-started, architecture, and evaluation docs to include `analyze-intent`, intent embeddings, and the read-only `search` command.
- Refreshed diagrams and sequences to show intent cards/embeddings and search flows.

Validation
- Not run (docs-only changes).

### 2026-02-21 — Entry 69 (LLM retry typing + regression tests)

What we did
- Updated `retry_with_backoff` to use Python 3.12 type-parameter syntax (ruff UP047 fix).
- Added regression coverage for paginated GitHub retry rollbacks in `tests/test_github_client.py`.
- Added `tests/test_llm_text.py` for shared LLM response text extraction.

Validation
- `uv run ruff check src tests`
- `uv run pyright`
- `uv run pytest` (343 passed)

### 2026-02-22 — Entry 70 (LLM CLI reference + docs)

What we did
- Added `dupcanon llm` JSON output with compact defaults plus `--full` for verbose metadata.
- Added a built-in how-to section with workflows, guardrails, and example command sequences.
- Updated Mintlify docs (`docs/index.mdx`, `docs/get-started.mdx`, `docs/architecture.mdx`) to document `llm`.
- Updated `AGENTS.md` to keep the LLM reference output in sync with CLI changes.

Validation
- `uv run dupcanon llm`
