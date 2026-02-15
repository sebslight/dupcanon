# Duplicate Canonicalization CLI

Status: Draft design doc for internal discussion

Implementation status snapshot (2026-02-14)
- Implemented commands: `init`, `sync`, `refresh`, `embed`, `candidates`, `judge`, `judge-audit`, `report-audit`, `detect-new`, `canonicalize`, `maintainers`, `plan-close`, `apply-close`.
- Current apply gate: reviewed persisted `close_run` + explicit `--yes` (no approval-file workflow).
- Current canonical preference: open-first, then English-language preference, then maintainer preference.
- Judge runtime path is centralized in `src/dupcanon/judge_runtime.py` and reused by judge/audit/online detect.
- Shared retry/backoff/attempt validation primitives are centralized in `src/dupcanon/llm_retry.py`.
- Known remaining gaps: no first-class Phase 9 evaluation command yet.

This doc proposes a human-operated CLI that detects duplicate GitHub issues and PRs, canonicalizes duplicates into a single “canonical” item, and optionally closes duplicates. Storage is Supabase (Postgres + pgvector). The key design choice is graph-based canonicalization so we never create “closed in favor of” chains.


## Why we are doing this

Today, teams often close duplicates by pointing to whatever the latest “best match” was. Over time this creates chains (A closed as dup of B, B closed as dup of C), which is annoying for users and makes it hard to understand the real canonical thread.

We want a workflow that always converges on one canonical item per cluster, and always closes to that canonical.


## Goals

- Provide a CLI a human can run locally to:
  - sync a repo’s issues/PRs into a DB
  - embed items and store vectors in pgvector
  - retrieve candidate duplicates using vector search
  - have an LLM judge duplicates from that candidate set
  - canonicalize clusters and propose a close plan
  - apply closes via GitHub API with strong guardrails

- Prevent duplicate chains by always closing to a canonical item.

- Make it incremental:
  - do not re-embed or re-judge items unnecessarily
  - support `refresh --refresh-known` to update state for previously-seen items


## Non-goals (v1)

- A fully automated GitHub Action that runs on every new issue/PR.
- Perfect clustering or perfect canonical selection.
- Backfilling every historical closed issue in a large repo (we can add later).


## Scope constraints (v1)

- Operates on one repository per run.
- No multi-repo orchestration in v1.


## Terminology

Item
- A GitHub issue or PR.
- Identity is (repo, type, number). Type is required so we never compare issues against PRs.

Candidate set
- The output of the retrieval step for one item: the top K most similar items (same type) from pgvector, with similarity scores.
- Why it exists: reproducibility and cost control. You can change LLM prompts and re-run “judge” without redoing vector search.

Duplicate edge
- An accepted directed relationship “A is a duplicate of B”.
- Why it exists: pairwise decisions become a graph, which lets us build clusters and compute a stable canonical. That is how we kill chains.

Canonical
- The single representative item for a duplicate cluster. In v1 it should always be open if any open exists in the cluster.


## High-level workflow

1) sync
- Pull issues/PRs from GitHub into Postgres.
- Store enough metadata to do safety checks and canonical ranking later (author, assignees, state, timestamps, comment counts).

2) embed
- For items whose content changed or have no embedding, compute embeddings and store them in pgvector.

3) candidates
- For each target item, retrieve top K similar items from pgvector restricted to the same type.
- Persist the candidate set.

4) judge
- For each candidate set, ask an LLM to decide if the item is a duplicate of exactly one candidate.
- If duplicate, record an accepted duplicate edge (subject to an upsert policy).

5) canonicalize
- Build clusters from accepted edges.
- Compute canonical per cluster.

6) plan-close / apply-close
- Build a plan that maps each duplicate item to its canonical.
- Apply closes with guardrails.


## Proposed CLI interface

Single CLI, subcommands. Example name: dupcanon.

- dupcanon init
  - Validates local runtime configuration and required environment variables.
  - Prints run id, artifacts directory, and DSN guidance.

