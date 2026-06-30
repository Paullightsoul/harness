from __future__ import annotations

from pathlib import Path

import pytest

from harness.domain.enums import TaskStatus
from harness.domain.models import Run, Task
from harness.domain.state_machine import InvalidTransition
from harness.store.repository import Store


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def test_run_and_task_roundtrip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_run(Run(id="run-1", project="p", goal="g", status="planning"))
    s.upsert_task(Task(id="001", run_id="run-1", title="t", spec_path="x", status="pending"))
    task = s.get_task("run-1", "001")
    assert task is not None
    assert task.title == "t"


def test_transition_validated(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_run(Run(id="run-1", project="p", goal="g", status="planning"))
    s.upsert_task(Task(id="001", run_id="run-1", title="t", spec_path="x", status="pending"))
    task = s.get_task("run-1", "001")
    assert task is not None
    s.transition_task(task, TaskStatus.READY)
    assert s.get_task("run-1", "001").status == "ready"  # type: ignore[union-attr]
    with pytest.raises(InvalidTransition):
        s.transition_task(s.get_task("run-1", "001"), TaskStatus.DONE)  # type: ignore[arg-type]


def test_events_and_spend(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_run(Run(id="run-1", project="p", goal="g", status="planning"))
    spent = s.add_spend("run-1", 1.5)
    assert spent == 1.5
    events = s.list_events("run-1")
    assert any(e.type == "run_created" for e in events)
    assert any(e.type == "budget_spent" for e in events)
