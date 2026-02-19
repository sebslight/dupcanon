# Operator Runbook (v1)

This runbook describes the current end-to-end workflow for running `dupcanon` safely.

## Prerequisites

- `uv` installed
- `gh` authenticated for the target repo
- Supabase/Postgres reachable via DSN (`SUPABASE_DB_URL`)
- Credentials/runtime for selected embedding/judge providers:
  - Default embeddings use OpenAI (`DUPCANON_EMBEDDING_PROVIDER=openai`) -> requires `OPENAI_API_KEY`
  - Default judge uses OpenAI Codex via `pi` RPC (`DUPCANON_JUDGE_PROVIDER=openai-codex`)
  - Optional global judge thinking default: `DUPCANON_JUDGE_THINKING` (`off|minimal|low|medium|high|xhigh`)
  - Provider/model fallback behavior:
    - `judge` / `detect-new`:
      - if command `--model` is set, it wins
      - else if selected provider matches configured provider, `DUPCANON_JUDGE_MODEL` is used
      - else provider defaults are used (`gemini-3-flash-preview`, `gpt-5-mini`, `minimax/minimax-m2.5`, `gpt-5.1-codex-mini`)
    - `judge-audit` applies the same rule independently to cheap and strong lanes using:
      - `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER` / `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`
      - `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER` / `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`
  - Optional judge-audit defaults:
    - `DUPCANON_JUDGE_AUDIT_CHEAP_PROVIDER`, `DUPCANON_JUDGE_AUDIT_CHEAP_MODEL`, `DUPCANON_JUDGE_AUDIT_CHEAP_THINKING`
    - `DUPCANON_JUDGE_AUDIT_STRONG_PROVIDER`, `DUPCANON_JUDGE_AUDIT_STRONG_MODEL`, `DUPCANON_JUDGE_AUDIT_STRONG_THINKING`
  - `GEMINI_API_KEY` only when embedding/judge provider is `gemini`
  - `OPENROUTER_API_KEY` only when judge provider is `openrouter`
- Optional Logfire sink:
  - `LOGFIRE_TOKEN`

## LLM control mapping (verifiable)

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
  - reads persisted audit tables only (no model calls)

## Setup

```bash
uv sync
cp .env.example .env
# edit .env
uv run dupcanon init
```

## Standard pipeline

### 1) Sync source items

```bash
uv run dupcanon sync --repo <org/repo> --since 3d
```

Optional incremental discovery refresh (new items only):

```bash
uv run dupcanon refresh --repo <org/repo>
```

Include known-item metadata refresh in the same run:

```bash
uv run dupcanon refresh --repo <org/repo> --refresh-known
```

### 2) Embeddings

```bash
uv run dupcanon embed --repo <org/repo> --type issue --only-changed
```

CLI override example (useful in production cutovers):

```bash
uv run dupcanon embed --repo <org/repo> --type issue --only-changed --provider openai --model text-embedding-3-large
```

Notes:
- Embedding provider can be set via env (`DUPCANON_EMBEDDING_PROVIDER`) or CLI (`embed --provider ...`).
- Keep `DUPCANON_EMBEDDING_DIM=3072` to match current pgvector schema.

### 3) Candidate retrieval

```bash
uv run dupcanon candidates --repo <org/repo> --type issue --k 4 --min-score 0.75 --include open
```

Notes:
- `--include` now defaults to `open` for operational runs.
- Prefer rebuilding candidate sets with `--include open` before judging older repos.

### 4) LLM judge

Default (OpenAI Codex via `pi` RPC):

```bash
uv run dupcanon judge --repo <org/repo> --type issue
```

Judge with explicit thinking level:

```bash
uv run dupcanon judge --repo <org/repo> --type issue --thinking medium
```

Explicit OpenAI Codex model example:

```bash
uv run dupcanon judge --repo <org/repo> --type issue --provider openai-codex --model gpt-5.1-codex-mini --thinking medium
```

OpenAI override example:

```bash
uv run dupcanon judge --repo <org/repo> --type issue --provider openai --model gpt-5-mini
```

OpenRouter override example:

```bash
uv run dupcanon judge --repo <org/repo> --type issue --provider openrouter --model minimax/minimax-m2.5 --thinking low
```

Thinking levels:
- `off`, `minimal`, `low`, `medium`, `high`, `xhigh`
- `xhigh` is rejected for Gemini provider paths.

