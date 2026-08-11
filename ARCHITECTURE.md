# Архитектура harness (TaskTool-first **v4-dev**)

> Clone: `/home/1.harness-v4` (не трогать prod `/home/1.harness` до cutover).
> Version: `4.0.0-dev`. Design:
> `ZY-2nd-flight/harness_reports/HARNESS-V4-TECHNICAL-DESIGN-2026-08-05.md`.

Детерминированный control plane вокруг стохастических агентов Cursor.
**Primary execution plane — Cursor Task Tool** (pull). Bash Phase 0 и push
Engine остаются as compatibility; unattended server workers are backlog
(`BACKLOG-background-mode.md`). ADR: `brain/decisions/ADR-0011-…`.

## Главный принцип

> Агенты недетерминированы. Harness — детерминирован.
> Root Cursor chat — **единственный** dispatcher Task Tool.
> Python **никогда** не вызывает Task Tool, не стартует IDE-агентов и не
> держит prose-poller как основной UX.
> Всё вокруг (состояние, leases, гейты, мерж, бюджет, checkpoints) — конечный
> автомат в `1.harness`, который переживает закрытие чата.

## Слои

```
INTERFACE   CLI tasktool · projects · notify          harness/interface
CONTROL     TaskToolController · Store · ResourcePolicy
            FSM · DAG · leases · checkpoints          harness/tasktool, store, policy
DATA        Cursor Task Tool (root chat dispatcher)   .cursor/skills/harness-orchestrate
SUBSTRATE   git worktrees · scoped/full gates · merge harness/worktree, gates
```

Legacy (compatibility only): `SdkRunner` / `CliRunner` / `CursorTaskRunner` +
file-spool bridge + push `Engine` — not the recommended path.

## Поток данных (pull)

```
goal + attachments
   │ harness context --project …   (project context: структура, стек, brain)
   │ Task Tool planner (root chat) → PLAN.md + tasks/*
   │ harness verify (deterministic)
   │ plan approval (AskQuestion)
   ▼
harness tasktool start --approve-plan --spec-source …
   │ freeze PlanArtifact · ingest Run · enqueue dispatches
   ▼
loop while JSON says advance is possible:
   harness tasktool next --limit 12
        → ≤12 DispatchEnvelope (prompt_path / result_path / model / worktree)
        → root chat invokes Task Tool (never Python)
   write result JSON → harness tasktool report
   harness tasktool advance
        → large task: sub-orchestrator (stage plan) → ExpansionArtifact →
          child tasks fan-out → parent integrates with children Provides
        → finalize worker · scoped gate · review / goal-judge
        → pre-merge full gate · ship pause · merge · post-merge full gate
```

Gates with `stage: gate` must **never** appear as Task Tool jobs — they are
control-plane work inside `advance`. Envelopes with `stage: plan`
(sub-orchestrator) **are** normal Task Tool jobs: read-only decomposition.

## Agent-in-agent (decompose)

`large`-задача (или спека с `Decompose: yes`) сперва получает
**sub-orchestrator** dispatch (`stage: plan`, kind `sub-orchestrator`). Агент
читает frozen spec + project context, исследует репо и возвращает fenced JSON:

- `{"decompose": true, "subtasks": [...]}` — контроллер валидирует
  (ownership детей ⊆ ownership родителя, пары дизъюнктны, DAG без циклов,
  ≤ `HARNESS_DECOMPOSE_MAX_CHILDREN`, детям запрещён `large`), замораживает
  child-спеки + `ExpansionArtifact` в spool, создаёт дочерние задачи и парит
  родителя в PENDING. Дети идут обычным worker→gate→review конвейером.
  Когда все DONE — родитель возвращается **интеграционным воркером** с
  `Provides` всех детей в контексте.
- `{"decompose": false, "reason": …}` — анализ уходит прямому воркеру как
  prior context (research-пасс не пропадает).
- Невалидный ответ — retry с конкретными ошибками валидации в feedback
  (обычный retry-бюджет).

Root PlanArtifact остаётся замороженным: расширения — отдельные versioned
артефакты `expansions/<parent>.json` с sha256 в controller meta. Root chat
по-прежнему единственный dispatcher Task Tool (ADR-0011); глубина — один
уровень (дети не декомпозируются, их спеки несут `Decompose: no`).

## Project context / brain

`harness/tasktool/project_context.py` детерминированно собирает бриф проекта:
профиль (`.harness/project.toml`), layout (2 уровня), манифесты зависимостей,
README head, graphify-хинт, из `brain/` — architecture note, релевантные ADR и
инциденты, счётчик lessons. Кэш: `<repo>/.harness/context/project-context.{md,json}`
по git HEAD (+24h TTL). CLI: `harness context --project … [--refresh]`.

