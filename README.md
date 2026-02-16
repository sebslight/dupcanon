# dupcanon

Human-operated duplicate canonicalization CLI for GitHub issues/PRs.

## What it does

`dupcanon` is a DB-first duplicate canonicalization system with two coordinated paths:

### Batch canonicalization path (many items)

1. `sync` / `refresh` GitHub items into Postgres
2. `embed` title/body content into pgvector
3. `candidates` retrieve nearest neighbors per item
4. `judge` duplicate candidates with an LLM
5. `canonicalize` compute canonical representatives per cluster
6. `plan-close` produce auditable close plans with guardrails
7. `apply-close` execute reviewed close plans

### Online path (single new item)

- `detect-new` classifies one issue/PR as `duplicate`, `maybe_duplicate`, or `not_duplicate`
- emits structured JSON for workflow integration
- stays precision-first with deterministic downgrade guardrails

Use commands as `uv run dupcanon ...` (or `dupcanon ...` if your venv is activated).

## Documentation map

Public docs (Mintlify):
- Overview: `docs/index.mdx`
- Get started: `docs/get-started.mdx`
- Architecture: `docs/architecture.mdx`
- Evaluation flow (issues + PRs): `docs/evaluation.mdx`

Internal deep docs:
- Batch design/spec + journal: `docs/internal/duplicate_triage_cli_python_spec_design_doc_v_1.md`
- Online detection design/spec + journal: `docs/internal/online_duplicate_detection_pipeline_design_doc_v1.md`
- Operator runbook: `docs/internal/operator_runbook_v1.md`
- Full architecture review: `docs/internal/full_architecture_review.md`

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

- Batch pipeline is DB-first and auditable (`items`, `embeddings`, `candidate_sets`, `judge_decisions`, `close_runs`).
- Apply gate is: reviewed persisted `close_run(mode=plan)` + explicit `--yes`.
- There is **no approval-file / approve-plan flow** in v1.
- `refresh` defaults to discovering new items only; use `refresh --refresh-known` to also update known-item metadata.
- Operational candidate retrieval defaults to open items (`candidates --include open`) with default clustering `k=4`, `min_score=0.75`.
- Judge acceptance defaults: `min_edge=0.85`, target must be open, and selected candidate score gap vs best alternate must be `>= 0.015`.
- `plan-close` requires a **direct accepted edge** to canonical and `min_close=0.90`, plus maintainer author/assignee protections.
- `detect-new` is a precision-first online classifier (`duplicate` / `maybe_duplicate` / `not_duplicate`) with stricter duplicate thresholds and downgrade guardrails.
- In v1, `detect-new` persists source/corpus state (`items`, source `embeddings` when stale) but does not persist online judge outcomes to `judge_decisions`.
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
# batch freshness + duplicate edge pipeline
uv run dupcanon sync --repo openclaw/openclaw --since 3d
uv run dupcanon refresh --repo openclaw/openclaw --refresh-known
uv run dupcanon embed --repo openclaw/openclaw --type issue --only-changed
uv run dupcanon candidates --repo openclaw/openclaw --type issue --include open
uv run dupcanon judge --repo openclaw/openclaw --type issue --thinking medium
uv run dupcanon canonicalize --repo openclaw/openclaw --type issue

# safe close planning/apply
uv run dupcanon plan-close --repo openclaw/openclaw --type issue --dry-run
# persist a reviewable plan when ready:
# uv run dupcanon plan-close --repo openclaw/openclaw --type issue
# then apply only after review:
# uv run dupcanon apply-close --close-run <id> --yes

# online single-item path (shadow/suggest)
uv run dupcanon detect-new --repo openclaw/openclaw --type issue --number 123 --thinking low \
  --json-out .local/artifacts/detect-new-123.json

# optional sampled cheap-vs-strong audit
# uv run dupcanon judge-audit --repo openclaw/openclaw --type issue --sample-size 100 --seed 42 \
#   --cheap-provider gemini --cheap-thinking low --strong-provider openai --strong-thinking high --workers 4
# uv run dupcanon report-audit --run-id 4 --simulate-gates --gate-gap-min 0.02
```

## GitHub Actions online shadow workflow

Current workflow file:
- `.github/workflows/detect-new-shadow.yml`

It triggers on:
- `issues.opened`
- `pull_request.opened`

It runs `dupcanon detect-new` and uploads JSON artifacts (no mutation).

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

## Additional references

- Agent operating guide: `AGENTS.md`
- Public docs navigation config: `docs/docs.json`
