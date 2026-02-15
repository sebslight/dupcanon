# AGENTS.md

Project operating guide for coding agents working in this repository.

## 1) Mission and source of truth

We are building a **human-operated duplicate canonicalization CLI** for GitHub issues/PRs.

Primary spec:
- `docs/duplicate_triage_cli_python_spec_design_doc_v_1.md`

When implementing behavior, schema, or defaults, follow the spec above first.

## 2) Locked v1 decisions (do not drift)

- Embeddings default: OpenAI `text-embedding-3-large` (`DUPCANON_EMBEDDING_PROVIDER=openai`)
- Embedding dimension: `3072` (pgvector column is `vector(3072)`)
- Judge default: OpenAI Codex via `pi` RPC (`openai-codex`, model `gpt-5.1-codex-mini`)
- Optional judge overrides for evaluation: OpenAI `gpt-5-mini`, Gemini `gemini-3-flash-preview`, OpenRouter `minimax/minimax-m2.5`
- Retrieval defaults:
  - candidates `k=4` (clustering), `min_score=0.75`
  - detect-new `k=8`, `min_score=0.75`
- Thresholds: `min_edge=0.85`, `min_close=0.90`
- Input content for modeling: title + body only (no comments)
- Edge policy: first accepted edge wins unless explicit `--rejudge`
- Canonical preference: if an eligible English item exists in a cluster, prefer it; then prefer maintainer-authored among remaining eligible items
- No manual override system in v1
- Apply gate: requires reviewed plan close_run + explicit `--yes`
- Precision gate before production apply: `>= 0.90` on at least 100 labeled proposed closes

## 3) Required Python/tooling stack

- Package/env management: **uv**
- CLI framework: **Typer**
- Terminal UX and progress bars: **Rich**
- Logging: **stdlib logging** with Rich console handler + Logfire sink (`logfire.LogfireLoggingHandler`) and consistent key-value fields
- Data/config models and validation: **Pydantic**
- Lint/format: **ruff**
- Type checking: **pyright**
- Tests: **pytest**

### Hard rules

- Do **not** use system `python`/`python3`; use `uv run ...`
- Do **not** use `tqdm` in v1; use Rich progress APIs
- Use **Pydantic whenever possible** for settings, request/response contracts, and validation at boundaries
- Use stdlib logging throughout command entrypoints and internal services (Rich console + Logfire sink)

## 4) Database and migrations

Supabase is schema source for this project.

Migration workflow:
1. Create/update migration SQL under `supabase/migrations/`
2. Validate locally:
   - `supabase db reset`
   - `supabase db lint`
3. Push to hosted only after local success:
   - `supabase db push`

Current initial schema migration:
- `supabase/migrations/20260213152733_init_duplicate_canonicalization_schema.sql`

### Supabase connectivity guidance

- Prefer the **Supabase Transaction Pooler (Shared Pooler)** for CLI/runtime DB access.
- This is ideal for stateless, short-lived interactions and is IPv4 compatible.
- Transaction pooler mode does **not** support server-side prepared statements.
- For psycopg, keep prepare disabled (use `prepare_threshold=None`).
- Do not hardcode connection strings in repo files; read DSNs from env/config only.

## 5) Logging and observability requirements

Use stdlib logging everywhere with consistent key-value fields (Rich console + Logfire sink).

Minimum fields where applicable:
- `run_id`, `command`, `repo`, `type`, `stage`, `item_id`, `status`, `duration_ms`, `error_class`

Artifacts/debug outputs:
- Emit failure/debug artifact payloads to Logfire (no local failure-artifact file writes)
- Keep `.local/artifacts/` for operator-directed outputs (for example `detect-new --json-out ...`)

## 6) Development sequencing (high-level)

Implement in this order unless explicitly reprioritized:
1. bootstrap
2. migrations
3. sync/refresh
4. embed
5. candidates
6. judge
7. canonicalize
8. plan-close
9. apply-close
10. evaluation/hardening

Never implement `apply-close` before guardrails and planning exist.

## 7) Safety/guardrails to preserve

- Maintainer protection on close actions
- Assignee protection on close actions
- Skip uncertain maintainer identity cases
- Require open canonical if any open item exists in cluster
- Judge acceptance guardrails: target must be open; selected candidate score gap vs best alternative must be >= 0.015
- Close comment template (v1):
  - `Closing as duplicate of #{}. If this is incorrect, please contact us.`

## 8) Definition of done for code changes

Before considering work complete:
- `uv run ruff check`
- `uv run pyright`
- `uv run pytest`
- Update docs/spec if behavior or defaults changed
- Keep changes minimal and aligned with v1 scope (no overengineering)

## 9) Scope constraints

- Single repo per run in v1
- No reopen/remediation automation in v1
- No multi-repo orchestration in v1