Judge guardrails:
- Duplicate targets that are not open are rejected (`veto_reason=target_not_open`).
- Accepted duplicates must clear a minimum selected-candidate score gap vs the best alternate candidate (default `0.015`; veto reason `candidate_gap_too_small`).

### 4b) Judge audit (sampled cheap-vs-strong, optional)

Run a sampled audit over latest fresh candidate sets for open items only, filtered to sets with at least one candidate member:

```bash
uv run dupcanon judge-audit \
  --repo <org/repo> \
  --type issue \
  --sample-size 100 \
  --seed 42 \
  --min-edge 0.85 \
  --cheap-provider gemini --cheap-model gemini-3-flash-preview --cheap-thinking low \
  --strong-provider openai --strong-model gpt-5-mini --strong-thinking high \
  --workers 4
```

Troubleshooting long-running `openai-codex` audits:
- Add `--debug-rpc` to print raw `pi --mode rpc` stdout/stderr events.
- Add `--verbose` for per-item audit logs.

This command writes audit rows to `judge_audit_runs` and `judge_audit_run_items` and prints:
- confusion matrix counts (`tp`, `fp`, `fn`, `tn`)
- `conflict` count (both accepted but different duplicate target)
- `incomplete` rows (skipped/invalid model outputs)
- a disagreement table (`fp`, `fn`, `conflict`, `incomplete`) with source and cheap/strong decisions.

Useful flags:
- `--disagreements-limit N` (default `20`)
- `--no-show-disagreements` to suppress the table

Print a previously completed audit report (without re-running model calls):

```bash
uv run dupcanon report-audit --run-id <audit_run_id>
```

Optional:
- `--disagreements-limit N`
- `--no-show-disagreements`
- `--simulate-gates` with one or more gates:
  - `--gate-rank-max N`
  - `--gate-score-min X`
  - `--gate-gap-min X`
- `--simulate-sweep gap --sweep-from A --sweep-to B --sweep-step C`

### 5) Canonical stats (optional but recommended)

```bash
uv run dupcanon canonicalize --repo <org/repo> --type issue
```

### 6) Plan close actions

Dry-run first:

```bash
uv run dupcanon plan-close --repo <org/repo> --type issue --min-close 0.90 --dry-run
```

Optional transitive-recovery mode (still review before apply):

```bash
uv run dupcanon plan-close --repo <org/repo> --type issue --min-close 0.90 --target-policy direct-fallback --dry-run
```

Persist reviewed plan:

```bash
uv run dupcanon plan-close --repo <org/repo> --type issue --min-close 0.90
```

This creates a `close_run` (mode=`plan`) and corresponding `close_run_items`.

### 7) Apply reviewed plan

After human review of the persisted plan rows:

```bash
uv run dupcanon apply-close --close-run <plan_run_id> --yes
```

### 8) Online single-item detection (shadow mode)

Manual one-off check for a newly opened item (default source is `intent`):

```bash
uv run dupcanon detect-new --repo <org/repo> --type issue --number <n> --thinking low
```

Use `--source raw` for rollback/A-B or when intent extraction is unavailable:

```bash
uv run dupcanon detect-new --repo <org/repo> --type issue --number <n> --source raw
```

PR example (includes changed-file + bounded patch excerpt context in judge prompt):

```bash
uv run dupcanon detect-new --repo <org/repo> --type pr --number <n>
```

Write workflow-friendly JSON output file:

```bash
uv run dupcanon detect-new --repo <org/repo> --type issue --number <n> --json-out .local/artifacts/detect-new.json
```

Online decision notes (current behavior)
- `duplicate` is intentionally strict.
- High-confidence model duplicate outputs can still be downgraded to `maybe_duplicate` when:
  - strict duplicate guardrails are not met (`reason=online_strict_guardrail:*`), or
  - retrieval support is below the duplicate floor (`reason=duplicate_low_retrieval_support`).

GitHub Actions shadow workflow
- File: `.github/workflows/detect-new-shadow.yml`
- Triggers on:
  - `issues.opened`
  - `pull_request.opened`
- Requires secrets:
  - `SUPABASE_DB_URL`
  - provider key matching configured provider (`GEMINI_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY`)
  - for `openai-codex`, ensure `pi` CLI is available in the runner environment