- dupcanon sync --repo org/name [--type issue|pr|all] [--state open|closed|all] [--since 30d|YYYY-MM-DD] [--dry-run]
  - Fetches items and upserts into items table.
  - Updates content_hash/content_version.
  - With --dry-run: computes and prints sync stats without DB writes.

- dupcanon maintainers --repo org/name
  - Lists collaborator-derived maintainer logins (`admin|maintain|push`).

- dupcanon refresh --repo org/name [--refresh-known] [--dry-run]
  - Refreshes incrementally from GitHub.
  - Default: discover new items only (does not update already-known item metadata).
  - With --refresh-known: also refreshes metadata for already-known items.
  - With --dry-run: computes and prints refresh stats without DB writes.

- dupcanon embed --repo org/name [--type issue|pr] [--only-changed] [--provider gemini|openai] [--model ...]
  - Embeds items missing embeddings or with content_version advanced.
  - Embedding provider/model can be overridden per run.

- dupcanon candidates --repo org/name --type issue|pr [--k 4] [--min-score 0.75] [--include open|closed|all] [--dry-run] [--workers N]
  - Creates candidate_sets and candidate_set_members.
  - With --dry-run: computes candidate stats without DB writes.
  - v1 default clustering retrieval is k=4 with `--include open` (configurable).

- dupcanon judge --repo org/name --type issue|pr [--provider gemini|openai|openrouter|openai-codex] [--model ...] [--thinking off|minimal|low|medium|high|xhigh] [--min-edge 0.85] [--allow-stale] [--rejudge] [--workers N]
  - Reads fresh candidate sets, calls LLM, writes judge_decisions.
  - Default configured provider/model is OpenAI Codex via `pi` RPC (`openai-codex`, `gpt-5.1-codex-mini`). Gemini/OpenAI/OpenRouter are available as overrides.
  - Model resolution:
    - `--model` overrides everything.
    - If selected provider matches configured provider, use configured model.
    - Otherwise use provider defaults (`gemini-3-flash-preview`, `gpt-5-mini`, `minimax/minimax-m2.5`, `gpt-5.1-codex-mini`).
  - Thinking defaults can be set globally via `DUPCANON_JUDGE_THINKING`.

- dupcanon judge-audit --repo org/name --type issue|pr [--sample-size 100] [--seed 42] [--min-edge 0.85] [--cheap-provider ...] [--cheap-model ...] [--cheap-thinking ...] [--strong-provider ...] [--strong-model ...] [--strong-thinking ...] [--workers N] [--verbose] [--debug-rpc] [--show-disagreements/--no-show-disagreements] [--disagreements-limit N]
  - Samples latest fresh candidate sets (open source items only) that have at least one candidate member, runs cheap and strong judges on the same sample, and records audit outcomes into `judge_audit_runs` + `judge_audit_run_items`.
  - Cheap/strong model resolution is independent per lane (`--*-model` override, otherwise lane provider-match with lane env defaults, otherwise provider defaults).
  - Produces immediate confusion-matrix style counts (`tp`, `fp`, `fn`, `tn`) plus `conflict` (both accepted, different target).
  - By default prints a disagreement table (`fp`, `fn`, `conflict`, `incomplete`) with source + cheap/strong decision details; limit defaults to 20 rows.
  - `--debug-rpc` prints raw `pi --mode rpc` stdout/stderr event lines for `openai-codex` troubleshooting.
  - Judge-audit defaults can be set via env:
    - `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER`, `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`, `DUPCANON_JUDGE_AUDIT_CHEAP_THINKING`
    - `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER`, `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`, `DUPCANON_JUDGE_AUDIT_STRONG_THINKING`

- dupcanon report-audit --run-id N [--show-disagreements/--no-show-disagreements] [--disagreements-limit N] [--simulate-gates --gate-rank-max N --gate-score-min X --gate-gap-min X] [--simulate-sweep gap --sweep-from A --sweep-to B --sweep-step C]
  - Prints a persisted judge-audit run summary from `judge_audit_runs` without re-running model calls.
  - Optionally prints disagreement rows (`fp`, `fn`, `conflict`, `incomplete`) from `judge_audit_run_items`.
  - Optional non-LLM simulation modes let operators estimate precision/recall tradeoffs from stored rows:
    - single scenario (`--simulate-gates` + gate values)
    - sweep mode (`--simulate-sweep gap ...`) for threshold tuning.

