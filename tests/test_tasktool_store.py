from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.domain.enums import DispatchStatus, ModelTier, ResourceClass, Role
from harness.domain.models import Run, Task
from harness.store.db import connect, init_db
from harness.store.repository import DispatchConflictError, Store
from harness.tasktool.models import DispatchEnvelope, TaskStage

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="run-1", project="p", goal="g", status="running"))
    store.upsert_task(
        Task(id="task-1", run_id="run-1", title="Task", spec_path="spec.md", status="ready")
    )
    return store


def _dispatch(
    dispatch_id: str = "dispatch-1",
    *,
    created_at: datetime = NOW,
    run_id: str = "run-1",
    task_id: str = "task-1",
) -> DispatchEnvelope:
    timestamp = created_at.isoformat()
    return DispatchEnvelope(
        run_id=run_id,
        dispatch_id=dispatch_id,
        task_id=task_id,
        role=Role.WORKER,
        stage=TaskStage.IMPLEMENT,
        status=DispatchStatus.PENDING,
        prompt_path=f"prompts/{dispatch_id}.md",
        result_path=f"results/{dispatch_id}.json",
        model="composer",
        model_tier=ModelTier.STANDARD,
        repo="/repo",
        worktree=f"/worktrees/{dispatch_id}",
        files_owned=("harness/store/repository.py", "tests/test_tasktool_store.py"),
        read_only_context=("harness/tasktool/models.py",),
        frozen_contracts=("DispatchEnvelope",),
        acceptance=("targeted tests pass",),
        resource_class=ResourceClass.STANDARD,
        source_document_paths=("spec.md",),
        dependencies=("plan-1",),
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_fresh_database_crud_and_json_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dispatch = _dispatch()
    store.create_dispatch(dispatch)

    assert store.get_dispatch(dispatch.dispatch_id) == dispatch
    assert store.list_dispatches("run-1") == [dispatch]
    assert store.list_dispatches(status=DispatchStatus.PENDING) == [dispatch]
    event_types = [event.type for event in store.list_events("run-1")]
    assert event_types.count("dispatch_created") == 1


def test_upsert_is_replay_safe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dispatch = _dispatch()
    store.upsert_dispatch(dispatch)
    store.upsert_dispatch(dispatch)

    assert store.list_dispatches("run-1") == [dispatch]
    assert [event.type for event in store.list_events("run-1")].count("dispatch_created") == 1


def test_migrates_minimal_legacy_database_without_data_loss(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL,
            status TEXT NOT NULL, base_branch TEXT NOT NULL DEFAULT 'main',
            budget_credits REAL, spent_credits REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE tasks (
            id TEXT NOT NULL, run_id TEXT NOT NULL, title TEXT NOT NULL,
            spec_path TEXT NOT NULL, status TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]',
            provides TEXT NOT NULL DEFAULT '', complexity TEXT NOT NULL DEFAULT 'normal',
            attempts INTEGER NOT NULL DEFAULT 0, branch TEXT NOT NULL DEFAULT '',
            worktree_path TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, id)
        );
        INSERT INTO runs VALUES (
            'run-1','p','g','running','main',NULL,0,
            '2026-07-20T12:00:00+00:00','2026-07-20T12:00:00+00:00'
        );
        INSERT INTO tasks VALUES (
            'task-1','run-1','Task','spec.md','ready','[]','','normal',0,'','','',
            '2026-07-20T12:00:00+00:00','2026-07-20T12:00:00+00:00'
        );
        """
    )
    legacy.close()

    connection = connect(db_path)
    init_db(connection)
    connection.close()
    store = Store(db_path)
    store.create_dispatch(_dispatch())

    assert store.get_run("run-1") is not None
    assert store.get_task("run-1", "task-1") is not None
    assert store.get_dispatch("dispatch-1") is not None
    store.close()


def test_claim_limit_is_fifo(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(3):
        store.create_dispatch(
            _dispatch(
                f"dispatch-{index}",
                created_at=NOW + timedelta(seconds=index),
            )
        )

    claimed = store.claim_pending_dispatches(
        agent_id="agent-1",
        limit=2,
        lease_duration=timedelta(minutes=1),
        now=NOW + timedelta(minutes=1),
    )

    assert [dispatch.dispatch_id for dispatch in claimed] == ["dispatch-0", "dispatch-1"]
    assert all(dispatch.status is DispatchStatus.CLAIMED for dispatch in claimed)
    assert store.get_dispatch("dispatch-2").status is DispatchStatus.PENDING  # type: ignore[union-attr]


def test_claim_can_be_atomically_scoped_to_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_run(Run(id="run-2", project="p", goal="g2", status="running"))
    store.upsert_task(
        Task(id="task-2", run_id="run-2", title="Task 2", spec_path="spec.md", status="ready")
    )
    store.create_dispatch(_dispatch("other-run", run_id="run-2", task_id="task-2"))
    store.create_dispatch(
        _dispatch("target-run", created_at=NOW + timedelta(seconds=1))
    )

    claimed = store.claim_pending_dispatches(
        agent_id="agent-1",
        limit=1,
        run_id="run-1",
        now=NOW + timedelta(minutes=1),
    )

    assert [dispatch.dispatch_id for dispatch in claimed] == ["target-run"]
    assert store.get_dispatch("other-run").status is DispatchStatus.PENDING  # type: ignore[union-attr]


def test_stale_lease_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_dispatch(_dispatch())
    store.claim_pending_dispatches(
        agent_id="agent-1",
        limit=1,
        lease_duration=timedelta(seconds=30),
        now=NOW,
    )
    store.mark_dispatch_running("dispatch-1", agent_id="agent-1", now=NOW)

    assert store.recover_stale_dispatches(now=NOW + timedelta(seconds=29)) == []
    recovered = store.recover_stale_dispatches(now=NOW + timedelta(seconds=30))
    assert [dispatch.dispatch_id for dispatch in recovered] == ["dispatch-1"]
    assert recovered[0].status is DispatchStatus.PENDING
    assert [event.type for event in store.list_events("run-1")].count(
        "dispatch_recovered"
    ) == 1


def test_completion_is_idempotent_and_conflicts_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_dispatch(_dispatch())
    store.claim_pending_dispatches(agent_id="agent-1", limit=1, now=NOW)
    store.mark_dispatch_running("dispatch-1", agent_id="agent-1", now=NOW)

    first = store.complete_dispatch(
        "dispatch-1",
        agent_id="agent-1",
        result_path="results/final.json",
        now=NOW,
    )
    repeated = store.complete_dispatch(
        "dispatch-1",
        agent_id="agent-1",
        result_path="results/final.json",
        now=NOW + timedelta(seconds=1),
    )

    assert first == repeated
    assert first.status is DispatchStatus.SUCCEEDED
    assert [event.type for event in store.list_events("run-1")].count(
        "dispatch_completed"
    ) == 1
    with pytest.raises(DispatchConflictError):
        store.complete_dispatch(
            "dispatch-1",
            agent_id="agent-2",
            result_path="results/other.json",
            now=NOW + timedelta(seconds=2),
        )


def test_dispatch_application_receipts_are_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_dispatch(_dispatch())

    first = store.begin_dispatch_application(
        dispatch_id="dispatch-1",
        run_id="run-1",
        task_id="task-1",
        stage="implement",
        now=NOW,
    )
    again = store.begin_dispatch_application(
        dispatch_id="dispatch-1",
        run_id="run-1",
        task_id="task-1",
        stage="implement",
        now=NOW + timedelta(seconds=1),
    )
    assert first.status == "applying"
    assert again.status == "applying"
    assert store.is_dispatch_applied("dispatch-1") is False

    done = store.complete_dispatch_application("dispatch-1", now=NOW + timedelta(seconds=2))
    repeated = store.complete_dispatch_application(
        "dispatch-1", now=NOW + timedelta(seconds=3)
    )
    assert done.status == "applied"
    assert repeated.status == "applied"
    assert store.is_dispatch_applied("dispatch-1") is True
    assert store.list_dispatch_applications("run-1", status="applied") == [done]


def test_create_or_assert_rejects_mutable_dispatch_rewrite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dispatch = _dispatch()
    store.create_or_assert_dispatch(dispatch)
    twin = store.create_or_assert_dispatch(dispatch)
    assert twin == dispatch

    with pytest.raises(DispatchConflictError, match="immutable"):
        store.create_or_assert_dispatch(
            replace(dispatch, status=DispatchStatus.RUNNING)
        )
    with pytest.raises(DispatchConflictError, match="immutable"):
        store.upsert_dispatch(replace(dispatch, files_owned=("evil.py",)))
    with pytest.raises(DispatchConflictError, match="immutable"):
        store.upsert_dispatch(replace(dispatch, model="other-model"))


def test_renewed_lease_survives_a_long_running_job(tmp_path: Path) -> None:
    """The pull loop is the heartbeat: renewal must outrun stale recovery.

    Regression: a 5-minute hard-coded lease with no renewal let
    ``recover_stale_dispatches`` reset live work to ``pending``, so the same task
    (and the same ``files_owned``) went out to a second agent.
    """
    store = _store(tmp_path)
    store.create_dispatch(_dispatch())
    store.claim_pending_dispatches(agent_id="root-chat", limit=1, run_id="run-1", now=NOW)
    store.mark_dispatch_running("dispatch-1", agent_id="root-chat")

    # 90 minutes in, the job is still running but the dispatcher kept polling.
    late = NOW + timedelta(minutes=90)
    store.renew_leases(agent_id="root-chat", run_id="run-1", now=late)
    recovered = store.recover_stale_dispatches(run_id="run-1", now=late)

    assert recovered == []
    assert store.get_dispatch("dispatch-1").status is DispatchStatus.RUNNING


def test_lease_is_reclaimed_once_the_dispatcher_stops_polling(tmp_path: Path) -> None:
    """Crash recovery still works — renewal is the only thing keeping a lease."""
    store = _store(tmp_path)
    store.create_dispatch(_dispatch())
    store.claim_pending_dispatches(agent_id="root-chat", limit=1, run_id="run-1", now=NOW)

    dead = NOW + timedelta(minutes=90)
    recovered = store.recover_stale_dispatches(run_id="run-1", now=dead)

    assert [d.dispatch_id for d in recovered] == ["dispatch-1"]
    assert store.get_dispatch("dispatch-1").status is DispatchStatus.PENDING


def test_recovery_does_not_reach_into_another_run(tmp_path: Path) -> None:
    """One run's pull loop must not reset leases held for a different run."""
    store = _store(tmp_path)
    store.create_run(Run(id="run-2", project="p", goal="g", status="running"))
    store.upsert_task(
        Task(id="task-2", run_id="run-2", title="T", spec_path="s.md", status="ready")
    )
    store.create_dispatch(_dispatch())
    store.create_dispatch(_dispatch("dispatch-2", run_id="run-2", task_id="task-2"))
    store.claim_pending_dispatches(agent_id="chat-a", limit=1, run_id="run-1", now=NOW)
    store.claim_pending_dispatches(agent_id="chat-b", limit=1, run_id="run-2", now=NOW)

    late = NOW + timedelta(minutes=90)
    recovered = store.recover_stale_dispatches(run_id="run-1", now=late)

    assert [d.dispatch_id for d in recovered] == ["dispatch-1"]
    assert store.get_dispatch("dispatch-2").status is DispatchStatus.CLAIMED


