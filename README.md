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
- `dupcanon report-audit`
- `dupcanon detect-new`
- `dupcanon canonicalize`
- `dupcanon maintainers`
- `dupcanon plan-close`
- `dupcanon apply-close`

## Key current behavior

- Apply gate is: reviewed persisted `close_run` + `--yes`.
- There is **no approval-file / approve-plan flow**.
- `refresh` defaults to discovering new items only; use `refresh --refresh-known` to also update known-item metadata.
- Operational candidate retrieval defaults to open items (`candidates --include open`) with default clustering `k=4`.
- Judge rejects duplicate targets that are not open (`veto_reason=target_not_open`).
- `detect-new` uses extra precision guardrails and may downgrade high-confidence model duplicates to `maybe_duplicate` when structural/retrieval support is weak.
- Accepted duplicate decisions require a minimum candidate-score gap vs the best alternate candidate (default gap: `0.015`), reducing near-tie false positives.
- Judge prompt/parse/veto/runtime logic is centralized in `src/dupcanon/judge_runtime.py` and reused by `judge`, `judge-audit`, and `detect-new`.
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
- optional Logfire remote sink:
  - `LOGFIRE_TOKEN` (send logs to Logfire project)

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

Judge-audit model resolution follows the same pattern independently for cheap and strong models:
- cheap path uses `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER` / `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`
- strong path uses `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER` / `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`
- judge-audit env defaults (optional):
  - `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER`, `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`, `DUPCANON_JUDGE_AUDIT_CHEAP_THINKING`
  - `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER`, `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`, `DUPCANON_JUDGE_AUDIT_STRONG_THINKING`
- keep `DUPCANON_EMBEDDING_DIM=3072`

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
# print a prior audit report without re-running models:
# uv run dupcanon report-audit --run-id 4 --disagreements-limit 30
# simulate non-LLM gates on stored audit rows:
# uv run dupcanon report-audit --run-id 4 --simulate-gates --gate-rank-max 3 --gate-score-min 0.88 --gate-gap-min 0.02
# sweep gap gate for tuning:
# uv run dupcanon report-audit --run-id 4 --simulate-sweep gap --sweep-from 0.00 --sweep-to 0.04 --sweep-step 0.005
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
  - flags: `--cheap-provider`, `--cheap-model`, `--cheap-thinking`, `--strong-provider`, `--strong-model`, `--strong-thinking`, `--show-disagreements/--no-show-disagreements`, `--disagreements-limit`
  - env defaults:
    - `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER`, `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`, `DUPCANON_JUDGE_AUDIT_CHEAP_THINKING`
    - `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER`, `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`, `DUPCANON_JUDGE_AUDIT_STRONG_THINKING`
- `report-audit`
  - flags: `--run-id`, `--show-disagreements/--no-show-disagreements`, `--disagreements-limit`, `--simulate-gates`, `--gate-rank-max`, `--gate-score-min`, `--gate-gap-min`, `--simulate-sweep gap`, `--sweep-from`, `--sweep-to`, `--sweep-step`
  - uses persisted `judge_audit_runs` + `judge_audit_run_items` data only (no LLM calls)

Thinking values: `off|minimal|low|medium|high|xhigh`.
Gemini paths reject `xhigh`.

## Logfire behavior (token / no-token)

`dupcanon` configures Logfire with `send_to_logfire="if-token-present"`.

- If `LOGFIRE_TOKEN` is present:
  - standard logging events are sent to Logfire
  - local console output still uses Rich formatting
- If no `LOGFIRE_TOKEN` is present:
  - nothing is sent to Logfire
  - console logging works exactly as normal

Current wiring:
- console: `RichHandler`
- remote sink: `logfire.LogfireLoggingHandler`
- artifact payloads are logged with full payload for online searchability.
- local failure-artifact files are not written (Logfire-only artifact policy).

## Runtime reliability defaults

- Retry behavior for GitHub + LLM/embedding HTTP status handling is centralized in `src/dupcanon/llm_retry.py`.
- Retryable statuses: `429`, `5xx`, and network/unknown status paths (`None`).
- Backoff: exponential with jitter (`1, 2, 4, ...` seconds, capped at ~30s + jitter).
- Client defaults:
  - GitHub + most model clients: `max_attempts=5`
  - openai-codex (`pi` RPC): `max_attempts=3`
- Client constructors validate critical runtime inputs (`max_attempts > 0`, provider-specific dimension/timeout constraints).

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
uv run dupcanon report-audit --help
uv run dupcanon detect-new --help

# env-backed settings
rg "validation_alias=\"DUPCANON_" src/dupcanon/config.py

# provider/model/thinking resolution + guardrails
rg "def default_judge_model|validate_thinking_for_provider|require_judge_api_key" src/dupcanon/judge_providers.py

# shared judge runtime used by judge + judge-audit + detect-new
rg "SYSTEM_PROMPT|def get_thread_local_judge_client|def build_user_prompt|def parse_judge_decision" src/dupcanon/judge_runtime.py

# retry + validation primitives shared across clients
rg "def should_retry_http_status|def retry_delay_seconds|def validate_max_attempts" src/dupcanon/llm_retry.py

# logging + remote sink wiring
rg "LogfireLoggingHandler|send_to_logfire|LOGFIRE_" src/dupcanon/logging_config.py .env.example

# workflow online vars (shadow mode)
rg "DUPCANON_ONLINE_" .github/workflows/detect-new-shadow.yml
```

## Docs

- One-pager (quick overview): `docs/one-pager.mdx`
- Batch design/spec + journal: `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`
- Online detection design/spec + journal: `docs/internal/online_duplicate_detection_pipeline_design_doc_v1.md`
- Operator runbook: `docs/internal/operator_runbook_v1.md`
- Full architecture review: `docs/internal/full_architecture_review.md`
- Agent operating guide: `AGENTS.md`