- dupcanon detect-new --repo org/name --type issue|pr --number N [--provider ...] [--model ...] [--thinking off|minimal|low|medium|high|xhigh] [--k 8] [--min-score 0.75] [--maybe-threshold 0.85] [--duplicate-threshold 0.92] [--json-out path]
  - Runs one-item online duplicate detection using the same provider/model/thinking controls as judge.
  - Model resolution matches judge behavior (`--model` override, otherwise configured-provider match, otherwise provider defaults).

- dupcanon canonicalize --repo org/name --type issue|pr
  - Computes canonical statistics on the fly from accepted edges (no canonical table materialization in v1).

- dupcanon plan-close --repo org/name --type issue|pr [--min-close 0.90] [--maintainers-source collaborators] [--dry-run]
  - Produces a close_run and close_run_items for explicit human review.

- dupcanon apply-close --close-run <id> [--yes]
  - Executes GitHub close operations for a reviewed plan run.


## Locked model/provider choices (v1)

- Embeddings: provider-configurable (`gemini` or `openai`) with output dimensionality locked to 3072.
  - default model/provider is OpenAI `text-embedding-3-large`.
- Duplicate judge default: OpenAI Codex via `pi` RPC with model `gpt-5.1-codex-mini`.
- Optional evaluation overrides: OpenAI `gpt-5-mini` or OpenRouter `minimax/minimax-m2.5`.
- Credentials: `.env` or environment variables for local/operator runs.


## Architecture

Everything is DB-first. Supabase hosts Postgres and pgvector.
- Postgres tables hold item metadata, decisions, candidate sets, and close audit trails.
- pgvector holds embeddings and supports similarity search.

The CLI is a thin orchestrator:
- GitHub fetch via GitHub API (gh CLI, REST, or GraphQL) depending on implementation choice.
- Embeddings via configured provider/model (`gemini` or `openai`, 3072 dimensions).
- LLM judging default via OpenAI Codex `pi` RPC (`openai-codex`, model `gpt-5.1-codex-mini`), with optional Gemini/OpenAI/OpenRouter overrides for evaluation.
- Python CLI stack: Typer for command surface + Rich for terminal UX/progress rendering.
- Data/config modeling and validation: Pydantic.
- Logging stack: stdlib logging with Rich console handler + Logfire sink.
  - Console remains Rich-formatted (`rich.logging.RichHandler`).
  - Remote sink uses `logfire.LogfireLoggingHandler`.
  - Logfire is configured with `send_to_logfire="if-token-present"` for safe token-optional local runs.
- Progress bars: Rich progress only (do not use tqdm in v1).


## Data model (Postgres / Supabase)

This section is the authoritative schema target for v1. All tables are in Supabase Postgres.

### repos
- id (bigint pk)
- github_repo_id (bigint unique not null)
- org (text not null)
- name (text not null)
- created_at, updated_at (timestamptz)

Constraints
- unique (github_repo_id)
- unique (org, name)

### items
Represents both issues and PRs.

- id (bigint pk)
- repo_id (fk repos.id)
- type (text not null)  -- 'issue' | 'pr'
- number (int not null)
- url (text not null)
- title (text not null)
- body (text null)
- state (text not null) -- 'open' | 'closed'
- author_login (text null)
- assignees (jsonb null)
- labels (jsonb null)
- comment_count (int not null default 0)          -- issue comments
- review_comment_count (int not null default 0)   -- PR review comments (0 for issues)
- created_at_gh (timestamptz null)
- updated_at_gh (timestamptz null)
- closed_at_gh (timestamptz null)
- content_hash (text not null)
- content_version (int not null default 1)
- last_synced_at (timestamptz not null)
- created_at, updated_at (timestamptz)

Unique identity
- unique (repo_id, type, number)

Why not “just number” as unique id
- GitHub numbers are only unique within a repo and across issue/PR namespaces you can collide. So we must include repo_id and type.

