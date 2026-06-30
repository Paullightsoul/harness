from __future__ import annotations

import pytest

from harness.domain.enums import TaskStatus
from harness.domain.models import Task
from harness.scheduler.dag import CycleError, TaskGraph


def _task(tid: str, deps: list[str]) -> Task:
    return Task(id=tid, run_id="r", title=tid, spec_path="", status="pending", depends_on=deps)


def test_topo_order_respects_dependencies() -> None:
    g = TaskGraph([_task("001", []), _task("002", ["001"]), _task("003", ["001", "002"])])
    order = g.topo_order()
    assert order.index("001") < order.index("002") < order.index("003")


def test_ready_returns_only_unblocked() -> None:
    g = TaskGraph([_task("001", []), _task("002", ["001"])])
    statuses = {"001": TaskStatus.PENDING, "002": TaskStatus.PENDING}
    assert g.ready(statuses) == ["001"]
    statuses["001"] = TaskStatus.DONE
    assert g.ready(statuses) == ["002"]


def test_cycle_detected() -> None:
    with pytest.raises(CycleError):
        TaskGraph([_task("001", ["002"]), _task("002", ["001"])])


def test_unknown_dependency_rejected() -> None:
    with pytest.raises(ValueError):
        TaskGraph([_task("001", ["999"])])
