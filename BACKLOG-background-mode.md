# Backlog — background / unattended mode

> **Not implemented. Not default.** This file is the holding pen for server-driven
> execution that must stay off until the shared VPS can absorb it safely.
> Primary path today: TaskTool-first pull mode (`harness tasktool …` +
> `.cursor/skills/harness-orchestrate/SKILL.md`). See `ARCHITECTURE.md` and
> `ADR-0011`.

## Why it is deferred

TaskTool mode keeps agent work inside Cursor and caps local load (2 Task jobs /
2 agent slots, 1 heavy, 1 gate; soft MemAvailable ≤3 GiB / hard ≤2 GiB). A
return to autonomous `cursor-agent` / SDK / DBOS workers on the same host would
again stack worker + full gates + optional de-sloppify + reviewer per DAG task
and risk the `/home` meta-root / memory incidents already documented.

## Prerequisites before re-enabling as a product path

| Area | Requirement |
|---|---|
| Process isolation | cgroups v2 and/or systemd `MemoryMax=` / `CPUQuota=` per worker and per gate |
| Memory headroom | zram and/or swap; `doctor` must refuse zero-swap hosts for unattended runs |
| Remote execution | remote workers or managed cloud sandboxes (not local `MAX_PARALLEL` alone) |
| Queue safety | admission backpressure beyond today’s soft/hard MemAvailable + load throttle |
| Validation | load tests that prove ≤1 heavy gate and bounded RSS under fan-out |
| Authoritative store | DBOS/Temporal (or equivalent) as the durable authority — not “SQLite plus hope” |
| Operations | explicit unattended mode flag, alerts, cancel/abort, and lease recovery runbooks |

## Explicitly out of scope for now

- Autonomous subprocess agents as the default UX
- DBOS/Temporal fleet orchestration as the recommended path
- Managed multi-tenant cloud sandboxes / web multi-user product
- SkillOpt nightly auto-edits of skills without held-out replay + green gates
- Cross-vendor API farm
- Adopting [harness/harness](https://github.com/harness/harness) SCM/CI platform into the agent layer

## Compatibility note

Legacy push Engine (`harness run`), file-spool chat bridge (`harness mode chat` /
`CursorTaskRunner`), and optional `HARNESS_DURABLE=1` remain in-tree for
compatibility and experiments. They are **not** the TaskTool-first product path
and must not be documented as production-ready background mode.

When prerequisites above land, promote items into `ROADMAP.md` with measurable
acceptance criteria — do not flip defaults silently.