### embeddings
Stores the latest embedding per (item, model).

Important note on pgvector dimensions
- pgvector requires a fixed dimension in the column definition.
- v1 locks embedding output dimensionality to 3072 (provider/model may vary).
- If we switch to a model/dimension outside 3072, we do a migration or add a new embeddings table/column.

- id (bigint pk)
- item_id (fk items.id)
- model (text not null)  -- embedding model used for this row (e.g. `gemini-embedding-001`, `text-embedding-3-large`)
- dim (int not null)     -- 3072 in v1
- embedding (vector(3072) not null)
- embedded_content_hash (text not null)
- created_at (timestamptz)

Constraints
- unique (item_id, model)

Indexes
- note: with 3072-dimensional vectors on current pgvector host limits (>2000 dims), ANN indexes (`ivfflat`/`hnsw`) are unavailable; retrieval currently uses exact distance scan.

### candidate_sets

What is a candidate set?
- It is the retrieval snapshot for one item: the exact list of candidates we showed to the LLM, plus retrieval parameters and the item’s content_version at the time.

Why do we want it?
- Reproducible judging: if we tweak the prompt, we can re-judge the same retrieval set.
- Cheaper iteration: candidate retrieval is fast but still worth caching, and it decouples retrieval tuning from LLM prompting.
- Debuggable: we can inspect “what did we consider” per decision.

- id (bigint pk)
- repo_id (fk repos.id)
- item_id (fk items.id)
- type (text not null)
- embedding_model (text not null)
- k (int not null)
- min_score (real not null)
- include_states (text[] not null)
- created_at (timestamptz)
- status (text not null) -- 'fresh' | 'stale'
- item_content_version (int not null)

Staleness policy
- On sync, if items.content_version increments, mark candidate_sets for that item as stale.
- Judge refuses stale sets unless --allow-stale is provided.

### candidate_set_members
- candidate_set_id (fk candidate_sets.id)
- candidate_item_id (fk items.id)
- score (real not null)
- rank (int not null)
- created_at

Constraints
- unique (candidate_set_id, candidate_item_id)
- unique (candidate_set_id, rank)

### judge_decisions

What is a judge decision?
- A persisted LLM judgment row for one SOURCE item against its candidate set.
- Accepted duplicate edges are represented by rows where `final_status='accepted'`.

Why store decisions?
- Keep a full audit log (accepted, rejected, skipped) for confidence matrices.
- Preserve model metadata and veto reasons.
- Derive operational accepted-edge graph for canonicalization/close planning from the same table.

- id (bigint pk)
- repo_id (fk repos.id)
- type (text not null)  -- must match involved items.type
- from_item_id (fk items.id)
- candidate_set_id (fk candidate_sets.id, nullable)
- to_item_id (fk items.id, nullable)
- model_is_duplicate (boolean not null)
- final_status (text not null) -- 'accepted' | 'rejected' | 'skipped'
- confidence (real not null)
- reasoning (text null)
- relation, root_cause_match, scope_relation, path_match, certainty (text null)
- veto_reason (text null)
- min_edge (real not null)
- llm_provider (text not null)
- llm_model (text not null)
- created_by (text not null)
- created_at (timestamptz)

Constraints
- Enforce repo/type consistency between decision and referenced items.
- If `model_is_duplicate=true`, `to_item_id` must be non-null.

Cardinality rule (chain-killer)
- Allow at most one accepted outgoing edge per item:
  unique (repo_id, type, from_item_id) where final_status='accepted'

Rejudge policy (stability)
- Default: first accepted edge wins.
- With explicit `--rejudge`, prior accepted row for that source is demoted and the new accepted row is inserted.
- Rationale: avoids cluster flip-flopping while preserving audit history.

### judge_audit_runs and judge_audit_run_items
Sampled judge audit telemetry.

