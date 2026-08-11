# Harness V4 (dev) — TaskTool-first control plane

> **This tree is `/home/1.harness-v4`** — clone-and-upgrade of `/home/1.harness`.
> Production dogfood stays on `1.harness` until Honesty-MVP cutover.
> Design: `ZY-2nd-flight/harness_reports/HARNESS-V4-TECHNICAL-DESIGN-2026-08-05.md`.

> **Primary path:** attach text/docs in Cursor, say «используй harness», follow
> `.cursor/skills/harness-orchestrate/SKILL.md`. Root chat is the only Task Tool
> dispatcher; Python never invokes Task Tool.
>
> Quick path → **`QUICKSTART.md`**. Full ops → **`GUIDE.md`**. Architecture →
> **`ARCHITECTURE.md`**. Background mode (not default) → **`BACKLOG-background-mode.md`**.

## V4 vs production `1.harness`

| | `/home/1.harness` | `/home/1.harness-v4` (this tree) |
|--|--|--|
| Status | dogfood / prod path | experimental upgrade |
| Version | 0.1.x | **4.0.0-dev** |
| Worktrees default | `HARNESS_USE_WORKTREES=0` | **`1`** |
| Soft gates | as in project.toml | inventoried; `HARNESS_ALLOW_SOFT_GATES=0` |
| Doctor | env check | + `--false-coverage-report` / `--multi-tenant-report` / `--soft-gates-report` |
| Skills | auto-discovery | + curated `skills/catalog.json` (Phase 0.5) |
| Brain | `/home/brain` write | **`/home/brain-agents`** agent layer + `harness brain sync` |
| Economics | — | `HARNESS_ECONOMICS` + solo/team fan-out (Phase 3, default off) |
| Meta | — | AHE-lite `harness meta` + skill GC (Phase 4, optional) |

```bash
# Invoke v4 explicitly (do not shadow prod unless you mean to):
export HARNESS_HOME=/home/1.harness-v4   # alias of HARNESS_ROOT
cd /home/1.harness-v4 && . .venv/bin/activate
harness doctor
harness doctor --false-coverage-report --write /tmp/fc.md
harness doctor --multi-tenant-report --write /tmp/mt.md
harness doctor --soft-gates-report --write /tmp/sg.md
harness doctor --meta-eval
harness skills catalog
harness skills select --role worker --stage implement
harness meta eval
harness skills gc propose
```

Deterministic orchestration around Cursor agents: a frozen plan + DAG lives in
files and SQLite; workers/reviewers run as Task Tool jobs claimed via
`harness tasktool next` (default wave 12, hard ceiling 16); gates, leases,
optional worktrees/merge, and checkpoints stay in this control-plane tree.

```
User (ТЗ + docs) ──► Cursor root chat (Task Tool dispatcher)
                         │ planner Task → PLAN.md + tasks/*
                         │ harness verify → plan approval
                         │ harness tasktool start (freeze plan + scope)
                         ▼
              next (--limit 12) → root chat invokes ≤12 Task Tool jobs
              (large task → sub-orchestrator `stage: plan` → advance fans out
               child tasks from its JSON; children never nest Task Tool)
                         │ write result JSON → report → advance
                         ▼
         scoped gates → review → DONE (manual ship; optional merge mode)
```

## Why this shape

- **Context stays in files** (`PLAN.md`, `tasks/*`, prompt/result paths) — not in
  an endless chat transcript.
- **Python is deterministic** — FSM, store, resource policy, gates, merge. All
  stochastic work is inside Cursor Task Tool.
- **Sized for the /home host (24 CPU / 125 GiB)** — defaults: 12 Task jobs / 12 agent slots (hard ceiling 16),
  worktrees on (v4), manual ship, 5 heavy, 4 gate slots; soft/hard
  MemAvailable default to ⅛ / ¹⁄₁₆ of capacity (≈16 / 8 GiB here, sane on
  smaller hosts and inside a cgroup). Admission gates **new claims only** —
  it cannot stop Task Tool jobs already running in the IDE.
- **Agent-in-agent (ADR-0012)** — large tasks get a sub-orchestrator dispatch
  that decomposes into child tasks; the parent returns as the integration
  worker with children `Provides`.
- **Context-first** — `harness context` собирает project context (структура,
  стек, гейты, graphify, brain: ADR/incidents/lessons); компактный brief
  инжектится в каждый dispatch.
- **Resume after chat restart** — leases + checkpoints in SQLite; call
  `harness tasktool resume`, do not invent a second run.
