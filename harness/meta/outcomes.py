"""Collect actual run metrics for AHE-lite verification."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harness.store.repository import Store

# Event types that count as stalls / governor pauses.
_STALL_EVENT_TYPES = frozenset(
    {
        "loop_stuck",
        "budget_predicate_hit",
        "human_gate_wait",
        "resource_throttled",
        "resource_denied",
    }
)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    status: str
    project: str
    created_at: str
    pipeline_pct: float | None
    evidence_pct: float | None
    tokens_est: int | None
    stall_count: int
    pass_at_1: float | None
    task_total: int
    task_done: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sum_tokens(store: Store, run_id: str) -> int | None:
    """Sum attempt tokens_in+tokens_out when present; None if never recorded."""
    total = 0
    seen = False
    for task in store.list_tasks(run_id):
        for attempt in store.list_attempts(run_id, task.id):
            tin = int(getattr(attempt, "tokens_in", 0) or 0)
            tout = int(getattr(attempt, "tokens_out", 0) or 0)
            if tin or tout:
                seen = True
            total += tin + tout
    return total if seen else None


def _stall_count(store: Store, run_id: str) -> int:
    return sum(
        1
        for event_type in store.list_event_types(run_id)
        if event_type.lower() in _STALL_EVENT_TYPES
    )


def collect_run_outcome(
    store: Store,
    run_id: str,
    *,
    harness_root: Path | None = None,
    repo_root: Path | None = None,
) -> RunOutcome | None:
    """Snapshot evidence% / tokens / stalls for one run."""
    from harness.evidence.acceptance import evidence_pct_for_run  # noqa: PLC0415
    from harness.tenant.run_layout import resolve_run_root  # noqa: PLC0415

    run = store.get_run(run_id)
    if run is None:
        return None
    tasks = store.list_tasks(run_id)
    done = sum(1 for t in tasks if str(t.status) == "done")
    pipeline = (done / len(tasks) * 100.0) if tasks else None

    evidence_pct: float | None = None
    search_roots: list[Path] = []
    if repo_root is not None:
        search_roots.append(repo_root)
    if harness_root is not None:
        search_roots.append(harness_root)
    for root in search_roots:
        try:
            paths = resolve_run_root(root, run_id)
            evidence_pct = evidence_pct_for_run(paths.root)
            if evidence_pct is not None:
                break
        except (OSError, ValueError, json.JSONDecodeError):
            continue

    return RunOutcome(
        run_id=run_id,
        status=str(run.status),
        project=str(run.project),
        created_at=str(getattr(run, "created_at", "") or ""),
        pipeline_pct=pipeline,
        evidence_pct=evidence_pct,
        tokens_est=_sum_tokens(store, run_id),
        stall_count=_stall_count(store, run_id),
        pass_at_1=store.pass_at_k(run_id, 1),
        task_total=len(tasks),
        task_done=done,
    )


def recent_run_ids(store: Store, *, limit: int = 8) -> list[str]:
    return store.list_run_ids(limit=limit)


def runs_after(store: Store, iso_at: str, *, limit: int = 20) -> list[str]:
    """Return run ids with created_at >= iso_at (string compare on ISO stamps)."""
    return store.list_run_ids(created_since=iso_at, limit=limit, oldest_first=True)