Использование: планнер читает полный markdown; каждый dispatch получает
компактный `### Project brief` в ContextPack (бюджет 9 000 символов, приоритет
усечения: contracts > checkpoint/feedback > brief > lessons); sub-orchestrator
дополнительно получает путь к полному файлу в Read-only paths. Brain root —
`HARNESS_BRAIN_ROOT` (default `/home/brain-agents`); human canon
`HARNESS_BRAIN_CANON=/home/brain` is sync-only. Lessons write to the agent
layer on DONE. CLI: `harness brain sync|query`. Economics (optional):
`HARNESS_ECONOMICS=1` → breadth gate / `DECOMPOSE_DENIED` + solo|team fan-out
caps (`docs/PHASE3-ECONOMICS-BRAIN.md`). Meta (Phase 4, optional):
`harness meta eval` / `harness skills gc` — AHE-lite predictions + skill
quarantine (`docs/PHASE4-META.md`).

## Project journal (ADR-0013)

На каждую DONE-задачу и по завершении run'а контроллер детерминированно пишет
запись истории в сам целевой репозиторий:
`<repo>/docs/history/YYYY-MM-DD-HHMMSS-<slug>.md` (`harness/journal.py`).
Содержимое собирается из store/чекпоинтов без LLM: review-вердикты, вопросы
пользователю, декомпозиция (ExpansionArtifact), changed files, `Provides`,
число попыток и причины доработок. Идемпотентно через событие
`JOURNAL_WRITTEN`; ошибки журнала не роняют run; секреты редактируются.
Off-switch: `HARNESS_PROJECT_JOURNAL=0`; каталог: `HARNESS_JOURNAL_DIR`.
Диалоговые записи (вне harness) пишет stop-hook + правило
`project-history.mdc` в том же формате (`source: dialog`).

## Ownership boundary

| Owns | Does not own |
|---|---|
| `1.harness` — FSM, SQLite store, dispatch leases, worktrees, resource admission, gates, guard, merge, checkpoints, event-log | Invoking Cursor Task Tool |
| Root Cursor chat — planner Task, worker/reviewer Task Tool calls, AskQuestion approvals, writing result files | Replacing store/FSM truth with chat memory |
| Legacy Engine/bridge | New-run UX (deprecated compatibility) |

## Конечный автомат задачи

`harness/domain/state_machine.py` — единственное место разрешённых переходов.
TaskTool path drives the same statuses via `TaskLifecycleService`
(`harness/tasktool/lifecycle.py`): prepare → worker finalize → gating → review →
merge queue → done, plus recovery.

**Dispatch leases** (`DispatchStatus`):
`pending → claimed → running → succeeded | failed | cancelled`.
There is **no** `expired` status. When a claimed/running lease times out,
`recover_stale_dispatches` (called from `next` / `resume`) resets the row to
`pending` for FIFO re-claim.

**Run status:** `planning` / `plan_draft` / `running` / `paused` / `done` /
`failed` / `aborted`.
- `paused` — human gate, resource hard-limit, or merge-approval wait. Resource
  hard-limit makes `next` return `status: pause` without further claims in that
  call; after any pause, call `resume` before continuing the pull loop.
- `abort` — terminal: active dispatches → `cancelled`; do not
  `resume`/`next`/`advance` an aborted run (meta status `aborted`; store may
  record `aborted` or `failed` depending on controller revision).
- `start` freezes a `PlanArtifact` (`plan.json`) with `spec_sources` and
  per-task `files_owned` (scope from the task markdown); that freeze is the
  ownership source of truth for the run.

## Resource defaults

Tuned for the shared /home host — 24 CPU / ~125 GiB
(`harness/tasktool/resources.py`); controller hard-ceiling is 16:

