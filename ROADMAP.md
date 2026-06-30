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
- [ ] **3.4 Сверить имена моделей** (`opus-4.8`, `sonnet-4.6`, `auto`) с `cursor-agent`
  и SDK `Cursor.models.list()`. Поправить дефолты в `harness/config.py`.
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
- [ ] **3.8 Учёт стоимости для CLI-ролей.** CLI не отдаёт cost машиночитаемо (сейчас 0).
  Прикрутить парсинг usage из dashboard API или хотя бы грубую оценку по токенам.

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

---

## 4. Известные ограничения (важно помнить)

- **Не git-репо из коробки** — нужен `git init` (шаг 2.1).
- **CLI cost = 0** — бюджет реально считается только по SDK-ролям (см. 3.8).
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
