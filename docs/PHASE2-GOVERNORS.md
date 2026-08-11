# Phase 2 governors (Judge seats + budgets)

Solo-safe defaults keep aggressive governors (**economics / precall / meta**) **off**.
Honesty (`HARNESS_EVIDENCE_GATE`) defaults **ON** in v4 — see `docs/HONESTY-MODE.md`.
Loop-detect stays independent (default on).

## Enable

```bash
export HARNESS_HOME=/home/1.harness-v4
cd /home/1.harness-v4 && . .venv/bin/activate

# Honesty (Phase 1.5) — ON by default; set 0 only as emergency escape:
export HARNESS_EVIDENCE_GATE=1
# Semantic AC↔evidence 1:1 bind (default ON):
export HARNESS_EVIDENCE_EXCLUSIVE_BIND=1
export HARNESS_REQUIRE_NESTED_WORKERS=1
# Cosplay hard-fail (default ON; set 0 for warn-only emergency):
export HARNESS_NESTED_WORKERS_HARD_FAIL=1

# Pre-call budgets (pause next/advance when predicates hit):
export HARNESS_PRECALL_BUDGET=1
export HARNESS_MAX_STEPS=200
export HARNESS_MAX_WALL_CLOCK_SEC=14400   # 4h
# export HARNESS_MAX_TOKENS_EST=500000

# Loop / tool-hash stall (default on):
export HARNESS_LOOP_DETECT=1
export HARNESS_TOOL_HASH_REPEAT=3

# Sprint-contract before coding for medium/large (default off):
export HARNESS_SPRINT_CONTRACT=1

# Observation masking cap inside ContextPack feedback:
export HARNESS_OBSERVATION_MASK_CHARS=2400
```

## Behavior

| Feature | Flag | Effect |
|---|---|---|
| Judge APPROVE | `HARNESS_EVIDENCE_GATE=1` | APPROVE is **no-op** when evidence red; stays in REVIEW; emits `JUDGE_APPROVE_BLOCKED`. Judge cannot flip acceptance. |
| Cosplay hard-fail | `HARNESS_NESTED_WORKERS_HARD_FAIL=1` | Medium+ same-agent / missing nested workers → refuse DONE (`DONE_REFUSED`); escape `=0` warn-only |
| Pre-call budgets | `HARNESS_PRECALL_BUDGET=1` | `next` / `advance` refuse with `phase=budget_exhausted` + `BUDGET_PREDICATE_HIT` |
| Tool-hash loop | `HARNESS_LOOP_DETECT=1` | Repeated fingerprints → `LOOP_STUCK` + `phase=stuck` |
| Sprint-contract | `HARNESS_SPRINT_CONTRACT=1` | medium/large/high get `sprint-contract` stage; freezes AC hash before worker |
| Phase UX | always | `status` / dashboard show `phase` + `stall_reason` |

Phases: `researching` | `coding` | `testing` | `stuck` | `waiting-on-human` | `budget_exhausted` | `idle` | `done`.

## CLI

```bash
harness status --markdown
# includes ## Phase (governor UX) with phase + stall_reason
```
