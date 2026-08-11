"""Future-only stub: thin cgroup / OS governor for harness runs.

Phase 4 techdoc P4.3 — **not implemented**. Do not block cutover on this.

When (and only when) dogfood shows runaway CPU/RAM that Python pre-call
budgets and resource admission cannot contain, consider:

1. Measure first: `harness doctor` + host `MemAvailable` / load already feed
   TaskTool admission (`harness/tasktool/resources.py`).
2. Optional per-run cgroup v2 slice with memory/CPU max — metric-driven, not
   default-on for solo.
3. Keep control plane in Python; cgroup is an OS fuse, not a second orchestrator.

This file exists so ROADMAP / cutover docs can link a concrete path without
shipping half-baked isolation.
"""

from __future__ import annotations

STUB = True
STATUS = "future-only"
REASON = (
    "No measured need on solo ZY dogfood; Python pre-call + resource admission "
    "are the Phase 2/3 governors. Revisit after multi-tenant C scale."
)


def cgroup_governor_available() -> bool:
    """Always False in Phase 4 — stub only."""
    return False
