---
name: harness-bridge-worker
description: >
  LEGACY compatibility handler for already-existing v2 file-spool requests.
  New runs must use .cursor/skills/harness-orchestrate/SKILL.md.
metadata:
  harness_role: orchestrator
  legacy: true
  origin: harness v2-022b/c
---

# Legacy compatibility: harness-bridge-worker

> **LEGACY ONLY.** Не создавай новый bridge-прогон. Для нового запуска используй
> `.cursor/skills/harness-orchestrate/SKILL.md`.

Этот skill разрешён только для завершения уже существующих файлов
`<bridge>/requests/*.json`. Корневой Cursor chat остаётся единственным
диспетчером Task Tool. Python никогда не вызывает Task Tool.

## Ограниченный compatibility pass

1. Один раз получи снимок готовых `requests/*.json`: игнорируй `.tmp` и запросы,
   для которых уже есть `responses/<id>.json`.
2. Выбери не более двух самых старых запросов.
3. Корневой чат вызывает Task Tool для выбранных запросов. Допустимо максимум
   **2 параллельных вызова** (`max 2`); Python-процесс, фоновый poller и
   polling loop запрещены.
4. Передай `request.prompt` без изменения. `request.cwd` уже указан в prompt.
   Если `request.model` пуст или равен `auto`, не передавай model.
5. Ответ или ошибку каждого вызова запиши каноническим способом ниже. Не удаляй
   request: legacy harness сам отвечает за cleanup.
6. После этого pass верни управление вызывающему orchestration skill. Не жди
   новые файлы в цикле.

Для `role=orchestrator` существующий Task Tool agent можно продолжить через
resume, если он запросил ввод пользователя. Для `role=worker|reviewer` передай
финальный текст как есть; не отвечай за агента.

## Каноническая запись response

Реальный API находится в `harness/runner/bridge.py`:
`write_response(spool_dir: Path, resp: BridgeResponse)`. Он использует
канонический serializer и атомарную запись `.json.tmp` → `os.replace`.
Не собирай response JSON вручную, когда этот модуль доступен.

Из корня репозитория выполни одноразовую запись, подставив значения результата
Task Tool:

```bash
python3 - <bridge-dir> <request-id> <ok-json> <text> <error> <agent-id> <<'PY'
import json
import sys
from pathlib import Path

from harness.runner.bridge import BridgeResponse, write_response

spool, request_id, ok_json, text, error, agent_id = sys.argv[1:]
write_response(
    Path(spool),
    BridgeResponse(
        id=request_id,
        ok=json.loads(ok_json),
        text=text,
        error=error,
        agent_id=agent_id,
    ),
)
PY
```

Это вспомогательная запись результата, а не Python control plane. Если импорт
канонического writer невозможен, остановись и сообщи blocker; не изобретай
альтернативную схему или неатомарный writer.

Новый pull-based путь использует только:
`harness tasktool start|next|report|advance|status|abort|resume`, JSON stdout,
`prompt_path`/`result_path` и `harness tasktool next --limit 2`.
