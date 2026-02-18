# Duplicate Canonicalization CLI

Status: Draft design doc for internal discussion

Implementation status snapshot (2026-02-14)
- Implemented commands: `init`, `sync`, `refresh`, `embed`, `candidates`, `judge`, `judge-audit`, `report-audit`, `detect-new`, `canonicalize`, `maintainers`, `plan-close`, `apply-close`.
- Current apply gate: reviewed persisted `close_run` + explicit `--yes` (no approval-file workflow).
- Current canonical preference: open-first, then English-language preference, then maintainer preference.
- Judge runtime path is centralized in `src/dupcanon/judge_runtime.py` and reused by judge/audit/online detect.
- Shared retry/backoff/attempt validation primitives are centralized in `src/dupcanon/llm_retry.py`.
- Known remaining gaps: no first-class Phase 9 evaluation command yet.
- Proposed extension planning doc (not yet active default): `docs/internal/intent_card_pipeline_design_doc_v1.md`.

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

- dupcanon judge --repo org/name --type issue|pr [--source raw|intent] [--provider gemini|openai|openrouter|openai-codex] [--model ...] [--thinking off|minimal|low|medium|high|xhigh] [--min-edge 0.85] [--allow-stale] [--rejudge] [--workers N]
  - Reads fresh candidate sets, calls LLM, writes judge_decisions.
  - Default configured provider/model is OpenAI Codex via `pi` RPC (`openai-codex`, `gpt-5.1-codex-mini`). Gemini/OpenAI/OpenRouter are available as overrides.
  - Model resolution:
    - `--model` overrides everything.
    - If selected provider matches configured provider, use configured model.
    - Otherwise use provider defaults (`gemini-3-flash-preview`, `gpt-5-mini`, `minimax/minimax-m2.5`, `gpt-5.1-codex-mini`).
  - Thinking defaults can be set globally via `DUPCANON_JUDGE_THINKING`.

- dupcanon judge-audit --repo org/name --type issue|pr [--source raw|intent] [--sample-size 100] [--seed 42] [--min-edge 0.85] [--cheap-provider ...] [--cheap-model ...] [--cheap-thinking ...] [--strong-provider ...] [--strong-model ...] [--strong-thinking ...] [--workers N] [--verbose] [--debug-rpc] [--show-disagreements/--no-show-disagreements] [--disagreements-limit N]
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

- dupcanon canonicalize --repo org/name --type issue|pr [--source raw|intent]
  - Computes canonical statistics on the fly from accepted edges (no canonical table materialization in v1).

- dupcanon plan-close --repo org/name --type issue|pr [--source raw|intent] [--min-close 0.90] [--maintainers-source collaborators] [--dry-run]
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
- representation (`raw`|`intent`)
- created_by (text not null)
- created_at (timestamptz)

Constraints
- Enforce repo/type consistency between decision and referenced items.
- If `model_is_duplicate=true`, `to_item_id` must be non-null.

Cardinality rule (chain-killer)
- Allow at most one accepted outgoing edge per item per representation source:
  unique (repo_id, type, from_item_id, representation) where final_status='accepted'

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
- representation (`raw`|`intent`)
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
- representation (`raw`|`intent`)
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
  - Embedding text:
    - title max 300 chars
    - body max 7,700 chars
    - combined max 8,000 chars
  - Judge prompt excerpts (source + each candidate):
    - title max 300 chars
    - body max 4,000 chars
- Normalize line endings and trim surrounding whitespace before hashing/embedding/prompt construction.


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
- Finalize runbooks and troubleshooting docs (`docs/internal/operator_runbook_v1.md` is the current baseline).
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

Development journal entries were moved to `docs/internal/journal.md`.

- This includes all prior chronology previously embedded in this spec.
- Continue journaling all new work there, including intent-card phase implementation.