def test_lease_minutes_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DISPATCH_LEASE_MINUTES", "5")
    store = _store(tmp_path)
    store.create_dispatch(_dispatch())
    store.claim_pending_dispatches(agent_id="root-chat", limit=1, run_id="run-1", now=NOW)

    assert store.recover_stale_dispatches(run_id="run-1", now=NOW + timedelta(minutes=4)) == []
    stale = store.recover_stale_dispatches(run_id="run-1", now=NOW + timedelta(minutes=6))
    assert [d.dispatch_id for d in stale] == ["dispatch-1"]


def test_targeted_claim_takes_only_the_named_dispatch(tmp_path: Path) -> None:
    """Admission decides per candidate, so the claim must be for that candidate.

    Regression: the claim was untargeted FIFO, so a concurrent claimer could take
    the admitted row and this call would claim a different one — potentially a
    HEAVY job under a decision made for a STANDARD one.
    """
    store = _store(tmp_path)
    store.create_dispatch(_dispatch("dispatch-1", created_at=NOW))
    store.create_dispatch(
        _dispatch("dispatch-2", created_at=NOW + timedelta(seconds=1))
    )

    claimed = store.claim_pending_dispatches(
        agent_id="root-chat", limit=1, run_id="run-1", dispatch_id="dispatch-2"
    )

    assert [d.dispatch_id for d in claimed] == ["dispatch-2"]
    # The older one is untouched, not swallowed by FIFO.
    assert store.get_dispatch("dispatch-1").status is DispatchStatus.PENDING


def test_targeted_claim_returns_empty_when_already_taken(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_dispatch(_dispatch())
    store.claim_pending_dispatches(agent_id="other-agent", limit=1, run_id="run-1")

    claimed = store.claim_pending_dispatches(
        agent_id="root-chat", limit=1, run_id="run-1", dispatch_id="dispatch-1"
    )

    assert claimed == []
    assert store.get_dispatch("dispatch-1").status is DispatchStatus.CLAIMED
