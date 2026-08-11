---
date: 2026-07-29T12:05:00+00:00
source: dialog
project: 1.harness
---

# Continuous refill: не ждать всю волну воркеров

## Решения
- Stall между волнами был процедурным (root ждал все Task Tool), не багом control plane: `report`/`advance`/`next` уже умеют partial progress.
- Канон диспетчера: `run_in_background` + на каждый finish → report → advance → next(free) → spawn.

## Что сделано
- Обновлён `harness-orchestrate` SKILL + reference (workspace и `1.harness/.cursor/skills`)
- Тест `test_primary_skill_forbids_full_wave_batch_wait`; AI_MEMORY one-liner

## Итерации
- 1 проход, код controller не меняли — хватало skill contract

## Диалог
Пользователь: 4 воркера, 1 закончил — следующий должен стартовать сразу. Объяснили причину и зафиксировали continuous refill в skill.
