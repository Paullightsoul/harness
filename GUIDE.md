# Harness — практический гайд (TaskTool-first)

Обвязка (`1.harness`) для разработки через Cursor: **root chat** диспатчит
Task Tool, Python держит FSM/store/leases/gates/merge. Планировщик пишет
`PLAN.md` + `tasks/*`, воркеры исполняют, ревьюер проверяет, гейты — машинный
оракул истины.

> Глубже: `ARCHITECTURE.md`, `QUICKSTART.md`, `BACKLOG-background-mode.md`,
> ADR-0011. Primary skill: `.cursor/skills/harness-orchestrate/SKILL.md`.

---

## 0. TL;DR (primary path)

```bash
cd /home/1.harness
make setup && . .venv/bin/activate
harness doctor
harness init --repo /abs/path/to/repo
harness projects add --name myapp --repo /abs/path/to/repo --base main
```

В Cursor: приложи ТЗ/доки и скажи **«используй harness»**. Чат:

1. Planner Task → `PLAN.md` + `tasks/*`
2. `harness verify --project myapp`
3. Plan approval → `harness tasktool start … --approve-plan --spec-source …`
4. Цикл `next` (≤2) → Task Tool → `report` → `advance`
5. Risk / ship approvals → `advance --approved-merge`
6. После рестарта чата → `status` + `resume` (не второй run)

Не запускай long-lived `harness run` и не поднимай file-spool poller для новых задач.

---

## 1. Что нужно один раз

1. **Python 3.12+**, `git`, `make` (опционально).
2. **Cursor IDE** с Task Tool (основной путь).
3. Целевой **git-репозиторий** с base branch (не `/home`).
4. Профиль `.harness/project.toml` в целевом репо (`harness init`).
5. `.env` — см. `.env.example` (TaskTool model routing).
6. Spend Limit в Cursor dashboard — потолок для агентных циклов.

Cursor CLI / `CURSOR_API_KEY` нужны только для **legacy** SDK/CLI Engine.

---

## 2. Primary UX — TaskTool pull

### 2.1 Intake

- Текст пользователя + вложения/пути спек = `spec-source`.
- Root chat — единственный, кто вызывает Task Tool.
- Planner Task **read-only** по application source; пишет только plan-артефакты.

### 2.2 Verify + approvals

```bash
harness verify --project myapp
```

Детерминированная проверка `PLAN.md` + `tasks/` до ingest. После зелёного
verify — явное **plan approval**. Позже отдельно: **risk approval** и
**ship approval** (plan approval не покрывает ship).

### 2.3 Frozen CLI (stdout = JSON)

```bash
harness tasktool start "<goal>" \
  --project myapp --repo /abs/path/to/repo --base-branch main \
  --approve-plan --spec-source /path/a.md --spec-source /path/b.md

harness tasktool next "<run-id>" \
  --project myapp --repo /abs/path/to/repo \
  --agent-id "<root-chat-id>" --limit 12

harness tasktool report "<dispatch-id>" \
  --project myapp --repo /abs/path/to/repo \
  --result-file /abs/path/to/result.json --agent-id "<root-chat-id>" \
  --worker-id "<task-tool-job-id>" --ok
# failure: замени --ok на --error "…"
# --agent-id — держатель лиза (root chat), --worker-id — исполнитель.
# У каждого воркера свой --worker-id, иначе worker-boundary видит orch==worker
# cosplay и HARNESS_NESTED_WORKERS_HARD_FAIL=1 не даёт закрыть run.

harness tasktool advance "<run-id>" \
  --project myapp --repo /abs/path/to/repo

harness tasktool advance "<run-id>" \
  --project myapp --repo /abs/path/to/repo --approved-merge   # только после ship OK

harness tasktool status "<run-id>" --project myapp --repo /abs/path/to/repo
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/repo
# после ответа на question-protocol:
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/repo \
  --answer q1="ответ" --answer q2="ответ"
# либо: --answers-file /abs/path/to/answers.json
# после отдельного AskQuestion risk approval:
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/repo \
  --approve-risk
harness tasktool abort  "<run-id>" --project myapp --repo /abs/path/to/repo --reason "…"
```

