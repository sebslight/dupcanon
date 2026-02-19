# Semantic Query CLI (v1) — Spec, Architecture, and Design

Status: Phase 1 implemented (retrieval-only `search` command); answer mode deferred  
Owner: dupcanon  
Date: 2026-02-18

---

## 1) Purpose

Define a production-grade, **read-only semantic query experience** in the existing `dupcanon` CLI so operators can ask natural-language questions like:

- “what PRs are related to the issue on cron?”
- “show open issues similar to flaky CI timeout failures”

This design intentionally starts with a **CLI-first query system** before any API service.

---

## 2) Product decisions (v1)

These decisions are locked for the first implementation unless explicitly revised:

1. New command: `dupcanon search`.
2. Query mode is **read-only** (no close/apply/comment mutation paths).
3. Representation source default is `intent`; `--source raw` remains rollback.
4. Same repository scope as the rest of v1 (`--repo org/name` required).
5. Supports issue-only, PR-only, or combined retrieval.
6. Default state filter is `open`.
7. Answer synthesis is opt-in only (`--answer`), never default-on.
8. Default UX is ranked issue/PR result output; optional machine output via `--json`.
9. Include constraints default to `include-mode=boost` (soft rerank) with `include-weight=0.15`.
10. Search runs are **not persisted** in DB in v1.
11. Interaction mode is one-shot in v1 (no interactive chat loop).
12. Observability uses existing stdlib logging + Rich + Logfire conventions.
13. Existing embedding defaults remain unchanged:
   - provider: OpenAI
   - model: `text-embedding-3-large`
   - dimension: `3072`

---

## 3) Goals and non-goals

### Goals

1. Accept natural-language queries and return semantically related issues/PRs.
2. Reuse existing intent/raw representation infrastructure.
3. Keep behavior deterministic and auditable.
4. Make results easy to consume in terminal and machine-readable JSON.
5. Keep rollout safe (no GitHub write side effects).

### Non-goals (v1)

1. Multi-repo semantic search.
2. Autonomous issue/PR mutation from search output.
3. Full conversational memory agent with tool autonomy.
4. Cross-system indexing (Slack, docs, wiki) in first release.

---

## 4) User experience and CLI surface

## 4.1 Primary commands

```bash
# query-driven search
uv run dupcanon search \
  --repo org/name \
  --query "what PRs are related to the issue on cron" \
  --type pr \
  --state open

# anchor-driven search with semantic exclusion
uv run dupcanon search \
  --repo org/name \
  --similar-to 128 \
  --type issue \
  --exclude whatsapp
```

## 4.2 Proposed flags

- Required:
  - `--repo org/name`
  - exactly one base signal:
    - `--query <text>`
    - `--similar-to <number>`
- Retrieval controls:
  - `--type issue|pr|all` (default: `all`)
  - `--state open|closed|all` (default: `open`)
  - `--limit N` (default: `10`, max `50`)
  - `--min-score FLOAT` (default: `0.30`)
  - `--source raw|intent` (default: `intent`)
  - `--include TERM` (repeatable semantic include constraint)
  - `--exclude TERM` (repeatable semantic exclude constraint)
  - `--include-mode boost|filter` (default: `boost`)
  - `--include-weight FLOAT` (default: `0.15`, boost mode only)
  - `--include-threshold FLOAT` (default: `0.20`)
  - `--exclude-threshold FLOAT` (default: `0.20`)
  - `--debug-constraints/--no-debug-constraints` (default: off)
- Optional answer synthesis:
  - `--answer/--no-answer` (default: `--no-answer`)
  - `--provider`, `--model`, `--thinking` (same semantics as judge/detect-new)
- Output controls:
  - `--json/--no-json` (default: `--no-json`; when enabled prints full `SearchResult` JSON to stdout)
  - `--show-body-snippet/--no-show-body-snippet` (default: off)

## 4.3 Output contract

Default terminal output:
- query summary (repo, type/state/source, min_score, limit)
- ranked table with:
  - rank
  - type
  - number
  - state
  - score
  - title
  - URL

JSON output (`--json`) uses a strict Pydantic contract (see Section 8).

---

## 5) End-to-end architecture

The command runs six stages:

1. Parse and validate base signal + flags.
2. Resolve query text from `--query` or anchor item (`--similar-to`).
3. Embed the resolved query text.
4. Retrieve candidate items by vector similarity (intent/raw).
5. Apply deterministic include/exclude semantic constraint filtering/reranking.
6. Render terminal + optional JSON artifact; emit structured logs.

### Stage graph

`parse -> resolve-base-signal -> embed -> retrieve -> include/exclude-filter -> output`

---

## 6) Retrieval and ranking design

## 6.1 Query embedding

