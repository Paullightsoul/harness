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


def _seed_runs(store: Store) -> None:
    for index, (rid, project, created) in enumerate(
        [
            ("run-a", "alpha", "2026-08-01T10:00:00+00:00"),
            ("run-b", "beta", "2026-08-02T10:00:00+00:00"),
            ("run-c", "alpha", "2026-08-03T10:00:00+00:00"),
        ]
    ):
        store.create_run(
            Run(
                id=rid,
                project=project,
                goal=f"g{index}",
                status="done",
                created_at=created,
                spent_credits=float(index),
            )
        )


def test_list_run_ids_orders_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_runs(store)

    assert store.list_run_ids() == ["run-c", "run-b", "run-a"]
    assert store.list_run_ids(limit=2) == ["run-c", "run-b"]
    assert store.list_run_ids(oldest_first=True) == ["run-a", "run-b", "run-c"]


def test_list_run_ids_filters_by_project_and_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_runs(store)

    assert store.list_run_ids(project="alpha") == ["run-c", "run-a"]
    assert store.list_run_ids(created_since="2026-08-02T00:00:00+00:00") == [
        "run-c",
        "run-b",
    ]


def test_latest_run_id_scopes_to_project(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_runs(store)

    assert store.latest_run_id() == "run-c"
    assert store.latest_run_id(project="beta") == "run-b"
    assert store.latest_run_id(project="nope") is None


def test_latest_run_id_on_empty_store(tmp_path: Path) -> None:
    assert _store(tmp_path).latest_run_id() is None


def test_list_runs_returns_models_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_runs(store)

    runs = store.list_runs(limit=2)

    assert [r.id for r in runs] == ["run-c", "run-b"]
    assert runs[0].spent_credits == 2.0
    assert runs[0].project == "alpha"


def test_event_type_and_detail_readers(tmp_path: Path) -> None:
    from harness.domain.enums import EventType  # noqa: PLC0415

    store = _store(tmp_path)
    _seed_runs(store)
    store.add_event("run-a", EventType.GATE_RESULT, detail={"passed": True})
    store.add_event("run-a", EventType.GATE_RESULT, detail={"passed": False})
    store.add_event("run-a", EventType.RUN_CREATED, detail={"goal": "x"})

    types = store.list_event_types("run-a")
    assert types.count(EventType.GATE_RESULT.value) == 2

    details = store.list_event_details("run-a", EventType.GATE_RESULT.value)
    assert [d["passed"] for d in details] == [True, False]
    assert store.list_event_details("run-a", "no-such-type") == []


def test_like_counters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_runs(store)

    # Nothing recorded yet — counters must be 0, not raise.
    assert store.count_agent_events_matching("anything") == 0
    assert store.count_dispatches_with_skill("anything") == 0
