# Duplicate Canonicalization CLI

Status: Draft design doc for internal discussion

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
  - support “refresh-known-only” to update state for previously-seen items


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
  - Validates DB connection, checks pgvector available, prints current schema version.

- dupcanon sync --repo org/name [--type issue|pr|all] [--state open|closed|all] [--since 30d|YYYY-MM-DD]
  - Fetches items and upserts into items table.
  - Updates content_hash/content_version.

- dupcanon refresh --repo org/name [--known-only]
  - Refreshes state and timestamps from GitHub.
  - With --known-only: only touches items already in DB, does not discover new ones.

- dupcanon embed --repo org/name [--type issue|pr] [--only-changed]
  - Embeds items missing embeddings or with content_version advanced.

- dupcanon candidates --repo org/name --type issue|pr [--k 8] [--min-score 0.75] [--include closed|open|all]
  - Creates candidate_sets and candidate_set_members.
  - v1 default retrieval is k=8 (configurable).

- dupcanon judge --repo org/name --type issue|pr [--provider gemini] [--model gemini-2.5-flash] [--min-edge 0.85] [--allow-stale] [--rejudge]
  - Reads fresh candidate sets, calls LLM, writes duplicate_edges.

- dupcanon canonicalize --repo org/name --type issue|pr
  - Computes canonicals (on the fly or materializes clusters table).

- dupcanon plan-close --repo org/name --type issue|pr [--min-close 0.90] [--maintainers-source collaborators] [--dry-run]
  - Produces a close_run and close_run_items.

- dupcanon apply-close --close-run <id> --approval-file <path> [--yes]
  - Executes GitHub close operations after verifying an explicit approval checkpoint.


## Locked model/provider choices (v1)

- Embeddings: Gemini API `gemini-embedding-001` with output dimensionality locked to 768.
- Duplicate judge: Gemini API `gemini-2.5-flash`.
- Credentials: `.env` or environment variables for local/operator runs.


## Architecture

Everything is DB-first. Supabase hosts Postgres and pgvector.
- Postgres tables hold item metadata, decisions, candidate sets, and close audit trails.
- pgvector holds embeddings and supports similarity search.

The CLI is a thin orchestrator:
- GitHub fetch via GitHub API (gh CLI, REST, or GraphQL) depending on implementation choice.
- Embeddings via Gemini API (`gemini-embedding-001`).
- LLM judging via Gemini API (`gemini-2.5-flash`) using strict JSON responses.
- Python CLI stack: Typer for command surface + Rich for terminal UX/progress rendering.
- Data/config modeling and validation: Pydantic.
- Logging stack: structlog for structured, pretty logs across all commands and internal pipeline steps.
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
- v1 locks to Gemini `gemini-embedding-001` with output dimensionality 768.
- If we switch embedding models later (dimension change), we do a migration or add a new embeddings table/column.

- id (bigint pk)
- item_id (fk items.id)
- model (text not null)  -- `gemini-embedding-001` in v1
- dim (int not null)     -- 768 in v1
- embedding (vector(768) not null)
- embedded_content_hash (text not null)
- created_at (timestamptz)

Constraints
- unique (item_id, model)

Indexes
- vector index on embedding (ivfflat or hnsw depending on preference)

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

### duplicate_edges

What is a duplicate edge?
- An accepted directed link from one item to another: “from is a duplicate of to”.

Why store edges?
- A pile of pairwise decisions is hard to reason about. Edges let us:
  - form clusters (connected components)
  - compute a single canonical per cluster
  - always close to canonical, eliminating chains

- id (bigint pk)
- repo_id (fk repos.id)
- type (text not null)  -- must match both items.type
- from_item_id (fk items.id)
- to_item_id (fk items.id)
- confidence (real not null)
- reasoning (text null)
- llm_provider (text not null)
- llm_model (text not null)
- created_by (text not null)
- created_at (timestamptz)
- status (text not null) -- 'accepted' | 'rejected'

Constraints
- from_item_id != to_item_id
- Enforce type consistency: from_item.type == to_item.type == duplicate_edges.type

Cardinality rule (chain-killer)
- Allow at most one accepted outgoing edge per item:
  unique (repo_id, type, from_item_id) where status='accepted'

Upsert policy (stability)
- Default: first accepted edge wins.
- If a later run suggests a different to_item, it is recorded as rejected (or ignored) unless an explicit --rejudge flag is used.
- Rationale: avoids cluster flip-flopping.

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
2) Prefer the most active discussion among eligible items.
   - issues: higher `comment_count`
   - PRs: higher (`comment_count` + `review_comment_count`)