- **Honesty (Phase 1.5)** — evidence% + acceptance ledger become SoT; FSM% is pipeline only.
  Honesty ON by default (`HARNESS_EVIDENCE_GATE=1`; escape `=0`). Soft sensors
  stay refused unless `HARNESS_ALLOW_SOFT_GATES=1` (see `docs/HONESTY-MODE.md`).
  require `HARNESS_ALLOW_SOFT_GATES=1`.

## Requirements

- Cursor IDE with Task Tool (primary).
- Python 3.12+, git; `make setup` (or `pip install -e .`) in this repo.
- Target git repo with a base branch (never `/home` meta-root).
- Optional: Cursor CLI / `CURSOR_API_KEY` only for **legacy** `harness plan` /
  `harness run` (SDK/CLI), not for TaskTool-first.

## Primary loop (exact CLI)

```bash
make setup && . .venv/bin/activate
harness doctor
harness init --repo /abs/path/to/target-repo
harness projects add --name myapp --repo /abs/path/to/target-repo --base main

# Planner Task (from root chat) writes PLAN.md + tasks/* — then:
harness verify --project myapp
# AskQuestion: plan approval — only then:
harness tasktool start "goal" --project myapp --repo /abs/path/to/target-repo \
  --base-branch main --approve-plan --spec-source /path/to/spec.md

harness tasktool next "<run-id>" --project myapp --repo /abs/path/to/target-repo \
  --agent-id "<root-chat-id>" --limit 12
# … invoke ≤12 Task Tool jobs; write result JSON to result_path …
harness tasktool report "<dispatch-id>" --project myapp --repo /abs/path/to/target-repo \
  --result-file /path/to/result.json --agent-id "<root-chat-id>" --ok
harness tasktool advance "<run-id>" --project myapp --repo /abs/path/to/target-repo

# Questions/risk are separate resumable checkpoints:
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/target-repo \
  --answer q1="answer"
harness tasktool resume "<run-id>" --project myapp --repo /abs/path/to/target-repo \
  --approve-risk

# After ship approval:
harness tasktool advance "<run-id>" --project myapp --repo /abs/path/to/target-repo \
  --approved-merge
harness tasktool status "<run-id>" --project myapp --repo /abs/path/to/target-repo
```

Every `tasktool` command prints **JSON on stdout**. Prompt/result payloads are
**files**. Minimal result artifact:

```json
{
  "dispatch_id": "dispatch-…",
  "final_text": "HARNESS_DONE\n\n## Provides\n…"
}
```

(`text` / `result` string keys are also accepted; see `GUIDE.md`.)

## Model routing (TaskTool preference)

Grok-first — see `docs/GROK-DEFAULTS.md`.

| Role | Preferred Task Tool model | Env override |
|---|---|---|
| Root / planner | Grok 4.5 High (`cursor-grok-4.5-high`) | `TASKTOOL_ORCH_MODEL` |
| Main reviewer / goal-judge | Grok 4.5 High (`cursor-grok-4.5-high`) | `TASKTOOL_REVIEWER_MODEL` |
| Worker / sub-orchestrator / research | Grok 4.5 High (`cursor-grok-4.5-high`) | `TASKTOOL_WORKER_MODEL` |

Legacy SDK/CLI model envs (`ORCH_MODEL` / `WORKER_MODEL` / `REVIEWER_MODEL`,
escalation ladder, `claude-opus-4-8`, `kimi-k2.5`, `glm-5.2`, …) remain for push
Engine only — TaskTool routing ignores them. See `.env.example`.

## Legacy / compatibility (not primary)

| Path | Status |
|---|---|
| `harness mode chat` + file-spool bridge | **Deprecated compatibility** — do not use for new runs |
| `harness plan` → `ingest` → `run` (SDK/CLI Engine) | Compatibility / experiments |
| DBOS durable / unattended server workers | **Backlog only** — `BACKLOG-background-mode.md` |

No breaking removal yet.

## Layout

```
.cursor/skills/harness-orchestrate/   # primary TaskTool-first skill
harness/tasktool/                     # pull controller, resources, contracts
harness/store/                        # SQLite + dispatch leases + events
harness/worktree/                     # worktrees + generated-artifact excludes
prompts/  tasks/  PLAN.md
GUIDE.md  ARCHITECTURE.md  QUICKSTART.md
BACKLOG-background-mode.md
```

## Verification

Use the project’s verification commands (`ruff` / `mypy` / `pytest` / fake-driver
TaskTool tests). Isolated dogfood only after those results are green — do not
treat this README as a production readiness claim.