- Base text is normalized with existing text normalization helpers.
- Base text source is either explicit `--query` or anchor-derived text from `--similar-to`.
- Embedding client path mirrors existing embed/detect-new provider logic.
- Query vector dimensionality must match configured embedding dim (`3072`).

## 6.2 Source representations

### `--source intent` (default)

- Search against latest **fresh** `intent_cards` + `intent_embeddings` per item.
- Require matching schema/prompt versions from intent-card service constants.
- If intent prerequisites fail, fallback behavior is explicit:
  - `requested_source=intent`
  - `effective_source=raw`
  - `source_fallback_reason=<reason>`

### `--source raw`

- Search against `embeddings` table joined to `items`.

## 6.3 Similarity metric

- Use pgvector cosine distance path already used elsewhere:
  - score = `1 - (embedding <=> query_vector)`
- Results are ordered by descending score.

## 6.4 Filtering

Deterministic filters are applied in SQL where possible:
- repo
- type (`issue|pr|all`)
- state (`open|closed|all`)
- `score >= min_score`

## 6.5 Optional deterministic reranking (v1.1)

A lightweight reranker can be enabled after v1 baseline:
- recency boost (small positive weight)
- open-state boost for actionable surfacing
- lexical overlap boost for exact component names

This reranker must be deterministic and logged feature-by-feature.

---

## 7) Chat-style grounded answer (optional)

When `--answer` is enabled:

1. Use top retrieval hits as context (default top 8).
2. Prompt an LLM to answer in concise prose.
3. Require citation references by rank/number.
4. Reject/repair ungrounded output that references items not in context.

### Guardrails

- Retrieval results remain the source of truth.
- Answer mode cannot introduce non-cited claims as facts.
- On synthesis failure, still return ranked retrieval results.

---

## 8) Data contracts (Pydantic)

Add new models in `src/dupcanon/models.py`.

## 8.1 `SearchHit`

- `rank: int`
- `item_id: int`
- `type: ItemType`
- `number: int`
- `state: StateFilter`
- `title: str`
- `url: str`
- `score: float` (0..1)
- `body_snippet: str | None`
- `constraint_debug: SearchConstraintDebug | None`

`SearchConstraintDebug`:
- `include_scores: dict[str, float]`
- `exclude_scores: dict[str, float]`
- `include_max_score: float | None`
- `exclude_max_score: float | None`

## 8.2 `SearchAnswer`

- `text: str`
- `citations: list[int]`  (issue/PR numbers)
- `provider: str`
- `model: str`

## 8.3 `SearchResult`

- `schema_version: Literal["v1"]`
- `repo: str`
- `query: str`
- `similar_to_number: int | None`
- `include_terms: list[str]`
- `exclude_terms: list[str]`
- `include_mode: SearchIncludeMode`
- `include_weight: float`
- `include_threshold: float`
- `exclude_threshold: float`
- `type_filter: TypeFilter`
- `state_filter: StateFilter`
- `requested_source: RepresentationSource`
- `effective_source: RepresentationSource`
- `source_fallback_reason: str | None`
- `limit: int`
- `min_score: float`
- `hits: list[SearchHit]`
- `answer: SearchAnswer | None`
- `run_id: str`
- `timestamp: datetime`

Validation rules:
- `0 <= min_score <= 1`
- `0 <= include_weight <= 1`
- `0 <= include_threshold <= 1`
- `0 <= exclude_threshold <= 1`
- `1 <= limit <= 50`
- every citation in `answer.citations` must exist in `hits.number`

---

## 9) Database design

## 9.1 Query-time retrieval methods (required)

Add retrieval/scoring methods in `src/dupcanon/database.py`:

1. `search_similar_items_raw(...)`
   - inputs: repo_id, query_vector, type_filter, state_filter, min_score, limit, model
   - joins `embeddings` + `items`
2. `search_similar_items_intent(...)`
   - same inputs + `intent_schema_version`, `intent_prompt_version`
   - joins latest fresh `intent_cards` + `intent_embeddings` + `items`
3. `get_search_anchor_item(...)`
   - resolves `--similar-to` anchor item by number/type filter.
4. `score_search_items_raw(...)` / `score_search_items_intent(...)`
   - score specific candidate item IDs against include/exclude concept embeddings.

These methods return typed rows/maps used by search service filtering logic.

## 9.2 Persistence of search runs (v1 decision)

Search runs are **not persisted** in database tables in v1.

Rationale:
- keeps first release simpler and lower-risk,
- preserves one-shot operator workflow,
- avoids migration overhead before relevance quality is validated.

Auditability in v1 is provided by:
- structured logs (`run_id`, filters, source fallback metadata, latency),
- optional `--json` output capture by operators/automation.

Future option (post-v1): add `search_runs` / `search_run_hits` persistence once product needs justify storage and migration complexity.

---

## 10) Service/module design

## 10.1 New service module

Create `src/dupcanon/search_service.py` with:

