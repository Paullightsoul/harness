---
name: harness-orchestrate
description: >-
  Primary TaskTool-first harness entrypoint for the /home engineering workspace.
  Drives 1.harness as a deterministic control plane (PlanArtifact, FSM, DAG, leases,
  gates, review, merge) while the root Cursor chat is the only Task Tool dispatcher.
  Use when the user says «используй harness», «use harness», «run through harness»,
  «обвязка», «harness-orchestrate», attaches a ТЗ/spec, or asks to deliver work via
  harness tasktool pull loop — not via long-lived Python runners or file-spool bridge.
metadata:
  harness_role: root-dispatcher
  protocol: tasktool-v3
  adrs: [ADR-0011, ADR-0003, ADR-0005]
---

# Harness Orchestrate (TaskTool-first)

You are the **root Cursor chat** for this run. Python (`1.harness`) is a deterministic
control plane and **cannot** invoke Task Tool. You alone call Task Tool, AskQuestion,
and write result files. Never start a long-lived `harness run`, never poll a file-spool
bridge, never invent FSM truth from chat memory.

**North-star (brain):** agents are stochastic; the harness is deterministic.
Goal → plan → DAG → gates → review → (optional) merge. Canon lives in `/home`
(`brain/`, `shared-context/`, graphify). Details: [brain-canon.md](brain-canon.md).

Activate the project venv before CLI calls:

```bash
cd /home/1.harness && . .venv/bin/activate
```

Frozen CLI (stdout = JSON). Read `--help` before first use of each subcommand:

```text
harness verify
harness tasktool start|next|report|advance|status|abort|resume
```

Defaults (ADR-0011/0012 + host sizing 24 CPU / 125 GiB): **in-place** checkout
(`HARNESS_USE_WORKTREES=0`), **≤12** parallel Task Tool jobs (hard ceiling **16**)
with disjoint `files_owned`, **manual ship** (`HARNESS_SHIP_MODE=manual`
→ DONE after review; human commits/merges). Opt into worktrees / `--approved-merge`
only via env + explicit ship approval. Agent-in-agent: `large` tasks first get a
**sub-orchestrator** dispatch (`stage: plan`) that decomposes into child tasks
(ADR-0012); dispatch those like any worker job.

Exact command forms and JSON contracts: [reference.md](reference.md).

## Checklist

```
- [ ] 1. Intake: resolve --project / --repo (never /home), collect spec-sources
- [ ] 2. harness context --project <p>  → give markdown_path to the planner
- [ ] 3. Planner Task → PLAN.md + tasks/task-*.md (plan artifacts only)
- [ ] 4. harness verify --project <p>  (deterministic; fix plan until green)
- [ ] 5. AskQuestion plan approval → tasktool start … --approve-plan --spec-source …
- [ ] 6. Pull loop (**continuous refill**): next → Task Tool (`run_in_background`) →
         on EACH finish: write result → report → advance → next(free slots) → spawn more.
         Never wait for the whole wave. (stage: plan = dispatch; stage: gate = never)
- [ ] 7. Human pauses: questions / risk / red gate → AskQuestion → resume
- [ ] 8. Finish: status; ship only if ship_mode=merge + separate AskQuestion
- [ ] 9. Chat restart: status + resume (never second start for same active run)
- [ ] 10. Task report → <repo>/harness_reports/<ISSUE>/ → YouTrack comment + readiness % in description
         (language: work done on the task only; never mention harness/run/tooling)
```

## 9. Task report → YouTrack (after meaningful delivery)

Templates: `report_templates/` (`README.md`, `harness-run-report.md`).

**Voice rule:** describe **what was delivered for the product task**. Do **not** mention harness,
runs, Task Tool, workers, DAG, plan_hash, ship_mode, or how orchestration worked.