`next --limit` по умолчанию **12** (CLI default / ADR-0014 wave; hard ceiling 16).
Для solo dogfood можно сузить (`--limit 4`–`6`). Не превышай ceiling.

### 2.4 JSON / file contract

**CLI stdout** (пример `next`):

```json
{
  "ok": true,
  "run_id": "run-…",
  "dispatches": [
    {
      "protocol_version": "3.0",
      "dispatch_id": "dispatch-…",
      "task_id": "001",
      "role": "worker",
      "stage": "implement",
      "status": "claimed",
      "prompt_path": "…/prompts/….md",
      "result_path": "…/results/….json",
      "model": "cursor-grok-4.5-high",
      "model_tier": "standard",
      "repo": "/abs/path/to/repo",
      "worktree": "/abs/path/to/repo/.worktrees/…",
      "files_owned": ["src/api.py"],
      "read_only_context": ["PLAN.md"],
      "frozen_contracts": ["…"],
      "acceptance": ["…"],
      "resource_class": "standard",
      "source_document_paths": ["…"],
      "dependencies": [],
      "created_at": "2026-07-20T00:00:00+00:00",
      "updated_at": "2026-07-20T00:00:00+00:00"
    }
  ]
}
```

Если `stage` / role выглядят как **gate** — это protocol error: не вызывай
Task Tool; гейты делает `advance`.

**Minimal result file** (то, что пишет агент в `result_path` / передаёт в
`--result-file`):

```json
{
  "dispatch_id": "dispatch-…",
  "final_text": "HARNESS_DONE\n\n## Provides\nPublic API surface for dependents.\n"
}
```

Допустимы строковые ключи `text` или `result` вместо `final_text`. `report`
идемпотентен при повторной сдаче того же содержимого.

### 2.5 Pull loop (корневой чат)

1. `next --limit 12`
2. Прочитать `prompt_path` (минимальный Task Brief)
3. До восьми параллельных Task Tool вызовов
4. Атомарно записать полный machine-readable ответ в `result_path`
5. `report` на каждый dispatch, затем один `advance`
6. Повторять, пока JSON позволяет; на паузах — `AskQuestion` + `resume`

Дерево: small → root→leaf (root invokes every Task Tool call). Large-задачи
декомпозируются **control-plane-нативно** (ADR-0012): `next` выдаёт
sub-orchestrator envelope (`stage: plan`), `advance` сам создаёт child-задачи
из его JSON и потом возвращает родителя интеграционным воркером — чат просто
продолжает pull-цикл. Sub-orchestrator не вызывает Task Tool детей сам.
Conceptual depth ≤ 2 — not nested Task Tool dispatch.

---

## 3. Resource defaults (24 CPU / 125 GiB host)

| Limit | Default |
|---|---|
| TaskTool jobs / agent slots | 12 / 12 (hard ceiling 16) |
| Heavy jobs / gates | 5 / 4 slots |
| Soft / hard MemAvailable | ⅛ / ¹⁄₁₆ ёмкости (здесь ≈16 / 8 GiB); ёмкость = cgroup-лимит или MemTotal |
| Load | throttle when `load1 ≥ cpu_count × LOAD_PER_CPU_THRESHOLD` |

Env: `MAX_TASKTOOL_JOBS`, `MAX_AGENT_SLOTS`, `MAX_HEAVY_JOBS`, `MAX_GATES`,
`SOFT_MEM_AVAILABLE_BYTES`, `HARD_MEM_AVAILABLE_BYTES`,
`LOAD_PER_CPU_THRESHOLD` (также с префиксом `HARNESS_`).