`run_search(...) -> SearchResult`

Inputs:
- settings, repo_value
- base signal: `query` or `similar_to_number`
- optional repeatable `include_terms` / `exclude_terms`
- type/state filters
- source
- limit/min_score
- run_id/logger

Responsibilities:
- parse repo
- resolve repo id
- resolve base text from query or anchor item
- embed resolved base text
- retrieve hits from selected representation
- apply include/exclude semantic filtering
- return typed `SearchResult`

## 10.2 CLI integration

Update `src/dupcanon/cli.py`:
- add `search` command
- add option constants (similar to detect/judge style)
- support `--json` stdout output mode
- render Rich table for top hits (default)

## 10.3 Config additions

Add to `src/dupcanon/config.py`:
- `search_default_limit` (default 10)
- `search_max_limit` (default 50)
- `search_default_min_score` (default 0.30)
- `search_answer_provider`/`model`/`thinking` (default to judge defaults unless overridden)

All new settings validated with Pydantic.

---

## 11) Logging, observability, and artifacts

Use existing logging stack and fields.

Required fields where applicable:
- `run_id`
- `command=search`
- `repo`
- `stage` (`parse|embed|retrieve|answer|complete`)
- `status`
- `duration_ms`
- `error_class`

Additional search-specific fields:
- `query_hash` (avoid full plaintext in standard logs)
- `type_filter`, `state_filter`
- `requested_source`, `effective_source`, `source_fallback_reason`
- `result_count`

Artifacts:
- JSON output via `--json` (stdout)
- Logfire payload for failures and answer-generation errors

---

## 12) Evaluation and quality gates

## 12.1 Offline relevance set

Create a labeled query set per repo:
- `query`
- relevant item numbers (graded relevance if available)

## 12.2 Metrics

Track per-source (`intent` vs `raw`):
- Recall@K (K=5,10)
- MRR@10
- nDCG@10
- p50/p95 latency
- fallback rate (`requested=intent`, `effective=raw`)

## 12.3 Initial acceptance targets

Before declaring production-ready default behavior:
- nDCG@10 >= 0.80 on curated query set
- Recall@10 >= 0.85
- p95 latency <= 3s retrieval-only, <= 8s with answer mode

---

## 13) Security and safety model

1. Read-only command with no GitHub mutation calls.
2. Same-repo scope enforcement (`repo_id` bound).
3. Strict option validation and bounded limits.
4. Answer mode must be citation-grounded.
5. No hidden autonomous actions or long-running tool loops.

---

## 14) Rollout plan

### Phase 0 — Spec + migration planning
- finalize this design
- confirm command/flags/defaults

### Phase 1 — Retrieval-only `search`
- implement embedding + vector retrieval + Rich/JSON output
- add tests and docs

### Phase 2 — Optional `--answer`
- add grounded answer synthesis with citations
- add answer-specific tests

### Phase 3 — Hardening
- query evaluation harness
- latency/cost tuning
- optional deterministic reranking

### Phase 4 — API follow-on (future)
- expose same service via read-only HTTP API
- preserve identical contracts and guardrails

---

## 15) Test plan

Required test additions:

1. `tests/test_search_service.py`
   - query embedding path
   - intent/raw retrieval path
   - fallback metadata
   - `--similar-to` anchor path
   - include/exclude semantic filtering
2. `tests/test_cli.py`
   - `search --help`
   - default values
   - option propagation
3. `tests/test_database.py`
   - retrieval method row mapping
   - type/state/source filter behavior
4. `tests/test_models.py`
   - `SearchResult`/`SearchAnswer` validators

Validation commands:
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`

---

## 16) Resolved product defaults

Resolved decisions for v1:
1. Default `--state` is `open`.
2. Base signal requires exactly one of `--query` or `--similar-to`.
3. Repeatable `--include` / `--exclude` semantic constraints are supported in both base-signal modes.
4. Include behavior defaults to `include-mode=boost` (soft rerank); strict mode is `include-mode=filter`.
5. Constraint tuning defaults are `include-weight=0.15`, `include-threshold=0.20`, `exclude-threshold=0.20`.
6. `--answer` remains opt-in (`--no-answer` default).
7. Search run persistence in DB is out of scope for v1.
8. Interaction model is one-shot only in v1 (no interactive chat loop).

---

## 17) Recommended initial defaults

For first implementation pass:
- base signal: `--query` or `--similar-to` (exactly one)
- optional repeatable `--include` / `--exclude`
- `--include-mode boost`
- `--include-weight 0.15`
- `--include-threshold 0.20`
- `--exclude-threshold 0.20`
- `--type all`
- `--state open`
- `--limit 10`
- `--min-score 0.30`
- `--source intent`
- `--no-answer` by default
- `--no-json` by default (tabular CLI output)
- one-shot invocation model only
- `--source raw` rollback always available
