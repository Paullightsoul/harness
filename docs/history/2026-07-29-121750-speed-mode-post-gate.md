---
date: 2026-07-29T12:20:00+00:00
source: dialog
project: 1.harness
---

# Speed mode: skip medium review + DAG unlock post_gate

## Решения
- Wall-time на ZY run ~23m: peak 5 workers, но dependents ждали DONE после reviewer (3–10m на task). Слоты 12 не помогали — critical path = review.
- `HARNESS_REVIEW_MIN_COMPLEXITY=large`, `HARNESS_DAG_UNLOCK=post_gate`.

## Что сделано
- config + controller helpers; .env; tests `test_tasktool_speed_mode.py`; skill reference

## Итерации
- 1

## Диалог
Пользователь: всё равно медленно. Разобрали timeline и включили speed mode.
