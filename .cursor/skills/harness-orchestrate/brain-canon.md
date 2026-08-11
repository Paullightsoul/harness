# Harness Orchestrate — brain canon

Pointers into `/home/brain` that this skill operationalizes. Prefer reading the
linked ADR when a decision is contested; do not re-litigate settled defaults mid-run.

## ADR map

| ADR | Status | What it locks for this skill |
|---|---|---|
| [[ADR-0011-tasktool-first-harness]] | accepted | Root chat = only Task Tool dispatcher; Python = pull control plane; `tasktool next/report/advance`; ≤12 jobs (hard ceiling 16); in-place + manual ship defaults; plan freeze; resource governor |
| [[ADR-0012-harness-agent-in-agent-project-context]] | accepted | Agent-in-agent: `stage: plan` sub-orchestrator → ExpansionArtifact → child fan-out → parent integration; `harness context` (структура/стек/brain) в планнер и каждый dispatch; лимиты под 24 CPU / 125 GiB; `HARNESS_BRAIN_ROOT=/home/brain` |
| [[ADR-0014-harness-parallelism-12-16]] | accepted | Wave 12 / hard ceiling 16 / heavy 5 / gates 4 / load×1.1 / decompose children 12 — fuller host utilization |
| [[ADR-0003-harness-polyglot-protocol-durable-execution]] | proposed | Harness = protocol (FSM + event-log + DAG + briefs + GateProfile), not a Python monolith; computational gates before inferential review; polyglot via `.harness/project.toml` |
| [[ADR-0005-harness-v2-observability-memory-channels]] | proposed | Understanding-brief / question-protocol / Provides / lessons in `brain/` / tiered pipeline depth / de-sloppify as focused pass / truthful cost+tokens — close white spots **inside** the protocol |
| [[ADR-0006-brain-autonomous-writeback-hook]] | — | Non-trivial outcomes → `brain/` (hook may write; still offer manual ADR/lesson when warranted) |

Harvest index: [[ideal-cursor-harness-harvest]].

## Hard invariants (never surrender)

From harvest log + ADR-0011:

1. **FSM + event-log + gates are the oracle** — chat memory is not truth.
2. **Cursor Task Tool is the agent runtime** for the primary path.
3. **`/home` canon** (`brain/`, graphify, `shared-context/`) projects into briefs.
4. **Single-writer-per-file** (worktrees optional; ownership always required).
5. **Plan freeze / replay** — verified PlanArtifact before execution.
6. **Human checkpoints** at plan, material risk, and ship (separate approvals).

## Harvest → behavior (already folded into SKILL)

Cherry-picked; we do **not** adopt foreign control planes wholesale.

| Source | Take | Reject |
|---|---|---|
| OMA | planOnly → freeze → runFromPlan; inspectable DAG; per-task verify | Rewrite on TS / replace Cursor runtime |
| gstack | office-hours forcing questions; freeze/guard; learn → brain | 23 persona slash-commands as second orchestrator |
| Osmani agent-skills | anti-rationalization; DoD ≠ acceptance; personas don't invoke personas | `/build auto` without our FSM |
| ECC / Ralphinho | tiered pipeline depth; eviction context on merge conflict; SHARED_TASK_NOTES | Import 261 skills into every prompt |
| Ponytail / MiMo / others | minimalism ladder; checkpoints; goal-judge on large only | Product replacement of `1.harness` |

Full notes: `brain/architecture/harvest-*.md`.

## Incidents → guardrails

### 2026-07-02 — git pollution / E2BIG
[[2026-07-02-harness-git-pollution-cpu-degradation]]

- Never `harness init` / `--repo` on `/home` meta-workspace.
- Ensure `.venv/`, caches, `node_modules` are gitignored **before** first commit.
- Never stuff multi-MB `git diff` into argv prompts (prompt-file / size guard).

### 2026-07-21 — TaskTool dogfood gate/retry loop
[[2026-07-21-tasktool-zy-dogfood-gate-retry-loop]]

- PLAN dependency tables must have a recognizable depends column; don't let a
  following partitions table wipe `dependencies` to `[]`.
- Task ownership: support `# Файлы` **and** `## Files (owned)` / markdown tables /
  body `Depends on:`.
- Empty `files_owned` must not mean “entire dirty tree is scope exit”.
- Gate failures that need vendor/install → fix profile or add install task; don't
  infinite `_queue_worker_retry`.
- Retry budget must increment → BLOCKED + risk pause, not silent storms.
- Workers may write `HARNESS_DONE` while control plane is broken — always trust
  `status`/FSM over chat claims of “done”.

## Layers (ADR-0003 taxonomy, skill view)

```
L7 Interface   → this skill + harness tasktool CLI + AskQuestion
L6 Observability → status JSON, event-log, IDE Task windows (not prose poller)
L5 Guardrails  → ownership, PROTECTED_PATHS, budgets, resource governor
L4 Verification → GateProfile (computational) then reviewer (inferential)
L3 Context     → minimal briefs + frozen contracts + brain/graphify pointers
L2 Orchestration → DAG + root-only Task Tool (+ optional sub-orch briefs)
L1 Control     → 1.harness Store / leases / PlanArtifact / checkpoints
```

Background unattended (cgroups, remote workers, Temporal park) is **backlog** —
see `1.harness/BACKLOG-background-mode.md`. Do not promise it in user reports.

## Memory write-back (when the run taught something)

| Outcome | Where |
|---|---|
| Architectural trade-off | `brain/decisions/ADR-NNNN-*.md` |
| Production/dogfood failure mode | `brain/incidents/YYYY-MM-DD-*.md` |
| Recurring agent mistake | `brain/lessons/<service>/…` + optional `AI_MEMORY.md` anti-pattern line |
| New GateProfile / integration | `brain/architecture/` or `brain/integrations/` |

Update the relevant `_MOC.md`. Use Obsidian wiki-links.

## Authority order on conflict

1. Installed CLI behavior (`harness … --help`, JSON stdout) — ground truth for flags.
2. Accepted ADR-0011 defaults for TaskTool-first UX.
3. This skill's procedure.
4. `1.harness/{ARCHITECTURE,GUIDE}.md` narrative.
5. Harvest notes (ideas, not mandates).
6. Legacy bridge / push Engine docs — compatibility only.