judge_audit_runs
- id
- repo_id
- type
- sample_policy (`random_uniform` in v1)
- sample_seed
- sample_size_requested
- sample_size_actual
- candidate_set_status (`fresh` in v1)
- source_state_filter (`open` in v1)
- min_edge
- cheap_llm_provider, cheap_llm_model
- strong_llm_provider, strong_llm_model
- status (`running`|`completed`|`failed`)
- compared_count, tp, fp, fn, tn, conflict, incomplete
- created_by, created_at, completed_at

judge_audit_run_items
- audit_run_id
- source_item_id, source_number, source_state
- candidate_set_id
- cheap_* decision fields (`model_is_duplicate`, `final_status`, `to_item_id`, `confidence`, `veto_reason`, `reasoning`)
- strong_* decision fields (same shape)
- outcome_class (`tp`|`fp`|`fn`|`tn`|`conflict`|`incomplete`)
- created_at

Matrix semantics
- Positive = `final_status='accepted'`.
- `conflict` = both accepted but `cheap_to_item_id != strong_to_item_id`.
- `conflict` rows are excluded from TP.

### close_runs and close_run_items
Close auditing.

close_runs
- id
- repo_id
- type
- mode ('plan'|'apply')
- min_confidence_close
- created_by
- created_at

close_run_items
- close_run_id
- item_id
- canonical_item_id
- action ('close'|'skip')
- skip_reason
- applied_at
- gh_result (jsonb)


## Canonicalization

Cluster definition
- Treat accepted edges as an undirected graph for clustering purposes (connected components).

Canonical selection heuristic (v1)
1) If the cluster has any open items, canonical must be an open item.
2) If any eligible item appears English (lightweight title/body heuristic), prefer eligible English items.
3) If any eligible item is opened by a maintainer, prefer maintainer-opened items.
   - Maintainer resolution uses collaborators with `admin|maintain|push` permissions.
4) Prefer the most active discussion among eligible items.
   - issues: higher `comment_count`
   - PRs: higher (`comment_count` + `review_comment_count`)
5) Then prefer earliest `created_at_gh` (oldest).
6) Final tie-breaker: lowest item number.

Important note about canonical drift
- If two clusters later merge (new edge connects them), the canonical may change.
- v1 policy: we accept drift (we do not go reopen and re-close previously closed items). We document this as a known limitation.

Closing rule
- When closing an item, always close it in favor of the current computed canonical for its cluster.
- Never close in favor of a non-canonical intermediate.
- Safety hardening: only plan/apply a close when there is a **direct accepted edge**
  `from_item -> canonical_item` with confidence `>= min_close`.
  - A transitive path through intermediate nodes is not sufficient for close eligibility.


## Similarity retrieval

We only compare within the same type.
- Issue queries only retrieve issues.
- PR queries only retrieve PRs.

Retrieval query
- For item X, compute top K neighbors by cosine distance (pgvector cosine operator).
- We do not manually normalize vectors in application code for v1.
- Filter by:
  - same repo
  - same type
  - state include (default: open-only for operational judging)

Store candidates in candidate_sets + candidate_set_members.

Default retrieval params (v1)
- k = 8
- min_score = 0.75
- include = open


## PR handling policy (v1)

- PRs are in scope in v1 (same as issues).
- Comparison is type-restricted: PRs are compared only to PRs.
- Close plans include only currently open PRs; merged PRs are never targeted for closing.
- Canonicalization still follows the cluster rules (prefer open canonical if any open item exists).


## Content construction and truncation (v1)

- Use title + body only for embedding and judging context.
- Do not include comments in v1.
- Deterministic truncation policy:
  - title max 300 chars
  - body max 7,700 chars
  - combined max 8,000 chars
- Normalize line endings and trim surrounding whitespace before hashing/embedding.


## LLM judging

Input
- Current item (title + body excerpt)
- Candidate set (top K titles + excerpts + retrieval rank)
- Similarity score is retrieval metadata, not duplicate evidence by itself.

Output contract (strict JSON)
- is_duplicate: boolean
- duplicate_of: integer (candidate number) or 0/null
- confidence: float 0..1
- reasoning: short string
- relation: `same_instance` | `related_followup` | `partial_overlap` | `different` (optional)
- root_cause_match: `same` | `adjacent` | `different` (optional)
- scope_relation: `same_scope` | `source_subset` | `source_superset` | `partial_overlap` | `different_scope` (optional)
- path_match: `same` | `different` | `unknown` (optional)
- certainty: `sure` | `unsure` (optional)

