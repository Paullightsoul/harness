# Harness — практический гайд

Обвязка (harness) для автоматизации разработки агентами Cursor: дорогая модель
**планирует**, дешёвые воркеры **исполняют**, отдельный ревьюер **проверяет**, гейты
проекта — **машинный оракул истины**. Вся стохастика заперта в агенте; всё вокруг
(состояние, переходы, мерж, бюджет) — детерминированный конечный автомат, который можно
убить в любой момент и продолжить.

> Глубже: `ARCHITECTURE.md` (как устроено), `ROADMAP.md` (что доделать),
> `brain/decisions/ADR-0003-...` (почему так).

---

## 0. TL;DR (самый короткий путь)

См. также **`QUICKSTART.md`** — запуск за 5 минут + готовый промпт для нового чата.

```bash
cd 1.harness/orchestration
make setup                          # venv + зависимости + .env + doctor (или: make setup --all)
. .venv/bin/activate
curl https://cursor.com/install -fsS | bash   # Cursor CLI (воркеры на подписке)
# заполни .env (CURSOR_API_KEY, Telegram), затем:
harness doctor                      # проверка готовности

harness init --repo /path/to/repo   # подготовить целевой проект (git + профиль под стек)
harness plan   "Добавить REST API пользователей"
#   → проверь PLAN.md и tasks/*.md глазами, поправь
harness ingest "Добавить REST API пользователей"
harness run
harness status
```

После `make setup` (или `pip install -e .`) доступна короткая команда `harness`.
harness автоматически подхватывает `.env` из каталога.

---

## 1. Что нужно один раз

1. **Python 3.12+**, `git`, `make` (опционально).
2. **Cursor CLI** — для воркеров в рамках подписки: `curl https://cursor.com/install -fsS | bash`.
   Проверь имена моделей: `cursor-agent --help`.
3. **API-ключ** для SDK-ролей (оркестратор/ревьюер тарифицируются из пула API):
   `export CURSOR_API_KEY="cursor_..."`.
4. **Целевой репозиторий** — git-репо с первым коммитом и веткой `main`.
5. **Профиль проекта** в целевом репо: `.harness/project.toml` (см. §3).
6. **Spend Limit** в `cursor.com/dashboard → Billing` — обязательный потолок для авто-цикла.

---

## 2. Поток работы (plan → ingest → run)

```
Цель ─► plan (оркестратор, Opus) ─► PLAN.md + tasks/*.md   ← проверь глазами
        ↓ ingest
     Run + задачи в store (SQLite)
        ↓ run
  по DAG: worker → гейты → guard → reviewer → APPROVE? → merge → DONE
            ▲ фидбэк, эскалация моделей, re-plan ┘
```

| Команда | Что делает |
|---|---|
| `harness init [--repo PATH] [--language L]` | Подготовить целевой репо: git + `.harness/project.toml` под стек. |
| `harness doctor [--project NAME]` | Проверка готовности окружения (python/git/cursor-agent/ключ/профиль). |
| `harness plan "<цель>"` | Оркестратор раскладывает цель на `PLAN.md` + `tasks/task-*.md`. **Проверь руками.** |
| `harness ingest "<цель>" [--project NAME]` | Грузит `tasks/` в store как новый Run (зависимости из `PLAN.md`). |
| `harness run [run_id] [--project NAME]` | Исполняет Run (по умолчанию последний). Можно прерывать и запускать снова — продолжит. |
| `harness status [run_id]` | Статусы задач, расход бюджета. |
| `harness events [run_id]` | Журнал событий (источник правды). |
| `harness approve <run_id> <task_id> [--project NAME]` | Подтвердить отложенный мерж (human-gate). |
| `harness projects add --name api --repo /srv/api [--base main]` | Реестр проектов (мульти-проект). |
| `harness projects list` | Список проектов. |
| `harness bot` | Telegram-бот: команды `/status`, `/runs`, `/approve` (см. §6). |

Важно: `plan` создаёт черновик — **всегда просматривай `PLAN.md` и `tasks/` перед `run`**.
Узкие задачи с проверяемыми acceptance criteria = меньше галлюцинаций.

