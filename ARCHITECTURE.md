# Архитектура harness

Детерминированный control plane вокруг стохастических агентов Cursor. Bash-скрипты
(`scripts/`) остаются как Phase 0; пакет `harness/` — их эволюция в resumable-движок.

## Главный принцип

> Агенты недетерминированы. Harness — детерминирован.
> Вся стохастика заперта внутри `cursor-agent`/SDK. Всё вокруг (состояние, переходы,
> гейты, мерж, бюджет) — конечный автомат, который можно убить в любой момент и
> продолжить с того же места.

## Слои

```
INTERFACE   CLI · реестр проектов · нотификации              harness/interface, projects
CONTROL     scheduler(DAG) · store(SQLite) · policy(бюджет)   harness/scheduler, store, policy
DATA        orchestrator(Opus) · worker(auto) · reviewer       harness/runner
SUBSTRATE   git worktrees · merge queue · make check · MCP     harness/worktree, gates
```

## Поток данных

```
goal ──plan──► PLAN.md + tasks/*.md ──ingest──► Run + Tasks (store)
                                                     │
                                          scheduler (DAG, до MAX_PARALLEL)
                                                     │ для каждой READY-задачи
                                       ┌─────────────▼──────────────┐
                                       │ worktree → worker → gates  │
                                       │  → reviewer → APPROVE?      │
                                       │     merge-queue : feedback  │
                                       └─────────────┬──────────────┘
                                          merge в base + повторный gate
                                                     ▼
                                                   DONE
```

## Конечный автомат задачи

`harness/domain/state_machine.py` — единственное место, где разрешены переходы.

```
PENDING → READY → RUNNING → GATING → REVIEW
REVIEW  → MERGE_QUEUE (approve & gates pass) → DONE
        → READY        (changes, есть попытки)
        → ESCALATE      (попытки исчерпаны) → PENDING|READY|BLOCKED
MERGE_QUEUE → READY (конфликт/гейт после интеграции)
BLOCKED/FAILED → READY
{RUNNING,GATING,REVIEW,MERGE_QUEUE} → RECOVERING → READY  (resume после падения)
```

Любой переход идёт через `Store.transition_task`, который валидирует его по автомату
и пишет событие — состояние и журнал не расходятся.

## Ключевые решения

| Решение | Почему |
|---|---|
| **Runner-абстракция** (`SdkRunner`/`CliRunner`) | Дорогие роли — SDK (пул API), массовый воркер — CLI (подписка). Экономика под контролем. |
| **SQLite + append-only events** | Resume после падения; журнал — источник правды для дашборда/аудита. |
| **DAG вместо `ls \| sort`** | Порядок и параллелизм по зависимостям из PLAN.md, а не по алфавиту. |
| **git worktree + merge queue** | Параллельные задачи без конфликтов рабочей копии; мерж сериализован, после интеграции — повторный `make check`. |
| **Context contracts (`provides`)** | Зависимой задаче инжектится интерфейс, произведённый родителем. |
| **Cost governor + многоступенчатая лестница эскалации** | Жёсткий потолок бюджета; при провалах воркер поднимается по ступеням `auto → kimi-k2.5 → glm-5.2` (по одной за попытку). Ревьюер тоже на glm-5.2. |
| **Гейт как оракул истины** | Гейты из профиля проекта — машинная проверка, галлюцинация физически не пройдёт. |
| **Anti-gaming guard** | Зону спеков/acceptance (`tests/spec/**`) воркеру править нельзя; правка → авто-CHANGES без ревьюера. Нельзя «подогнать» приёмку. |
| **Sandbox исполнения** | Гейты гоняются через абстракцию Sandbox: `local` (хост) или `docker` (изолированный контейнер из образа профиля, сеть off). Безопасность + горизонтальный масштаб. |

## Масштабируемость и полиглотность (см. ADR-0003)

Текущий каркас — Python + один `asyncio`-процесс + SQLite, и ведёт только
Python-проекты. Целевой дизайн развязывает «привязку к Python» по трём
независимым осям (полностью — в `brain/decisions/ADR-0003-...`):

| Ось | Сейчас | Цель |
|---|---|---|
| 1. Язык контрол-плейна | Python, один процесс | протокол поверх durable execution (DBOS→Temporal), task queues |
| 2. Язык агент-рантайма | Cursor CLI/SDK | runner-adapter (CLI/SDK/cloud) — seam уже есть |
| 3. Язык целевого проекта | Python (гейт `make check` зашит) | `GateProfile` из `.harness/project.toml` + per-language canon overlay |

Главный приём: **harness — это протокол** (state machine + event log + DAG +
briefs-как-файлы + project-profile), а компоненты вокруг — заменяемые адаптеры,
общающиеся через JSON-события и ФС. Любой компонент можно переписать на другом
языке, не трогая остальные.

