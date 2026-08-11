"""Speed-mode: skip review below threshold + DAG unlock post_gate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import TaskStatus
from harness.tasktool.controller import TaskToolController, _complexity_rank
from test_tasktool_controller import _controller, _report


def test_complexity_rank_order() -> None:
    assert _complexity_rank("trivial") < _complexity_rank("small")
    assert _complexity_rank("small") < _complexity_rank("medium")
    assert _complexity_rank("medium") < _complexity_rank("large")


@pytest.mark.asyncio
async def test_review_min_large_skips_medium_reviewer(tmp_path: Path) -> None:
    settings = Settings(
        root=tmp_path,
        review_min_complexity="large",
        ship_mode="manual",
        use_worktrees=False,
    )
    controller, store, _ = _controller(tmp_path, complexity="medium", settings=settings)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent", limit=1)["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE\n## Provides\n- x")
    await controller.advance(run_id)

    task = store.get_task(run_id, "001")
    assert task is not None
    assert task.status == TaskStatus.DONE.value
    kinds = [
        controller.run_meta.dispatch_kind(controller._read_meta(run_id), d.dispatch_id)
        for d in store.list_dispatches(run_id)
    ]
    assert "reviewer" not in kinds


@pytest.mark.asyncio
async def test_dag_unlock_post_gate_starts_dependent_during_review(
    tmp_path: Path,
) -> None:
    settings = Settings(
        root=tmp_path,
        review_min_complexity="small",  # keep reviewer for parent
        dag_unlock="post_gate",
        ship_mode="manual",
        use_worktrees=False,
    )
    controller, store, _ = _controller(
        tmp_path, tasks=2, chain=True, complexity="medium", settings=settings
    )
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent", limit=1)["dispatches"][0]
    assert worker["task_id"] == "001"
    _report(controller, tmp_path, worker, "HARNESS_DONE\n## Provides\n- parent")
    await controller.advance(run_id)

    parent = store.get_task(run_id, "001")
    child = store.get_task(run_id, "002")
    assert parent is not None and child is not None
    assert parent.status == TaskStatus.REVIEW.value
    assert child.status in {TaskStatus.READY.value, TaskStatus.RUNNING.value}

    claimed = controller.next(run_id, "agent", limit=2)["dispatches"]
    # Child worker may already be claimed by advance→enqueue_ready_workers.
    live = [
        d
        for d in store.list_dispatches(run_id, task_id="002")
        if d.role.value == "worker" or "worker" in d.dispatch_id
    ]
    assert live or any(d.get("task_id") == "002" for d in claimed)
