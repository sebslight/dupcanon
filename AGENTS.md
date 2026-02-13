# AGENTS.md

Project operating guide for coding agents working in this repository.

## 1) Mission and source of truth

We are building a **human-operated duplicate canonicalization CLI** for GitHub issues/PRs.

Primary spec:
- `docs/duplicate_triage_cli_python_spec_design_doc_v_1.md`

When implementing behavior, schema, or defaults, follow the spec above first.

## 2) Locked v1 decisions (do not drift)

- Embeddings: Gemini API `gemini-embedding-001`
- Embedding dimension: `768` (pgvector column is `vector(768)`)
- Judge model: Gemini API `gemini-2.5-flash`
- Retrieval defaults: `k=8`, `min_score=0.75`
- Thresholds: `min_edge=0.85`, `min_close=0.90`
- Input content for modeling: title + body only (no comments)
- Edge policy: first accepted edge wins unless explicit `--rejudge`
- Canonical preference: if an eligible maintainer-authored item exists in a cluster, prefer it
- No manual override system in v1
- Apply gate: requires reviewed plan + approval file + `--yes`
- Precision gate before production apply: `>= 0.90` on at least 100 labeled proposed closes

## 3) Required Python/tooling stack

- Package/env management: **uv**
- CLI framework: **Typer**
- Terminal UX and progress bars: **Rich**
- Logging: **Rich logging** (`rich.logging.RichHandler`) with consistent key-value fields
- Data/config models and validation: **Pydantic**
- Lint/format: **ruff**
- Type checking: **pyright**
- Tests: **pytest**

### Hard rules

- Do **not** use system `python`/`python3`; use `uv run ...`
- Do **not** use `tqdm` in v1; use Rich progress APIs
- Use **Pydantic whenever possible** for settings, request/response contracts, and validation at boundaries
- Use Rich-based logging throughout command entrypoints and internal services

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

Use Rich logger everywhere with consistent key-value fields.

Minimum fields where applicable:
- `run_id`, `command`, `repo`, `type`, `stage`, `item_id`, `status`, `duration_ms`, `error_class`

Artifacts/debug outputs:
- Store under `.local/artifacts/`

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

Never implement `apply-close` before guardrails, planning, and approval-file verification exist.

## 7) Safety/guardrails to preserve

- Maintainer protection on close actions
- Assignee protection on close actions
- Skip uncertain maintainer identity cases
- Require open canonical if any open item exists in cluster
- Close comment template (v1):
  - `Closing as duplicate of {}. If this is incorrect, please contact us.`

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
