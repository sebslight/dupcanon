# Adoption Readiness Notes (Peter Feedback)

## Context

Peter’s concerns are clear and valid:

- adoption
- maintenance / gardening
- proof of effectiveness
- legal clarity

## What he is actually asking for

Not just “write docs” — confidence on:

1. Can we trust this technically? (architecture + design)
2. Does it work better than existing tools? (measured comparison)
3. Who will maintain it? (gardening/ownership model)
4. Can we legally adopt it? (license)
5. What are the boundaries? (what it detects, what it does **not** do)

---

## Must-have docs (priority order)

### 0) LICENSE (blocker)

Without a clear license, adoption is effectively blocked for many teams.

### 1) One-pager (for async readers)

Current location:
- `docs/index.mdx`

Keep it very short:
- problem
- why existing tools hurt
- what dupcanon does differently
- current status
- links to deeper docs

### 2) `docs/ARCHITECTURE.md`

- clean diagram + data flow
- `sync → embed → candidates → judge → canonicalize → plan-close → apply-close`
- where deterministic gates apply
- where state is persisted

### 3) `docs/REQUIREMENTS_AND_NON_GOALS.md`

Capture requirement intent clearly:
- what v1 must do
- what v1 explicitly won’t do
- safety constraints

### 4) `docs/EVALUATION.md`

Core trust artifact:
- what it detects
- metrics: precision/recall/conflict/false-close risk
- sample size + labeling method
- current numbers (even if imperfect)
- known failure modes

### 5) `docs/COMPARISON.md`

Compare against simili-bot and alternatives:
- feature matrix
- statefulness / canonical stability
- reproducibility
- safety gates
- where each tool wins

### 6) `docs/MAINTENANCE_AND_GOVERNANCE.md`

Answer the gardening concern:
- owner(s)
- issue triage cadence
- release cadence
- supported environments
- migration policy
- maintainer handoff / continuity plan

### 7) `docs/MODEL_POLICY.md`

Address model concerns explicitly:
- supported providers
- default model rationale (cost/latency)
- quality profiles (cheap/default/strong)
- when stronger lanes are required (e.g., judge-audit)

### 8) `docs/EXAMPLE_FIRST.md`

Concrete examples first (per user feedback):
- 2–3 issue examples
- input, decision, canonical chosen, close plan outcome
- at least one “high-confidence model guess blocked by deterministic gate” example

---

## Why this matters

This set directly answers the three biggest adoption blockers:

1. **Trust** (architecture + measured outcomes)
2. **Safety** (boundaries + deterministic gates)
3. **Sustainability** (ownership + maintenance model)
