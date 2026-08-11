"""Retention: what prune is allowed to touch, and what it must never touch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness.domain.enums import (
    DispatchStatus,
    EventType,
    ModelTier,
    ResourceClass,
    Role,
    TaskStatus,
)
from harness.domain.models import Run, Task
from harness.store.repository import Store
from harness.store.retention import apply_prune, plan_prune, render_plan_markdown
from harness.tasktool.models import DispatchEnvelope, TaskStage


def _seed_run(
    store: Store,
    repo: Path,
    run_id: str,
    *,
    status: str,
    created_at: str,
    project: str = "demo",
) -> None:
    store.create_run(
        Run(
            id=run_id,
            project=project,
            goal="g",
            status=status,
            created_at=created_at,
        )
    )
    store.upsert_task(
        Task(
            id="001",
            run_id=run_id,
            title="t",
            spec_path="s.md",
            status=TaskStatus.DONE.value,
        )
    )
    stamp = datetime.now(tz=UTC).isoformat()
    store.create_dispatch(
        DispatchEnvelope(
            run_id=run_id,
            dispatch_id=f"{run_id}-d1",
            task_id="001",
            role=Role.WORKER,
            stage=TaskStage.IMPLEMENT,
            status=DispatchStatus.SUCCEEDED,
            prompt_path="p.md",
            result_path="r.json",
            model="m",
            model_tier=ModelTier.STANDARD,
            repo=str(repo),
            worktree=str(repo),
            files_owned=(),
            read_only_context=(),
            frozen_contracts=(),
            acceptance=(),
            resource_class=ResourceClass.STANDARD,
            source_document_paths=(),
            dependencies=(),
            created_at=stamp,
            updated_at=stamp,
        )
    )
    store.add_event(run_id, EventType.GATE_RESULT, task_id="001", detail={"passed": True})
    spool = repo / ".harness" / "runs" / run_id
    spool.mkdir(parents=True, exist_ok=True)
    (spool / "acceptance.json").write_text('{"items": []}\n', encoding="utf-8")


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[Store, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(tmp_path / "state.db")
    for index in range(5):
        _seed_run(
            store,
            repo,
            f"run-{index}",
            status="done",
            created_at=f"2026-08-0{index + 1}T10:00:00+00:00",
        )
    return store, repo


def test_plan_keeps_the_newest_and_lists_the_rest(seeded: tuple[Store, Path]) -> None:
    store, _ = seeded

    plan = plan_prune(store, harness_root=Path("/nonexistent"), keep=2)

    assert plan.kept_run_ids == ["run-4", "run-3"]
    assert [c.run_id for c in plan.candidates] == ["run-2", "run-1", "run-0"]
    assert plan.total_rows > 0


def test_active_runs_are_never_candidates(tmp_path: Path) -> None:
    """An unfinished run is skipped even when it is the oldest thing there."""
    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(tmp_path / "state.db")
    _seed_run(store, repo, "old-active", status="running", created_at="2026-01-01T00:00:00+00:00")
    _seed_run(store, repo, "new-done", status="done", created_at="2026-08-01T00:00:00+00:00")

    plan = plan_prune(store, harness_root=tmp_path, keep=0)

    assert [c.run_id for c in plan.candidates] == ["new-done"]
    assert plan.skipped_active == ["old-active"]


def test_keep_larger_than_history_removes_nothing(seeded: tuple[Store, Path]) -> None:
    store, _ = seeded

    plan = plan_prune(store, harness_root=Path("/nonexistent"), keep=99)

    assert plan.candidates == []
    assert "Nothing to prune" in render_plan_markdown(plan)


def test_project_filter(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(tmp_path / "state.db")
    _seed_run(store, repo, "a1", status="done", created_at="2026-08-01T00:00:00+00:00", project="a")
    _seed_run(store, repo, "b1", status="done", created_at="2026-08-02T00:00:00+00:00", project="b")

    plan = plan_prune(store, harness_root=tmp_path, keep=0, project="a")

    assert [c.run_id for c in plan.candidates] == ["a1"]


def test_apply_removes_rows_from_every_table(seeded: tuple[Store, Path]) -> None:
    """Regression guard: events/agent_events/attempts have no FK to runs.

    Deleting only the ``runs`` row would leave the largest tables orphaned.
    """
    store, _ = seeded
    plan = plan_prune(store, harness_root=Path("/nonexistent"), keep=2)

    result = apply_prune(store, plan)

    assert result["runs_removed"] == 3
    assert store.get_run("run-0") is None
    assert store.get_run("run-4") is not None
    for run_id in ("run-0", "run-1", "run-2"):
        assert store.count_rows_for_run(run_id) == {
            "tasks": 0,
            "dispatches": 0,
            "events": 0,
            "agent_events": 0,
            "attempts": 0,
            "reviews": 0,
        }
    # Survivors are untouched.
    assert store.count_rows_for_run("run-4")["events"] > 0


def test_apply_removes_spools(seeded: tuple[Store, Path]) -> None:
    store, repo = seeded
    plan = plan_prune(store, harness_root=repo, keep=4)
    assert plan.candidates and plan.candidates[0].spool_bytes > 0

    result = apply_prune(store, plan)

    assert not (repo / ".harness" / "runs" / "run-0").exists()
    assert (repo / ".harness" / "runs" / "run-4").exists()
    assert result["spools_removed"]


def test_keep_spools_shrinks_db_but_leaves_evidence(seeded: tuple[Store, Path]) -> None:
    store, repo = seeded
    plan = plan_prune(store, harness_root=repo, keep=4)

    apply_prune(store, plan, drop_spools=False)

    assert store.get_run("run-0") is None
    assert (repo / ".harness" / "runs" / "run-0" / "acceptance.json").is_file()


def test_negative_keep_is_rejected(seeded: tuple[Store, Path]) -> None:
    store, _ = seeded
    with pytest.raises(ValueError, match="keep must be non-negative"):
        plan_prune(store, harness_root=Path("/nonexistent"), keep=-1)


def test_markdown_preview_warns_about_evidence(seeded: tuple[Store, Path]) -> None:
    store, repo = seeded
    plan = plan_prune(store, harness_root=repo, keep=1)

    text = render_plan_markdown(plan)

    assert "destroys its evidence ledger" in text
    assert "harness prune --apply" in text
