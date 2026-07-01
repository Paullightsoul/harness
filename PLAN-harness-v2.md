# PLAN — Harness v2: наблюдаемость, понимание, канал↔пользователь, память

> Источник правды по декомпозиции для ADR-0005 (`brain/decisions/ADR-0005-...`).
> Прожарка кода `1.harness/` + реального прогона `run-20260630-093910` (ZY-2nd-flight:
> 14 задач / 50 attempts / 4 escalated / 4 blocked / 6 recovered) вскрыла 13 дыр.
> План закрывает их, подтягивая рабочие концепты с [affaan-m/ECC](https://github.com/affaan-m/ECC)
> (Ralphinho DAG, iterative-retrieval, continuous-learning-v2, cost-aware-llm-pipeline,
> verification-loop, de-sloppify, ecc2 control-plane, AgentShield, strategic-compact).
>
> Спеки задач ниже оформлены по `tasks/_TEMPLATE.md` — после завершения Фазы 1
> (когда harness сам начнёт стримить/считать токены) план можно разбить на отдельные
> `tasks/task-v2-*.md` и ingest'нуть в harness (dogfood).

## Цель

Превратить harness из скелета с дырами в систему, где:

1. Пользователь видит живой транскрипт агента, бюджет и остаток контекста в реальном
   времени (Фаза 2).
2. Формальное «агент понял задачу» через understanding-brief + iterative-retrieval,
   не «ну начал писать» (Фаза 3).
3. Оркестратор и воркер могут задать конкретный вопрос пользователю и получить ответ
   через Telegram/чат (Фаза 4).
4. Бюджет не врёт, ревьюер реально проверяет код, план не галлюцинирует (Фаза 1).
5. Память между прогонами: lessons-learned, multi-run memory, опционально instincts
   (Фаза 5).
6. Chat-native режим даёт живые окна сабагентов в IDE (Фаза 4.3).

## Архитектурные решения

| Область | Решение |
|---|---|
| Stream-режим runner'а | `SdkRunner`/`CliRunner` → стриминг сообщений агента в `logs/*.ndjson` + event-log как `AGENT_EVENT`. Fallback: polling/stdout-tail если SDK не стримит |
| Token-учёт | Расширить `AgentResult` полями `tokens_in/out`, `context_window_remaining`, `cost_kind=estimate\|actual`. Из SDK — `usage`, из CLI — парсинг stderr |
| Reviewer CWD | Запуск ревьюера в worktree воркера (`cwd=worktree`), не в `self._s.root`. Ревьюер и тест-стадия делят worktree (Ralphinho "Worktree Isolation") |
| Plan CWD | `harness plan --project` ставит `cwd=repo_root` оркестратора. Plan по умолчанию требует `--project` или явного `--root` |
| Question-protocol | Новый статус `NEEDS_INPUT` + `INPUT_REQUESTED` event. Оркестратор/воркер возвращают `{"need_input": true, "questions": [...]}` с options → Telegram inline-кнопки → `/answer` |
| Chat-native режим | Новый `CursorTaskRunner: AgentRunner` поверх Task tool (Cursor IDE subagents). `scheduler/engine.py` не меняется. Параллельно с subprocess-режимом |
| Tiered pipeline depth | `complexity: trivial\|small\|medium\|large` вместо `normal\|high`. Меняет **число стадий** (trivial = worker→gate, large = +research+plan+PRD-review+review-fix+final-review), не только модель |
| Память между прогонами | `SHARED_TASK_NOTES.md` per task (мост между попытками) + `brain/lessons/<service>/` после run + multi-run summary в `cmd_plan` |
| Безопасность | AgentShield-скан промптов/правил при `harness doctor`. Adversarial red-team/blue-team на Opus — off по умолчанию, opt-in |
| Запрет | Никаких `git push` / `gh` / PR в Фазах 1-5 (кроме 6.6.6 после 3.14). Не ломать обратную совместимость SQLite-стора без миграции |

## Задачи

| id | фаза | название | depends_on | complexity | status |
|---|---|---|---|---|---|
| v2-001 | 1 | Reviewer в worktree воркера | — | normal | **done** |
| v2-002 | 1 | `harness plan --project` с правильным CWD | — | normal | **done** |
| v2-003 | 1 | Точный cost из SDK + `cost_kind` | — | normal | **done** |
| v2-004 | 1 | `Provides:` валидация + воркер заполняет | v2-001 | normal | **done** |
| v2-005 | 1 | Idempotent ingest (дедуп run'ов) | — | normal | **done** |
| v2-006 | 1 | Verdict parser malformed-safe | — | normal | **done** |
| v2-007 | 1 | Prompt-file для аудита + shell-mode опция | — | normal | **done** |
| v2-008 | 1 | Унификация промптов (`prompts/` = канон) | — | normal | **done** |
| v2-009 | 2 | Stream-режим runner'а + `AGENT_EVENT` | v2-007 | high | **done** |
| v2-010 | 2 | `harness tail` live-tail | v2-009 | normal | **done** |
| v2-011 | 2 | Token-учёт в `AgentResult` | v2-003 | normal | **done** |
| v2-012 | 2 | `harness status --markdown` handoff | — | normal | **done** |
| v2-013 | 2 | Контекст-монитор (warnings) | v2-011 | normal | **done** |
| v2-014 | 2 | Web-дашборд (FastAPI + WebSocket) | v2-009, v2-011 | high | **done** |
| v2-015 | 3 | Pre-flight understanding-brief + `NEEDS_CLARIFICATION` | v2-004 | high | **done** |
| v2-016 | 3 | Iterative-retrieval в worker-промпт | — | normal | **done** |
| v2-017 | 3 | `graphify` в воркере | — | normal | **done** |
| v2-018 | 3 | Plan-verification (детерминированный) | v2-002 | normal | **done** |
| v2-019 | 3 | Tiered pipeline depth (trivial/small/medium/large) | v2-001 | high | **done** (minimal; multi-stage follow-up v2-019b) |
| v2-020 | 4 | `NEEDS_INPUT` + question-protocol | v2-015 | high | **done** (через NEEDS_CLARIFICATION + /answer) |
| v2-021 | 4 | Completion signal (`HARNESS_DONE`) | — | normal | **done** |
| v2-022 | 4 | `harness chat` (CursorTaskRunner) | v2-009 | high | **done** (stub + seam; live bridge v2-022b) |
| v2-023 | 4 | Orchestrator checkpoint (`PLAN_DRAFT`) | v2-018 | normal | **done** |
| v2-024 | 5 | `SHARED_TASK_NOTES.md` per task | — | normal | **done** |
| v2-025 | 5 | Lessons-learned → `brain/lessons/` | v2-024 | normal | **done** |
| v2-026 | 5 | Multi-run memory в `cmd_plan` | v2-025 | normal | **done** (входит в v2-025 — `format_lessons_for_plan` в cmd_plan) |
| v2-027 | 5 | Instincts (opt-in, позднее) | v2-025 | high | todo (отложено) |
| v2-028 | 6 | De-sloppify отдельный pass | v2-001 | normal |
| v2-029 | 6 | Merge queue with eviction context | — | normal |
| v2-030 | 6 | Pass@k / pass^k метрики | — | normal |
| v2-031 | 6 | AgentShield-скан промптов/правил | — | high |
| v2-032 | 6 | Strategic compaction | v2-011 | normal |
| v2-033 | 6 | CI failure recovery | v2-014 | high |
| v2-034 | 6 | Durable-плейн в боевое (`EngineTaskExecutor`) | v2-009 | high |
| v2-035 | 6 | Skill-stocktake | v2-025 | normal |

### Граф зависимостей по волнам

```
Wave 1 (Phase 1, параллельно):  v2-001  v2-002  v2-003  v2-005  v2-006  v2-007  v2-008
                                   |       |       |       |       |       |
                                   |       |       v       |       v       |
                                   |       |    v2-011     |    v2-009     |
                                   |       |       |       |       |       |
                                   v       |       |       |       v       |
                                v2-004    |       |       |    v2-010      |
                                   |      |       |       |       |        |
                                   v      |       |       |    v2-014      |
                                v2-015 <--+-------+-------+       |        |
                                   |                              |        |
                                   v                              |        |
Wave 2 (Phase 2-3):             v2-013, v2-016, v2-017, v2-018, v2-019, v2-012
                                   |              |       |
                                   v              |       |
Wave 3 (Phase 4):             v2-020, v2-021, v2-022, v2-023
                                   |
Wave 4 (Phase 5):             v2-024, v2-025, v2-026, v2-027
                                   |
Wave 5 (Phase 6):             v2-028, v2-029, v2-030, v2-031, v2-032, v2-033, v2-034, v2-035
```

Single-writer-per-file: в каждой волне задачи трогают непересекающиеся наборы
файлов. Если две задачи Wave 1 хотят править `engine.py` — координировать через
mini-фичи (reviewer-cwd, verdict-parser — разные функции, конфликта нет).

---

## Спеки задач

### v2-001 — Reviewer в worktree воркера

```yaml
id: "v2-001"
title: "Reviewer в worktree воркера"
complexity: "normal"
status: "done"
```

**Контекст:** Сейчас `Engine._process_task` зовёт `_reviewer_runner.run(..., cwd=self._s.root)`
— ревьюер запускается в доме harness, не в worktree задачи. В CWD у него нет кода
воркера, он читает `git diff` текстом из промпта и галлюцинирует. Промпт
`prompts/reviewer.md:21` говорит «при необходимости запускай тесты» — но он не может.
Ralphinho (ECC) решает это: pipeline-стадии **делят worktree**.

**Файлы:**
- `harness/scheduler/engine.py` (строки ~207-213)
- `prompts/reviewer.md` (обновить инструкцию — теперь CWD = worktree, можно гонять гейты)

**Что сделать:**
1. В `_process_task` заменить `cwd=self._s.root` на `cwd=worktree` для reviewer.
2. Передать ревьюеру путь к worktree в промпте (`=== WORKTREE ===\n{worktree}`).
3. Добавить в `_reviewer_prompt` инструкцию: «гейты можно гонять в CWD».
4. Лог ревьюера писать в `logs/reviewer-<task>-a<N>.log` как раньше.

**Acceptance criteria:**
- [ ] `pytest tests/test_engine_reviewer_cwd.py` — мок runner проверяет, что
      `runner.run(..., cwd=worktree_path)` вызван с путём worktree, не с `settings.root`.
- [ ] `mypy harness/scheduler/engine.py` без ошибок.
- [ ] В `prompts/reviewer.md` есть строка «CWD = worktree задачи, гоняй гейты прямо тут».

**Запрещено:**
- Менять логику verdict-парсера (это v2-006).
- Трогать orchestrator CWD (это v2-002).

---

### v2-002 — `harness plan --project` с правильным CWD

```yaml
id: "v2-002"
title: "harness plan --project с правильным CWD оркестратора"
complexity: "normal"
status: "done"
```

**Контекст:** `cmd_plan` в `cli.py:62` ставит `cwd=settings.root` (дом harness), а
`--project` в `plan` не передаётся (`sp.add_argument("goal")` без `--project`).
Оркестратор изучает репо по absolute path — не гарантировано, для нового проекта
план галлюцинирует.

**Файлы:**
- `harness/interface/cli.py` (`cmd_plan`, `build_parser` — `plan` subparser)
- `tests/test_cli_plan_cwd.py` (новый)

**Что сделать:**
1. Добавить `--project` в `plan` subparser.
2. В `cmd_plan` резолвить проект через `_resolve_project`, брать `_repo_root`.
3. Запускать оркестратора с `cwd=repo_root`, не `settings.root`.
4. В промпт добавить блок `=== REPO ROOT ===\n{repo_root}`.
5. Если `--project` не задан и `settings.root` не git-репо — ошибка с подсказкой.

**Acceptance criteria:**
- [ ] `harness plan "цель" --project api` запускает оркестратора с `cwd=/srv/api`.
- [ ] `pytest tests/test_cli_plan_cwd.py` — мок runner проверяет `cwd=repo_root`.
- [ ] `harness plan "цель"` без `--project` в не-git-каталоге → exit 1 с сообщением
      «укажи --project или запусти в git-репо».

**Запрещено:**
- Менять `cmd_ingest` / `cmd_run` (там `--project` уже есть).

---

### v2-003 — Точный cost из SDK + `cost_kind`

```yaml
id: "v2-003"
title: "Точный cost из SDK + cost_kind=estimate|actual"
complexity: "normal"
status: "done"
```

**Контекст:** `SdkRunner` читает только `result.cost_credits`, который для `glm-5.2-high`
возвращается `0` → 7 ревьюеров и 7 эскалаций не попали в бюджет. `spent_credits=14.2`
при реальной ~38.5. `RUN_BUDGET` — иллюзия. ECC `cost-aware-llm-pipeline` решает это.

**Файлы:**
- `harness/runner/sdk_runner.py` (строки ~63-74)
- `harness/runner/cli_runner.py` (строки ~15-49, ~86-88)
- `harness/runner/base.py` (`AgentResult` — добавить поля)
- `harness/store/schema.sql`, `harness/store/repository.py` (`attempts.cost_kind`,
  `attempts.tokens_in`, `attempts.tokens_out`)
- `tests/test_runner_cost.py`

**Что сделать:**
1. В `AgentResult` добавить `tokens_in: int = 0`, `tokens_out: int = 0`,
   `context_window_remaining: int | None = None`, `cost_kind: str = "estimate"`.
2. В `SdkRunner` читать `usage`/`billing` поля у `result` (проверить фактический API
   `cursor-sdk` через SDK skill), ставить `cost_kind="actual"`.
3. В `CliRunner` оставить эвристику, но ставить `cost_kind="estimate"`.
4. В `attempts` таблицу добавить колонки `cost_kind`, `tokens_in`, `tokens_out`.
5. `Engine._account` пишет `cost_kind` в event-log.

**Acceptance criteria:**
- [ ] `pytest tests/test_runner_cost.py` — мок SDK с `usage={input: 1000, output: 500}`
      → `AgentResult.tokens_in=1000`, `cost_kind="actual"`.
- [ ] `CliRunner` всегда ставит `cost_kind="estimate"`.
- [ ] `harness events` показывает `cost_kind` для `BUDGET_SPENT`.
- [ ] `mypy harness/runner/` без ошибок.

**Запрещено:**
- Менять `RUN_BUDGET` логику governor'а — только точные данные в `add_spend`.

---

### v2-004 — `Provides:` валидация + воркер заполняет

```yaml
id: "v2-004"
title: "Provides: валидация + воркер заполняет в output"
complexity: "normal"
status: "done"
depends_on: ["v2-001"]
```

**Контекст:** Движок читает `task.provides` и инжектит зависимому воркеру
(`_collect_provides`), но воркеру **нигде не сказано** заполнять `Provides:` секцию.
Для всех последующих задач интерфейсы от родителей пустые — context contracts мертвы.
Ralphinho "Data Flow Between Stages" — каждая стадия объявляет что производит.

**Файлы:**
- `prompts/worker.md` (добавить обязательную секцию `Provides:` в output-формат)
- `harness/tasks_io/parser.py` (`parse_task_file` — читать `Provides:` секцию,
  валидация)
- `harness/scheduler/engine.py` (после worker-прогона парсить output, заполнять
  `task.provides`, `update_task_fields`)
- `tasks/_TEMPLATE.md` (добавить секцию `# Provides` с примером)
- `tests/test_provides_parsing.py`

**Что сделать:**
1. В `_TEMPLATE.md` добавить секцию `# Provides` с примером интерфейсов
   (сигнатуры, типы, пути файлов).
2. В `prompts/worker.md` добавить в формат вывода обязательный блок
   `Provides: <сигнатуры произведённого кода>`.
3. В `parser.py` — `read_section(text, "Provides")` уже есть; валидировать
   непустоту для leaf-задач, от которых зависят другие.
4. В `engine.py` после `wres = await self._worker_runner.run(...)` парсить
   `wres.text` на `Provides:` секцию, писать в `task.provides`.
5. Если у задачи есть dependents, а `Provides:` пустой после прогона — событие
   `GUARD_VIOLATION`-стиля `PROVIDES_MISSING`, CHANGES без ревьюера.

**Acceptance criteria:**
- [ ] `pytest tests/test_provides_parsing.py` — спека с `Provides:` парсится.
- [ ] Воркер-вывод без `Provides:` для задачи с dependents → `PROVIDES_MISSING` event.
- [ ] `tasks/_TEMPLATE.md` содержит секцию `# Provides` с примером.
- [ ] Зависимая задача в `_worker_prompt` получает `=== ИНТЕРФЕЙСЫ ЗАВИСИМОСТЕЙ ===`
      с реальным `provides` родителя (не пустым).

**Запрещено:**
- Ломать существующие спеки без `Provides:` (warn, не error) — миграция мягкая.

---

### v2-005 — Idempotent ingest

```yaml
id: "v2-005"
title: "Idempotent ingest — дедуп run'ов"
complexity: "normal"
status: "done"
```

**Контекст:** `ingest_run` не дедуплицирует → 3 run'а с одинаковой goal
(`run-20260630-093709/55/910`). `cmd_run` берёт `_latest_run_id` → первый потерян.

**Файлы:**
- `harness/ingest.py`
- `harness/store/repository.py` (`create_run` — проверка существующего)
- `tests/test_ingest_dedup.py`

**Что сделать:**
1. В `ingest_run` считать `goal_hash = sha256(project + goal + base_branch)`.
2. Перед `create_run` искать существующий run с тем же `goal_hash` и
   `status in (planning, paused, running)`.
3. Если найден и не терминальный — вернуть его id (warning в event-log
   `RUN_REUSED`).
4. Если найден терминальный (`done`) — создать новый (пользователь явно
   перезапускает).
5. Добавить колонку `goal_hash` в `runs`.

**Acceptance criteria:**
- [ ] `pytest tests/test_ingest_dedup.py` — два `ingest_run` с одинаковой goal →
      один run_id.
- [ ] `harness ingest "цель"` дважды → второй выводит `RUN_REUSED <id>`.
- [ ] `harness ingest "цель" --force` → новый run даже при существующем.

**Запрещено:**
- Менять `cmd_run` (там `_latest_run_id` — отдельно).

---

### v2-006 — Verdict parser malformed-safe

```yaml
id: "v2-006"
title: "Verdict parser malformed-safe"
complexity: "normal"
status: "done"
```

**Контекст:** `_parse_verdict` defaults to `CHANGES` → малейший malformed-вывод
ревьюера = доработка. note `unblock: verdict-parser fix` у task-009 — ручное
латание посреди прогона.

**Файлы:**
- `harness/scheduler/engine.py` (`_parse_verdict`, строки ~509-525)
- `tests/test_verdict_parser.py`

**Что сделать:**
1. Regex `VERDICT:\s*(APPROVE|CHANGES)` case-insensitive, ищет последнее
   совпадение.
2. Если ничего не найдено — fallback = `ESCALATE` (не `CHANGES`), с event
   `VERDICT_UNPARSED` — пусть оркестратор решит, не сжигаем попытку.
3. Поддержка markdown-обёртки (`**VERDICT: APPROVE**`) — уже есть, оставить.

**Acceptance criteria:**
- [ ] `pytest tests/test_verdict_parser.py` — 8 кейсов: чистый APPROVE/CHANGES,
      markdown-обёртка, без вердикта, несколько вердиктов (последний выигрывает),
      опечатка `APROVE` → ESCALATE+VERDICT_UNPARSED.
- [ ] Воркер-вывод без `VERDICT:` → `ESCALATE`, не `CHANGES`.

**Запрещено:**
- Менять промпт ревьюера (он уже требует `VERDICT:` последней строкой).

---

### v2-007 — Prompt-file вместо `-p` argv

```yaml
id: "v2-007"
title: "Prompt-file для аудита + HARNESS_PROMPT_VIA_SHELL опция"
complexity: "normal"
status: "done"
```

**Контекст:** `CliRunner` передавал весь worker-prompt (10-30KB) как один argv
`-p prompt` → попадает в `ps`/shell history, нечитаемо в логах.

**Реализация (скорректирована под реальность):** Web-поиск по docs.cursor.com +
gsd-core#669 показал: **`cursor-agent` CLI не поддерживает `--prompt-file` или
stdin** — только argv. Полностью убрать argv-передачу нельзя. Реалистичный объём
дыры #9:

- **Всегда** сохранять prompt в `.harness/prompts/<stem>.md` (для аудита/дебага) —
  закрывает «нечитаемо в логах».
- Опция `HARNESS_PROMPT_VIA_SHELL=1` — shell + `"$(cat file)"` чтобы скрыть из
  `ps` (по умолчанию off, backwards compat).
- ARG_MAX (128KB Linux) переживает 30KB промпты — не лечится на CLI, для больших
  промптов нужен переход на SDK (v2-003 область).

**Файлы:**
- `harness/runner/cli_runner.py`
- `tests/test_cli_runner_prompt_file.py` (новый, 4 теста)

**Acceptance criteria (достигнуты):**
- [x] `pytest tests/test_cli_runner_prompt_file.py` — 4 кейса (audit-file saved,
      shell-mode hides prompt, default exec argv, file-not-found).
- [x] `HARNESS_PROMPT_VIA_SHELL=1` → `ps` не показывает промпт, только `$(cat path)`.
- [x] Именованные промпты (с log_path) остаются в `.harness/prompts/` всегда.

**Запрещено:**
- Менять SdkRunner (там prompt идёт через API, не argv).

---

### v2-008 — Унификация промптов

```yaml
id: "v2-008"
title: "Унификация промптов: prompts/ = канон"
complexity: "normal"
status: "done"
```

**Контекст:** `prompts/*.md` (движок) и `.cursor/agents/*.md` (Cursor IDE)
расходятся. Канон не определён.

**Файлы:**
- `.cursor/agents/orchestrator.md`, `reviewer.md`, `worker.md`
- `prompts/orchestrator.md`, `reviewer.md`, `worker.md`

**Что сделать:**
1. Канон = `prompts/` (движок читает их).
2. `.cursor/agents/*.md` — только frontmatter + ссылка на `prompts/<role>.md`
   (для Cursor IDE; полный текст не дублировать).
3. Добавить регресс-тест: `tests/test_prompts_canonical.py` — сравнивает
   frontmatter, требует что `.cursor/agents/<role>.md` ссылается на
   `prompts/<role>.md`.

**Acceptance criteria:**
- [ ] `pytest tests/test_prompts_canonical.py` зелёный.
- [ ] `.cursor/agents/worker.md` содержит `See prompts/worker.md`.
- [ ] `diff prompts/orchestrator.md .cursor/agents/orchestrator.md` —
      `.cursor/agents/` только frontmatter + ссылка.

**Запрещено:**
- Удалять `.cursor/agents/` (Cursor IDE их использует для ручного чата).

---

### v2-009 — Stream-режим runner'а + `AGENT_EVENT`

```yaml
id: "v2-009"
title: "Stream-режим runner'а + AGENT_EVENT в event-log"
complexity: "high"
depends_on: ["v2-007"]
```

**Контекст:** `logs/*.log` — финальный stdout агента (труп). Пользователь не
видит, **чем** живёт агент. ECC `ecc2` control-plane даёт live-tail; нам нужен
стриминг tool-call'ов и assistant-сообщений в реальном времени.

**Файлы:**
- `harness/runner/base.py` (`AgentResult` — добавить `events: list[AgentEvent]`,
  или отдельный callback)
- `harness/runner/sdk_runner.py` (`run.stream()` или аналог — сверить с SDK skill)
- `harness/runner/cli_runner.py` (tail stdout по строкам, эвристика для tool-call)
- `harness/domain/enums.py` (`EventType.AGENT_EVENT`)
- `harness/store/repository.py` (`add_agent_event` — bulk insert)
- `harness/store/schema.sql` (`agent_events` таблица: `run_id, task_id, attempt,
  kind, payload, at`)
- `tests/test_runner_streaming.py`

**Что сделать:**
1. Изучить SDK skill — поддерживает ли `cursor_sdk` streaming (`run.stream()`
   или `agent.events()`). Если да — использовать. Если нет — fallback: poll
   `run.messages()` каждые 500ms.
2. Для CLI — `asyncio.create_subprocess_exec` с `stdout=PIPE`, читать по
   строкам асинхронно, эвристика для tool-call (regex по cursor-agent output).
3. Каждое событие = `AgentEvent(kind, payload, at)` где kind ∈
   `{tool_call, assistant_msg, file_edit, usage, error}`.
4. Писать в `logs/worker-<task>-a<N>.ndjson` (по строке на событие) **и** в
   `agent_events` таблицу bulk-insert'ом (batch 10/100ms).
5. `AgentResult.events` — агрегат для пост-анализа.

**Acceptance criteria:**
- [ ] `pytest tests/test_runner_streaming.py` — мок SDK стримит 3 события →
      все 3 в `agent_events` и в `.ndjson`.
- [ ] `harness events <run> --kind tool_call` показывает tool-call'ы.
- [ ] `logs/worker-008-a1.ndjson` содержит ≥10 строк для прогона с реальным
      воркером.
- [ ] Fallback (если SDK не стримит) — polling каждые 500ms, не блокирует.

**Запрещено:**
- Блокировать основной цикл движка на стриминг (async, не sync).

---

### v2-010 — `harness tail` live-tail

```yaml
id: "v2-010"
title: "harness tail — live-tail event-log"
complexity: "normal"
depends_on: ["v2-009"]
```

**Контекст:** `harness events` — статичный слепок. Нужен live-tail в терминале.

**Файлы:**
- `harness/interface/cli.py` (`cmd_tail`, `tail` subparser)
- `harness/store/repository.py` (`list_events` уже есть с `after_id`)
- `tests/test_cli_tail.py`

**Что сделать:**
1. `harness tail [run_id] [--task ID] [--kind agent_event]` — long-poll
   `SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id` каждые 500ms.
2. Вывод: `<timestamp> <type> <task> <detail>` (colorized через ANSI).
3. `--kind agent_event` — только агент-события (tool_call / file_edit).
4. Ctrl-C → clean exit.

**Acceptance criteria:**
- [ ] `harness tail <run>` в одном терминале, `harness run` в другом → видит
      события в реальном времени.
- [ ] `--task 008` фильтрует по задаче.
- [ ] `pytest tests/test_cli_tail.py` — мок store добавляет события, tail их
      подхватывает.

**Запрещено:**
- WebSocket (это для дашборда v2-014) — здесь только terminal long-poll.

---

### v2-011 — Token-учёт в `AgentResult`

```yaml
id: "v2-011"
title: "Token-учёт: tokens_in/out, context_window_remaining"
complexity: "normal"
depends_on: ["v2-003"]
```

**Контекст:** Никто не логирует `tokens_in/out`, `context_window_remaining` →
пользователь не знает, упёрся ли воркер в лимит.

**Файлы:**
- `harness/runner/sdk_runner.py` (читать `usage` поля)
- `harness/runner/cli_runner.py` (парсить stderr cursor-agent — он пишет usage
  в некоторых режимах; проверить)
- `harness/store/schema.sql`, `repository.py` (`attempts.tokens_in/out`,
  `events` для `CONTEXT_LOW`)
- `tests/test_runner_tokens.py`

**Что сделать:**
1. Из SDK `result.usage.{input_tokens, output_tokens, cache_read, cache_write}`.
2. `context_window_remaining` = `model_window - usage.input_tokens` (или из SDK
   поля, если есть).
3. Для CLI — парсить stderr cursor-agent (regex `tokens: in=N out=M` или
   аналог; проверить фактический вывод).
4. Писать в `attempts.tokens_in/out` и в event-log при `context_window_remaining
   < 20%` → `CONTEXT_LOW` event.

**Acceptance criteria:**
- [ ] `pytest tests/test_runner_tokens.py` — мок SDK с `usage={input: 50000}` →
      `AgentResult.tokens_in=50000`, `context_window_remaining` заполнен.
- [ ] `harness status` показывает `tokens: 50k in / 12k out` для последней
      попытки.
- [ ] `CONTEXT_LOW` event при `remaining < 20%`.

**Запрещено:**
- Менять `RUN_BUDGET` (бюджет — в кредитах, не токенах).

---

### v2-012 — `harness status --markdown` handoff

```yaml
id: "v2-012"
title: "harness status --markdown --write status.md"
complexity: "normal"
```

**Контекст:** `harness status` plain text. ECC `ecc status --markdown --write
status.md` даёт portable handoff между сменами/чата.

**Файлы:**
- `harness/interface/cli.py` (`cmd_status` — добавить `--markdown`, `--write`)
- `tests/test_cli_status_markdown.py`

**Что сделать:**
1. `--markdown` — вывод в markdown-таблице (задачи, статусы, попытки, модели,
   бюджет).
2. `--write <path>` — писать в файл (atomic), не stdout.
3. Секции: readiness, active runs, blocked tasks, бюджет, последние события.

**Acceptance criteria:**
- [ ] `harness status --markdown --write status.md` → файл создан, валидный
      markdown.
- [ ] `pytest tests/test_cli_status_markdown.py` — структура таблицы.

**Запрещено:**
- Менять дефолт (без флагов — plain text как сейчас).

---

### v2-013 — Контекст-монитор (warnings)

```yaml
id: "v2-013"
title: "Контекст-монитор: CONTEXT_LOW, SCOPE_EXIT, LOOP_STUCK"
complexity: "normal"
depends_on: ["v2-011"]
```

**Контекст:** ECC `ECC_CONTEXT_MONITOR_*` — warnings агенту в чат. У нас их нет.

**Файлы:**
- `harness/domain/enums.py` (`EventType.CONTEXT_LOW, SCOPE_EXIT, LOOP_STUCK`)
- `harness/scheduler/engine.py` (после каждого `wres` — анализ)
- `harness/policy/scope.py` (новый — проверка changed_files vs `Файлы:` из спеки)
- `tests/test_context_monitor.py`

**Что сделать:**
1. `CONTEXT_LOW` — при `context_window_remaining < 20%`.
2. `SCOPE_EXIT` — `changed_files` содержит пути вне списка `Файлы:` из спеки
   (парсить секцию).
3. `LOOP_STUCK` — один и тот же tool-call (same args) > 3 раз в
   `agent_events` за попытку.
4. Все три — в event-log, `LOOP_STUCK` → auto-CHANGES без ревьюера.

**Acceptance criteria:**
- [ ] `pytest tests/test_context_monitor.py` — 3 кейса (low/scope/loop).
- [ ] Воркер, трижды зовущий `Read("src/x.py")` → `LOOP_STUCK`, CHANGES.

**Запрещено:**
- Блокировать воркера (warnings, не exceptions) — кроме `LOOP_STUCK`.

---

### v2-014 — Web-дашборд

```yaml
id: "v2-014"
title: "Web-дашборд (FastAPI + WebSocket)"
complexity: "high"
depends_on: ["v2-009", "v2-011"]
```

**Контекст:** ROADMAP 3.12. Один экран «всё что происходит»: таймлайн задач,
живой транскрипт выбранного воркера, budget-метр, current model.

**Файлы:**
- `harness/dashboard/app.py` (новый — FastAPI)
- `harness/dashboard/ws.py` (WebSocket — стримит `agent_events`)
- `harness/dashboard/templates/` (Jinja2 / или SPA)
- `tests/test_dashboard.py`

**Что сделать:**
1. FastAPI: `/` — список run'ов; `/run/<id>` — таймлайн задач; `/run/<id>/task/<tid>`
   — живой транскрипт.
2. WebSocket `/ws/run/<id>` — стримит новые `events` и `agent_events`.
3. Budget-метр (из `runs.spent_credits` / `budget_credits`).
4. `harness dashboard` — запуск uvicorn на `:8420`.

**Acceptance criteria:**
- [ ] `harness dashboard` → `http://localhost:8420` открывается.
- [ ] WebSocket стримит события в реальном времени (тест с мок-стором).
- [ ] `pytest tests/test_dashboard.py`.

**Запрещено:**
- Тяжёлые JS-фреймворки (HTMX / vanilla JS / минимальный React).

---

### v2-015 — Pre-flight understanding-brief + `NEEDS_CLARIFICATION`

```yaml
id: "v2-015"
title: "Pre-flight understanding-brief + NEEDS_CLARIFICATION"
complexity: "high"
depends_on: ["v2-004"]
```

**Контекст:** Закрывает «формальное агент понял». Перед основным прогоном
воркер возвращает maшиночитаемый brief. ECC `iterative-retrieval` EVALUATE phase.

**Файлы:**
- `harness/domain/enums.py` (`TaskStatus.NEEDS_CLARIFICATION`,
  `EventType.UNDERSTANDING_BRIEF`)
- `harness/domain/state_machine.py` (переходы `PENDING → NEEDS_CLARIFICATION →
  READY`, `RUNNING → NEEDS_CLARIFICATION → READY`)
- `prompts/worker.md` (новый обязательный шаг перед кодом)
- `harness/scheduler/engine.py` (`_process_task` — перед worker-loop, отдельный
  agent-call для brief)
- `harness/tasks_io/brief.py` (новый — парсер JSON brief)
- `tests/test_understanding_brief.py`

**Что сделать:**
1. Воркер получает спеку, возвращает JSON:
   ```json
   {"understand": "...", "files_i_will_touch": [...],
    "acceptance_i_will_satisfy": ["AC1", "AC2"],
    "assumptions": [...], "missing_context": ["..."]}
   ```
2. Движок парсит. Если `missing_context` непустой → `NEEDS_CLARIFICATION`,
   event `UNDERSTANDING_BRIEF` с вопросом → инжектится в v2-020 question-protocol.
3. Если пустой → `READY`, основной worker-loop.
4. Brief пишется в `attempts.brief` (новая колонка) для аудита.

**Acceptance criteria:**
- [ ] `pytest tests/test_understanding_brief.py` — brief с `missing_context` →
      `NEEDS_CLARIFICATION`.
- [ ] Brief без `missing_context` → `READY`, основной прогон.
- [ ] `harness events` показывает `UNDERSTANDING_BRIEF` с содержанием.

**Запрещено:**
- Использовать expensive model для brief (это `auto` / `kimi`, не Opus).

---

### v2-016 — Iterative-retrieval в worker-промпт

```yaml
id: "v2-016"
title: "Iterative-retrieval в worker-промпт"
complexity: "normal"
```

**Контекст:** Воркер получает spec+provides+feedback один раз и гадает. ECC
`iterative-retrieval` — 4-phase loop: DISPATCH→EVALUATE→REFINE→LOOP ≤3, **до**
написания кода.

**Файлы:**
- `prompts/worker.md` (новая секция «Iterative retrieval (обязательно)»)
- `1.harness/.cursor/skills/iterative-retrieval/` (новый скилл — порт из ECC)
- `tests/test_worker_prompt_has_retrieval.py`

**Что сделать:**
1. Скопировать `skills/iterative-retrieval/SKILL.md` из ECC в
   `.cursor/skills/iterative-retrieval/SKILL.md` (адаптировать frontmatter
   `harness_role: worker`).
2. В `prompts/worker.md` добавить обязательный шаг перед TDD-циклом:
   «Iterative retrieval: DISPATCH (rg/graphify) → EVALUATE (relevance 0-1) →
   REFINE → LOOP ≤3. Верни `retrieval_result` в output».
3. Воркер пишет в output `retrieval_result:` с найденными файлами и relevance.

**Acceptance criteria:**
- [ ] `prompts/worker.md` содержит секцию "Iterative retrieval".
- [ ] `.cursor/skills/iterative-retrieval/SKILL.md` существует, frontmatter
      `harness_role: worker`.
- [ ] `pytest tests/test_worker_prompt_has_retrieval.py` — проверка секции.

**Запрещено:**
- Делать retrieval обязательным для `trivial` задач (v2-019 — там pipeline
  короче).

---

### v2-017 — `graphify` в воркере

```yaml
id: "v2-017"
title: "graphify query в worker-промпте"
complexity: "normal"
```

**Контекст:** `search-first` скилл есть, но воркер зовёт rg вслепую. Workspace
rule требует `graphify query` для архитектурных вопросов — не подключено.

**Файлы:**
- `prompts/worker.md` (в iterative-retrieval секции — `graphify query` для
  архитектурных, `rg` для текстовых)
- `1.harness/.cursor/rules/30-graphify.mdc` (новый — workspace rule для воркера)
- `tests/test_worker_prompt_has_graphify.py`

**Что сделать:**
1. В `prompts/worker.md` добавить: «Перед новым кодом — `graphify query
   "<подзадача>" --graph <repo>/graphify-out/graph.json` для архитектурных
   вопросов; `rg` для текстовых».
2. Создать `.cursor/rules/30-graphify.mdc` — короткое правило.
3. Если граф не построен — fallback на `rg` (warn, не error).

**Acceptance criteria:**
- [ ] `prompts/worker.md` упоминает `graphify query`.
- [ ] `.cursor/rules/30-graphify.mdc` существует.
- [ ] `pytest tests/test_worker_prompt_has_graphify.py`.

**Запрещено:**
- Авто-билд графа в воркере (это отдельный процесс, не в run-loop).

---

### v2-018 — Plan-verification (детерминированный)

```yaml
id: "v2-018"
title: "Plan-verification: проверка PLAN.md перед ingest"
complexity: "normal"
depends_on: ["v2-002"]
```

**Контекст:** `harness plan` создаёт черновик, пользователь проверяет глазами.
Не механически. Ralphinho "Decomposition Rules" — декомпозиция должна
валидироваться.

**Файлы:**
- `harness/tasks_io/verifier.py` (новый — детерминированная проверка)
- `harness/interface/cli.py` (`harness verify` команда + auto в `cmd_ingest`)
- `tests/test_plan_verifier.py`

**Что сделать:**
1. После `harness plan` (или явно `harness verify`):
   - для каждой задачи проверить существование файлов из секции `Файлы:`
     (relative to repo_root);
   - проверить что `Provides:` заполнен для задач с dependents;
   - проверить что acceptance criteria — исполняемые команды (начинаются с
     `` ` `` или `pytest`/`make`/`rg`/`grep`);
   - проверить что `depends_on` ссылаются на существующие id.
2. Красный → exit 1 с конкретными ошибками; `cmd_ingest` отказывается
   ingest'нуть без `--force`.

**Acceptance criteria:**
- [ ] `pytest tests/test_plan_verifier.py` — 5 кейсов (good plan, missing
      file, missing provides, bad AC, bad deps).
- [ ] `harness ingest "..."` после красного verify → exit 1.
- [ ] `harness verify` → markdown-отчёт.

**Запрещено:**
- Использовать LLM (это детерминированная проверка).

---

### v2-019 — Tiered pipeline depth

```yaml
id: "v2-019"
title: "Tiered pipeline depth: trivial|small|medium|large"
complexity: "high"
depends_on: ["v2-001"]
```

**Контекст:** Сейчас `complexity: normal|high` влияет только на модель
(escalation ladder). Ralphinho — tier определяет **число стадий**: trivial =
worker→gate; small = +review; medium = +research+plan+PRD-review+review-fix;
large = +final-review. Дешёвые задачи не получают дорогой пайплайн.

**Файлы:**
- `harness/domain/models.py` (`Task.complexity` — расширить enum)
- `harness/domain/state_machine.py` (новые статусы для medium/large:
  `RESEARCH`, `PLAN`, `PRD_REVIEW`, `REVIEW_FIX`, `FINAL_REVIEW`)
- `harness/scheduler/engine.py` (`_process_task` — branching по tier)
- `harness/policy/escalation.py` (модель по tier, не только attempt)
- `prompts/research.md`, `prompts/prd_review.md`, `prompts/final_review.md`
  (новые роли)
- `tasks/_TEMPLATE.md` (обновить `complexity` enum)
- `tests/test_tiered_pipeline.py`

**Что сделать:**
1. Расширить `complexity` до `trivial|small|medium|large` (обратная
   совместимость: `normal` → `small`, `high` → `medium`).
2. В `_process_task` — pipeline по tier:
   - trivial: worker → gate (без reviewer).
   - small: + reviewer (текущая схема).
   - medium: research → plan → worker → gate → PRD-review + code-review →
     review-fix.
   - large: + final-review.
3. Каждая стадия — отдельный agent-call в своём контексте, делят worktree.
4. Модель по tier: trivial=auto, small=auto→kimi, medium=kimi→glm, large=
   opus→kimi→glm.

**Acceptance criteria:**
- [ ] `pytest tests/test_tiered_pipeline.py` — 4 кейса по tier, проверка
      последовательности статусов.
- [ ] `complexity: trivial` задача проходит без reviewer-call.
- [ ] `complexity: large` задача проходит через `FINAL_REVIEW`.
- [ ] Старые спеки с `normal` → `small` (миграция мягкая).

**Запрещено:**
- Ломать существующие run'ы (миграция `normal→small`, `high→medium`).

---

### v2-020 — `NEEDS_INPUT` + question-protocol

```yaml
id: "v2-020"
title: "NEEDS_INPUT статус + question-protocol + /answer"
complexity: "high"
depends_on: ["v2-015"]
```

**Контекст:** Оркестратор↔пользователь — канала нет. Вся human-in-loop =
`HUMAN_GATE_MERGE` и `BLOCKED`. Оркестратор **никогда** не уточняет спеку.

**Файлы:**
- `harness/domain/enums.py` (`TaskStatus.NEEDS_INPUT`,
  `EventType.INPUT_REQUESTED, INPUT_ANSWERED`)
- `harness/domain/state_machine.py` (`RUNNING/REVIEW/NEEDS_CLARIFICATION →
  NEEDS_INPUT → RUNNING`)
- `harness/domain/models.py` (`Question` dataclass: id, question, options,
  answer)
- `harness/scheduler/engine.py` (`_ask_user` — пауза, Telegram inline-buttons)
- `harness/interface/cli.py` (`harness answer <run> <task> q1="..."`)
- `harness/interface/bot.py` (`/answer` команда, inline-keyboard)
- `prompts/replan.md` (новый формат `{"need_input": true, "questions": [...]}`)
- `harness/tasks_io/question.py` (парсер question-блока)
- `tests/test_question_protocol.py`

**Что сделать:**
1. Оркестратор (replan) / воркер (brief) могут вернуть
   `{"need_input": true, "questions": [{"id": "q1", "question": "...",
   "options": ["a","b"], "why": "..."}]}`.
2. Движок парсит, ставит `NEEDS_INPUT`, шлёт в Telegram inline-кнопки по
   `options`, пишет `INPUT_REQUESTED` event.
3. `harness answer <run> <task> q1="a"` или Telegram `/answer` — инжектит
   ответ в feedback, продолжает Run.
4. Таймаут (настраиваемо, по умолчанию 24h) → `BLOCKED`.

**Acceptance criteria:**
- [ ] `pytest tests/test_question_protocol.py` — replan с `need_input` →
      `NEEDS_INPUT`, `INPUT_REQUESTED` event.
- [ ] `harness answer ...` → `INPUT_ANSWERED`, задача `READY`, feedback
      содержит ответ.
- [ ] Telegram inline-кнопки работают (мок-тест).

**Запрещено:**
- Блокировать движок на ответ (Run PAUSED, не exception).

---

### v2-021 — Completion signal

```yaml
id: "v2-021"
title: "Completion signal: HARNESS_DONE magic-phrase"
complexity: "normal"
```

**Контекст:** Решает только ревьюер (APPROVE/CHANGES). ECC Continuous Claude —
воркер сам сигналом «я закончил», N=3 consecutive → стоп.

**Файлы:**
- `harness/scheduler/engine.py` (парсинг `HARNESS_DONE` в `wres.text`)
- `prompts/worker.md` (инструкция: «если выполнил все AC — выведи
  `HARNESS_DONE` последней строкой»)
- `harness/domain/enums.py` (`EventType.COMPLETION_SIGNAL`)
- `tests/test_completion_signal.py`

**Что сделать:**
1. Воркер выводит `HARNESS_DONE` → +1 к счётчику consecutive signals для
   задачи.
2. N=3 consecutive (настраиваемо `COMPLETION_SIGNAL_THRESHOLD`) → `DONE` без
   ревьюера (если гейты зелёные).
3. Между сигналами — обычный ревьюер.
4. Любой CHANGES сбрасывает счётчик.

**Acceptance criteria:**
- [ ] `pytest tests/test_completion_signal.py` — 3 `HARNESS_DONE` подряд с
      зелёными гейтами → `DONE`.
- [ ] CHANGES между сигналами сбрасывает счётчик.
- [ ] `HARNESS_DONE` без зелёных гейтов → игнор.

**Запрещено:**
- Заменять reviewer (сигнал — дополнение, не замена для high-tier).

---

### v2-022 — `harness chat` (CursorTaskRunner)

```yaml
id: "v2-022"
title: "harness chat — CursorTaskRunner поверх Task tool
complexity: "high"
depends_on: ["v2-009"]
```

**Контекст:** Идея пользователя — живые окна сабагентов в IDE. Сейчас runner =
subprocess (CLI) или SDK (`Agent.create`). Cursor IDE `Task` tool уже даёт
отдельные окна для сабагентов — нужно новый runner-adapter поверх него.

**Файлы:**
- `harness/runner/cursor_task_runner.py` (новый — `CursorTaskRunner:
  AgentRunner`)
- `harness/runner/factory.py` (добавить `RunnerKind.CURSOR_TASK`)
- `harness/interface/cli.py` (`harness chat` — режим, где текущий чат Cursor
  становится оркестратором, воркеры — Task tool)
- `harness/config.py` (`HARNESS_RUNNER=cursor_task` для chat-режима)
- `tests/test_cursor_task_runner.py`

**Что сделать:**
1. `CursorTaskRunner.run(prompt, model, cwd, log_path)` — вызвать Task tool
   (`subagent_type: generalPurpose`, `model`, `prompt`, `run_in_background:
   false`), дождаться финального message, обернуть в `AgentResult`.
2. `harness chat "<goal>"` — режим: текущий чат = root orchestrator, для
   каждой ready-задачи — `Task` вызов (сабагент в отдельном окне IDE).
3. Гейты/мерж/бюджет/DAG — на harness (движок не меняется).
4. Streaming: Task tool возвращает финальный message, не стрим — но окно IDE
   показывает всё живьём пользователю.

**Acceptance criteria:**
- [ ] `pytest tests/test_cursor_task_runner.py` — мок Task tool, проверка
      `AgentResult`.
- [ ] `HARNESS_RUNNER=cursor_task harness run` использует новый runner.
- [ ] Документация в `GUIDE.md` — когда `chat` vs `run`.

**Запрещено:**
- Менять `scheduler/engine.py` (контракт `AgentRunner` тот же).

---

### v2-023 — Orchestrator checkpoint (`PLAN_DRAFT`)

```yaml
id: "v2-023"
title: "Orchestrator checkpoint: PLAN_DRAFT перед ingest
complexity: "normal"
depends_on: ["v2-018"]
```

**Контекст:** QUICKSTART §D говорит «не запускай run, пока я не подтвердил PLAN»
— ручное правило. Должно быть механическое.

**Файлы:**
- `harness/domain/enums.py` (`RunStatus.PLAN_DRAFT`)
- `harness/interface/cli.py` (`cmd_plan` ставит `PLAN_DRAFT`; `cmd_ingest`
  требует `--approve` или интерактивного Q&A)
- `tests/test_plan_draft.py`

**Что сделать:**
1. `harness plan` → пишет `PLAN.md` + tasks, создаёт Run со status
   `PLAN_DRAFT` (не `planning`).
2. `harness ingest` для `PLAN_DRAFT` run'а:
   - `--approve` флаг → `planning`, дальше как обычно;
   - без `--approve` в TTY → показывает summary, спрашивает `[y/N]`;
   - без `--approve` в non-TTY → exit 1 с подсказкой.
3. `harness verify` автоматически перед ingest (из v2-018).

**Acceptance criteria:**
- [ ] `pytest tests/test_plan_draft.py` — `plan` создаёт `PLAN_DRAFT`,
      `ingest` без `--approve` в non-TTY → exit 1.
- [ ] `harness ingest --approve` → `planning`.

**Запрещено:**
- Ломать существующий flow (`ingest` после `plan` работал — теперь с
  `--approve`).

---

### v2-024 — `SHARED_TASK_NOTES.md` per task

```yaml
id: "v2-024"
title: "SHARED_TASK_NOTES.md — мост между попытками
complexity: "normal"
```

**Контекст:** feedback = только текст последнего ревьюера. ECC Continuous
Claude — файл-мост между итерациями: Progress / What worked / Next.

**Файлы:**
- `harness/scheduler/engine.py` (перед worker-call — читать
  `worktree/SHARED_TASK_NOTES.md`; после — обновлять)
- `prompts/worker.md` (инструкция: читай NOTES в начале, пиши в конце)
- `tests/test_shared_notes.py`

**Что сделать:**
1. В worktree воркера — `SHARED_TASK_NOTES.md` (если нет, создаётся пустым).
2. В `_worker_prompt` — блок `=== SHARED TASK NOTES ===\n{content}`.
3. После `wres` — воркер должен обновить NOTES (парсим `=== NOTES UPDATE ===`
   секцию в output, append в файл).
4. Файл переживает попытки (не удаляется между ними), удаляется только при
   `remove(task_id)`.

**Acceptance criteria:**
- [ ] `pytest tests/test_shared_notes.py` — 2 попытки, вторая видит NOTES
      первой.
- [ ] NOTES содержит `## Progress`, `## What worked`, `## Next steps`.

**Запрещено:**
- Перезаписывать NOTES (только append / structured update).

---

### v2-025 — Lessons-learned → `brain/lessons/`

```yaml
id: "v2-025"
title: "Lessons-learned → brain/lessons/<service>/<pattern>.md
complexity: "normal"
depends_on: ["v2-024"]
```

**Контекст:** Память между прогонами отсутствует. После терминального статуса
— оркестратор пишет lesson в `brain/`. `cmd_plan` читает последние N.

**Файлы:**
- `harness/scheduler/engine.py` (`_finalize` — для каждой задачи вызов
  lesson-extractor)
- `harness/lessons/extractor.py` (новый — LLM-call: «дай lesson по исходу
  задачи»)
- `prompts/lesson.md` (новый)
- `brain/lessons/` (структура: `<service>/<pattern>.md`)
- `tests/test_lessons.py`

**Что сделать:**
1. После `DONE` / `BLOCKED` задачи — оркестратор (kimi, дёшево) читает спеку +
   attempts + reviews → пишет 3-5 предложений в `brain/lessons/<service>/<id>.md`.
2. Формат: `## Pattern`, `## What worked`, `## What failed`, `## Suggested
   approach next time`.
3. `cmd_plan` читает последние N lessons для проекта, инжектит в промпт
   оркестратора: «=== PAST LESSONS ===».

**Acceptance criteria:**
- [ ] `pytest tests/test_lessons.py` — мок, после `DONE` создаётся lesson-файл.
- [ ] `cmd_plan` включает `PAST LESSONS` блок.
- [ ] `brain/lessons/<service>/` структура.

**Запрещено:**
- Писать lessons для тривиальных задач (только `medium`/`large`).

---

### v2-026 — Multi-run memory в `cmd_plan`

```yaml
id: "v2-026"
title: "Multi-run memory: summary прошлых run'ов в plan
complexity: "normal"
depends_on: ["v2-025"]
```

**Контекст:** Оркестратор видит только GOAL строку. Не знает, что прошлые 3
задачи на JWT закончились CHANGES с причиной X.

**Файлы:**
- `harness/interface/cli.py` (`cmd_plan` — читать последние N run'ов)
- `harness/store/repository.py` (`list_recent_runs(project, limit)`)
- `tests/test_plan_multi_run_memory.py`

**Что сделать:**
1. В `cmd_plan` — `list_recent_runs(project, 5)`.
2. Для каждого — summary: goal, % done, blocked tasks, top failure reasons.
3. Инжектить в промпт оркестратора: `=== PAST RUNS ===`.

**Acceptance criteria:**
- [ ] `pytest tests/test_plan_multi_run_memory.py` — мок store с 3 run'ами →
      промпт содержит `PAST RUNS`.
- [ ] `cmd_plan` оркестратор видит прошлые провалы.

**Запрещено:**
- Более 5 run'ов (контекст).

---

### v2-027 — Instincts (opt-in)

```yaml
id: "v2-027"
title: "Instincts — auto-extract паттернов с confidence
complexity: "high"
depends_on: ["v2-025"]
```

**Контекст:** ECC `continuous-learning-v2` — instincts с confidence scoring,
`/evolve` кластеризует в skills. Для нас — опционально, позднее.

**Файлы:**
- `harness/lessons/instincts.py` (новый)
- `brain/instincts/` (структура)
- `harness/interface/cli.py` (`harness instincts` — list, evolve)
- `tests/test_instincts.py`

**Что сделать:**
1. Из lessons — extract паттернов (LLM-call: «найди повторяющийся паттерн»).
2. Confidence = сколько раз паттерн встречался / общее число lessons.
3. `harness instincts list` — топ по confidence.
4. `harness instincts evolve` — кластеризация в skills (копирует в
   `.cursor/skills/`).

**Acceptance criteria:**
- [ ] `pytest tests/test_instincts.py`.
- [ ] 3 lessons с одинаковым паттерном → instinct с confidence 1.0.

**Запрещено:**
- Включать по умолчанию (opt-in через `HARNESS_INSTINCTS=1`).

---

### v2-028 — De-sloppify отдельный pass

```yaml
id: "v2-028"
title: "De-sloppify отдельный cleanup-pass
complexity: "normal"
depends_on: ["v2-001"]
```

**Контекст:** Ревьюер совмещает качество+безопасность. ECC autonomous-loops §5
— отдельный cleanup-pass в своём контексте: сносит over-defensive checks,
тесты языка, console.log, мёртвый код. «Два focused агента > один
constrained».

**Файлы:**
- `prompts/de-sloppify.md` (новый — cleanup-роль)
- `harness/scheduler/engine.py` (после worker→gate, перед reviewer —
  отдельный agent-call для `small`/`medium`/`large` tier)
- `harness/domain/enums.py` (`EventType.DE_SLOPPIFIED`)
- `tests/test_de_sloppify.py`

**Что сделать:**
1. После зелёных гейтов, перед reviewer — `de-sloppify` агент в worktree
   (своя модель `auto`, дёшево).
2. Читает diff, сносит: тесты языка/фреймворка, redundant type checks,
   over-defensive error handling, console.log, commented-out code.
3. Гоняет гейты после cleanup. Если красные → откат, CHANGES.
4. Reviewer видит уже очищенный diff.

**Acceptance criteria:**
- [ ] `pytest tests/test_de_sloppify.py` — мок, воркер оставил console.log →
      de-sloppify снёс.
- [ ] Гейты после de-sloppify зелёные (или откат).
- [ ] Не запускается для `trivial` tier.

**Запрещено:**
- Менять бизнес-логику (только cleanup).

---

### v2-029 — Merge queue with eviction context

```yaml
id: "v2-029"
title: "Merge queue with eviction context
complexity: "normal"
```

**Контекст:** На merge conflict — `note="merge conflict"`, feedback пустой.
Ralphinho — полный контекст (diffs, conflicting files, test output) → в
feedback следующей попытки. Не слепой retry.

**Файлы:**
- `harness/scheduler/engine.py` (`_merge` — при конфликте собрать eviction
  context)
- `harness/worktree/manager.py` (`merge_to_base` — вернуть конфликтующие
  файлы и diff)
- `tests/test_eviction_context.py`

**Что сделать:**
1. `MergeOutcome` расширить: `conflicting_files: list[str]`,
  `conflict_diff: str`.
2. При `merged=False, conflict=True` — собрать контекст.
3. В feedback следующей попытки: `=== MERGE CONFLICT ===\n<conflict_diff>`.
4. Event `MERGE_EVICTED` с контекстом.

**Acceptance criteria:**
- [ ] `pytest tests/test_eviction_context.py` — мок merge conflict →
      feedback содержит `MERGE CONFLICT` блок.
- [ ] `MERGE_EVICTED` event с conflicting_files.

**Запрещено:**
- Авто-resolve (воркер сам решает на следующей попытке).

---

### v2-030 — Pass@k / pass^k метрики

```yaml
id: "v2-030"
title: "pass@k / pass^k метрики в status
complexity: "normal"
```

**Контекст:** Гейты на каждой попытке, без метрик. ECC `eval-harness` —
количественная оценка «агент справляется».

**Файлы:**
- `harness/store/repository.py` (`pass_at_k(run_id, k)`, `pass_all_k`)
- `harness/interface/cli.py` (`harness status` — блок метрик)
- `tests/test_pass_at_k.py`

**Что сделать:**
1. `pass@k` = ≥1 из k попыток успех / всего задач.
2. `pass^k` = все k попыток успех / всего.
3. В `harness status` — блок `Metrics: pass@1=0.7 pass@3=0.91 pass^3=0.34`.

**Acceptance criteria:**
- [ ] `pytest tests/test_pass_at_k.py` — синтетические данные, корректные
      метрики.
- [ ] `harness status` показывает метрики.

**Запрещено:**
- Считать по `trivial` задачам (они без попыток).

---

### v2-031 — AgentShield-скан промптов/правил

```yaml
id: "v2-031"
title: "AgentShield-скан промптов/правил/MCP
complexity: "high"
```

**Контекст:** `guard.sh` только на деструктивные команды. ECC `ecc-agentshield`
— скан конфигов на injection/secret-leak, 102 static rules + adversarial
red-team/blue-team на Opus.

**Файлы:**
- `harness/security/scanner.py` (новый — static rules)
- `harness/security/red_team.py` (новый — adversarial, opt-in на Opus)
- `harness/interface/cli.py` (`harness scan` команда, `harness doctor` —
  включает scan)
- `tests/test_security_scan.py`

**Что сделать:**
1. Static rules: secrets в `prompts/` (regex `sk-`, `ghp_`, `AKIA`),
  injection в `.cursor/rules/` (опасные bash-команды без sandbox), MCP
  configs (overly permissive).
2. `harness scan` → markdown-отчёт A-F.
3. Adversarial (opt-in `--opus`): red-team/blue-team/auditor на Opus, как в
  ECC.
4. `harness doctor` включает static scan, exit 2 на critical.

**Acceptance criteria:**
- [ ] `pytest tests/test_security_scan.py` — 5 кейсов (secret, injection,
  bad MCP, clean, opus-mode skip without key).
- [ ] `harness scan` → markdown-отчёт.
- [ ] `harness doctor` падает на critical-секрете в `prompts/`.

**Запрещено:**
- Включать `--opus` по умолчанию (дорого).

---

### v2-032 — Strategic compaction

```yaml
id: "v2-032"
title: "Strategic compaction для долгих воркеров
complexity: "normal"
depends_on: ["v2-011"]
```

**Контекст:** Долгие воркеры упираются в контекст. ECC `strategic-compact` —
manually compact на logical breakpoints, не ждать 95% авто.

**Файлы:**
- `harness/scheduler/engine.py` (при `context_window_remaining < 30%` —
  инжектить compact-команду в feedback)
- `prompts/worker.md` (инструкция compact)
- `tests/test_strategic_compact.py`

**Что сделать:**
1. При `CONTEXT_LOW` event — в feedback следующей попытки:
   «`/compact` — сохрани: спека, SHARED_TASK_NOTES, уже сделанные шаги.
   Снеси: exploration, неудачные подходы».
2. Воркер сам зовёт `/compact` (через tool-call, видим в agent_events).

**Acceptance criteria:**
- [ ] `pytest tests/test_strategic_compact.py` — `CONTEXT_LOW` → feedback
      содержит compact-инструкцию.
- [ ] `agent_events` показывает `tool_call: compact`.

**Запрещено:**
- Авто-compact без воркера (воркер решает).

---

### v2-033 — CI failure recovery

```yaml
id: "v2-033"
title: "CI failure recovery (после GitHub PR
complexity: "high"
depends_on: ["v2-014"]
```

**Контекст:** Локальный merge, без PR/CI. После ROADMAP 3.14 — gate fail на
PR → `gh run view <id>` → fix-pass с логами CI. ECC Continuous Claude §"CI
Failure Recovery".

**Файлы:**
- `harness/scheduler/engine.py` (после `git push` — poll `gh pr checks`,
  на fail — fix-pass)
- `harness/integrations/github.py` (новый — `gh` CLI wrapper)
- `tests/test_ci_recovery.py`

**Что сделать:**
1. После push (если `HARNESS_PUSH_AFTER_MERGE=1` и есть PR) — poll `gh pr
  checks --watch` (timeout 10 min).
2. Fail → fetch `gh run view <failed-id> --log-failed`, fix-pass с этим
  контекстом в feedback.
3. Re-push, re-poll. Max `CI_RETRY_MAX=1` попыток.

**Acceptance criteria:**
- [ ] `pytest tests/test_ci_recovery.py` — мок `gh`, fail → fix-pass с
      логами.
- [ ] `CI_RETRY_MAX=0` отключает.

**Запрещено:**
- Без ROADMAP 3.14 (GitHub PR-интеграция) — задача blocked.

---

### v2-034 — Durable-плейн в боевое

```yaml
id: "v2-034"
title: "EngineTaskExecutor — durable в боевое
complexity: "high"
depends_on: ["v2-009"]
```

**Контекст:** `harness/durable/executor.py` — fake. Нужен реальный с
worktree+runner+ProfileGate. Связано с ROADMAP 3.13 (распределённые воркеры).

**Файлы:**
- `harness/durable/executor.py` (реальный impl)
- `harness/durable/orchestrator.py` (без изменений контракта)
- `tests/test_durable_executor.py` (live, требует Postgres)

**Что сделать:**
1. `EngineTaskExecutor.worker/gate/review/merge` — реальная работа через
  `WorktreeManager`, `runner`, `ProfileGate`.
2. Идемпотентность: повторный прогон шага — no-op (статус уже dst).
3. Live-тест на Postgres (skip без `HARNESS_DURABLE=1`).

**Acceptance criteria:**
- [ ] `pytest tests/test_durable_executor.py` — live, 2 задачи end-to-end.
- [ ] Crash в середине → resume с последнего step.

**Запрещено:**
- Менять `DurableOrchestrator` контракт.

---

### v2-035 — Skill-stocktake

```yaml
id: "v2-035"
title: "Skill-stocktake — аудит промптов/скиллов
complexity: "normal"
depends_on: ["v2-025"]
```

**Контекст:** 7 скиллов + 4 промпта, без аудита. ECC `skill-stocktake` —
какие реально читались, какие жрут контекст зря.

**Файлы:**
- `harness/skills/stocktake.py` (новый)
- `harness/interface/cli.py` (`harness stocktake`)
- `tests/test_stocktake.py`

**Что сделать:**
1. Сканировать `prompts/` и `.cursor/skills/`: размер, последний раз
  изменён, упоминания в `agent_events`.
2. Топ «жрутщих контекст» (большие файлы, редко упоминаемые).
3. `harness stocktake` → markdown-отчёт с рекомендациями.

**Acceptance criteria:**
- [ ] `pytest tests/test_stocktake.py`.
- [ ] `harness stocktake` → отчёт с размерами и usage.

**Запрещено:**
- Авто-удаление (только отчёт).

---

## Карта файлов (быстрая навигация для воркеров)

```
1.harness/
  harness/
    scheduler/engine.py        ← v2-001, 004, 006, 013, 015, 019, 020, 021, 024, 025, 028, 029, 032, 033
    interface/cli.py           ← v2-002, 010, 012, 020, 023, 026, 031, 035
    runner/
      base.py                  ← v2-003, 009, 011
      sdk_runner.py            ← v2-003, 009, 011
      cli_runner.py            ← v2-003, 007, 009, 011
      cursor_task_runner.py    ← v2-022 (новый)
      factory.py               ← v2-022
    domain/
      enums.py                 ← v2-013, 015, 020, 021, 023, 028
      state_machine.py         ← v2-015, 019, 020
      models.py                ← v2-019, 020
    store/repository.py        ← v2-003, 005, 009, 011, 026, 030
    store/schema.sql           ← v2-003, 009, 011
    tasks_io/parser.py         ← v2-004
    tasks_io/verifier.py       ← v2-018 (новый)
    tasks_io/brief.py          ← v2-015 (новый)
    tasks_io/question.py       ← v2-020 (новый)
    policy/scope.py            ← v2-013 (новый)
    policy/escalation.py       ← v2-019
    lessons/extractor.py       ← v2-025 (новый)
    lessons/instincts.py       ← v2-027 (новый)
    security/scanner.py        ← v2-031 (новый)
    security/red_team.py       ← v2-031 (новый)
    integrations/github.py     ← v2-033 (новый)
    dashboard/app.py           ← v2-014 (новый)
    skills/stocktake.py        ← v2-035 (новый)
    durable/executor.py        ← v2-034
  prompts/
    worker.md                  ← v2-001, 004, 015, 016, 017, 021, 024, 032
    reviewer.md                ← v2-001
    orchestrator.md            ← v2-008
    replan.md                  ← v2-020
    research.md                ← v2-019 (новый)
    prd_review.md              ← v2-019 (новый)
    final_review.md            ← v2-019 (новый)
    lesson.md                  ← v2-025 (новый)
    de-sloppify.md             ← v2-028 (новый)
  tasks/_TEMPLATE.md           ← v2-004, 019
  .cursor/agents/*             ← v2-008
  .cursor/skills/iterative-retrieval/  ← v2-016 (новый)
  .cursor/rules/30-graphify.mdc        ← v2-017 (новый)
  brain/lessons/<service>/     ← v2-025 (структура)
  brain/instincts/             ← v2-027 (структура)
  tests/test_*.py              ← каждая задача
```

## Definition of Done для Phase 6 (всего ADR-0005)

1. Все 35 задач `done` (или осознанно `blocked` с reason).
2. Хотя бы один реальный проект на VPS прошёл цель end-to-end с:
   - live-tail `harness tail` работает в соседнем терминале;
   - `harness status` показывает `tokens`, `cost_kind=actual`, `pass@k`;
   - оркестратор задал ≥1 вопрос через `NEEDS_INPUT` и получил ответ;
   - `brain/lessons/<service>/` содержит ≥1 lesson после run.
3. `harness doctor` зелёный, `harness scan` без critical.
4. ADR-0005 статус `accepted`.
