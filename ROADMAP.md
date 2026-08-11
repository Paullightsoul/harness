# ROADMAP / HANDOFF — что остаётся доделать

Документ передачи: текущее состояние, как поднять на сервере и что доработать.
Архитектура — в `ARCHITECTURE.md`. Primary path — **TaskTool-first pull**
(`harness tasktool …`, skill `harness-orchestrate`). Background unattended —
`BACKLOG-background-mode.md` (не default). Этот файл — про «что дальше».

---

## 0. TaskTool harness v3 (primary)

| Слой | Состояние | Файлы |
|---|---|---|
| ADR / architecture docs | ✅ зафиксировано в дереве docs (+ ADR-0011 вне этого репо) | `ARCHITECTURE.md`, `GUIDE.md`, … |
| PlanArtifact / DispatchEnvelope | ✅ protocol 3.0 | `harness/tasktool/models.py` |
| Pull controller + CLI | ✅ `start/next/report/advance/status/abort/resume` | `harness/tasktool/`, `interface/cli.py` |
| Dispatch leases + store migration | ✅ stale recover ~5 min | `harness/store/` |
| ResourcePolicy | ✅ 2/2/1/1 + soft 3 GiB / hard 2 GiB | `harness/tasktool/resources.py` |
| Scoped + full gates + gate lock | ✅ | `lifecycle.py`, `profile_gate.py` |
| Checkpoints / review bundle / context pack | ✅ | `checkpoint.py`, `review_bundle.py`, `context.py` |
| Skill `harness-orchestrate` | ✅ primary; chat-orchestrator = legacy | `.cursor/skills/` |
| Generated artifact exclusion | ✅ | `harness/worktree/manager.py` |
| Fake-driver / unit TaskTool tests | ✅ присутствуют (`tests/test_tasktool_*`) | verification commands |
| Isolated real dogfood | ⏳ только после зелёных verification commands | не production claim |
| Legacy file-spool / `harness mode chat` | ⚠️ deprecated compatibility | `runner/bridge.py`, `mode` |
| Background / cgroups / remote workers | ❌ backlog | `BACKLOG-background-mode.md` |

**Не утверждать:** «production-ready unattended» или «DBOS fleet готов как default».

---

## 1. Статус на сейчас (legacy push Engine layers)

| Слой | Состояние | Файлы |
|---|---|---|
| Domain (автомат, модели) | ✅ готово, покрыт тестами | `harness/domain/` |
| Store (SQLite + журнал) | ✅ готово (+ dispatches) | `harness/store/` |
| Runner (SDK + CLI) | ✅ каркас; legacy vs TaskTool path | `harness/runner/` |
| Gates (`make check` / profile) | ✅ готово | `harness/gates/` |
| Worktree + merge queue | ✅ каркас | `harness/worktree/` |
| Парсер PLAN.md/tasks | ✅ готово, покрыт тестами | `harness/tasks_io/` |
| Policy (бюджет, эскалация) | ✅ готово (push Engine) | `harness/policy/` |
| Scheduler (DAG + движок) | ✅ каркас; compatibility | `harness/scheduler/` |
| CLI / реестр / нотификации | ✅ + `tasktool` | `harness/interface/`, `harness/projects/` |
| Bash-вариант (Phase 0) | ✅ рабочий, остаётся | `scripts/` |

**Проверено локально (исторический push Engine + нарастающий TaskTool):** Python 3.12;
verification commands (`ruff` / `mypy` / pytest including `tests/test_tasktool_*`) —
гоняй перед dogfood. Isolated dogfood TaskTool — только после зелёных результатов;
это **не** blanket production claim.

**Durable-плейн (ADR-0003):** опциональный experiment (`HARNESS_DURABLE=1`); для
unattended fleet см. `BACKLOG-background-mode.md` — не default TaskTool path.

**Фаза 3 (ADR-0003) push Engine:** лестница эскалации, re-plan, anti-gaming, sandbox
остаются на legacy path. TaskTool primary использует resource policy + scoped/full
gates вместо умножения full-suite на каждую параллельную задачу.

