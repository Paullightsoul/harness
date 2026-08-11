---
name: harness-chat-orchestrator
description: >
  LEGACY compatibility entry point for old file-spool chat runs. Do not use
  for new runs; use .cursor/skills/harness-orchestrate/SKILL.md instead.
metadata:
  harness_role: orchestrator
  legacy: true
  origin: harness v2-022c
---

# Legacy compatibility: harness-chat-orchestrator

> **LEGACY ONLY.** Для любого нового пользовательского запуска немедленно
> перейди к `.cursor/skills/harness-orchestrate/SKILL.md` и следуй ему.

Новый pull-based контракт:

- `harness tasktool start|next|report|advance|status|abort|resume`;
- stdout CLI — JSON;
- задания передаются через `prompt_path`, результаты — через `result_path`;
- `harness tasktool next --limit 2`;
- только корневой Cursor chat вызывает Task Tool; Python не может вызывать
  Task Tool и не является агентским control plane.

Не запускай прежние `harness plan`/`harness run` как фоновые Python-процессы,
не включай bridge-poller и не воспроизводи старый polling loop.

## Совместимость со старым bridge

Если уже существует незавершённый legacy file-spool
`.harness/bridge/requests/*.json`, не мигрируй его на ходу. Обработай только
оставшиеся запросы по
`.cursor/skills/harness-bridge-worker/SKILL.md`, максимум двумя параллельными
вызовами Task Tool. После завершения legacy-прогона все новые запуски веди
только через `.cursor/skills/harness-orchestrate/SKILL.md`.