Rules
- v1 default judge path is OpenAI Codex via `pi` RPC (`openai-codex`, model `gpt-5.1-codex-mini`).
- Optional judge provider/model overrides for evaluation: Gemini, OpenAI `gpt-5-mini`, or OpenRouter `minimax/minimax-m2.5`.
- Only accept an edge if confidence >= min_edge (default 0.85).
- Only allow duplicate_of that is in the candidate set.
- If model returns `certainty="unsure"` for a duplicate claim, reject via veto.
- Follow-up/partial-overlap/subset-superset and bug-vs-feature mismatches are vetoed from acceptance.
- If the candidate target is not open, reject via veto (`target_not_open`).
- If selected duplicate candidate score is too close to the best alternative (default min gap `0.015`), reject via veto (`candidate_gap_too_small`).
- If model output is invalid JSON, persist an artifact payload to Logfire and a `judge_decisions` skipped row (`veto_reason=invalid_response:*`).


## Safety and guardrails (closing)

We must not turn this into an auto-close footgun.

Guardrails
- Maintainer protection: do not close items authored by maintainers.
- Assignee protection: do not close items assigned to maintainers.
- Canonical must be open if any open exists in the cluster.
- If maintainer identity for a specific item cannot be resolved with confidence, skip that item as uncertain.
- If maintainer list lookup fails during planning/canonicalization, fail the command rather than planning unsafe closes.
- Require a higher threshold to close than to merely record edges (record >=0.85, close >=0.90).
- Close comment template is fixed in v1: `Closing as duplicate of #{}. If this is incorrect, please contact us.`

Maintainer resolution (v1)
- Use GitHub collaborators API (`gh api repos/<repo>/collaborators?affiliation=all`).
- Treat `admin`, `maintain`, and `push` permissions as maintainer-level.
- Cache the maintainer list per repo for the run.
- This mirrors the existing shell safety approach used in `../doppelgangers/scripts/close-duplicates.sh`.
- Optional later: CODEOWNERS integration or a config file override.


## Human review and apply gate (v1)

- `apply-close` must only run after an explicit reviewed `plan-close` output.
- Review is anchored to the persisted `close_run` plan in Postgres.
- `apply-close` requires `--close-run` and enforces that the referenced run is a `plan` run.
- `--yes` is still required to execute mutations.


## Refresh and incremental operation

We need two kinds of update:

1) sync (discover + upsert)
- Adds new items.
- Computes `content_hash` from normalized semantic fields only: `type`, `title`, `body`.
- Bumps `content_version` only when that semantic hash changes.
- Metadata-only changes (`state`, labels, assignees, timestamps, comment counters) do not bump `content_version`.

2) refresh
- Default (`refresh`): incremental discovery of new items only.
- With `--refresh-known`: also updates metadata/state/timestamps for already-known items.
- Purpose: separate scope expansion from known-item metadata maintenance while supporting both in one command.

Staleness propagation
- If content_version changes, mark embeddings stale (by comparing embedded_content_hash) and mark candidate_sets stale.


## Runtime defaults (v1)

- Embed batch size: 32 (configurable)
- Embed worker concurrency: 2 (configurable)
- Candidates concurrency: 4 (configurable)
- Judge concurrency: 4 (configurable)


## Error handling and retries

Retry policy (GitHub + model providers)
- Retry on 429, 5xx, and transient/unknown network failures.
- Backoff schedule is exponential with jitter (1s, 2s, 4s, 8s, 16s; cap ~30s + jitter).
- Default attempt counts are client-specific:
  - GitHub + most model clients: 5 attempts
  - openai-codex (`pi` RPC) judge: 3 attempts
- Shared retry/backoff and max-attempt validation helpers live in `src/dupcanon/llm_retry.py`.

GitHub API
- Partial failure policy: one failed page does not corrupt DB; log and continue where possible.

