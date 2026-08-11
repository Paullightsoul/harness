---
date: 2026-08-06T10:18:54+00:00
source: dialog
project: 1.harness-v4
---

# Cosplay hard-fail default ON

## Решения
- `HARNESS_NESTED_WORKERS_HARD_FAIL` default 0→1: medium+ cosplay блокирует DONE; escape `=0` для emergency warn-only.

## Что сделано
- `harness/evidence/binding.py`, `harness/config.py` — default `"1"`
- docs: HONESTY-MODE, PHASE2-GOVERNORS, `.env.example`; semantic-fix residual обновлён
- tests: default ON + DONE blocked + escape allows DONE (`test_worker_boundary_v4.py` 7 green)
- ZY notes: `HARNESS-V4-COSPLAY-HARD-FAIL-ON-2026-08-06.md`

## Итерации
- Одна итерация; pytest green с первого прогона.

## Диалог
Пользователь «руби жестко» — hard-fail cosplay по умолчанию в v4; `/home/1.harness` не трогали.
