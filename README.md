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
- `refresh` defaults to discovering new items only; use `refresh --refresh-known` to also update known-item metadata.
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
- `OPENAI_API_KEY` (required for default OpenAI embeddings)
- `GEMINI_API_KEY` (required only when embedding/judge provider is `gemini`)
- `OPENROUTER_API_KEY` (required only when judge provider is `openrouter`)
- default judge provider is `openai-codex` via `pi --mode rpc --provider openai-codex` (no API key in env)
- `GITHUB_TOKEN` (optional if `gh` is already authenticated)

Default model stack:
- `DUPCANON_EMBEDDING_PROVIDER=openai`
- `DUPCANON_EMBEDDING_MODEL=text-embedding-3-large`
- `DUPCANON_JUDGE_PROVIDER=openai-codex`
- `DUPCANON_JUDGE_MODEL=gpt-5.1-codex-mini`
- optional thinking default: `DUPCANON_JUDGE_THINKING` (`off|minimal|low|medium|high|xhigh`)

Judge model resolution (verifiable in `src/dupcanon/judge_providers.py`):
- if `--model` is set, it wins
- else if selected provider matches configured provider (`DUPCANON_JUDGE_PROVIDER`), use `DUPCANON_JUDGE_MODEL`
- else use provider defaults:
  - gemini -> `gemini-3-flash-preview`
  - openai -> `gpt-5-mini`
  - openrouter -> `minimax/minimax-m2.5`
  - openai-codex -> `gpt-5.1-codex-mini`
- judge-audit env defaults (optional):
  - `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER`, `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`, `DUPCANON_JUDGE_AUDIT_CHEAP_THINKING`
  - `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER`, `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`, `DUPCANON_JUDGE_AUDIT_STRONG_THINKING`
- keep `DUPCANON_EMBEDDING_DIM=768`

### 3) Validate runtime

```bash
uv run dupcanon init
```

## Typical local run

```bash
uv run dupcanon sync --repo openclaw/openclaw --since 3d
uv run dupcanon embed --repo openclaw/openclaw --type issue --only-changed
# OpenAI embeddings override example:
# uv run dupcanon embed --repo openclaw/openclaw --type issue --only-changed --provider openai --model text-embedding-3-large
uv run dupcanon candidates --repo openclaw/openclaw --type issue --include open
uv run dupcanon judge --repo openclaw/openclaw --type issue --thinking medium
uv run dupcanon judge-audit --repo openclaw/openclaw --type issue --sample-size 100 --seed 42 --cheap-provider gemini --cheap-thinking low --strong-provider openai --strong-thinking high --workers 4
# debugging openai-codex RPC behavior:
# uv run dupcanon judge-audit ... --cheap-provider openai-codex --strong-provider openai-codex --cheap-thinking medium --strong-thinking medium --debug-rpc --verbose
uv run dupcanon detect-new --repo openclaw/openclaw --type issue --number 123 --thinking low
uv run dupcanon plan-close --repo openclaw/openclaw --type issue --dry-run
# review plan output in DB, then:
uv run dupcanon apply-close --close-run <id> --yes
```

## LLM controls matrix (CLI flags + env defaults)

All LLM-using commands support explicit CLI flags and env-backed defaults.

- `judge`
  - flags: `--provider`, `--model`, `--thinking`
  - env defaults: `DUPCANON_JUDGE_PROVIDER`, `DUPCANON_JUDGE_MODEL`, `DUPCANON_JUDGE_THINKING`
- `detect-new`
  - flags: `--provider`, `--model`, `--thinking`
  - env defaults: `DUPCANON_JUDGE_PROVIDER`, `DUPCANON_JUDGE_MODEL`, `DUPCANON_JUDGE_THINKING`
- `judge-audit`
  - flags: `--cheap-provider`, `--cheap-model`, `--cheap-thinking`, `--strong-provider`, `--strong-model`, `--strong-thinking`
  - env defaults:
    - `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER`, `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`, `DUPCANON_JUDGE_AUDIT_CHEAP_THINKING`
    - `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER`, `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`, `DUPCANON_JUDGE_AUDIT_STRONG_THINKING`

Thinking values: `off|minimal|low|medium|high|xhigh`.
Gemini paths reject `xhigh`.

## Quality gates

```bash
uv run ruff check
uv run pyright
uv run pytest
```

## Quick verification against code

```bash
# command flags
uv run dupcanon judge --help
uv run dupcanon judge-audit --help
uv run dupcanon detect-new --help

# env-backed settings
rg "validation_alias=\"DUPCANON_" src/dupcanon/config.py

# provider/model/thinking resolution + guardrails
rg "def default_judge_model|validate_thinking_for_provider|require_judge_api_key" src/dupcanon/judge_providers.py

# workflow online vars (shadow mode)
rg "DUPCANON_ONLINE_" .github/workflows/detect-new-shadow.yml
```

## Docs

- Batch design/spec + journal: `docs/duplicate_triage_cli_python_spec_design_doc_v_1.md`
- Online detection design/spec + journal: `docs/online_duplicate_detection_pipeline_design_doc_v1.md`
- Operator runbook: `docs/operator_runbook_v1.md`
- Agent operating guide: `AGENTS.md`