Durable execution (L1): детерминированный workflow реплеится из чекпоинтов,
недетерминированные шаги (LLM/гейты/git/merge) — DBOS-steps с авто-восстановлением.
Каркас реализован в `harness/durable/` (DBOS+Postgres):

- `process_task` — per-task DBOS-workflow; шаги worker→gate→review→merge
  чекпоинтятся, переходы автомата идемпотентны (повторный прогон шага — no-op);
- драйвер кладёт готовые (и in-flight после рестарта) задачи в очередь с лимитом
  параллелизма; workflow-id детерминирован (`run:task`) → повторный enqueue
  дедуплицируется и переподхватывает восстановленный workflow;
- Postgres — `compose.yaml` (порт 5439) + `make pg-up`; включается `HARNESS_DURABLE=1`;
- без DBOS/Postgres движок работает на SQLite-сторе как раньше (опциональный слой).

Demo `scripts/durable_demo.py`: падение в середине шага → перезапуск → DBOS
дочинивает; завершённые шаги не переигрываются. Это закрывает частичный resume
(ROADMAP 3.3) и даёт распределённость по парку VPS (Temporal-grade поверх Postgres).

Ось 3 (приоритетная боль) **уже реализована** (Фаза 1): гейт перестал быть зашитым
`make check` — `harness/profile.py` читает `.harness/project.toml` (команды
lint/types/test/build per-language), `harness/gates/profile_gate.py` гоняет их
fail-fast. Конституция раздроблена на общую (`.cursor/rules/00-constitution.md`) +
language-overlay (`10-lang-python.md`, `10-lang-typescript.md`). Формат TOML
(stdlib `tomllib`, без внешних зависимостей).

## Конфигурация (env)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `ORCH_MODEL` / `ORCH_RUNNER` | `claude-opus-4-8-thinking-high` / `sdk` | планировщик |
| `WORKER_MODEL` / `WORKER_RUNNER` | `auto` / `cli` | исполнитель (база лестницы) |
| `REVIEWER_MODEL` / `REVIEWER_RUNNER` | `glm-5.2-high` / `sdk` | ревьюер |
| `MAX_ATTEMPTS` | `3` | попыток на задачу |
| `MAX_PARALLEL` | `3` | воркеров одновременно |
| `RUN_BUDGET` | — | потолок расходов на Run |
| `ESCALATE_AFTER` | `2` | сколько попыток держится стартовая ступень (auto) до подъёма |
| `ESCALATION_MODELS` | `kimi-k2.5,glm-5.2-high` | ступени эскалации (китайские модели) |
| `MAX_ATTEMPTS` (адаптивный) | `escalate_after + #ступеней` | хватает на всю лестницу |
| `MAX_REPLANS` | `1` | сколько раз оркестратор переразбивает задачу до BLOCKED |
| `PROTECTED_PATHS` | `tests/spec/**` | зона приёмки, запрещённая воркеру (anti-gaming) |
| `HARNESS_SANDBOX` | `local` | песочница гейтов: `local` (хост) \| `docker` (контейнер) |
| `HUMAN_GATE_MERGE` | `0` | ставить Run на паузу перед мержем в base |

Сложность задачи (`complexity: high` во frontmatter `tasks/*.md`) сдвигает стартовую
ступень лестницы на `kimi` — auto пропускается для заведомо тяжёлых задач.

## CLI

```bash
harness plan "Добавить REST API пользователей"   # Opus → PLAN.md + tasks/*.md
harness ingest "..." --project myapp              # загрузить tasks/ в store как Run
harness run                                       # исполнить последний Run
harness status                                    # статусы задач
harness events                                    # журнал событий
harness projects add --name api --repo /srv/api   # реестр проектов на VPS
```

## Карта дальнейших фаз

- **Phase 3** — ✅ re-plan «умного лида» (`harness/replan.py`): при исчерпании лестницы
  оркестратор уточняет (refine) или дробит (split) задачу вместо тихого BLOCKED.
  Остаётся: sandbox per-agent (Docker), дерево суб-оркестраторов из skill.
- **Phase 4** — Telegram/Slack `Notifier`, human-gate с кнопкой approve, потолок бюджета на парк.
- **Phase 5** — TUI/web-дашборд поверх журнала событий; распределённые воркеры по нескольким VPS.

## Что осознанно НЕ сделано в каркасе

- Реальная стоимость от CLI (он не отдаёт её машиночитаемо) — для CLI-ролей `cost=0`,
  бюджет считается по SDK-ролям; для точного учёта добавить парсер usage из dashboard API.
- Подсчёт токенов/кредитов вне SDK.
- Интеграция с GitHub PR (сейчас мерж локальный в base).
