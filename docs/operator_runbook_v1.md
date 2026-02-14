# Operator Runbook (v1)

This runbook describes the current end-to-end workflow for running `dupcanon` safely.

## Prerequisites

- `uv` installed
- `gh` authenticated for the target repo
- Supabase/Postgres reachable via DSN (`SUPABASE_DB_URL`)
- API key for selected judge provider:
  - `GEMINI_API_KEY` (default)
  - `OPENAI_API_KEY` (when using `--provider openai`)
  - `OPENROUTER_API_KEY` (when using `--provider openrouter`)
  - `--provider openai-codex` uses local `pi` CLI RPC (`pi --mode rpc --provider openai-codex`)

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

Optional metadata refresh for known items:

```bash
uv run dupcanon refresh --repo <org/repo> --known-only
```

### 2) Embeddings

```bash
uv run dupcanon embed --repo <org/repo> --type issue --only-changed
```

### 3) Candidate retrieval

```bash
uv run dupcanon candidates --repo <org/repo> --type issue --k 8 --min-score 0.75 --include open
```

Notes:
- `--include` now defaults to `open` for operational runs.
- Prefer rebuilding candidate sets with `--include open` before judging older repos.

### 4) LLM judge

Gemini default:

```bash
uv run dupcanon judge --repo <org/repo> --type issue
```

OpenAI override example:

```bash
uv run dupcanon judge --repo <org/repo> --type issue --provider openai --model gpt-5-mini
```

OpenRouter override example:

```bash
uv run dupcanon judge --repo <org/repo> --type issue --provider openrouter --model minimax/minimax-m2.5
```

OpenAI Codex via `pi` RPC:

```bash
uv run dupcanon judge --repo <org/repo> --type issue --provider openai-codex --model gpt-5.1-mini-codex
```

Judge guardrail:
- Duplicate targets that are not open are rejected (`veto_reason=target_not_open`).

### 4b) Judge audit (sampled cheap-vs-strong, optional)

Run a sampled audit over latest fresh candidate sets for open items only:

```bash
uv run dupcanon judge-audit \
  --repo <org/repo> \
  --type issue \
  --sample-size 100 \
  --seed 42 \
  --min-edge 0.85 \
  --cheap-provider gemini --cheap-model gemini-3-flash-preview \
  --strong-provider openai --strong-model gpt-5-mini \
  --workers 4
```

Troubleshooting long-running `openai-codex` audits:
- Add `--debug-rpc` to print raw `pi --mode rpc` stdout/stderr events.
- Add `--verbose` for per-item audit logs.

This command writes audit rows to `judge_audit_runs` and `judge_audit_run_items` and prints:
- confusion matrix counts (`tp`, `fp`, `fn`, `tn`)
- `conflict` count (both accepted but different duplicate target)
- `incomplete` rows (skipped/invalid model outputs)

### 5) Canonical stats (optional but recommended)

```bash
uv run dupcanon canonicalize --repo <org/repo> --type issue
```

### 6) Plan close actions

Dry-run first:

```bash
uv run dupcanon plan-close --repo <org/repo> --type issue --min-close 0.90 --dry-run
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

Manual one-off check for a newly opened item:

```bash
uv run dupcanon detect-new --repo <org/repo> --type issue --number <n>
```

PR example (includes changed-file + bounded patch excerpt context in judge prompt):

```bash
uv run dupcanon detect-new --repo <org/repo> --type pr --number <n>
```

Write workflow-friendly JSON output file:

```bash
uv run dupcanon detect-new --repo <org/repo> --type issue --number <n> --json-out .local/artifacts/detect-new.json
```

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
  - `DUPCANON_ONLINE_PROVIDER`, `DUPCANON_ONLINE_MODEL`
  - `DUPCANON_ONLINE_K`, `DUPCANON_ONLINE_MIN_SCORE`
  - `DUPCANON_ONLINE_MAYBE_THRESHOLD`, `DUPCANON_ONLINE_DUPLICATE_THRESHOLD`

## Guardrails to remember

- Maintainer-author and maintainer-assignee protections are applied during `plan-close`.
- `plan-close` requires a **direct accepted edge** from source -> chosen canonical with confidence `>= min_close`.
  - Accepted edges are now read from `judge_decisions` rows where `final_status='accepted'`.
- `apply-close` only accepts `close_run.mode = plan`.
- There is no approval-file workflow in current v1.
- Judge uncertainty handling:
  - If model returns `certainty="unsure"` for a duplicate claim, the decision is rejected (veto).
  - Follow-up/partial-overlap/scope-mismatch decisions are vetoed from acceptance.

## Artifacts and troubleshooting

- Failure artifacts are written to `.local/artifacts/`.
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