**Gates:** scoped после worker; full pre-merge и post-merge. Слоты
`.harness/tasktool-gate[-N].lock`; >1 слот работает только worktrees+scoped,
full-suite и in-place всегда сериализованы.

**De-sloppify:** в TaskTool path нет default extra-agent pass для small.
Generated caches не коммитятся worktree manager’ом.

---

## 3b. Agent-in-agent + project context (ADR-0012)

- `harness context --project <p>` — собрать/обновить project context
  (структура, стек, гейты, graphify, brain: ADR/incidents/architecture/lessons).
  Markdown: `<repo>/.harness/context/project-context.md`, кэш по git HEAD.
  Планнер читает файл целиком; каждый dispatch получает компактный
  `### Project brief` в ContextPack автоматически.
- `large`-задача (или `Decompose: yes` в спеке) сперва получает
  **sub-orchestrator** dispatch (`stage: plan`): агент исследует репо и
  возвращает fenced JSON `{"decompose": true|false, ...}`. Контроллер
  валидирует (ownership детей ⊆ родителя, дизъюнктность, DAG, ≤12 детей),
  замораживает `expansions/<parent>.json`, создаёт child-задачи и после их
  DONE возвращает родителя интеграционным воркером с `Provides` детей.
- Выключатели: `HARNESS_DECOMPOSE=0`, `Decompose: no` в спеке,
  `HARNESS_DECOMPOSE_MAX_CHILDREN` (default 12).
- **Economics (Phase 3, default off):** `HARNESS_ECONOMICS=1` +
  `HARNESS_FANOUT_PROFILE=solo|team` — breadth gate before fan-out
  (`DECOMPOSE_DENIED` → direct worker); solo clamps jobs/children to 4.
  See `docs/PHASE3-ECONOMICS-BRAIN.md`.
- Brain (Phase 3): `HARNESS_BRAIN_ROOT=/home/brain-agents` (agent layer,
  lessons writeback); `HARNESS_BRAIN_CANON=/home/brain` (human read-only
  sync source). Sync/query: `harness brain sync` / `harness brain query …`.
  ContextPack injects **pointers only**, never vault dumps.
- **Meta (Phase 4, optional):** `harness meta changelog add|list`,
  `harness meta record`, `harness meta eval` / `doctor --meta-eval` —
  falsifiable harness edits (AHE-lite). Skill GC:
  `harness skills gc propose|apply --approve` (never auto-delete).
  See `docs/PHASE4-META.md`. Flag `HARNESS_META=0` default.
- Журнал проекта (ADR-0013): на каждую DONE-задачу и по завершении run'а —
  запись в `<repo>/docs/history/YYYY-MM-DD-HHMMSS-<slug>.md` (решения /
  что сделано / итерации). Идемпотентно (`JOURNAL_WRITTEN` event), секреты
  редактируются. Env: `HARNESS_PROJECT_JOURNAL=1`,
  `HARNESS_JOURNAL_DIR=docs/history`.

---

## 4. Model routing

**Рекомендация TaskTool** (`harness/tasktool/model_map.py` — independent of
legacy Settings; **Grok-first**, см. `docs/GROK-DEFAULTS.md`):

| Role | Model slug | Env |
|---|---|---|
| Planner / root | `cursor-grok-4.5-high` | `TASKTOOL_ORCH_MODEL` |
| Main reviewer / goal-judge | `cursor-grok-4.5-high` | `TASKTOOL_REVIEWER_MODEL` |
| Worker / sub-orchestrator / research | `cursor-grok-4.5-high` | `TASKTOOL_WORKER_MODEL` |

### Legacy SDK/CLI models (push Engine / bridge only)

`ORCH_MODEL` / `WORKER_MODEL` / `REVIEWER_MODEL`, runners
(`ORCH_RUNNER`/`WORKER_RUNNER`/`REVIEWER_RUNNER`), escalation
`auto→kimi→glm`, and `HARNESS_RUNNER=cursor_task` belong to the push Engine /
deprecated bridge. TaskTool routing ignores them. Не смешивай без нужды.