Embeddings
- Idempotent: embeddings table unique(item_id, model) allows safe retries.
- Batch failures: retry the batch; if persistent, isolate item and continue.

LLM judge
- One bad response must not abort the run.
- If response is invalid JSON, treat as non-duplicate, record an error reason, and emit full payload to Logfire.


## Observability and artifacts (v1)

- Use stdlib logging everywhere (CLI entrypoints + internal services) with consistent key-value fields.
- Keep Rich console formatting, and forward logging events to Logfire for remote search/analysis.
- Minimum log fields: `run_id`, `command`, `repo`, `type`, `stage`, `item_id` (when relevant), `status`, `duration_ms`, `error_class` (when relevant).
- Persist per-run counters: synced, embedded, candidate sets built, judged, accepted edges, proposed closes, applied closes, skipped, failed.
- Track skip/failure reason categories for auditability.
- Persist debug artifact payloads (invalid JSON, model/API failures, apply failures) to Logfire.
- Keep Rich console output for operator ergonomics while sending stdlib logging events remotely.


## Supabase operational notes

- Use Supabase CLI for local dev with pgvector enabled.
- Migrations are SQL files under `supabase/migrations/` and are the source of truth.
- Recommended migration workflow:
  - `supabase db reset`
  - `supabase db lint`
  - `supabase db push` (after local validation)
- Access:
  - For local runs: use a dedicated DB user.
  - For CI or shared ops: service role key (carefully) or a privileged DB role.
- RLS:
  - If multiple operators share the same Supabase project, simplest is to keep RLS off for these tables and restrict by DB role.


## Phased development plan (thorough, with go/no-go gates)

Development principles
- Build in thin vertical slices with testable contracts.
- Treat `apply-close` as the last milestone.
- Require phase exit criteria before advancing.

Phase 0: project bootstrap
- Set up Python project scaffolding (`pyproject.toml`, `uv`, pydantic, ruff, pyright, pytest).
- Create Typer CLI skeleton with all subcommands stubbed.
- Use `rich` for CLI output (status panels, tables) and progress bars.
- Use stdlib logging with Rich console output from the start; log every command stage and important decision point.
- Add Logfire logging sink so command/service logs are searchable online.
- Use Pydantic wherever possible for settings models and boundary validation.
- Add config loading (`.env` + env vars) and run IDs.
- Keep `.local/artifacts` for operator-directed outputs (e.g., `detect-new --json-out`); failure/debug artifact payloads are emitted to Logfire.

Exit criteria
- `uv run ruff check`, `uv run pyright`, and `uv run pytest` pass.
- CLI help and command help surfaces are stable.

Phase 1: schema and migrations
- Implement Supabase SQL migrations for all v1 tables and constraints.
- Enable pgvector and create `vector(3072)` embedding column.
- Add indexes and edge cardinality constraints (vector ANN index intentionally omitted at 3072 dims due pgvector host limits).

Exit criteria
- Fresh database can be migrated from zero.
- Constraint tests validate uniqueness and type-consistency rules.

Phase 2: sync + refresh
- Implement `sync` for issue/PR discovery and upsert.
- Implement `refresh` default incremental discovery and optional `--refresh-known` metadata refresh mode.
- Implement semantic content hashing (`type`, `title`, `body`) and content_version bump logic.

Exit criteria
- Sync is idempotent.
- Metadata-only updates do not bump content_version.

Phase 3: embedding pipeline
- Implement deterministic text construction/truncation.
- Call configured embedding provider/model (`gemini` or `openai`) and enforce 3072-dim validation.
- Store embeddings with `embedded_content_hash` and skip unchanged content.

Exit criteria
- Embedding upserts are idempotent.
- Retries and partial-failure behavior are verified.

Phase 4: candidate retrieval
- Implement pgvector cosine retrieval by repo + type + state filter.
- Persist `candidate_sets` and ranked `candidate_set_members`.
- Mark stale sets when source content_version changes.

Exit criteria
- Candidate retrieval is reproducible with persisted retrieval parameters.
- No cross-type leakage.

