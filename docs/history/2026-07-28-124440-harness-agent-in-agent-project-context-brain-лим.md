---
date: 2026-07-28T12:44:40.601793+00:00
source: dialog
project: 1.harness
---

# Harness: agent-in-agent, project context/brain, лимиты хоста, пер-проектный журнал

## Решения
- ADR-0012: декомпозиция large-задач через sub-orchestrator dispatch (stage: plan) — control-plane-нативно, root chat остаётся единственным Task Tool dispatcher
- ExpansionArtifact как отдельный versioned артефакт — корневой PlanArtifact не мутирует (replay сохранён)
- Project context детерминированный с кэшем по git HEAD, без LLM; brain подключён через HARNESS_BRAIN_ROOT=/home/brain (раньше lessons молча не работали)
- Лимиты под 24 CPU / 125 GiB: волна 8, потолок 12, 3 heavy, 2 gate-слота (full/in-place сериализованы)
- ADR-0013: журнал работ в каждом проекте — docs/history/, по файлу на событие; harness пишет детерминированно, диалоги — через stop-hook + правило project-history.mdc

## Что сделано
- Новые модули: harness/tasktool/expansion.py, harness/tasktool/project_context.py, harness/journal.py
- Контроллер: sub-orchestrator consume/fan-out/integration, project brief в каждом dispatch, lessons при manual ship, journal на DONE + run-саммари
- CLI: harness context; state machine: RUNNING|READY→PENDING (decompose wait)
- Тесты: +6 файлов (expansion, decompose flow, project context, gate slots, journal), 557 passed
- Docs: ARCHITECTURE/GUIDE/README/QUICKSTART, skill harness-orchestrate (обе копии), ADR-0012/0013, AI_MEMORY, hooks.json, project-history.mdc

## Итерации
- decompose retry блокировался собственным неприменённым dispatch'ем — добавлен ignore_dispatch
- depends_on детей не персистился (update_task_fields не пишет deps) — переход на upsert_task
- контрактные тесты скилла ловили переносы строк во фразах-инвариантах — переформулировано без разрыва фраз

## Диалог
Пользователь попросил: исследовать харнесс и сделать его умным под мощности хоста — агент в агенте, скорость, оркестратор с контекстом проекта и опорой на brain; затем — чтобы каждый проект копил историю решений/диалогов/сделанного итерационно. Реализовано в 1.harness + workspace-обвязке (hook, rule, ADR-0012/0013).