**Honesty (Phase 1.5):** YouTrack **Готовность = evidence% only** — never FSM/pipeline
completion%. Prefer `harness acceptance show --run-id …` / controller status
`youtrack_readiness` payload. Without an evidence map, post an «in progress» comment
only — **do not invent a %**.

1. Map delivered scope → YouTrack issue(s) (e.g. strip 1 → `ZYN-371`).
2. Write report to `<repo>/harness_reports/<ISSUE_ID>/<YYYY-MM-DD>-<slug>.md` from the template.
3. POST a markdown **comment** on the issue (same content or short summary + file path).
4. GET description; upsert top block between
   `<!-- harness-coverage:start -->` … `<!-- harness-coverage:end -->`
   with visible line **Готовность: NN%** where **NN = evidence_pct** + date + report path
   (no tooling jargon in the visible text). Use the `marker_block` from
   `harness acceptance show` / `youtrack_readiness` when available.
5. NN% from acceptance ledger green required items — do **not** use done/tasks pipeline%.
6. Creds: `<repo>/.env.local.youtrack` (never commit). Unset HTTP proxies if CONNECT 403.
   If creds missing, keep the marker block ready but do not fake readiness from FSM%.


## 1. Intake

1. Treat user text + every attachment/path as a `spec-source`.
2. Resolve absolute `--repo` from explicit path or `harness projects` registry.
   **Refuse `/home`** as target (meta-root guard; incident 2026-07-02).
3. Confirm once via AskQuestion only if project/repo or a material requirement is
   ambiguous. Prefer proceeding when the path is clear.
4. Ensure target has `.harness/project.toml` (`harness init --repo …` +
   `harness projects add` + `harness doctor --project …` if missing).

## 2. Plan (root-invoked planner Task)

Before `start`, run `harness context --project <p>` and pass the returned
`markdown_path` (project structure, stack, gates, brain ADRs/incidents/lessons)
to the planner prompt. Then invoke **one** planner via Task Tool from this root
chat. This is **not** a harness dispatch.

Planner rules:

- Read the project context file first; base decomposition on it + `brain/`.
- Read-only on application source; writes **only** `PLAN.md` + `tasks/task-*.md`
  (or the run-plan location documented by the installed harness).
- Force clarity first (office-hours style): goal / non-goals, assumptions, frozen
  contracts **verbatim**, task DAG with real `depends on`, disjoint ownership,
  acceptance vs Definition of Done, evidence, risks, resources, ship conditions.
- Every task must have non-empty `files_owned` (EN/RU headings or tables the parser
  understands — see [brain-canon.md](brain-canon.md) § Incidents). Empty ownership
  + in-place mode caused infinite scope-exit retries.
- Dependencies must survive parsing (table with a depends column, or `Depends on:`
  in the task body). Do not let a second markdown table wipe the DAG.
- Prefer reuse: point workers at `graphify query`, `shared-context/`, `brain/`.
- Complexity → pipeline depth: `trivial|small|medium|large` (not just model tier).

Then:

```bash
harness verify --project "<project>"
```

On failure, return to the **same** planner for a plan-only fix; re-verify. Do not
`start` on a red plan.

Present the verified plan (goal, contracts, DAG, ownership, gates, risks, ship
action). AskQuestion for **plan approval**. Rejection → planner update → verify again.

## 3. Start (freeze + ingest)

Only after approval:

```bash
harness tasktool start "<goal>" \
  --project "<project>" --repo "<absolute-repo>" --base-branch "<base>" \
  --approve-plan \
  --spec-source "<source>"   # repeat per text/attachment/path
```

Parse JSON; keep `run_id`. `start` freezes `PlanArtifact` — ownership and
`spec_sources` become the source of truth. It never creates a planner dispatch.

## 4. Pull-dispatch loop (continuous refill)

**Why development "stops" between waves:** the control plane already allows
partial progress — `report` one dispatch, `advance` while siblings are still
`claimed`/`running`, then `next` fills freed slots. The anti-pattern is waiting
for every Task Tool in the wave to finish before reporting. Do **not** do that.

