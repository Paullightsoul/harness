# Grok-first defaults (Harness V4)

**Дата:** 2026-08-05  
**SoT:** `harness/tasktool/model_map.py`  
**Политика:** все TaskTool-роли по умолчанию → **Grok 4.5 High** (`cursor-grok-4.5-high`).  
Env-overrides (`TASKTOOL_*_MODEL`) **не ломаются**.

## Маппинг роль → slug → env

| Роль / alias | Default slug | Env override |
|---|---|---|
| Planner / root / orchestrator | `cursor-grok-4.5-high` | `TASKTOOL_ORCH_MODEL` |
| Worker / leaf / implement / de-sloppify | `cursor-grok-4.5-high` | `TASKTOOL_WORKER_MODEL` |
| Sub-orchestrator / research / researching | `cursor-grok-4.5-high` | `TASKTOOL_WORKER_MODEL` |
| Reviewer / goal-judge | `cursor-grok-4.5-high` | `TASKTOOL_REVIEWER_MODEL` |

Доступные slug'и — `AVAILABLE_TASKTOOL_MODELS` в `model_map.py` (в т.ч. GPT / Claude / Composer / Kimi / GLM).

## Как гонять с Grok defaults

```bash
export HARNESS_HOME=/home/1.harness-v4
cd /home/1.harness-v4 && . .venv/bin/activate

# Можно не задавать TASKTOOL_* — код уже Grok-first.
# Явно (как в .env.example):
export TASKTOOL_ORCH_MODEL=cursor-grok-4.5-high
export TASKTOOL_WORKER_MODEL=cursor-grok-4.5-high
export TASKTOOL_REVIEWER_MODEL=cursor-grok-4.5-high
```

Проверка без запуска задачи:

```bash
python -c "from harness.tasktool.model_map import tasktool_role_models; print(tasktool_role_models({}))"
# → orch/worker/reviewer = cursor-grok-4.5-high
```

## Вернуть GPT (или другую модель) на роль

```bash
export TASKTOOL_ORCH_MODEL=gpt-5.6-sol-medium
export TASKTOOL_REVIEWER_MODEL=gpt-5.6-sol-medium
# worker остаётся Grok, если не трогать TASKTOOL_WORKER_MODEL
```

Невалидный slug → `ValueError` при resolve (fail-fast).

## Honesty / evidence (v4 defaults)

| Flag | Default | Notes |
|---|---|---|
| `HARNESS_EVIDENCE_GATE` | **1 (ON)** | DONE requires sealed acceptance + attested ledger. Escape: `=0`. |
| `HARNESS_ALLOW_SOFT_GATES` | 0 | Soft/optional sensors refused. |
| `HARNESS_USE_RUN_ROOTS` | **1 (ON)** | Per-run `.harness/runs/<id>/` is SoT; `harness plan` does not overwrite shared `PLAN.md`. |
| `HARNESS_MULTI_TENANT` | **1 (ON)** | Lease index; concurrent starts OK with worktrees. |
| `HARNESS_USE_WORKTREES` | **1 (ON)** | Required for multi-active on one checkout (~4 tenants). |
| `HARNESS_BRAIN_ROOT` | `/home/brain-agents` | Never auto-write human `/home/brain`. |
| `HARNESS_EVIDENCE_CLI_MINT` | 0 | Blocks CLI mint of green gate evidence. |

See `docs/HONESTY-MODE.md`. With the three isolation defaults ON, v4 is intended
safe for up to ~4 parallel tenants on distinct goals (shared PLAN is no longer SoT).