**Вживую (TaskTool):** не утверждать полный production E2E без явного dogfood
результата в этой ветке. Legacy `harness run` E2E также требует cursor-agent/SDK
на хосте.

---

## 2. Поднять на сервере (по шагам)

**Primary (TaskTool-first):** см. `QUICKSTART.md` — `make setup`, register project,
Cursor chat «используй harness», `harness tasktool …`. Не инициализируй git в `/home`.

**Legacy push Engine (compatibility):**

```bash
# Python 3.12+, git, make
cd /home/1.harness
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# pip install -e ".[sdk]"   # только если нужны SDK-роли

# Cursor CLI — только для legacy CLI workers
# curl https://cursor.com/install -fsS | bash

export CURSOR_API_KEY="cursor_..."   # legacy SDK

ruff check harness tests
mypy harness
# verification commands / TaskTool tests — см. GUIDE §10

# Legacy cycle (не primary):
harness plan "Маленькая фича для проверки"
harness ingest "Маленькая фича" --project test
harness run --approve-plan
harness status
harness events
```

⚠️ **Экономика:** `WORKER_RUNNER=cli` по умолчанию (подписка). SDK тарифицируется из
пула API. Перед боевым прогоном выставь **Spend Limit** в `cursor.com/dashboard → Billing`
и при желании `export RUN_BUDGET=<кредитов>` как потолок на один Run.

---

## 3. Что доработать (по приоритету)

### P0 — довести каркас до боевого состояния

- [x] **3.1 Прогнать mypy strict на 3.11.** Сделано на Python 3.12: `ruff check harness tests`
  и `mypy harness` (35 файлов) — зелёные. Чистка: `StrEnum`, `X | None`, импорты наверх,
  `zip(strict=...)`, helper `_advance` для длинных строк.
- [ ] **3.2 Живой smoke цикла.** На реальном мелком репозитории прогнать полный путь
  worker→gates→reviewer→merge на одной задаче. Убедиться, что:
  - `cursor-agent` пишет изменения в worktree (cwd),
  - `make check` ловит провал,
  - вердикт ревьюера парсится (`VERDICT: APPROVE|CHANGES`),
  - мерж в `base` проходит и worktree снимается.
- [x] **3.3 Recovery прерванных задач.** Сделано: добавлен статус `RECOVERING` и
  `RECOVERABLE_STATUSES` в автомат; `Engine._recover_interrupted()` при старте `run`
  переводит in-flight задачи (RUNNING/GATING/REVIEW/MERGE_QUEUE) через RECOVERING → READY
  с событием `RECOVERED`. Покрыто `tests/test_recovery.py`.
  Известный edge: задача, упавшая после фактического merge, но до перехода в DONE,
  будет переисполнена (повторный `merge --no-ff` обычно no-op) — допустимо для каркаса.
- [x] **3.4 Сверить имена моделей** (`opus-4.8` → `claude-opus-4-8-thinking-high`, `kimi-k2.5` ✓, `glm-5.2-high` ✓, `auto` ✓) с `cursor-agent models`.
  Дефолты поправлены в `harness/config.py`, `scripts/orch/config.py`, `scripts/lib.sh`, `.env.example`.
- [ ] **3.5 Сверить интерфейс SDK** (`Agent.create/send/wait`, поля `result.status`,
  `result.cost_credits`) с реальной версией `cursor-sdk` в `harness/runner/sdk_runner.py` —
  писалось по доке, могло разойтись.

### P1 — Phase 3: «умный лид» (re-plan)

- [x] **3.6 Реальный re-plan в `Engine._escalate`.** Сделано: `harness/replan.py`
  (парсинг решения `refine|split|block`), `prompts/replan.md`, бюджет `MAX_REPLANS`.
  При исчерпании лестницы задача уходит оркестратору (с фидбэком/гейтами) → он либо
  уточняет спеку (refine, свежие попытки), либо дробит на под-задачи (split, влиты в
  store, DAG пересобирается), либо block. Покрыто `tests/test_replan.py`.
  Остаётся: живой прогон с реальным оркестратором (часть 3.2) и repointing зависимых
  при split (сейчас split лучше для leaf-задач).
