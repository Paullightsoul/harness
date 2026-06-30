from __future__ import annotations

from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType, TaskStatus
from harness.domain.models import Run, Task
from harness.domain.state_machine import (
    RECOVERABLE_STATUSES,
    can_transition,
)
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def test_inflight_statuses_recover_to_ready() -> None:
    for status in RECOVERABLE_STATUSES:
        assert can_transition(status, TaskStatus.RECOVERING), status
    assert can_transition(TaskStatus.RECOVERING, TaskStatus.READY)


def test_recovering_is_only_reachable_from_inflight() -> None:
    non_recoverable = {
        TaskStatus.PENDING,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
        TaskStatus.ESCALATE,
        TaskStatus.DONE,
    }
    for status in non_recoverable:
        assert not can_transition(status, TaskStatus.RECOVERING), status


def _drive_to(store: Store, run_id: str, task_id: str, target: TaskStatus) -> None:
    chain = [TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.GATING, TaskStatus.REVIEW]
    for dst in chain:
        task = store.get_task(run_id, task_id)
        assert task is not None
        store.transition_task(task, dst)
        if dst == target:
            return


def test_engine_recovers_interrupted_task(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    _drive_to(store, "r1", "001", TaskStatus.RUNNING)
    assert store.get_task("r1", "001").status == TaskStatus.RUNNING  # type: ignore[union-attr]

    engine = Engine(Settings(root=tmp_path), store)
    engine._recover_interrupted("r1")

    recovered = store.get_task("r1", "001")
    assert recovered is not None
    assert recovered.status == TaskStatus.READY
    events = store.list_events("r1")
    assert any(e.type == EventType.RECOVERED for e in events)


def test_engine_leaves_non_inflight_tasks_untouched(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    done = store.get_task("r1", "001")
    assert done is not None
    store.transition_task(done, TaskStatus.READY)

    engine = Engine(Settings(root=tmp_path), store)
    engine._recover_interrupted("r1")

    still = store.get_task("r1", "001")
    assert still is not None
    assert still.status == TaskStatus.READY
    assert not any(e.type == EventType.RECOVERED for e in store.list_events("r1"))
