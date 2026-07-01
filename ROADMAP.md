# ROADMAP / HANDOFF — что остаётся доделать

Документ передачи: текущее состояние, как поднять на сервере и что доработать.
Архитектура — в `ARCHITECTURE.md`. Этот файл — про «что дальше».

---

## 1. Статус на сейчас

| Слой | Состояние | Файлы |
|---|---|---|
| Domain (автомат, модели) | ✅ готово, покрыт тестами | `harness/domain/` |
| Store (SQLite + журнал) | ✅ готово | `harness/store/` |
| Runner (SDK + CLI) | ✅ каркас готов, нужен прогон вживую | `harness/runner/` |
| Gates (`make check`) | ✅ готово | `harness/gates/` |
| Worktree + merge queue | ✅ каркас готов, нужен прогон вживую | `harness/worktree/` |
| Парсер PLAN.md/tasks | ✅ готово, покрыт тестами | `harness/tasks_io/` |
| Policy (бюджет, эскалация) | ✅ готово, покрыт тестами | `harness/policy/` |
| Scheduler (DAG + движок) | ✅ каркас готов | `harness/scheduler/` |
| CLI / реестр / нотификации | ✅ каркас готов | `harness/interface/`, `harness/projects/` |
| Bash-вариант (Phase 0) | ✅ рабочий, остаётся | `scripts/` |

**Проверено локально:** Python 3.12; `ruff check` + `mypy harness` (strict, 39 файлов) —
зелёные; `pytest` — 37 passed (вкл. live durable-тесты на Postgres); dogfood `ProfileGate`
гоняет реальные ruff/mypy/pytest и проходит; durable-recovery доказан demo-скриптом
(crash→resume); CLI отвечает (`plan/ingest/run/status/events/projects`).

**Durable-плейн (ADR-0003 Фаза 2):** каркас `harness/durable/` на DBOS+Postgres готов и
проверен вживую. Поднять базу: `make pg-up`. Включение: `HARNESS_DURABLE=1`. Остаётся
реальный `EngineTaskExecutor` (worktree+runner+ProfileGate вместо fake) — это сводится
с задачей 3.2 (живой smoke), т.к. оба требуют установленного cursor-agent/SDK.

**Фаза 3 (ADR-0003):** сделаны лестница эскалации на китайских моделях
(`auto → kimi → glm`, китайские не сразу; high-сложность стартует с kimi), re-plan
«умного лида» (refine/split/block, 3.6), anti-gaming guard (3.7), sandbox per-agent
(`harness/sandbox/`, Local+Docker, `HARNESS_SANDBOX=docker`). Остаётся: дерево
суб-оркестраторов, воркер внутри sandbox, repointing зависимых при split.

**Вживую НЕ прогонялось** (не было git-репо, `cursor-sdk` и Cursor CLI): реальный
цикл worker→gates→reviewer→merge. Это первое, что надо сделать на сервере.

---

## 2. Поднять на сервере (по шагам)

```bash
# 0) Python 3.11+ (требование конституции и pyproject), git, make
python3 --version            # должно быть >= 3.11

# 1) git-репозиторий (сейчас каталог им НЕ является)
cd orchestration
git init && git add -A && git commit -m "init: harness"
git branch -M main

# 2) зависимости
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # ruff/mypy/pytest + сам harness
pip install -e ".[sdk]"      # только если будешь гонять роли через SDK
make install                 # инструменты гейтов в целевом проекте

# 3) Cursor CLI (для воркеров на подписке)
curl https://cursor.com/install -fsS | bash
cursor-agent --help          # сверь имена моделей с дефолтами в config.py

# 4) ключ для SDK-ролей (оркестратор/ревьюер)
export CURSOR_API_KEY="cursor_..."

# 5) проверка
pytest -q
ruff check harness && mypy harness   # см. задачу 3.1 — mypy ещё не гонялся вживую
chmod +x scripts/*.sh scripts/hooks/*.sh

# 6) первый прогон на тестовой цели
python3 -m harness.interface.cli plan "Маленькая фича для проверки"
#   проверь PLAN.md и tasks/ глазами
python3 -m harness.interface.cli ingest "Маленькая фича" --project test
python3 -m harness.interface.cli run
python3 -m harness.interface.cli status
python3 -m harness.interface.cli events
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
- [ ] **6.6.7** Durable-плейн в боевое: `EngineTaskExecutor` с реальным
  worktree+runner+ProfileGate (сейчас fake в `harness/durable/executor.py`). Distributed
  воркеры по парку VPS. Связано с 3.13.
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

- **Не git-репо из коробки** — нужен `git init` (шаг 2.1).
- **CLI cost — эвристика** — приблизительная оценка по модели (~0.5–8 кредитов), не реальные токены.
  SDK-роли дают точный cost. Для точного CLI-учёта нужен парсинг dashboard API (future).
- **Movie мерж локальный** — в `base` текущего репо, без PR (см. 3.14).
- **Resume частичный** — переживает падение между задачами; задача, прерванная
  в середине, требует recovery (см. 3.3).
- **Схема hooks/имена моделей** могут отличаться между версиями Cursor — сверять (3.4).
- **Engine конструируется безопасно** даже без `cursor-sdk` (ленивый импорт), но
  SDK-роль упадёт в рантайме с понятной ошибкой, если пакет не установлен.

---

## 5. Карта файлов (быстрая навигация)

```
harness/
  config.py                 все env-настройки и модели ролей
  ingest.py                 PLAN.md/tasks → Run в store
  domain/state_machine.py   ← единственное место смены статусов
  store/repository.py       ← вся работа с БД и журналом
  scheduler/engine.py       ← ГЛАВНЫЙ цикл, тут правится логика P0/P1
  scheduler/dag.py          топосорт + ready()
  runner/sdk_runner.py      ← сверить с реальным SDK (3.5)
  runner/cli_runner.py      обёртка cursor-agent
  worktree/manager.py       worktree + merge queue
  policy/budget.py          потолок расходов
  policy/escalation.py      лестница моделей
  interface/cli.py          точка входа CLI
tests/                      pytest (pure-logic, 21 шт.)
scripts/                    bash Phase 0 (рабочий вариант)
PLAN.md / tasks/            артефакты планирования
ARCHITECTURE.md             как всё устроено
```

---

## 6. Definition of Done для всей обвязки

Harness считается «готовым к проду», когда:
1. P0 закрыт: mypy зелёный, живой цикл проходит, recovery работает.
2. Хотя бы один реальный проект на VPS прошёл цель end-to-end без ручного вмешательства.
3. Выставлен Spend Limit + `RUN_BUDGET`, бюджет реально останавливает Run.
4. BLOCKED-задачи долетают до тебя нотификацией (3.9).