- [x] **3.7 Anti-gaming тестов.** Сделано: `harness/policy/guard.py` (защищённая зона
  `tests/spec/**`, настройка `PROTECTED_PATHS`); движок после гейтов проверяет git-diff
  и при правке зоны даёт авто-CHANGES без вызова ревьюера (событие `GUARD_VIOLATION`).
  `scripts/hooks/guard.sh` дополнен best-effort deny на shell-запись в зону. Правило
  добавлено в конституцию. Покрыто `tests/test_guard.py`.
- [x] **3.8 Учёт стоимости для CLI-ролей.** Сделано: эвристическая оценка в `cli_runner.py`
  (`_estimate_cost`) по имени модели. Не идеально, но даёт реалистичный бюджетный контроль.

### P2 — Phase 4: контроль и наблюдаемость

- [x] **3.9 Telegram Notifier + бот.** `harness/interface/notify.py` (Null/Console/Telegram,
  async, httpx) — вызовы на BLOCKED / BUDGET_EXCEEDED / HUMAN_GATE_WAIT / Run done. Плюс
  двухсторонний бот `harness/interface/bot.py` (`harness bot`: `/status` `/runs` `/approve`).
- [x] **3.10 Human-gate на мерж.** `Engine.approve_merge()` + `harness approve <run> <task>`
  (и `/approve` в боте): сливает MERGE_QUEUE-задачу и продолжает Run.
- [x] **3.11 Мульти-проект.** `Engine(..., repo_root=...)` + `--project` в CLI (ingest/run/
  approve) через реестр `harness/projects/`. Дом harness (prompts/tasks/state.db) отделён от
  целевого репо; у каждого репо свой `.harness/project.toml`.

### P3 — Phase 5: масштаб

- [ ] **3.12 TUI/web-дашборд** поверх журнала событий (live-tail, таймлайн задач,
  расход бюджета). Источник — таблица `events` (`store.list_events`).
- [ ] **3.13 Распределённые воркеры** по нескольким VPS (очередь задач между серверами).
- [ ] **3.14 GitHub PR-интеграция** вместо локального мержа (auto-create PR, ревью на PR).

### P4 — Phase 6: Harness v2 — наблюдаемость, понимание, канал↔пользователь, память

