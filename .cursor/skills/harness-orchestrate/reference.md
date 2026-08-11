# Harness Orchestrate — CLI & contracts

Companion to [SKILL.md](SKILL.md). Activate: `cd /home/1.harness && . .venv/bin/activate`.

## Exact command forms

```bash
harness context --project "<project>" [--refresh]
# → {"markdown_path": "<repo>/.harness/context/project-context.md", …}
#   структура, стек, гейты, graphify, brain (ADR/incidents/lessons) — отдай планнеру

harness verify --project "<project>"

harness tasktool start "<goal>" \
  --project "<project>" --repo "<absolute-repo>" --base-branch "<base>" \
  --approve-plan \
  --spec-source "<source>"          # repeatable

harness tasktool next "<run-id>" \
  --project "<project>" --repo "<absolute-repo>" \
  --agent-id "<root-chat-id>" --limit 12

harness tasktool report "<dispatch-id>" \
  --project "<project>" --repo "<absolute-repo>" \
  --result-file "<abs-result.json>" --agent-id "<root-chat-id>" \
  --worker-id "<task-tool-job-id>" --ok
# failure: replace --ok with --error "<message>"
# --agent-id = lease holder (root chat), --worker-id = executor, distinct per
# worker. Same value for both ⇒ cosplay_risk ⇒ DONE refused (hard-fail default).

harness tasktool advance "<run-id>" \
  --project "<project>" --repo "<absolute-repo>"
# ship (only ship_mode=merge + AskQuestion):  --approved-merge
# risk clear (never ship):                      --approve-risk

harness tasktool status "<run-id>" --project "<project>" --repo "<absolute-repo>"

harness tasktool resume "<run-id>" \
  --project "<project>" --repo "<absolute-repo>"
# optional: --approve-risk
# optional: --answers-file "<spool>/answers.json"
# optional: --answer q1="value"   (repeatable)

harness tasktool abort "<run-id>" \
  --project "<project>" --repo "<absolute-repo>" --reason "<reason>"
```

`--force` on `start` exists for reclaiming a stuck lock — use only with explicit
human intent after `status` shows why the previous run is blocked.

## One-time project setup

```bash
cd /home/1.harness && make setup && . .venv/bin/activate
harness init --repo /abs/path/to/repo          # never /home
harness projects add --name myapp --repo /abs/path/to/repo --base main
harness doctor --project myapp
```

Target profile: `.harness/project.toml` (`GateProfile` — lint/types/test cmds per
language). Missing profile → fallback `make check`.

## JSON: `next` dispatch envelope (shape)

```json
{
  "ok": true,
  "run_id": "run-…",
  "status": "ok",
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
      "worktree": "/abs/path/to/repo",
      "files_owned": ["src/api.py"],
      "read_only_context": ["PLAN.md"],
      "frozen_contracts": ["…"],
      "acceptance": ["…"],
      "resource_class": "standard",
      "dependencies": []
    }
  ]
}
```

`status` on the response may be `ok` | `throttle` | `pause` | `claimed` | `idle`.
On throttle/pause, do not claim more work; follow [SKILL.md](SKILL.md) § pull loop.
`claimed` / `idle` are normal fill outcomes (`idle` = nothing pending right now).

If `stage` / role looks like **gate** → protocol error (do not Task Tool).
`stage: plan` = sub-orchestrator decomposition job → dispatch normally.

## Continuous refill (root dispatcher)

Control plane is already per-dispatch. Root chat must match:

```text
fill:  next --limit N  →  Task Tool (run_in_background: true) × envelopes
each:  Task finishes → write result_path → report → advance → next(free) → spawn
```

| Do | Don't |
|---|---|
| `report` + `advance` as soon as **one** worker finishes | Wait for all Task Tools, then `report`×N |
| `next` immediately after `advance` to refill freed slots | Sit idle until the whole wave ends |
| `run_in_background: true` so the root can keep coordinating | Block the root chat on a synchronous multi-Task wait |
| Spawn **all** envelopes from `next` (up to 12; ceiling 16) | Self-cap at 4–6 when more were claimed |
| Cap only on admission / overlapping `files_owned` | Leave free slots empty «to be safe» |