- Uses repository variables for tuning (optional):
  - `DUPCANON_ONLINE_PROVIDER` (workflow default: `gemini`)
  - `DUPCANON_ONLINE_MODEL` (default: empty)
  - `DUPCANON_ONLINE_THINKING` (default: empty)
  - `DUPCANON_ONLINE_K`, `DUPCANON_ONLINE_MIN_SCORE`
  - `DUPCANON_ONLINE_MAYBE_THRESHOLD`, `DUPCANON_ONLINE_DUPLICATE_THRESHOLD`
- `DUPCANON_ONLINE_THINKING` accepts: `off|minimal|low|medium|high|xhigh`
  - For provider=`gemini`, `xhigh` is rejected by the app.

## Guardrails to remember

- Maintainer-author and maintainer-assignee protections are applied during `plan-close`.
- `plan-close` default policy (`--target-policy canonical-only`) requires a **direct accepted edge** from source -> chosen canonical with confidence `>= min_close`.
  - Optional `--target-policy direct-fallback` allows source -> direct-accepted-target when source -> canonical is missing.
  - Accepted edges are read from `judge_decisions` rows where `final_status='accepted'`.
- `apply-close` only accepts `close_run.mode = plan`.
- There is no approval-file workflow in current v1.
- Judge uncertainty handling:
  - If model returns `certainty="unsure"` for a duplicate claim, the decision is rejected (veto).
  - Follow-up/partial-overlap/scope-mismatch decisions are vetoed from acceptance.

## Runtime reliability notes

- Shared retry/backoff/validation primitives live in `src/dupcanon/llm_retry.py`.
- Retryable statuses are `429` and `5xx` (plus unknown/network paths).
- Backoff is exponential with jitter and a ~30s cap.
- Default attempts are client-specific:
  - GitHub + most model clients: `max_attempts=5`
  - openai-codex (`pi` RPC): `max_attempts=3`
- Logging pipeline:
  - console stays Rich-formatted
  - stdlib logging events are forwarded to Logfire via `logfire.LogfireLoggingHandler`
  - Logfire is configured with `send_to_logfire="if-token-present"`
    - with `LOGFIRE_TOKEN` present: events are sent online
    - without token: no remote send; console logging still works
  - artifact events are logged to Logfire with full payload for online searchability
- local failure-artifact files are not written (Logfire-only artifact policy)

## Verification checklist (docs vs code)

```bash
# command surface / flags
uv run dupcanon --help
uv run dupcanon judge --help
uv run dupcanon judge-audit --help
uv run dupcanon report-audit --help
uv run dupcanon detect-new --help

# env settings source of truth
rg "validation_alias=\"DUPCANON_" src/dupcanon/config.py

# provider/model/thinking resolution logic
rg "def default_judge_model|validate_thinking_for_provider|require_judge_api_key" src/dupcanon/judge_providers.py

# shared judge runtime path used by judge/audit/detect-new
rg "SYSTEM_PROMPT|def get_thread_local_judge_client|def build_user_prompt|def parse_judge_decision" src/dupcanon/judge_runtime.py

# retry/backoff defaults and validation helpers
rg "def should_retry_http_status|def retry_delay_seconds|def validate_max_attempts" src/dupcanon/llm_retry.py

# logging + Logfire sink wiring
rg "LogfireLoggingHandler|send_to_logfire|LOGFIRE_" src/dupcanon/logging_config.py .env.example

# online workflow tuning vars
rg "DUPCANON_ONLINE_" .github/workflows/detect-new-shadow.yml
```

## Artifacts and troubleshooting

- Failure artifact payloads are emitted to Logfire (no local failure-artifact files).
- Re-run with a smaller scope (`--since`, `--type`) when debugging.
- Judge persistence now uses `judge_decisions` as the source-of-truth table for outcomes (accepted/rejected/skipped).
- Use quality gates before shipping changes:

```bash
uv run ruff check
uv run pyright
uv run pytest
```

## Quick DB sanity queries

Latest judge outcomes by provider/model:

```sql
select
  llm_provider,
  llm_model,
  final_status,
  count(*)
from judge_decisions
group by llm_provider, llm_model, final_status
order by llm_provider, llm_model, final_status;
```

Accepted edges used by canonicalization/plan-close:

```sql
select from_item_id, to_item_id, confidence, created_at
from judge_decisions
where final_status = 'accepted'
order by created_at desc
limit 100;
```

## Known v1 limitations

- No reopen/remediation automation.
- No first-class Phase 9 evaluation command yet (labeling/precision gate still manual).
- No multi-repo orchestration.
