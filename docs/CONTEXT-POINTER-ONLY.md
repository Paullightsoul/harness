# ContextPack pointer-only policy (V4 Phase 0.5)

Flag: `HARNESS_CONTEXT_POINTER_ONLY=1` (default in v4).

## Rules

- ContextPack may include **path pointers** to brain / AI_MEMORY / shared-context — never full dumps.
- Lesson bodies are omitted under pointer-only; only `lesson:<path> — title`.
- Lines matching dump markers (`AI_MEMORY`, `/home/brain/`, `BEGIN DUMP`, …) longer than 80 chars are redacted; `ContextPack.truncations` counts them.
- Skills are injected as 1–3 paths from `skills/catalog.json`, not by loading every SKILL.md into the pack.

## Escape

Set `HARNESS_CONTEXT_POINTER_ONLY=0` for legacy budgeted lesson-body packing (tests / debug only).
