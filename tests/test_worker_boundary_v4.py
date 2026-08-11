"""Worker boundary / cosplay detection + enqueue duplicate guard."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import (
    DispatchStatus,
    EventType,
    ModelTier,
    ResourceClass,
    Role,
    RunStatus,
    TaskStatus,
)
from harness.domain.models import Run, Task
from harness.evidence.binding import nested_workers_hard_fail, nested_workers_required
from harness.store.repository import Store
from harness.tasktool.controller import TaskToolController
from harness.tasktool.models import DispatchEnvelope, TaskStage


def test_nested_workers_required_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_REQUIRE_NESTED_WORKERS", raising=False)
    assert nested_workers_required() is True
    monkeypatch.setenv("HARNESS_REQUIRE_NESTED_WORKERS", "0")
    assert nested_workers_required() is False


def test_nested_workers_hard_fail_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_NESTED_WORKERS_HARD_FAIL", raising=False)
    assert nested_workers_hard_fail() is True
    monkeypatch.setenv("HARNESS_NESTED_WORKERS_HARD_FAIL", "0")
    assert nested_workers_hard_fail() is False
    monkeypatch.setenv("HARNESS_NESTED_WORKERS_HARD_FAIL", "1")
    assert nested_workers_hard_fail() is True


class _NoopLifecycle:
    def __init__(self, store: Store) -> None:
        self.store = store


def _controller(tmp_path: Path) -> tuple[TaskToolController, Store, str, Path]:
    store = Store(tmp_path / "state.db")
    settings = Settings(root=tmp_path, use_run_roots=True, use_worktrees=False)
    run_id = "run-wb"
    store.create_run(
        Run(
            id=run_id,
            project="demo",
            goal="g",
            status=RunStatus.RUNNING.value,
            base_branch="main",
        )
    )
    run_root = tmp_path / ".harness" / "runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "controller.json").write_text(
        json.dumps(
            {
                "status": "running",
                "dispatch_kinds": {},
                "dispatch_agents": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctrl = TaskToolController(
        settings, store, tmp_path, lifecycle=_NoopLifecycle(store)
    )
    return ctrl, store, run_id, run_root


def _write_meta(run_root: Path, **updates: object) -> dict[str, object]:
    path = run_root / "controller.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(updates)
    path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return meta


def _add_succeeded_worker(
    store: Store,
    *,
    run_id: str,
    task_id: str,
    dispatch_id: str,
    run_root: Path,
    agent_id: str,
) -> None:
    now = datetime.now(tz=UTC).isoformat()
    (run_root / "prompts").mkdir(exist_ok=True)
    (run_root / "results").mkdir(exist_ok=True)
    prompt = run_root / "prompts" / f"{dispatch_id}.md"
    result = run_root / "results" / f"{dispatch_id}.json"
    prompt.write_text("p\n", encoding="utf-8")
    result.write_text(
        json.dumps(
            {
                "ok": True,
                "final_text": "done",
                "worker_id": agent_id,
                "agent_kind": "worker",
                "agent_id": agent_id,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store.create_dispatch(
        DispatchEnvelope(
            run_id=run_id,
            dispatch_id=dispatch_id,
            task_id=task_id,
            role=Role.WORKER,
            stage=TaskStage.IMPLEMENT,
            status=DispatchStatus.SUCCEEDED,
            prompt_path=str(prompt),
            result_path=str(result),
            model="cursor-grok-4.5-high",
            model_tier=ModelTier.STANDARD,
            repo=str(run_root),
            worktree=str(run_root),
            files_owned=(),
            read_only_context=(),
            frozen_contracts=("TaskTool protocol 3.0",),
            acceptance=("ok",),
            resource_class=ResourceClass.STANDARD,
            source_document_paths=(),
            dependencies=(),
            created_at=now,
            updated_at=now,
        )
    )


def _seed_cosplay_medium_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TaskToolController, Store, str]:
    """Two medium DONE tasks reported by the same agent (cosplay)."""
    monkeypatch.setenv("HARNESS_REQUIRE_NESTED_WORKERS", "1")
    ctrl, store, run_id, run_root = _controller(tmp_path)
    kinds: dict[str, str] = {}
    for tid in ("001", "002"):
        store.upsert_task(
            Task(
                id=tid,
                run_id=run_id,
                title=tid,
                spec_path="x",
                status=TaskStatus.DONE.value,
                complexity="medium",
            )
        )
        did = f"{run_id}-{tid}-worker-1"
        kinds[did] = "worker"
        _add_succeeded_worker(
            store,
            run_id=run_id,
            task_id=tid,
            dispatch_id=did,
            run_root=run_root,
            agent_id="orch-same",
        )
    _write_meta(run_root, dispatch_kinds=kinds)
    for tid in ("001", "002"):
        ctrl.run_meta.record_dispatch_agent(
            run_id, f"{run_id}-{tid}-worker-1", "orch-same", "worker"
        )
    return ctrl, store, run_id


def test_cosplay_snapshot_flags_same_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_REQUIRE_NESTED_WORKERS", "1")
    ctrl, store, run_id, run_root = _controller(tmp_path)
    kinds: dict[str, str] = {}
    for tid in ("001", "002"):
        store.upsert_task(
            Task(
                id=tid,
                run_id=run_id,
                title=tid,
                spec_path="x",
                status=TaskStatus.DONE.value,
                complexity="medium",
            )
        )
        did = f"{run_id}-{tid}-worker-1"
        kinds[did] = "worker"
        _add_succeeded_worker(
            store,
            run_id=run_id,
            task_id=tid,
            dispatch_id=did,
            run_root=run_root,
            agent_id="orch-same",
        )
    _write_meta(run_root, dispatch_kinds=kinds)
    for tid in ("001", "002"):
        ctrl.run_meta.record_dispatch_agent(
            run_id, f"{run_id}-{tid}-worker-1", "orch-same", "worker"
        )

    snap = ctrl._cosplay_snapshot(run_id)
    assert snap["cosplay_risk"] is True
    assert snap["reason"] == "same_agent_reported_all_workers"
    assert snap["hard_fail"] is True
    assert ctrl.status(run_id)["cosplay_risk"] is True


def test_cosplay_ok_with_distinct_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_REQUIRE_NESTED_WORKERS", "1")
    ctrl, store, run_id, run_root = _controller(tmp_path)
    kinds: dict[str, str] = {}
    for tid, agent in (("001", "worker-a"), ("002", "worker-b")):
        store.upsert_task(
            Task(
                id=tid,
                run_id=run_id,
                title=tid,
                spec_path="x",
                status=TaskStatus.DONE.value,
                complexity="medium",
            )
        )
        did = f"{run_id}-{tid}-worker-1"
        kinds[did] = "worker"
        _add_succeeded_worker(
            store,
            run_id=run_id,
            task_id=tid,
            dispatch_id=did,
            run_root=run_root,
            agent_id=agent,
        )
    _write_meta(run_root, dispatch_kinds=kinds)
    for tid, agent in (("001", "worker-a"), ("002", "worker-b")):
        ctrl.run_meta.record_dispatch_agent(run_id, f"{run_id}-{tid}-worker-1", agent, "worker")

    snap = ctrl._cosplay_snapshot(run_id)
    assert snap["cosplay_risk"] is False
    assert len(snap["distinct_worker_ids"]) == 2


def test_cosplay_hard_fail_blocks_done_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset HARD_FAIL env → default ON → DONE refused on cosplay."""
    monkeypatch.delenv("HARNESS_NESTED_WORKERS_HARD_FAIL", raising=False)
    ctrl, store, run_id = _seed_cosplay_medium_run(tmp_path, monkeypatch)
    assert nested_workers_hard_fail() is True
    ctrl._finish_run_if_complete(run_id)
    run = store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.PAUSED.value
    refused = [
        e
        for e in store.list_events(run_id)
        if e.type == EventType.DONE_REFUSED.value
        and (e.detail or {}).get("reason") == "cosplay_risk"
    ]
    assert refused, "expected DONE_REFUSED on cosplay hard-fail"