> ADR-0005. Концепты портированы с [affaan-m/ECC](https://github.com/affaan-m/ECC)
> (Ralphinho DAG, iterative-retrieval, continuous-learning-v2, cost-aware-llm-pipeline,
> verification-loop, de-sloppify, ecc2 control-plane). Прожарка кода + прогона
> run-20260630-093910 вскрыла 13 дыр; этот раздел их закрывает.

**Фаза 1 — Починки фундамента (P0, 2-3 дня).** Без этого остальные фазы строят на песке.

- [ ] **6.1.1** Reviewer в worktree воркера, не в `self._s.root`
  (`harness/scheduler/engine.py:211`). ECC: Ralphinho "Worktree Isolation".
- [ ] **6.1.2** `harness plan --project` с `cwd=repo_root` оркестратора
  (`harness/interface/cli.py:62`).
- [ ] **6.1.3** Точный cost из SDK: проверить поля `usage`/`billing` у `result`, не
  только `cost_credits`; пометить `cost_kind=estimate|actual`
  (`sdk_runner.py:63-69`, `cli_runner.py:15-49`). ECC: cost-aware-llm-pipeline.
- [ ] **6.1.4** `Provides:` обязателен в spec + воркер заполняет его в output; парсер
  валидирует (`prompts/worker.md`, `tasks_io/parser.py:50`). ECC: Ralphinho "Data Flow".
- [ ] **6.1.5** Idempotent ingest: дедуп по `(project, goal_hash, base_branch)` → не 3
  run'а (`harness/ingest.py`).
- [ ] **6.1.6** Verdict parser malformed-safe: regex `VERDICT: APPROVE|CHANGES`,
  fallback = ESCALATE (не CHANGES), чтобы не сжигать попытки
  (`engine.py:509-525`).
- [ ] **6.1.7** Prompt-file вместо `-p` argv: tmp-файл + `--prompt-file`/stdin
  (`cli_runner.py:64-70`).
- [ ] **6.1.8** Унификация промптов: `.cursor/agents/*.md` синхронизировать с `prompts/`
  (канон = `prompts/`).

**Фаза 2 — Наблюдаемость (P0, 3-4 дня).** Закрывает "не понимаю, чем живёт агент".

- [ ] **6.2.1** Stream-режим runner'а: `run.stream()` (или аналог), события в
  `logs/worker-XXX-aN.ndjson` **и** в event-log как `AGENT_EVENT` (tool_call /
  assistant_msg / file_edit / usage). Живой транскрипт вместо эпитафии.
- [ ] **6.2.2** `harness tail [run_id] [--task ID]`: long-poll поверх
  `events WHERE id > ?`. Live-tail в терминале. ECC: ecc2 `status`/`sessions`.
- [ ] **6.2.3** Token-учёт в `AgentResult`: `tokens_in/out`,
  `context_window_remaining` из SDK; для CLI — парсинг stderr cursor-agent.
- [ ] **6.2.4** `harness status --markdown --write status.md`: portable handoff.
  ECC: `ecc status --markdown`.
- [ ] **6.2.5** Dashboard (связано с 3.12): FastAPI + WebSocket, таймлайн задач +
  живой транскрипт выбранного воркера + budget-метр + current model. ECC:
  `ecc_dashboard.py`, ecc2 `dashboard`.
- [ ] **6.2.6** Контекст-монитор: warnings в event-log при
  `context_window_remaining < 20%`, scope-выходе (файлы вне `Файлы:`), loop-stuck
  (один tool-call > 3 раз). ECC: `ECC_CONTEXT_MONITOR_*`.

**Фаза 3 — Agent Understanding (P1, 2-3 дня).** Закрывает "агент реально всё понял".

- [ ] **6.3.1** Pre-flight understanding-brief: перед основным прогоном воркер
  возвращает машиночитаемый JSON (`understand`, `files_i_will_touch`,
  `acceptance_i_will_satisfy`, `assumptions`, `missing_context`). Если `missing_context`
  непустой → новый статус `NEEDS_CLARIFICATION`. ECC: iterative-retrieval EVALUATE.
- [ ] **6.3.2** Iterative-retrieval в `prompts/worker.md`: DISPATCH (rg/graphify) →
  EVALUATE (relevance scoring) → REFINE → LOOP ≤3, **до** написания кода.
  ECC: `skills/iterative-retrieval/SKILL.md`.
- [ ] **6.3.3** `graphify` в воркере: `prompts/worker.md` — "перед новым кодом
  `graphify query`". `search-first` скилл есть, но без инструмента.
- [ ] **6.3.4** Plan-verification шаг (детерминированный, без агента): после
  `harness plan` — для каждой задачи проверить существование файлов, совместимость
  `provides`, что acceptance criteria — исполняемые команды. Красный план → не пускать
  в `ingest`. ECC: Ralphinho "Decomposition Rules".
- [ ] **6.3.5** Tiered pipeline depth (Ralphinho): `complexity: trivial|small|medium|large`
  вместо `normal|high`. trivial = worker→gate (без reviewer); small = +review;
  medium = +research+plan+PRD-review+review-fix; large = +final-review. Меняет **число
  стадий**, не только модель. ECC: autonomous-loops §6.

**Фаза 4 — Orchestrator ↔ User channel (P1, 2-3 дня).** Закрывает "оркестратор ходит
ко мне".

- [ ] **6.4.1** `NEEDS_INPUT` статус + question-protocol: оркестратор/воркер возвращают
  `{"need_input": true, "questions": [...]}` с options. Движок ставит `NEEDS_INPUT`,
  шлёт в Telegram с inline-кнопками, пишет `INPUT_REQUESTED` в events.
  `/answer run-id task-id q1="..."` — инжектит ответ в feedback. Расширить
  `prompts/replan.md` форматом question.
- [ ] **6.4.2** Completion signal: воркер может выдать `HARNESS_DONE` magic-phrase.
  N=3 consecutive → стоп. Дополнительный сигнал помимо APPROVE. ECC: Continuous Claude
  §"Completion Signal".
- [ ] **6.4.3** Chat-native режим `harness chat`: новый `CursorTaskRunner: AgentRunner`
  поверх Task tool — сабагенты получают отдельные окна в IDE. `scheduler/engine.py` не
  меняется. Параллельно с `harness run` (subprocess).
- [ ] **6.4.4** Orchestrator checkpoint перед run: `harness plan` заканчивается
  `status=PLAN_DRAFT`; `ingest` требует `--approve` или Q&A в `chat` режиме.
  Механическое правило вместо "руками проверь PLAN". ECC: longform "Human review at
  leverage points".

**Фаза 5 — Память и обучение (P2, 3-4 дня).** Подключает `AI_MEMORY.md`/`brain/`.

- [ ] **6.5.1** `SHARED_TASK_NOTES.md` per task: файл в worktree, воркер читает в начале
  попытки, пишет в конце (Progress / What worked / Next). Мост между попытками. ECC:
  Continuous Claude §"Cross-Iteration Context".
- [ ] **6.5.2** Lessons-learned после run: после терминального статуса — оркестратор
  пишет в `brain/lessons/<service>/<pattern>.md`. `cmd_plan` читает последние N.
  ECC: `skills/continuous-learning-v2/`.
- [ ] **6.5.3** Multi-run memory в `cmd_plan`: оркестратор получает не только GOAL, но и
  summary последних N run'ов из store (типы задач, модели, попытки, исходы). ECC:
  memory-persistence hooks.
- [ ] **6.5.4** Instincts (опционально, позднее): extract паттернов из завершённых
  run'ов с confidence scoring; `/harness instincts` CLI. Хранить в `brain/instincts/`.
  ECC: `skills/continuous-learning-v2/`, `/evolve`.

**Фаза 6 — Качество, безопасность, масштаб (P2-P3, 5+ дней).**

- [ ] **6.6.1** De-sloppify отдельный pass: после worker→gate, перед reviewer —
  отдельный cleanup-агент (over-defensive checks, тесты языка, console.log, мёртвый
  код). ECC: autonomous-loops §5.
- [ ] **6.6.2** Merge queue with eviction context: на merge conflict — `git diff`
  conflicting files + test output → в feedback следующей попытки. Не `note="merge
  conflict"`. ECC: Ralphinho §"Merge Queue with Eviction".
- [ ] **6.6.3** Pass@k / pass^k метрики: в `harness status` показывать `pass@3` по типам
  задач. Источник — `attempts`. ECC: `skills/eval-harness/`.
- [ ] **6.6.4** AgentShield-скан промптов/правил: при `harness doctor` — проверка
  `prompts/`, `.cursor/rules/`, MCP-конфигов на injection/secret-leak. Опционально —
  adversarial red-team/blue-team на Opus (off по умолчанию). ECC: `skills/security-scan/`.
- [ ] **6.6.5** Strategic compaction для долгих воркеров: при
  `context_window_remaining < 30%` — воркер получает команду `/compact` с подсказкой,
  что сохранить. ECC: `skills/strategic-compact/`.
- [ ] **6.6.6** CI failure recovery (после 3.14 GitHub PR): gate fail на PR →
  `gh run view <id>` → fix-pass с логами CI. ECC: Continuous Claude §"CI Failure
  Recovery".
- [x] **6.6.7** Durable-плейн в боевое: `EngineTaskExecutor` с реальным
  worktree+runner+ProfileGate (было fake, см. v2-034 в `PLAN-harness-v2.md`) — сделано и
  live-верифицировано на DBOS+Postgres. `cmd_run` теперь тоже реально переключается на
  него по `HARNESS_DURABLE=1` (раньше проверялось только в `doctor`, `run` всегда шёл
  через asyncio `Engine` — durable-контур был недостижим из CLI; v2-036). Остаётся:
  distributed воркеры по парку VPS (связано с 3.13).
- [ ] **6.6.8** Skill-stocktake раз в N run'ов: аудит `prompts/`+`.cursor/skills/` —
  какие реально читались, какие жрут контекст зря. ECC: `skills/skill-stocktake/`.

**Карта зависимостей:**

```
Фаза 1 (починки) ──► Фаза 2 (наблюдаемость) ──► Фаза 5 (память)
                  └─► Фаза 3 (understanding) ──► Фаза 4 (orch↔user) ──► Фаза 6
```

Фазы 1, 2, 3 можно вести параллельно разными сабагентами. Фаза 4 зависит от 2+3. Фаза 5
— от 2. Фаза 6 — после 1.

---

## 4. Известные ограничения (важно помнить)

- **Primary path = TaskTool pull** — Python не вызывает Task Tool; root chat обязателен.
- **`harness mode chat` bridge deprecated** — compatibility only; no breaking removal yet.
- **Background unattended not default** — cgroups/zram/remote workers required first
  (`BACKLOG-background-mode.md`).
- **Не инициализировать `/home` как target repo** — meta-root guard.
- **CLI cost — эвристика** (legacy SDK/CLI roles).
- **Merge локальный** — в `base`, без обязательного GitHub PR (см. 3.14).
- **Resume** — через `harness tasktool resume` + leases/checkpoints, не из истории чата.
- **Схема hooks/имена моделей** сверять с актуальным Cursor; TaskTool prefs в `.env.example`.
- **Engine без `cursor-sdk`** конструируется, но SDK-роль упадёт в рантайме (legacy only).

---

## 5. Карта файлов (быстрая навигация)

```
harness/
  config.py                 env + ResourcePolicy settings
  tasktool/                 ← PRIMARY pull control plane (v3)
    controller.py           start/next/report/advance/resume
    resources.py            admission 2/2/1/1 + mem soft/hard
    models.py               PlanArtifact / DispatchEnvelope 3.0
    lifecycle.py            shared deterministic stages
  ingest.py                 PLAN.md/tasks → Run в store
  domain/state_machine.py   ← единственное место смены статусов
  store/repository.py       ← БД, журнал, dispatch leases
  scheduler/engine.py       ← legacy push cycle (compatibility)
  runner/                   sdk/cli + legacy cursor_task bridge
  worktree/manager.py       worktrees + generated-artifact excludes
  interface/cli.py          harness tasktool … + legacy commands
  policy/                   budget / escalation (push Engine)
tests/                      pytest (incl. tests/test_tasktool_*)
scripts/                    bash Phase 0
.cursor/skills/harness-orchestrate/   primary UX skill
BACKLOG-background-mode.md            unattended backlog (not default)
PLAN.md / tasks/            артефакты планирования
ARCHITECTURE.md / GUIDE.md  docs
```

---

## 6. Definition of Done для всей обвязки

Harness считается «готовым к проду», когда:
1. P0 закрыт: mypy зелёный, живой цикл проходит, recovery работает.
2. Хотя бы один реальный проект на VPS прошёл цель end-to-end без ручного вмешательства.
3. Выставлен Spend Limit + `RUN_BUDGET`, бюджет реально останавливает Run.
4. BLOCKED-задачи долетают до тебя нотификацией (3.9).
5. **TaskTool-first:** isolated dogfood зелёный; background mode остаётся в
   `BACKLOG-background-mode.md` до cgroups/remote workers (не silent default).
