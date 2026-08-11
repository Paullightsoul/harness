from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harness.domain.enums import DispatchStatus, ModelTier, ResourceClass, Role
from harness.tasktool.models import (
    ContractError,
    DispatchEnvelope,
    PlanArtifact,
    PlanTask,
    TaskStage,
    paths_overlap,
)

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC).isoformat()


def _task(task_id: str, dependencies: tuple[str, ...] = ()) -> PlanTask:
    return PlanTask(
        task_id=task_id,
        role=Role.WORKER,
        stage=TaskStage.IMPLEMENT,
        prompt_path=f"/spool/prompts/{task_id}.md",
        result_path=f"/spool/results/{task_id}.json",
        model="auto",
        model_tier=ModelTier.STANDARD,
        repo="/repo",
        worktree=f"/worktrees/{task_id}",
        files_owned=(f"src/{task_id}.py",),
        read_only_context=("src/contracts.py",),
        frozen_contracts=("API response remains stable",),
        acceptance=("targeted tests pass",),
        resource_class=ResourceClass.STANDARD,
        source_document_paths=("spec/requirements.md",),
        dependencies=dependencies,
    )


def _plan(tasks: tuple[PlanTask, ...]) -> PlanArtifact:
    return PlanArtifact(
        run_id="run-1",
        goal="Ship pull dispatch",
        project="harness",
        repo="/repo",
        base_branch="main",
        spec_sources=("spec/requirements.md",),
        tasks=tasks,
        created_at=NOW,
        updated_at=NOW,
    )


def test_plan_artifact_json_roundtrip_and_unknown_fields() -> None:
    artifact = _plan((_task("a"), _task("b", ("a",))))
    payload = artifact.to_dict()
    payload["future_field"] = {"ignored": True}

    restored = PlanArtifact.from_dict(payload)

    assert restored == artifact
    assert PlanArtifact.from_json(restored.to_json()) == artifact
    assert restored.tasks[1].dependencies == ("a",)


def test_plan_rejects_duplicate_and_missing_task_ids() -> None:
    with pytest.raises(ContractError, match="duplicate task IDs"):
        _plan((_task("a"), _task("a")))

    with pytest.raises(ContractError, match="missing dependencies"):
        _plan((_task("a", ("missing",)),))


def test_plan_rejects_dependency_cycle() -> None:
    with pytest.raises(ContractError, match="dependency cycle"):
        _plan((_task("a", ("b",)), _task("b", ("a",))))


def test_plan_rejects_absolute_owned_paths() -> None:
    with pytest.raises(ContractError, match="relative"):
        _task("a").__class__(
            task_id="a",
            role=Role.WORKER,
            stage=TaskStage.IMPLEMENT,
            prompt_path="/spool/prompts/a.md",
            result_path="/spool/results/a.json",
            model="auto",
            model_tier=ModelTier.STANDARD,
            repo="/repo",
            worktree="/worktrees/a",
            files_owned=("/abs.py",),
            read_only_context=(),
            frozen_contracts=(),
            acceptance=("ok",),
            resource_class=ResourceClass.STANDARD,
            source_document_paths=("spec.md",),
        )


def test_dispatch_envelope_json_roundtrip() -> None:
    task = _task("a")
    envelope = DispatchEnvelope(
        run_id="run-1",
        dispatch_id="dispatch-1",
        task_id=task.task_id,
        role=task.role,
        stage=task.stage,
        status=DispatchStatus.PENDING,
        prompt_path=task.prompt_path,
        result_path=task.result_path,
        model=task.model,
        model_tier=task.model_tier,
        repo=task.repo,
        worktree=task.worktree,
        files_owned=task.files_owned,
        read_only_context=task.read_only_context,
        frozen_contracts=task.frozen_contracts,
        acceptance=task.acceptance,
        resource_class=task.resource_class,
        source_document_paths=task.source_document_paths,
        dependencies=task.dependencies,
        created_at=NOW,
        updated_at=NOW,
    )

    restored = DispatchEnvelope.from_json(envelope.to_json())

    assert restored == envelope
    assert restored.prompt_path.endswith(".md")
    assert "prompt" not in restored.to_dict()


def test_paths_overlap_detects_intersecting_globs() -> None:
    assert paths_overlap("src/a*.py", "src/*b.py") is True
    assert paths_overlap("src/foo/**", "src/foo/bar.py") is True
    assert paths_overlap("src/a*.py", "src/b*.py") is True  # conservative
    assert paths_overlap("src/foo.py", "src/bar.py") is False
    assert paths_overlap("src/**", "tests/**") is False
    assert paths_overlap("src/pkg/**", "src/other/**") is False
