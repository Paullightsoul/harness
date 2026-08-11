---
date: 2026-08-06T07:41:00+00:00
source: dialog
project: 1.harness-v4
---

# Remaining gaps: PLAN isolation + ZY phpunit dogfood

## Решения
- Enforce per-run plan SoT by default; stop `_save_plan_to_project` from clobbering shared PLAN when run-roots ON.
- Snapshot plan into exclusive run root before ingest on `tasktool start`.
- Dogfood ProfileGate against real ZY phpunit via `s1_phpunit.sh` filter → attested ledger.

## Что сделано
- `harness/tenant/run_layout.py` — snapshot/write helpers, `point_latest_symlink`, `shared_plan_writes_enabled`
- `harness/tasks_io/parser.py` — `resolve_staging_plan_root`; concrete run roots short-circuit
- `harness/ingest.py` — optional pre-allocated `run_id`
- `harness/tasktool/controller.py` — freeze-before-ingest start path
- `harness/interface/cli.py` — isolated plan save
- `harness/doctor/diagnose.py` — multi-tenant defaults/notes
- `tests/test_isolation_v4.py` — concurrent start/save tests
- `scripts/dogfood_zy_phpunit_gate.py`
- Docs: GROK-DEFAULTS, .env.example; ZY cutover checklist + REMAINING-DONE report

## Итерации
- Isolation test worktree/lease interactions; staging vs latest priority for second start.
- PHP PATH vs s1 wrapper for dogfood.

## Диалог
Closed remaining A/B/C/D from user «делай»; prod `1.harness` and human `brain/` untouched.