`advance` while siblings are still running is supported: it consumes SUCCEEDED
receipts, runs that task's gates, and enqueues newly-ready dependents as PENDING.
`next` admission counts only `claimed`+`running`, so a finished job frees a slot.

## Speed mode (critical path)

On this host `.env` defaults to:

| Env | Value | Effect |
|---|---|---|
| `HARNESS_REVIEW_MIN_COMPLEXITY` | `large` | `medium`/`small` → gates → DONE (no reviewer agent) |
| `HARNESS_DAG_UNLOCK` | `post_gate` | Dependents READY when parent enters REVIEW/DONE, not after reviewer |
| `TASKTOOL_REVIEWER_MODEL` | `cursor-grok-4.5-high` | Faster when large still needs review |

Revert quality-first: `HARNESS_REVIEW_MIN_COMPLEXITY=small`, `HARNESS_DAG_UNLOCK=done`.

## JSON: sub-orchestrator decompose result (`stage: plan`)

The agent's `final_text` must contain exactly one fenced ```json block:

```json
{
  "decompose": true,
  "reason": "why this split",
  "subtasks": [
    {
      "id": "api",
      "title": "API layer",
      "summary": "self-contained brief for the child agent",
      "files_owned": ["src/api/**"],
      "depends_on": [],
      "acceptance": ["pytest tests/test_api.py -q"],
      "complexity": "small"
    }
  ]
}
```

or `{"decompose": false, "reason": "…"}` (analysis text then goes to the direct
worker). Control plane validates: 2..`HARNESS_DECOMPOSE_MAX_CHILDREN` children,
child ownership ⊆ parent `files_owned`, pairwise disjoint, sibling-only DAG,
complexity trivial|small|medium. Invalid → automatic retry with the exact
validation errors as feedback. `tasktool status` exposes `expansions:
{parent: [child ids]}`. Spec-level override: `Decompose: yes|no` line; kill
switch `HARNESS_DECOMPOSE=0`.

## JSON: result file (minimal)

```json
{
  "dispatch_id": "dispatch-…",
  "final_text": "HARNESS_DONE\n\n## Provides\nPublic API for dependents.\n"
}
```

Accepted aliases for the body: `text` or `result` instead of `final_text`.
`report` is idempotent for the same content. `dispatch_id` must match the envelope.

Worker body should include deterministic changed paths, relevant gate evidence,
`## Provides`, and terminal `HARNESS_DONE`. Reviewer body: structured findings +
`VERDICT: APPROVE` or `VERDICT: CHANGES`.

## Model routing (TaskTool)

Source of truth: envelope `model` from `next`, backed by
`harness/tasktool/model_map.py` under `HARNESS_HOME` (v4: `/home/1.harness-v4`).
Grok-first — `docs/GROK-DEFAULTS.md`. Env overrides (exact Task Tool slugs):

| Role | Default slug | Env |
|---|---|---|
| Planner / root | `cursor-grok-4.5-high` | `TASKTOOL_ORCH_MODEL` |
| Worker / sub-orchestrator / research | `cursor-grok-4.5-high` | `TASKTOOL_WORKER_MODEL` |
| Reviewer / goal-judge | `cursor-grok-4.5-high` | `TASKTOOL_REVIEWER_MODEL` |

Do **not** use legacy `ORCH_MODEL` / `WORKER_MODEL` / `REVIEWER_MODEL` for TaskTool
path. If the Task tool rejects a slug, re-read `--help` / available model list in
the current Cursor build and pass a supported slug — prefer what `next` returned.

Complexity → resource class: `trivial`→light, `large|high`→heavy (admission).

## Resource policy (defaults — 24 CPU / 125 GiB host)

