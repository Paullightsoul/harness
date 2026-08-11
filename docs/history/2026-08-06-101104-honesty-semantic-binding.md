---
date: 2026-08-06T10:11:04+00:00
source: dialog
project: 1.harness-v4
---

# Honesty semantic AC↔evidence binding + anti-cosplay

## Решения
- 1:1 exclusive evidence bind + declared `acceptance_item_ids` вместо free reuse одного gate blob на все AC (класс бага news-bot mass-flip).
- evidence% считает только passes с валидным exclusive attested bind; отдельно ac_bound%.
- Cosplay detect по worker_id (default warn); resume не refill'ит workers при acceptance_repair; enqueue не плодит duplicate live envelopes.

## Что сделано
- `harness/evidence/binding.py` + wiring в acceptance/ledger/done_allowed/CLI/status
- controller: report identity, cosplay_risk, acceptance_repair resume, enqueue guard
- docs HONESTY-MODE / PHASE2 / .env.example; tests semantic + worker boundary
- ZY report: `harness_reports/HARNESS-V4-HONESTY-SEMANTIC-FIX-2026-08-06.md`

## Итерации
- Сначала formula/binding; поправили phase4 fixture (unbound passes больше не дают 100%); упрощены worker-boundary тесты под controller.json.

## Диалог
Пользователь «делай» по hard analysis: semantic bind, cosplay, evidence% honesty, resume waste, tests, docs. `/home/1.harness` не трогали.