3) Then prefer earliest `created_at_gh` (oldest).
4) Final tie-breaker: lowest item number.

Important note about canonical drift
- If two clusters later merge (new edge connects them), the canonical may change.
- v1 policy: we accept drift (we do not go reopen and re-close previously closed items). We document this as a known limitation.

Closing rule
- When closing an item, always close it in favor of the current computed canonical for its cluster.
- Never close in favor of a non-canonical intermediate.


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
  - optional state include (open-only is safer for close decisions)

Store candidates in candidate_sets + candidate_set_members.

Default retrieval params (v1)
- k = 8
- min_score = 0.75


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
- Candidate set (top K titles + excerpts + similarity scores)

Output contract (strict JSON)
- is_duplicate: boolean
- duplicate_of: integer (candidate number) or 0/null
- confidence: float 0..1
- reasoning: short string

Rules
- v1 judge model is Gemini `gemini-2.5-flash`.
- Only accept an edge if confidence >= min_edge (default 0.85).
- Only allow duplicate_of that is in the candidate set.
- If the candidate target is closed, we still may record the edge, but closing policy will prefer an open canonical.
- If model output is invalid JSON, treat as non-duplicate and persist the response artifact under `.local/artifacts`.


## Safety and guardrails (closing)

We must not turn this into an auto-close footgun.

Guardrails
- Maintainer protection: do not close items authored by maintainers.
- Assignee protection: do not close items assigned to maintainers.
- Canonical must be open if any open exists in the cluster.
- If maintainer identity for a specific item cannot be resolved with confidence, skip that item as uncertain.
- If maintainer list lookup fails completely, refuse to apply closes.
- Require a higher threshold to close than to merely record edges (record >=0.85, close >=0.90).
- Close comment template is fixed in v1: `Closing as duplicate of {}. If this is incorrect, please contact us.`

Maintainer resolution (v1)
- Use GitHub collaborators API (`gh api repos/<repo>/collaborators?affiliation=all`).
- Treat `admin`, `maintain`, and `push` permissions as maintainer-level.
- Cache the maintainer list per repo for the run.
- This mirrors the existing shell safety approach used in `../doppelgangers/scripts/close-duplicates.sh`.
- Optional later: CODEOWNERS integration or a config file override.


## Human review and apply gate (v1)

- `apply-close` must only run after an explicit reviewed `plan-close` output.
- `plan-close` writes an approval checkpoint file containing at least:
  - close_run_id
  - deterministic plan hash
  - approved_by
  - approved_at
- `apply-close` requires `--approval-file` and verifies hash equality before any GitHub mutation.
- `--yes` is still required to skip interactive confirmation.


## Refresh and incremental operation

We need two kinds of update:

1) sync (discover + upsert)
- Adds new items.
- Computes `content_hash` from normalized semantic fields only: `type`, `title`, `body`.
- Bumps `content_version` only when that semantic hash changes.
- Metadata-only changes (`state`, labels, assignees, timestamps, comment counters) do not bump `content_version`.

2) refresh-known-only
- Does not discover new items.
- Only updates state/open/closed/timestamps for items already in DB.
- Purpose: keep the DB accurate without expanding scope.

Staleness propagation
- If content_version changes, mark embeddings stale (by comparing embedded_content_hash) and mark candidate_sets stale.


## Runtime defaults (v1)

- Embed batch size: 32 (configurable)
- Embed worker concurrency: 2 (configurable)
- Judge concurrency: 4 (configurable)


## Error handling and retries

Retry policy (GitHub + Gemini API)
- Retry on 429, 5xx, and transient network failures.
- Retry up to 5 attempts with exponential backoff and jitter.
- Base schedule: 1s, 2s, 4s, 8s, 16s (cap ~30s).
- If `Retry-After` is returned, honor it.

GitHub API
- Partial failure policy: one failed page does not corrupt DB; log and continue where possible.

Embeddings
- Idempotent: embeddings table unique(item_id, model) allows safe retries.
- Batch failures: retry the batch; if persistent, isolate item and continue.

LLM judge
- One bad response must not abort the run.
- If response is invalid JSON, treat as non-duplicate, record an error reason, and persist the raw response in `.local/artifacts`.


## Observability and artifacts (v1)