| Limit | Default | Env |
|---|---|---|
| TaskTool jobs | 12 | `MAX_TASKTOOL_JOBS` / `HARNESS_MAX_TASKTOOL_JOBS` |
| Agent slots | 12 | `MAX_AGENT_SLOTS` |
| Heavy jobs | 5 | `MAX_HEAVY_JOBS` |
| Concurrent gates | 4 slots | `MAX_GATES` (locks `.harness/tasktool-gate[-N].lock`) |
| Worktrees | **on** (v4) | `HARNESS_USE_WORKTREES=0` to disable |
| Ship | manual → DONE | `HARNESS_SHIP_MODE=merge` for `--approved-merge` |
| Soft MemAvailable | ⅛ of capacity (≈16 GiB here) | `SOFT_MEM_AVAILABLE_BYTES` |
| Hard MemAvailable | ¹⁄₁₆ of capacity (≈8 GiB here) | `HARD_MEM_AVAILABLE_BYTES` |
| Load / CPU | throttle at `load1 ≥ cpu_count * threshold` | `LOAD_PER_CPU_THRESHOLD` (default 1.1) |
| Hard job ceiling | 16 | code `_HARD_JOB_CEILING` (clamps jobs/slots) |
| Decompose | on | `HARNESS_DECOMPOSE` (+ `HARNESS_DECOMPOSE_MAX_CHILDREN`, default 12) |

Capacity comes from the cgroup v2 limit when one applies, else `MemTotal` —
so a container does not admit a full wave against the host's free memory.

**Admission gates new claims only.** Task Tool jobs run in the IDE's process
tree; a `PAUSE` stops issuing work but cannot stop what is already running.

Events: `RESOURCE_THROTTLED` / `RESOURCE_PAUSED`.

### Gates

- **Scoped** (`full=False`) after each worker — profile gates filtered by
  `task_ids` when present.
- **Full** (`full=True`) pre-merge and post-merge / integration.
- Gate slots: >1 concurrent gate only for scoped runs under worktrees
  (disjoint cwd). Full suites and in-place shared checkout are always
  serialized on slot 0.

### De-sloppify / artifacts

- TaskTool path: **no default de-sloppify agent** after green gates for small work
  (skill may still ask workers to clean their owned diff).
- Legacy push Engine may still run `HARNESS_DESLOPPIFY` — unrelated to TaskTool
  defaults.
- Worktree commits exclude generated caches (`__pycache__`, nested `venv`/
  `.venv`, `node_modules`, `build`, `dist`, `cache`, coverage, egg-info, …)
  via `:(glob,exclude)**/…` pathspecs — see `harness/worktree/manager.py`.

### Safety stops

- Plan approval before `start --approve-plan`
- Risk / question / red gate → pause; human via AskQuestion + `resume`
- Ship approval before `advance --approved-merge`
- Refuse second active TaskTool run on the same repo (active lock under
  `.harness/tasktool/`)
- Refuse `/home` as target repo (meta-root guard)

## Model routing

`harness/tasktool/model_map.py` is intentionally independent from legacy
`Settings.role(Role).*` SDK/CLI model names:

| Preference (TaskTool) | Slug | Typical role | Env override |
|---|---|---|---|
| Grok 4.5 High | `cursor-grok-4.5-high` | root planner / orch | `TASKTOOL_ORCH_MODEL` |
| Grok 4.5 High | `cursor-grok-4.5-high` | worker / sub-orchestrator / research | `TASKTOOL_WORKER_MODEL` |
| Grok 4.5 High | `cursor-grok-4.5-high` | main reviewer / goal-judge | `TASKTOOL_REVIEWER_MODEL` |

See `docs/GROK-DEFAULTS.md`. Env overrides still accept other `AVAILABLE_TASKTOOL_MODELS` slugs (GPT/Claude/…).

Complexity maps to resource class / tier (`trivial`→light, `large|high`→heavy).
Legacy SDK/CLI models and escalation ladders stay on the push Engine path.

## Protocol contracts

- `TASKTOOL_PROTOCOL_VERSION = "3.0"`
- `PlanArtifact` / `DispatchEnvelope` — versioned JSON, prompt/result **paths**
- CLI stdout — JSON (`ok`, `run_id`, `dispatches`, …)
- Result file — JSON object with `final_text` (or `text`/`result`) and optional
  matching `dispatch_id`

## CLI surface (frozen)

```text
harness tasktool start|next|report|advance|status|abort|resume
harness verify
```

Python TaskTool CLI never constructs an agent runner.

## Compatibility / backlog

| Component | Role today |
|---|---|
| File-spool + `harness mode chat` | Deprecated compatibility |
| Push `Engine` + SDK/CLI | Compatibility / experiments |
| DBOS / Temporal / remote workers / cgroups unattended | **Not implemented as product** — `BACKLOG-background-mode.md` |

## Карта фаз (кратко)

- **v3 TaskTool pull** — implemented control plane + skill (verify with suite /
  isolated dogfood; not a blanket production claim).
- **Background mode** — blocked on cgroups/zram/remote workers/load tests.
- Older Phase 3–6 items — see `ROADMAP.md` / `PLAN-harness-v2.md`.