def test_cosplay_hard_fail_escape_allows_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HARNESS_NESTED_WORKERS_HARD_FAIL=0 → warn-only; run may reach DONE."""
    monkeypatch.setenv("HARNESS_NESTED_WORKERS_HARD_FAIL", "0")
    ctrl, store, run_id = _seed_cosplay_medium_run(tmp_path, monkeypatch)
    assert nested_workers_hard_fail() is False
    ctrl._finish_run_if_complete(run_id)
    run = store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.DONE.value


def test_enqueue_reuses_live_pending_worker(tmp_path: Path) -> None:
    """Second _enqueue while PENDING exists must not mint worker-2."""
    ctrl, store, run_id, run_root = _controller(tmp_path)
    now = datetime.now(tz=UTC).isoformat()
    store.upsert_task(
        Task(
            id="001",
            run_id=run_id,
            title="t",
            spec_path="x",
            status=TaskStatus.READY.value,
            complexity="small",
        )
    )
    did = f"{run_id}-001-worker-1"
    _write_meta(run_root, dispatch_kinds={did: "worker"})
    (run_root / "prompts").mkdir(exist_ok=True)
    (run_root / "results").mkdir(exist_ok=True)
    prompt = run_root / "prompts" / f"{did}.md"
    result = run_root / "results" / f"{did}.json"
    prompt.write_text("p\n", encoding="utf-8")
    result.write_text("{}\n", encoding="utf-8")
    store.create_dispatch(
        DispatchEnvelope(
            run_id=run_id,
            dispatch_id=did,
            task_id="001",
            role=Role.WORKER,
            stage=TaskStage.IMPLEMENT,
            status=DispatchStatus.PENDING,
            prompt_path=str(prompt),
            result_path=str(result),
            model="m",
            model_tier=ModelTier.STANDARD,
            repo=str(tmp_path),
            worktree=str(tmp_path),
            files_owned=(),
            read_only_context=(),
            frozen_contracts=("TaskTool protocol 3.0",),
            acceptance=("ok",),
            resource_class=ResourceClass.STANDARD,
            source_document_paths=(),
            dependencies=(),
            created_at=now,
            updated_at=now,
        )
    )
    task = store.get_task(run_id, "001")
    assert task is not None
    again = ctrl._enqueue(task, TaskStage.IMPLEMENT, "worker")
    assert again.dispatch_id == did
    assert again.status is DispatchStatus.PENDING
    workers = [
        d
        for d in store.list_dispatches(run_id, task_id="001")
        if "worker" in d.dispatch_id
    ]
    assert len(workers) == 1
