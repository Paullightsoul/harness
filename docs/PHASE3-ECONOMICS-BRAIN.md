# Phase 3 — Economics + brain-agents

Solo-safe defaults: **`HARNESS_ECONOMICS=0`**. Human vault `/home/brain` is
**read-only** for agents; writeback goes to `/home/brain-agents`.

## Enable economics (breadth + fan-out profile)

```bash
export HARNESS_HOME=/home/1.harness-v4
cd /home/1.harness-v4 && . .venv/bin/activate

export HARNESS_ECONOMICS=1
export HARNESS_FANOUT_PROFILE=solo   # or team
# optional: keep baseline children clamp
# export HARNESS_DECOMPOSE_MAX_CHILDREN=8
```

| Flag | Default | Effect |
|------|---------|--------|
| `HARNESS_ECONOMICS` | `0` | `1` → breadth gate before fan-out + profile caps |
| `HARNESS_FANOUT_PROFILE` | `solo` | `solo`: 4 jobs / 4 children; `team`: 12 / 12 |
| `HARNESS_BRAIN_ROOT` | `/home/brain-agents` | Agent layer (lessons writeback) |
| `HARNESS_BRAIN_CANON` | `/home/brain` | Human canon (sync source only) |

When economics is on and subtasks are **not disjoint** (overlap, identical
ownership, or children mirroring parent wholesale), the control plane emits
`DECOMPOSE_DENIED` and falls through to a **direct worker** — no multi-agent tax
without isolation.

## brain-agents sync / query

```bash
export HARNESS_HOME=/home/1.harness-v4
cd /home/1.harness-v4 && . .venv/bin/activate

# Progressive sync: decisions, architecture, incidents, templates, integrations
# → /home/brain-agents + cards/ + index.json (never writes /home/brain)
harness brain sync

# Keyword search over cards (pointers)
harness brain query "TaskTool isolation ADR" --limit 5
harness brain query "auth redis" --kind adr --json

# Agent-authored card (stays in agent layer)
harness brain write-card --id lesson-zy-gate --title "PHPUnit soft gate" \
  --kind lesson --path /home/brain-agents/lessons/zy/note.md --summary "…"
```

### Layout

```text
/home/brain-agents/
  README.md          # agent-layer rules
  index.json         # card catalog + sync metadata
  cards/*.json       # compact pointers
  decisions/         # synced from canon
  architecture/
  incidents/
  templates/
  integrations/
  lessons/<project>/ # writable by harness DONE path
```

### ContextPack

With `HARNESS_CONTEXT_POINTER_ONLY=1` (default), ContextPack injects
`brain_root` + up to 3 `brain_card:` pointers from `index.json` — never ADR bodies.

Lessons on DONE write only when `HARNESS_BRAIN_ROOT` ≠ human canon; otherwise the
controller refuses and emits an error event.

## Related

- Tech design §14 Phase 3 — `harness_reports/HARNESS-V4-TECHNICAL-DESIGN-2026-08-05.md`
- Skills/memory brief — `harness_reports/HARNESS-SKILLS-MEMORY-BRAIN-2026-08-05.md`
- Phase 2 governors — `docs/PHASE2-GOVERNORS.md`