Phase 5: LLM judge + edge recording
- Implement judge prompt/response contract using default OpenAI Codex (`openai-codex`, `gpt-5.1-codex-mini`) with optional Gemini/OpenAI/OpenRouter overrides for evaluation.
- Enforce strict JSON parsing, candidate-bounded target validation, and `min_edge` threshold.
- Persist invalid model-response payloads to Logfire (no local failure-artifact file writes).
- Implement edge policy: first accepted edge wins; allow explicit `--rejudge` flow.

Exit criteria
- One bad model response cannot fail the whole run.
- Edge policy behavior is deterministic and tested.

Phase 6: canonicalization
- Build duplicate clusters from accepted edges (connected components).
- Implement canonical scoring in this order:
  1) open if any open exists
  2) English-language item if any eligible English item exists
  3) maintainer-authored if any eligible maintainer-authored item exists
  4) highest discussion activity
  5) oldest created date
  6) lowest item number

Exit criteria
- Canonical selection is deterministic across repeated runs.

Phase 7: close planning + guardrails
- Implement `plan-close` generation and persistence (`close_runs`, `close_run_items`).
- Apply guardrails (maintainer author/assignee protections, uncertainty skips, canonical-open rule, confidence threshold).
- Resolve maintainers via GitHub collaborators permissions (`admin|maintain|push`).

Exit criteria
- Plan output is reproducible and human-reviewable.
- Guardrail skip reasons are fully auditable.

Phase 8: apply close (mutation path)
- Implement `apply-close` with required `--close-run` + `--yes`.
- Require `close_run.mode = plan` before mutation.
- Copy planned rows into apply audit rows efficiently (bulk copy) before mutation.
- Execute GitHub close calls and persist per-item API results.

Exit criteria
- Invalid run references block all mutations.
- Partial failures are captured without corrupting run state.

Phase 9: evaluation gate
- Build manual labeling workflow for proposed close actions.
- Compute precision on labeled results.

Exit criteria
- Precision >= 0.90 on at least 100 proposed closes before production `apply-close`.

Phase 10: hardening and operator readiness
- Improve operator UX (Rich progress bars and summaries, categorized failures; no tqdm).
- Finalize runbooks and troubleshooting docs (`docs/operator_runbook_v1.md` is the current baseline).
- Validate end-to-end operation from clean setup.

Exit criteria
- A new operator can run full workflow from docs only.

Recommended implementation order
1) bootstrap
2) migrations
3) sync/refresh
4) embed
5) candidates
6) judge
7) canonicalize
8) plan-close
9) apply-close
10) evaluation and hardening


## Evaluation gate before production apply

- Before enabling `apply-close` in production workflows, evaluate on a manually labeled validation set.
- Minimum quality bar: precision >= 0.90 on proposed close actions.
- Practical minimum set size: at least 100 proposed closes.


## Locked v1 decisions summary

- Embeddings: provider-configurable (`gemini` or `openai`) with dimension 3072 (default `text-embedding-3-large` on `openai`).
- Judge default: OpenAI Codex via `pi` RPC (`openai-codex`, `gpt-5.1-codex-mini`) with optional Gemini/OpenAI/OpenRouter overrides for evaluation.
- Retrieval defaults:
  - candidates k=4 (clustering), min_score=0.75, include=open.
  - detect-new k=8, min_score=0.75.
- Thresholds: min_edge=0.85, min_close=0.90.
- Judge guardrails:
  - duplicate targets must be open (`target_not_open` veto otherwise)
  - selected duplicate candidate score must exceed best alternative by >= `0.015` (`candidate_gap_too_small` veto otherwise)
- Inputs to model: title + body only (no comments).
- CLI/tooling: Typer + Rich for terminal UX/progress, stdlib logging with Rich console + Logfire sink, and Pydantic for settings/contracts.
- Edge lifecycle: first accepted edge wins by default; `--rejudge` allows replacement runs.
- Overrides: no manual override system in v1.
- Apply gate: explicit reviewed plan close_run + `--yes`.
- Undo/remediation: no reopen automation in v1.


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
- Added `docs/operator_runbook_v1.md` as the baseline end-to-end operator procedure.

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