---

## 5. Профиль проекта (полиглот)

```toml
language = "typescript"
canon = "typescript-frontend"

[[gates]]
id = "lint"
cmd = "pnpm lint"
[[gates]]
id = "types"
cmd = "pnpm typecheck"
[[gates]]
id = "test"
cmd = "pnpm test --run"
# опционально: task_ids = ["001", "002"]  # scoped-only для этих задач

[review]
model = "cursor-grok-4.5-high"

[sandbox]
image = "node:20"
network = "none"
```

Нет файла → fallback `make check`. Гейт — оракул истины.

---

## 6. Anti-gaming и human checkpoints

- `PROTECTED_PATHS` (default `tests/spec/**`) — воркеру трогать нельзя.
- Plan / risk / ship — отдельные human gates через AskQuestion.
- `advance --approved-merge` только после ship approval.
- Legacy `HUMAN_GATE_MERGE=1` + `harness approve` относится к push Engine.

---

## 7. Мульти-проект

```bash
harness projects add --name api --repo /srv/api --base main
harness tasktool start "…" --project api --repo /srv/api --base-branch main --approve-plan
```

`settings.root` — дом harness (prompts/tasks/state). `--project` / `--repo` —
целевой репозиторий.

---

## 8. Legacy paths (compatibility)

### 8.1 Deprecated: `harness mode chat` bridge

Старый file-spool (`HARNESS_CHAT_BRIDGE`, `CursorTaskRunner`) и skill
`harness-chat-orchestrator` помечены **legacy**. Новые запуски →
`harness-orchestrate` + `harness tasktool …`. Ломать удалением пока нельзя.

### 8.2 Push Engine: `plan` → `ingest` → `run`

```bash
harness plan "…"
harness ingest "…" --project myapp
harness run --project myapp --approve-plan
```

Полезен для экспериментов / CLI-only хостов. Не primary UX.

### 8.3 Background / durable

`HARNESS_DURABLE=1`, remote workers, cgroups unattended — **не default и не
product claim**. См. `BACKLOG-background-mode.md`.

---

## 9. Переменные окружения (сводка)

| Переменная | Default / notes | Назначение |
|---|---|---|
| `TASKTOOL_ORCH_MODEL` | `cursor-grok-4.5-high` | TaskTool planner / root |
| `TASKTOOL_WORKER_MODEL` | `cursor-grok-4.5-high` | TaskTool workers / sub-orch / research |
| `TASKTOOL_REVIEWER_MODEL` | `cursor-grok-4.5-high` | TaskTool reviewer / goal-judge |
| `MAX_TASKTOOL_JOBS` | `12` (hard ceiling 16) | admission |
| `MAX_AGENT_SLOTS` | `8` | admission |
| `MAX_HEAVY_JOBS` | `3` | admission |
| `MAX_GATES` | `2` (слоты; full/in-place всё равно 1) | admission |
| `HARNESS_DECOMPOSE` | `1` | agent-in-agent для large-задач (ADR-0012) |
| `HARNESS_DECOMPOSE_MAX_CHILDREN` | `8` | лимит детей на декомпозицию |
| `HARNESS_BRAIN_ROOT` | `/home/brain` | lessons + ADR/incidents/architecture в контекст |
| `HARNESS_USE_WORKTREES` | `0` | `1` = per-task git worktrees |
| `HARNESS_SHIP_MODE` | `manual` | `manual` \| `merge` |
| `SOFT_MEM_AVAILABLE_BYTES` | 16 GiB | throttle |
| `HARD_MEM_AVAILABLE_BYTES` | 8 GiB | pause |
| `LOAD_PER_CPU_THRESHOLD` | `1.1` | throttle |
| `PROTECTED_PATHS` | `tests/spec/**` | anti-gaming |
| `HARNESS_SANDBOX` | `local` | gate sandbox |
| `BASE_BRANCH` | `main` | integration branch |
| `CURSOR_API_KEY` | — | legacy SDK only |
| `ORCH_MODEL` / `WORKER_MODEL` / `REVIEWER_MODEL` | — | **legacy** push Engine only |
| `HARNESS_RUNNER` / `HARNESS_CHAT_*` | — | **legacy bridge** |
| `HARNESS_DURABLE` / `DBOS_*` | off | **backlog / experiment** |
| `TELEGRAM_*` | — | optional notify/bot |