| Limit | Default | Notes |
|---|---|---|
| TaskTool jobs | 12 (hard ceiling 16) | `MAX_TASKTOOL_JOBS` / `HARNESS_MAX_TASKTOOL_JOBS` |
| Agent slots | 12 | `MAX_AGENT_SLOTS` |
| Heavy jobs | 5 | `MAX_HEAVY_JOBS` |
| Concurrent gates | 4 slots | locks `.harness/tasktool-gate[-N].lock`; >1 только worktrees+scoped |
| Worktrees | off | `HARNESS_USE_WORKTREES=1` |
| Ship | manual | `HARNESS_SHIP_MODE=merge` for merge queue |
| Soft MemAvailable | 16 GiB | throttle (`RESOURCE_THROTTLED`) |
| Hard MemAvailable | 8 GiB | pause (`RESOURCE_PAUSED`) |
| Load | `load1 ≥ cpu×threshold` | `LOAD_PER_CPU_THRESHOLD` (default 1.1) |
| Decompose | on | `HARNESS_DECOMPOSE`, `HARNESS_DECOMPOSE_MAX_CHILDREN=12` |
| Brain | `/home/brain` | `HARNESS_BRAIN_ROOT` (lessons, ADR, architecture в контекст) |

Scoped gates after each worker; full gates pre/post merge when ship_mode=merge.
Full-repo suites are always serialized; in-place shared checkout keeps a single
gate slot regardless of `MAX_GATES`.

## Run / dispatch lifecycle

**Run:** `planning` / `plan_draft` / `running` / `paused` / `done` / `failed` / `aborted`.

**Dispatch lease:** `pending → claimed → running → succeeded | failed | cancelled`.
No `expired`. Timed-out claimed/running → reset to `pending` via
`recover_stale_dispatches` inside `next`/`resume`.

**Abort:** terminal; active dispatches → `cancelled`. Do not continue the pull loop.

Spool root: `<repo>/.harness/tasktool/<run_id>/` (prompts, results, plan.json,
answers). Keep result paths under that tree.

## Project.toml sketch

```toml
language = "python"
canon = "python-backend"

[[gates]]
id = "lint"
cmd = "ruff check ."
[[gates]]
id = "types"
cmd = "mypy ."
[[gates]]
id = "test"
cmd = "pytest -q"
```

PHP/TS/etc.: use language-appropriate cmds; avoid gates that require missing
`vendor/`/`node_modules` without a prior install task (dogfood lesson).

## Troubleshooting

| Symptom | Action |
|---|---|
| `RESOURCE_PAUSED` / `status: pause` | Show evidence; `resume` after mem/load recovers or human clears merge/risk |
| `RESOURCE_THROTTLED` | Shrink wave; wait; `next` again |
| Second active run refused | `status` existing; `abort` or finish; no duplicate `start` |
| `/home is the harness meta-root` | Pass real project `--repo` |
| Gate lock busy | Wait; one deterministic gate at a time |
| Invalid result JSON | Need object with `final_text`/`text`/`result` + matching `dispatch_id` |
| Chat restart | `status` + `resume`, not new `start` |
| Sub-orchestrator result rejected | Re-dispatch the retry envelope — feedback already contains exact validation errors |
| Infinite worker retries | Check `files_owned` non-empty; gate profile; retry budget → BLOCKED |
| DAG lost (`dependencies: []`) | Fix PLAN tables; re-verify; see brain-canon incidents |
| Legacy bridge orphans | Finish via `harness-bridge-worker`, then only TaskTool for new work |
| `harness: command not found` | Activate `/home/1.harness/.venv` |

## Legacy (do not use for new runs)

- `harness mode chat` / file-spool / `CursorTaskRunner` bridge
- Skills `harness-chat-orchestrator`, `harness-bridge-worker` (orphan cleanup only)
- Push `plan` → `ingest` → `run` Engine (experiments / CLI-only hosts)
- `HARNESS_DURABLE` / remote workers / cgroups unattended — backlog, not product

Canonical docs in-repo: `/home/1.harness/{ARCHITECTURE,GUIDE,QUICKSTART,ROADMAP}.md`.