---

## 3. Профиль проекта — любой язык (`.harness/project.toml`)

Harness не привязан к Python. Гейты и канон задаёт профиль в целевом репо:

```toml
language = "typescript"        # для подсказок и выбора canon-overlay
canon = "typescript-frontend"

[[gates]]                       # computational sensors — гоняются по порядку, fail-fast
id = "lint"
cmd = "pnpm lint"
[[gates]]
id = "types"
cmd = "pnpm typecheck"
[[gates]]
id = "test"
cmd = "pnpm test --run"

[review]                        # модель ревьюера (по умолчанию glm-5.2-high)
model = "glm-5.2-high"

[sandbox]                       # опционально: гонять гейты в контейнере (HARNESS_SANDBOX=docker)
image = "node:20"
network = "none"
```

- Нет файла → дефолт `make check` (обратная совместимость с Python-репо).
- Python-пример — в `.cursor/rules/10-lang-python.md`, TS — в `10-lang-typescript.md`.
- Гейт — это **оракул истины**: выдуманный код физически не пройдёт `pnpm test`/`pytest`.

---

## 4. Модели и лестница эскалации (китайские модели)

Политика: китайские модели подключаются **не сразу**. Дешёвый `auto` получает несколько
попыток с правками ревьюера; только если не справился — `kimi`, затем `glm`.

```
attempt 1..ESCALATE_AFTER → auto      (по умолчанию 2 попытки)
затем по одной ступени за попытку: → kimi-k2.5 → glm-5.2-high
```

- **Повышенная сложность:** пометь задачу `complexity: high` во frontmatter
  `tasks/task-*.md` — она стартует сразу с `kimi` (auto пропускается), потом `glm`.
- Ревьюер по умолчанию `glm-5.2-high`. Планировщик — `opus-4.8` (сильный на декомпозиции).
- `MAX_ATTEMPTS` по умолчанию адаптивный: `ESCALATE_AFTER + число ступеней` (= 4), чтобы
  лестница проходилась целиком.

Когда лестница исчерпана — **re-plan «умного лида»**: задача уходит оркестратору, и он
либо уточняет спеку (`refine`, свежий заход), либо дробит на под-задачи (`split`), либо
помечает `block` (нужен человек). Бюджет — `MAX_REPLANS` (по умолчанию 1).

---

## 5. Защита приёмки (anti-gaming) и human-gate

- **Anti-gaming:** воркеру запрещено править зону спеков/acceptance (`PROTECTED_PATHS`,
  по умолчанию `tests/spec/**`). Тронул — автоматический CHANGES без ревьюера. Нельзя
  «подогнать» тесты под себя.
- **Human-gate на мерж:** `HUMAN_GATE_MERGE=1` ставит Run на паузу перед мержем в base и
  шлёт уведомление. Подтвердить: `harness approve <run_id> <task_id>` (или `/approve` в боте).

---

## 6. Telegram-нотификации и бот

Получай уведомления (BLOCKED, бюджет исчерпан, нужен аппрув, Run завершён) и управляй из чата.

1. Создай бота у **@BotFather** → получи `TELEGRAM_BOT_TOKEN`.
2. Узнай свой `TELEGRAM_CHAT_ID` (напиши боту, затем
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → поле `chat.id`).
3. Поставь зависимость и переменные:

