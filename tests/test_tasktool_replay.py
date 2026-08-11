"""Crash-safe dispatch application / replay probes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.domain.enums import EventType, RunStatus, TaskStatus
from harness.store.repository import APPLICATION_APPLIED, APPLICATION_APPLYING
from test_tasktool_controller import _controller, _report


@pytest.mark.asyncio
async def test_application_receipt_marks_applied_after_advance(tmp_path: Path) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(worker["dispatch_id"])
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)

    receipt = store.get_dispatch_application(dispatch_id)
    assert receipt is not None
    assert receipt.status == APPLICATION_APPLIED
    assert store.is_dispatch_applied(dispatch_id)


@pytest.mark.asyncio
async def test_crash_after_transition_before_receipt_is_replay_safe(
    tmp_path: Path,
) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(worker["dispatch_id"])
    _report(controller, tmp_path, worker, "HARNESS_DONE")

    # Simulate crash: worker finalized + gating done, successor enqueued, but
    # receipt never flipped to applied (only 'applying' or missing).
    await controller.advance(run_id)
    receipt = store.get_dispatch_application(dispatch_id)
    assert receipt is not None
    assert receipt.status == APPLICATION_APPLIED

    # Force a stale applying receipt and remove legacy consumed marker.
    store._conn.execute(
        "UPDATE dispatch_applications SET status=? WHERE dispatch_id=?",
        (APPLICATION_APPLYING, dispatch_id),
    )
    meta = controller._read_meta(run_id)
    meta["consumed"] = []
    controller._write_meta(run_id, meta)

    # Replay must not re-finalize or raise invalid transition.
    await controller.advance(run_id)
    await controller.advance(run_id)

    assert sum(call[0] == "finalize" for call in lifecycle.calls) == 1
    assert store.get_dispatch_application(dispatch_id).status == APPLICATION_APPLIED  # type: ignore[union-attr]
    reviewers = [
        item
        for item in store.list_dispatches(run_id)
        if "reviewer" in item.dispatch_id
    ]
    assert len(reviewers) == 1


@pytest.mark.asyncio
async def test_gate_contention_then_replay_uses_receipts(tmp_path: Path) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(dispatch["dispatch_id"])
    _report(controller, tmp_path, dispatch, "HARNESS_DONE")
    lock = controller.settings.root / ".harness" / "tasktool-gate.lock"
    lock.write_text(f"{os.getpid()}:other:task\n", encoding="utf-8")

    contended = await controller.advance(run_id)
    assert contended["tasks"][0]["status"] == TaskStatus.GATING.value
    assert store.get_dispatch_application(dispatch_id) is not None
    assert not store.is_dispatch_applied(dispatch_id)

    lock.unlink()
    replayed = await controller.advance(run_id)
    assert replayed["tasks"][0]["status"] == TaskStatus.REVIEW.value
    assert store.is_dispatch_applied(dispatch_id)
    assert sum(call[0] == "finalize" for call in lifecycle.calls) == 1


@pytest.mark.asyncio
async def test_reviewer_approve_crash_before_receipt_replays_safely(
    tmp_path: Path,
) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(reviewer["dispatch_id"])
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)

    assert store.get_task(run_id, "001").status == TaskStatus.MERGE_QUEUE.value  # type: ignore[union-attr]
    assert sum(call[0] == "review" for call in lifecycle.calls) == 1

    # Crash after apply_review / pause, before receipt completion.
    store._conn.execute(
        "UPDATE dispatch_applications SET status=? WHERE dispatch_id=?",
        (APPLICATION_APPLYING, dispatch_id),
    )
    meta = controller._read_meta(run_id)
    meta["consumed"] = [item for item in meta.get("consumed", []) if item != dispatch_id]
    controller._write_meta(run_id, meta)

    await controller.advance(run_id, approved_merge=False)
    # Still paused for merge; replay must not re-apply review.
    assert sum(call[0] == "review" for call in lifecycle.calls) == 1
    assert store.get_dispatch_application(dispatch_id).status == APPLICATION_APPLIED  # type: ignore[union-attr]
    assert store.get_task(run_id, "001").status == TaskStatus.MERGE_QUEUE.value  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reviewer_changes_crash_before_receipt_replays_safely(
    tmp_path: Path,
) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(reviewer["dispatch_id"])
    _report(controller, tmp_path, reviewer, "VERDICT: CHANGES\nfix the bug")
    await controller.advance(run_id)

    assert store.get_task(run_id, "001").status in {
        TaskStatus.READY.value,
        TaskStatus.RUNNING.value,
    }
    assert sum(call[0] == "review" for call in lifecycle.calls) == 1
    workers_after = [
        item
        for item in store.list_dispatches(run_id)
        if "worker" in item.dispatch_id
    ]
    worker_count = len(workers_after)

    store._conn.execute(
        "UPDATE dispatch_applications SET status=? WHERE dispatch_id=?",
        (APPLICATION_APPLYING, dispatch_id),
    )
    meta = controller._read_meta(run_id)
    meta["consumed"] = [item for item in meta.get("consumed", []) if item != dispatch_id]
    controller._write_meta(run_id, meta)

    # Force RUNNING so normal advance can complete the receipt without merge approval.
    store.set_run_status(run_id, "running")
    controller._set_meta_status(run_id, "running")
    await controller.advance(run_id)

    assert sum(call[0] == "review" for call in lifecycle.calls) == 1
    assert store.get_dispatch_application(dispatch_id).status == APPLICATION_APPLIED  # type: ignore[union-attr]
    workers_replay = [
        item
        for item in store.list_dispatches(run_id)
        if "worker" in item.dispatch_id
    ]
    assert len(workers_replay) == worker_count


@pytest.mark.asyncio
async def test_partial_worker_review_replay_creates_exactly_one_reviewer(
    tmp_path: Path,
) -> None:
    """Crash after gates→REVIEW before successor enqueue + receipt."""
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(worker["dispatch_id"])
    _report(controller, tmp_path, worker, "HARNESS_DONE")

    store.begin_dispatch_application(
        dispatch_id=dispatch_id, run_id=run_id, task_id="001", stage="implement"
    )
    await lifecycle.finalize_worker(
        run_id, "001", "HARNESS_DONE", files_owned=("src/task_1.py",)
    )
    await lifecycle.run_gates(run_id, "001", full=False)
    assert store.get_task(run_id, "001").status == TaskStatus.REVIEW.value  # type: ignore[union-attr]
    assert not any("reviewer" in item.dispatch_id for item in store.list_dispatches(run_id))

    replayed = await controller.advance(run_id)
    await controller.advance(run_id)

    reviewers = [
        item for item in store.list_dispatches(run_id) if "reviewer" in item.dispatch_id
    ]
    assert len(reviewers) == 1
    assert reviewers[0].dispatch_id == f"{run_id}-001-reviewer-1"
    assert store.is_dispatch_applied(dispatch_id)
    assert sum(call[0] == "finalize" for call in lifecycle.calls) == 1
    assert sum(call[0] == "gates" for call in lifecycle.calls) == 1
    checkpoint = controller._checkpoints.read(run_id, "001")
    assert checkpoint is not None
    assert checkpoint.stage == "gate"
    assert checkpoint.next_action == "reviewer"
    assert replayed["tasks"][0]["status"] == TaskStatus.REVIEW.value


@pytest.mark.asyncio
async def test_partial_worker_review_replay_creates_exactly_one_goal_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Large tasks recover the goal-judge successor, not a reviewer."""
    monkeypatch.setenv("HARNESS_DECOMPOSE", "0")
    controller, store, lifecycle = _controller(tmp_path, complexity="large")
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(worker["dispatch_id"])
    _report(controller, tmp_path, worker, "HARNESS_DONE")

    store.begin_dispatch_application(
        dispatch_id=dispatch_id, run_id=run_id, task_id="001", stage="implement"
    )
    await lifecycle.finalize_worker(
        run_id, "001", "HARNESS_DONE", files_owned=("src/task_1.py",)
    )
    await lifecycle.run_gates(run_id, "001", full=False)

    await controller.advance(run_id)
    await controller.advance(run_id)

    judges = [
        item for item in store.list_dispatches(run_id) if "goal-judge" in item.dispatch_id
    ]
    reviewers = [
        item for item in store.list_dispatches(run_id) if "reviewer" in item.dispatch_id
    ]
    assert len(judges) == 1
    assert judges[0].dispatch_id == f"{run_id}-001-goal-judge-1"
    assert reviewers == []
    assert store.is_dispatch_applied(dispatch_id)
    checkpoint = controller._checkpoints.read(run_id, "001")
    assert checkpoint is not None
    assert checkpoint.next_action == "goal-judge"