Repeat while command JSON says the run can advance. Keep the in-flight set full.

### Fill (bootstrap or after refill)

1. `harness tasktool next "<run_id>" … --agent-id "<root-chat-id>" --limit 12`
   (or `--limit <free_slots>` when topping up; admission already counts active
   claimed/running jobs).
2. **Validate envelopes.** If any dispatch has `stage: gate` or a gate role/kind —
   protocol error: do **not** call Task Tool, do not fabricate a result; stop and
   report. Gates are control-plane work inside `advance`. Envelopes with
   `stage: plan` are **sub-orchestrator decomposition** jobs — dispatch them
   normally (read-only research; result must contain the fenced decompose JSON).
3. On `status: pause` / `throttle` (resource hard/soft limits) → show evidence,
   wait / reduce wave, then `resume` before continuing. Never fight admission.
4. Read each `prompt_path`. Confirm minimal Task Brief sections (below). Overlapping
   `files_owned` across **in-flight** dispatches → serialize those tasks; never
   claim both at once.
5. Invoke new Task Tool jobs with **`run_in_background: true`**. Pass the envelope's
   `model` slug when set. Multiple new jobs → one message. **Always fill to capacity:**
   spawn `min(12, len(envelopes))` — never self-cap at 4 or 6 when `next` returned more.
   Hard ceiling 16. **Only this root chat** may call Task Tool — workers,
   reviewers, goal-judge, and any decomposition sub-orchestrator. Children must
   **not** spawn Task Tool.

### On each completion (immediate — do not wait for siblings)

When **one** background Task returns (or you are notified it finished):

6. Atomically write the full machine-readable response to exact `result_path`
   (minimal shape in [reference.md](reference.md)). Never infer completion from prose.
7. `harness tasktool report "<dispatch_id>" … --result-file … --worker-id "<task-tool-job-id>" --ok`
   (or `--error "…"` on Task Tool failure) — **this dispatch only**.
   `--agent-id` stays your root-chat id (it holds the lease); `--worker-id` is the
   id of the Task Tool job that actually did the work. **They must differ, and
   each worker needs its own `--worker-id`** — otherwise the worker-boundary check
   reads the run as orch==worker cosplay and `HARNESS_NESTED_WORKERS_HARD_FAIL=1`
   refuses DONE. Use the Task Tool job/agent id when you have one, otherwise
   `worker-<dispatch_id>`.
8. `harness tasktool advance "<run_id>" …` — applies this receipt, runs gates for
   that task when due, and enqueues newly-unblocked dependents as PENDING.
9. Immediately `next … --limit <free_slots>` and spawn any new envelopes
   (`run_in_background: true`). Free slots ≈ `max_jobs - in_flight` (in_flight =
   claimed+running you have not yet reported).

Keep looping 6→9 until `next` returns idle and no background Tasks remain, or the
run is `paused` / terminal. **Never** batch-wait for the whole in-flight set then
report×N. **Never** leave free slots empty while `next` still returns dispatches.

**Agent-in-agent is control-plane native (ADR-0012):** a `large` task's first
dispatch is a sub-orchestrator (`stage: plan`). `advance` validates its fenced
decompose JSON, freezes child specs (ExpansionArtifact), fans out child worker
dispatches and later returns the parent as the integration worker with children
`Provides`. You just keep pulling — no manual orchestration.

Medium/large work thus flows through a decomposition **sub-orchestrator** Task
that returns leaf briefs (the fenced decompose JSON) to the control plane;
that agent must not invoke Task Tool children. Conceptual depth ≤ 2
(root → sub-orchestrator → leaf); children never decompose again.
Max depth is **not** nested Task Tool dispatch.

### Exact minimal Task Brief

Prompts must contain these operational sections (no persona essays):