```bash
pip install -e ".[telegram]"
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

Теперь движок шлёт уведомления автоматически. Для двухсторонних команд запусти бота:

```bash
harness bot      # long-polling; отвечает только в твоём чате
```

Команды бота: `/runs`, `/status [run_id]`, `/approve <run_id> <task_id>`, `/help`.
Без токена/чата нотификации идут в консоль (stderr) — ничего настраивать не нужно.

---

## 7. Durable-режим (переживает падения, масштаб на парк)

По умолчанию состояние в SQLite (`.harness/state.db`), resume — между задачами и для
прерванных (recovery). Для durable-исполнения на DBOS+Postgres (чекпоинт каждого шага,
авто-восстановление, распределённость):

```bash
make pg-up                       # Postgres в docker (порт 5439), один раз
pip install -e ".[durable]"
export HARNESS_DURABLE=1
export DBOS_SYSTEM_DATABASE_URL="postgresql://harness:harness@localhost:5439/harness"
```

Демо восстановления: `python scripts/durable_demo.py crash` затем `... resume` — увидишь,
что завершённые шаги не переигрываются, прерванный выполняется заново.

---

## 8. Мульти-проект (парк репозиториев)

```bash
harness projects add --name api --repo /srv/api --base main
harness projects add --name web --repo /srv/web --base develop
harness ingest "фича X" --project api     # Run привязан к base проекта
harness run --project api                  # воркеры/гейты идут в /srv/api
```

`settings.root` (где лежат `prompts/`, `tasks/`, `.harness/state.db`) — дом harness;
`--project` указывает целевой репозиторий. У каждого целевого репо — свой
`.harness/project.toml`.

---

## 9. Все переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `HARNESS_ROOT` | `.` | дом harness (prompts, tasks, state.db, logs) |
| `CURSOR_API_KEY` | — | ключ для SDK-ролей (оркестратор/ревьюер) |
| `CURSOR_FLAGS` | — | доп. флаги `cursor-agent` |
| `ORCH_MODEL` / `ORCH_RUNNER` | `opus-4.8` / `sdk` | планировщик |
| `WORKER_MODEL` / `WORKER_RUNNER` | `auto` / `cli` | исполнитель (база лестницы) |
| `REVIEWER_MODEL` / `REVIEWER_RUNNER` | `glm-5.2-high` / `sdk` | ревьюер |
| `ESCALATE_AFTER` | `2` | попыток на стартовой ступени до подъёма |
| `ESCALATION_MODELS` | `kimi-k2.5,glm-5.2-high` | ступени эскалации |
| `MAX_ATTEMPTS` | адаптивный (4) | попыток на задачу |
| `MAX_REPLANS` | `1` | сколько раз оркестратор переразбивает задачу |
| `MAX_PARALLEL` | `3` | воркеров одновременно |
| `RUN_BUDGET` | — | потолок расходов на Run (жёсткий стоп → PAUSED) |
| `PROTECTED_PATHS` | `tests/spec/**` | зона приёмки, запрещённая воркеру |
| `HARNESS_SANDBOX` | `local` | песочница гейтов: `local` \| `docker` |
| `HUMAN_GATE_MERGE` | `0` | пауза + аппрув перед мержем в base |
| `BASE_BRANCH` | `main` | ветка интеграции (если не задана проектом) |
| `HARNESS_DURABLE` | `0` | durable-режим на DBOS+Postgres |
| `DBOS_SYSTEM_DATABASE_URL` | localhost:5439 | Postgres для durable-плейна |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Telegram-нотификации и бот |

---

## 10. Разработка и проверки

```bash
make pg-up                 # Postgres для durable-тестов (иначе они скипаются)
ruff check harness tests   # линт
mypy harness               # типы (strict)
pytest -q                  # тесты (durable/sandbox требуют docker+pg)
```

Гейты самого harness описаны в его `.harness/project.toml` (dogfood).

---

## 11. Типичные проблемы

- **`cursor-agent не найден`** — поставь Cursor CLI и проверь `PATH`.
- **`cursor-sdk не установлен`** — для SDK-ролей: `pip install -e ".[sdk]"` или переведи
  роль на `*_RUNNER=cli`.
- **Гейт всегда красный** — проверь команды в `.harness/project.toml` и что тулчейн
  установлен (или используй `HARNESS_SANDBOX=docker` с готовым образом).
- **Run «завис»** — проверь `harness events`: задача в BLOCKED ждёт человека (re-plan/budget).
- **Нет уведомлений в Telegram** — задай `TELEGRAM_BOT_TOKEN`+`TELEGRAM_CHAT_ID` и
  `pip install -e ".[telegram]"`; напиши боту любое сообщение, чтобы он узнал chat_id.
- **Дорого** — `WORKER_RUNNER=cli` (подписка), выставь `RUN_BUDGET` и Spend Limit.