@pytest.mark.asyncio
async def test_partial_reviewer_approve_replay_pauses_without_sibling_claim(
    tmp_path: Path,
) -> None:
    """Crash after APPLY→MERGE_QUEUE before HUMAN_GATE_WAIT / receipt."""
    controller, store, lifecycle = _controller(tmp_path, tasks=2, chain=True)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent", limit=1)["dispatches"][0]
    assert worker["task_id"] == "001"
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)

    reviewer = controller.next(run_id, "agent", limit=1)["dispatches"][0]
    assert "reviewer" in str(reviewer["dispatch_id"])
    dispatch_id = str(reviewer["dispatch_id"])
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")

    store.begin_dispatch_application(
        dispatch_id=dispatch_id, run_id=run_id, task_id="001", stage="review"
    )
    lifecycle.apply_review(run_id, "001", "VERDICT: APPROVE")
    assert store.get_task(run_id, "001").status == TaskStatus.MERGE_QUEUE.value  # type: ignore[union-attr]
    assert store.get_run(run_id).status == RunStatus.RUNNING.value  # type: ignore[union-attr]
    assert not any(
        event.type == EventType.HUMAN_GATE_WAIT.value for event in store.list_events(run_id)
    )
    assert store.get_task(run_id, "002").status == TaskStatus.PENDING.value  # type: ignore[union-attr]

    await controller.advance(run_id)
    await controller.advance(run_id)

    assert store.get_run(run_id).status == RunStatus.PAUSED.value  # type: ignore[union-attr]
    assert store.get_task(run_id, "001").status == TaskStatus.MERGE_QUEUE.value  # type: ignore[union-attr]
    assert store.is_dispatch_applied(dispatch_id)
    assert sum(call[0] == "review" for call in lifecycle.calls) == 1
    gate_events = [
        event
        for event in store.list_events(run_id)
        if event.type == EventType.HUMAN_GATE_WAIT.value and event.task_id == "001"
    ]
    assert len(gate_events) == 1
    checkpoint = controller._checkpoints.read(run_id, "001")
    assert checkpoint is not None
    assert checkpoint.next_action == "await merge approval"
    # Dependent sibling must not be activated or claimed while merge gate waits.
    assert store.get_task(run_id, "002").status == TaskStatus.PENDING.value  # type: ignore[union-attr]
    assert store.list_dispatches(run_id, task_id="002") == []
    blocked = controller.next(run_id, "agent")
    assert blocked["ok"] is False
    assert blocked["dispatches"] == []


@pytest.mark.asyncio
async def test_partial_reviewer_changes_replay_does_not_duplicate_workers(
    tmp_path: Path,
) -> None:
    """Crash after CHANGES→READY before receipt; retry stays single."""
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(reviewer["dispatch_id"])
    _report(controller, tmp_path, reviewer, "VERDICT: CHANGES\nfix it")

    store.begin_dispatch_application(
        dispatch_id=dispatch_id, run_id=run_id, task_id="001", stage="review"
    )
    lifecycle.apply_review(run_id, "001", "VERDICT: CHANGES\nfix it")
    assert store.get_task(run_id, "001").status == TaskStatus.READY.value  # type: ignore[union-attr]

    await controller.advance(run_id)
    await controller.advance(run_id)

    assert store.is_dispatch_applied(dispatch_id)
    assert sum(call[0] == "review" for call in lifecycle.calls) == 1
    workers = [
        item for item in store.list_dispatches(run_id) if "worker" in item.dispatch_id
    ]
    assert len(workers) == 2  # original succeeded + exactly one retry