Полный legacy набор (escalation, `MAX_PARALLEL`, Telegram bot commands) сохраняется
для push Engine; см. исторические секции ROADMAP / PLAN.

---

## 10. Проверки

```bash
ruff check harness tests
mypy harness
# TaskTool / fake-driver suite — через pytest по tests/test_tasktool_*.py
# Isolated dogfood — только после зелёных verification commands на fixture repo
```

Не трактуй «тесты есть» как production SLA. Формулируй осторожно: verification
commands + isolated dogfood.

---

## 11. Troubleshooting

| Симптом | Что делать |
|---|---|
| `RESOURCE_PAUSED` / run `paused` | Resource hard-limit: `next` returns `status: pause` and stops claiming in that call; human/merge pauses wait on AskQuestion. Then `status`/`events` → `resume` |
| `RESOURCE_THROTTLED` | Уменьши волну; жди soft mem / load / slot limits; `next` returns `status: throttle` without new claims |
| Stale claimed/running lease | Lease expiry resets status back to `pending` (there is **no** `expired` dispatch status). `next`/`resume` call `recover_stale_dispatches` before claiming |
| `abort` | Terminal: active dispatches → `cancelled`; run marked aborted (meta `aborted`; store `aborted` or `failed`). Do not `resume`/`next`/`advance` |
| Frozen plan / scope | `start` freezes `PlanArtifact` (`plan.json`) with `spec_sources` + per-task `files_owned` from the task spec; review uses the control-plane changed-file bundle. Do not widen ownership ad hoc |
| Second active run refused | `status` существующего run; `abort` или доведи его; не стартуй дубликат |
| Gate lock / «one deterministic gate is already active» | Подожди; не гоняй два full gate |
| Chat restart | `tasktool status` + `resume` — не новый `start` |
| `/home is the harness meta-root` | Укажи реальный `--repo` проекта |
| Result JSON invalid | Нужен объект с `final_text`/`text`/`result`; `dispatch_id` должен совпадать |
| Legacy bridge orphans | Доиграй через `harness-bridge-worker`, новые runs — только TaskTool |
| `cursor-agent` / SDK missing | Нормально для TaskTool-first; нужно только legacy Engine |

---

## 12. Telegram / dashboard (optional legacy ops)

`harness bot`, `harness dashboard`, `harness tail` остаются для push Engine
observability. Primary TaskTool progress живёт в IDE окнах + JSON CLI +
`harness tasktool status` / event-log.

## Retention (`harness prune`)

Run history is the audit trail: the evidence ledger, the acceptance seal and every
gate result live in `<repo>/.harness/runs/<id>/`. Nothing prunes automatically —
`harness prune` previews by default and only acts with `--apply`.

```bash
harness prune --keep 20                 # preview: what would go, rows + MiB
harness prune --keep 20 --json          # same, machine-readable
harness prune --keep 20 --apply         # delete rows + spools, then VACUUM
harness prune --keep 20 --apply --keep-spools   # shrink the DB, keep evidence on disk
harness prune --keep 5 --project myapp  # scope to one project
```

Two rules hold regardless of flags: only terminal runs (`done` / `failed` /
`aborted`) are eligible, and the newest `--keep` of them always survive. Journals
under `docs/history/` live in the target repo and are never touched.

`events`, `agent_events`, `attempts` and `reviews` carry no foreign key to `runs`,
so prune deletes them explicitly — dropping the run row alone would orphan exactly
the largest tables.
