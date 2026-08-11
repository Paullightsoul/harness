# Phase 4 — Meta (AHE-lite + Skill GC)

Solo-safe: **`HARNESS_META=0`** (CLI always works; flag is dogfood habit /
orch reminder). Thin **cgroup governor is future-only** — see stub below.

## AHE-lite (falsifiable harness edits)

Every meaningful harness change should record a **prediction**, then attach
**actual** metrics from a dogfood run.

```bash
export HARNESS_HOME=/home/1.harness-v4
cd /home/1.harness-v4 && . .venv/bin/activate

# 1) After editing harness policies / honesty / budgets:
harness meta changelog add \
  --change "Enable HARNESS_EVIDENCE_GATE for ZY dogfood" \
  --hypothesis "evidence% will track product truth; stalls stay low" \
  --components evidence,lifecycle \
  --predict-evidence-min 40 \
  --predict-stall-max 3 \
  --predict-tokens-delta-pct -5 \
  --baseline-runs <old-run-id>

# 2) After a real run:
harness meta record --run-id <run_id> --changelog-id <ahe-…> --repo /home/ZY-2nd-flight

# 3) Compare prediction vs outcome:
harness meta eval
harness meta eval --json --write /tmp/ahe-eval.json
harness doctor --meta-eval
```

| Predicted field | Meaning |
|-----------------|---------|
| `evidence_pct_min` | Mean evidence% on verify runs ≥ N |
| `evidence_pct_delta_pp` | Mean evidence% − baseline ≥ N pp |
| `tokens_est_max` | Mean tokens_in+out ≤ N (when recorded) |
| `tokens_est_delta_pct` | % change vs baseline (e.g. `-12` = ≤12% drop) |
| `stall_count_max` | Max stall events (`loop_stuck`, budget, human_gate, resource_*) ≤ N |

Changelog path: `.harness/meta/ahe-changelog.jsonl` under `HARNESS_HOME`.

Verdicts: `supported` | `contradicted` | `inconclusive` | `open`.

## Skill GC

Propose unused / rarely selected catalog skills; **never auto-delete**.
Quarantine flips `status` in `skills/catalog.json` so `select_skills` skips them.

```bash
harness skills gc propose
harness skills gc propose --json --write /tmp/gc.md
# Human review, then:
harness skills gc apply --approve --ids dusty-skill,orphan-skill
harness skills gc list
harness skills gc restore --approve --ids dusty-skill
```

Defaults protect `worker_core` / `reviewer_core` / `orch_core` and skip
`human_only`. Mid-run deletion is forbidden by design (`--approve` required).

## Feature flags

| Flag | Default | Effect |
|------|---------|--------|
| `HARNESS_META` | `0` | Opt-in reminder that changelog+eval is part of dogfood |
| (CLI) | always on | `meta` / `skills gc` / `doctor --meta-eval` work regardless |

## Cgroup governor (future-only stub)

P4.3 from the tech design is **not shipped**. Python pre-call budgets +
TaskTool resource admission remain the governors. Stub:
`harness/meta/cgroup_stub.py` (`cgroup_governor_available() → False`).

Revisit only after measured multi-tenant CPU/RAM runaway that admission cannot
contain.

## Related

- Cutover day-to-day: ZY `harness_reports/HARNESS-V4-CUTOVER-CHECKLIST.md`
- Tech design §14 Phase 4
- Deep research AHE notes: `harness_reports/HARNESS-DEEP-RESEARCH-2026-08-05.md` §2.10
