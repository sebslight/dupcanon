# dupcanon

Human-operated duplicate canonicalization CLI for GitHub issues/PRs.

## What it does

`dupcanon` provides a DB-first pipeline:

1. `sync` / `refresh` GitHub items into Postgres
2. `embed` title/body content into pgvector
3. `candidates` retrieve nearest neighbors per item
4. `judge` duplicate candidates with an LLM
5. `canonicalize` compute canonical representatives per cluster
6. `plan-close` produce auditable close plans with guardrails
7. `apply-close` execute reviewed close plans

Use commands as `uv run dupcanon ...` (or `dupcanon ...` if your venv is activated).

## Current command surface

- `dupcanon init`
- `dupcanon sync`
- `dupcanon refresh`
- `dupcanon embed`
- `dupcanon candidates`
- `dupcanon judge`
- `dupcanon judge-audit`
- `dupcanon detect-new`
- `dupcanon canonicalize`
- `dupcanon maintainers`
- `dupcanon plan-close`
- `dupcanon apply-close`

## Key current behavior

- Apply gate is: reviewed persisted `close_run` + `--yes`.
- There is **no approval-file / approve-plan flow**.
- Operational candidate retrieval defaults to open items (`candidates --include open`).
- Judge rejects duplicate targets that are not open (`veto_reason=target_not_open`).
- `detect-new` uses extra precision guardrails and may downgrade high-confidence model duplicates to `maybe_duplicate` when structural/retrieval support is weak.
- Canonical selection priority is:
  1. open if any open item exists
  2. English-language preference (title/body heuristic)
  3. maintainer-authored preference
  4. activity, age, number tie-breakers

## Setup

### 1) Install deps

```bash
uv sync
```

### 2) Configure environment

Copy `.env.example` to `.env` and set at minimum:

- `SUPABASE_DB_URL` (Postgres DSN, not Supabase HTTPS URL)
- `GEMINI_API_KEY` (or `OPENAI_API_KEY` with `--provider openai`, or `OPENROUTER_API_KEY` with `--provider openrouter`; `--provider openai-codex` uses `pi --mode rpc --provider openai-codex` and forwards `--model`, e.g. `gpt-5.1-mini-codex`)
- `GITHUB_TOKEN` (optional if `gh` is already authenticated)

### 3) Validate runtime

```bash
uv run dupcanon init
```

## Typical local run

```bash
uv run dupcanon sync --repo openclaw/openclaw --since 3d
uv run dupcanon embed --repo openclaw/openclaw --type issue --only-changed
uv run dupcanon candidates --repo openclaw/openclaw --type issue --include open
uv run dupcanon judge --repo openclaw/openclaw --type issue
uv run dupcanon judge-audit --repo openclaw/openclaw --type issue --sample-size 100 --seed 42 --cheap-provider gemini --strong-provider openai --workers 4
# debugging openai-codex RPC behavior:
# uv run dupcanon judge-audit ... --cheap-provider openai-codex --strong-provider openai-codex --debug-rpc --verbose
uv run dupcanon detect-new --repo openclaw/openclaw --type issue --number 123
uv run dupcanon plan-close --repo openclaw/openclaw --type issue --dry-run
# review plan output in DB, then:
uv run dupcanon apply-close --close-run <id> --yes
```

## Quality gates

```bash
uv run ruff check
uv run pyright
uv run pytest
```

## Docs

- Batch design/spec + journal: `docs/duplicate_triage_cli_python_spec_design_doc_v_1.md`
- Online detection design/spec + journal: `docs/online_duplicate_detection_pipeline_design_doc_v1.md`
- Operator runbook: `docs/operator_runbook_v1.md`
- Agent operating guide: `AGENTS.md`