- Use `structlog` everywhere (CLI entrypoints + internal services) with consistent structured fields.
- Minimum log fields: `run_id`, `command`, `repo`, `type`, `stage`, `item_id` (when relevant), `status`, `duration_ms`, `error_class` (when relevant).
- Persist per-run counters: synced, embedded, candidate sets built, judged, accepted edges, proposed closes, applied closes, skipped, failed.
- Track skip/failure reason categories for auditability.
- Persist debug artifacts (invalid JSON, model/API failures, apply failures) under `.local/artifacts`.


## Supabase operational notes

- Use Supabase CLI for local dev with pgvector enabled.
- Migrations: Alembic (Python) recommended.
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
- Use `structlog` for structured pretty logging from the start; log every command stage and important decision point.
- Use Pydantic wherever possible for settings models and boundary validation.
- Add config loading (`.env` + env vars) and run IDs.
- Standardize local artifacts path under `.local/artifacts`.

Exit criteria
- `uv run ruff check`, `uv run pyright`, and `uv run pytest` pass.
- CLI help and command help surfaces are stable.

Phase 1: schema and migrations
- Implement Alembic migrations for all v1 tables and constraints.
- Enable pgvector and create `vector(768)` embedding column.
- Add indexes (including vector index) and edge cardinality constraints.

Exit criteria
- Fresh database can be migrated from zero.
- Constraint tests validate uniqueness and type-consistency rules.

Phase 2: sync + refresh
- Implement `sync` for issue/PR discovery and upsert.
- Implement `refresh --known-only` with no new item discovery.
- Implement semantic content hashing (`type`, `title`, `body`) and content_version bump logic.

Exit criteria
- Sync is idempotent.
- Metadata-only updates do not bump content_version.

Phase 3: embedding pipeline
- Implement deterministic text construction/truncation.
- Call Gemini embeddings (`gemini-embedding-001`) and enforce 768-dim validation.
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
- Implement judge prompt/response contract using Gemini `gemini-2.5-flash`.
- Enforce strict JSON parsing, candidate-bounded target validation, and `min_edge` threshold.
- Persist artifacts for invalid model responses under `.local/artifacts`.
- Implement edge policy: first accepted edge wins; allow explicit `--rejudge` flow.

Exit criteria
- One bad model response cannot fail the whole run.
- Edge policy behavior is deterministic and tested.

Phase 6: canonicalization
- Build duplicate clusters from accepted edges (connected components).
- Implement canonical scoring in this order:
  1) open if any open exists
  2) highest discussion activity
  3) oldest created date
  4) lowest item number

Exit criteria
- Canonical selection is deterministic across repeated runs.

Phase 7: close planning + guardrails
- Implement `plan-close` generation and persistence (`close_runs`, `close_run_items`).
- Apply guardrails (maintainer author/assignee protections, uncertainty skips, canonical-open rule, confidence threshold).
- Resolve maintainers via GitHub collaborators permissions (`admin|maintain|push`).
- Generate approval checkpoint file with deterministic plan hash.

Exit criteria
- Plan output is reproducible and human-reviewable.
- Guardrail skip reasons are fully auditable.

Phase 8: apply close (mutation path)
- Implement `apply-close` with required `--approval-file` + `--yes`.
- Verify checkpoint hash equals current computed plan hash before mutation.
- Execute GitHub close calls and persist per-item API results.

Exit criteria
- Hash mismatch blocks all mutations.
- Partial failures are captured without corrupting run state.

Phase 9: evaluation gate
- Build manual labeling workflow for proposed close actions.
- Compute precision on labeled results.

Exit criteria
- Precision >= 0.90 on at least 100 proposed closes before production `apply-close`.

Phase 10: hardening and operator readiness
- Improve operator UX (Rich progress bars and summaries, categorized failures; no tqdm).
- Finalize runbooks and troubleshooting docs.
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

- Embeddings: Gemini API `gemini-embedding-001`, dimension 768.
- Judge: Gemini API `gemini-2.5-flash`.
- Retrieval defaults: k=8, min_score=0.75.
- Thresholds: min_edge=0.85, min_close=0.90.
- Inputs to model: title + body only (no comments).
- CLI/tooling: Typer + Rich for terminal UX/progress, structlog for structured logging, and Pydantic for settings/contracts.
- Edge lifecycle: first accepted edge wins by default; `--rejudge` allows replacement runs.
- Overrides: no manual override system in v1.
- Apply gate: explicit approval checkpoint file + `--yes`.
- Undo/remediation: no reopen automation in v1.