```text
Task Brief
Worktree absolute path: <absolute path>
Owned files only: <exact paths>
Read-only paths: <exact paths>
Frozen contracts (verbatim): <exact contracts>
Acceptance: <observable criteria>
Resource class: <light|standard|heavy|gate>
Relevant canon pointer: <one path/section>
Report format: changed files; evidence; gates; provides; deviations/questions/risks
```

Worker terminal signal: include `## Provides` for dependents and `HARNESS_DONE`.
Review findings: `path:line:severity: message`. Verdicts: `APPROVE` | `CHANGES` only
(malformed → escalate, never silent CHANGES default in your own summaries).

## 5. Quality without prompt bloat

Behavioral hats (not extra persona files):

| Hat | When |
|---|---|
| Architect | Freeze contracts before fan-out |
| Onboarding | Briefs self-contained (subagents have no chat history) |
| Reality Checker | Scope drift, invented APIs, disabled checks, “probably works” → stop |
| Reviewer | Deterministic changed-file bundle from control plane is authoritative |
| AppSec | Security-sensitive diffs; never waive gates |

Retrieval then minimalism (seven rungs): clarify → reuse → delete → simplify →
narrow → automate verification → polish. Security / correct errors / a11y are
non-negotiable. Iterative retrieval before edits; de-sloppify only the owned diff;
compact to goal/contracts/evidence at checkpoints.

## 6. Questions, risks, ship

Three **separate** human approvals — never conflate:

| Gate | When | How |
|---|---|---|
| Plan | Before `start --approve-plan` | AskQuestion |
| Risk | New material risk / scope change | AskQuestion → `resume\|advance … --approve-risk` |
| Ship | Before merge/push/PR/deploy | AskQuestion → `advance … --approved-merge` (only if `ship_mode=merge`) |

If a result requests input: persist answers under the run spool, then
`resume … --answers-file …` or repeated `--answer q1="…"`.

Retry only when JSON authorizes it. Use `abort … --reason` for cancel or
unrecoverable safety violations. After `abort`, do not `next`/`advance`/`resume`.

Default `ship_mode=manual`: after review the task is DONE — you summarize; the
human commits. Do **not** call `--approved-merge` unless env + explicit ship OK.

## 7. Recovery and finish

Chat restart / reconnect:

```bash
harness tasktool status "<run_id>" --project "<p>" --repo "<repo>"
harness tasktool resume "<run_id>" --project "<p>" --repo "<repo>"
```

Do **not** create a duplicate run for the same active repo/goal. Stale
claimed/running leases reset to `pending` on `next`/`resume` (no `expired` status).

At completion: `status` + report task tree, roles, gates, approvals, artifact or
blocker, `run_id`. Offer brain write-back (ADR / lesson) for non-trivial outcomes
per workspace knowledge-writeback rules. Per-task/run history entries are written
automatically by the control plane into `<repo>/docs/history/` (ADR-0013) — do
not duplicate them; you may append a short `## Диалог` summary if the user
discussion added material context.

## Hard refuse

- Python/subprocess as Task Tool dispatcher; file-spool / `harness mode chat` for new runs
- `/home` as `--repo`; `harness init` at meta-root
- Nesting Task Tool inside child agents
- Dispatching gate stages via Task Tool
- Shipping on plan approval alone; risk approval as ship
- Blind retry storms when JSON says BLOCKED / pause / budget exhausted
- Committing `.venv` / caches; stuffing multi-MB diffs into argv prompts

## Relation to other skills

| Skill | Use |
|---|---|
| **harness-orchestrate** (this) | Spec → durable harness run via TaskTool pull |
| `orchestrate-build` | Pure chat-native tree without `1.harness` FSM/store |
| `harness-chat-orchestrator` / bridge-worker | **Legacy only** — finish orphans, then switch here |

If the user wants harness semantics (resume across chat close, gates as oracle,
PlanArtifact freeze), use **this** skill. If they want a one-shot chat tree with
no SQLite control plane, use `orchestrate-build`.
