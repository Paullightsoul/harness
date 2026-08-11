"""Safety probes for TaskTool hard ceilings, abort, path scope, and pauses."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness.domain.enums import (
    DispatchStatus,
    ModelTier,
    ResourceClass,
    Role,
    RunStatus,
    TaskStatus,
)
from harness.tasktool.models import (
    ContractError,
    PlanArtifact,
    PlanTask,
    TaskStage,
    normalize_owned_path,
)
from harness.tasktool.resources import ResourcePolicy
from test_tasktool_controller import _controller, _report, _Result


@pytest.mark.asyncio
async def test_next_rejects_limit_above_hard_ceiling(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])

    with pytest.raises(ValueError, match="hard TaskTool ceiling"):
        controller.next(run_id, "agent", limit=17)


@pytest.mark.asyncio
async def test_controller_clamps_resource_policy_ceiling(tmp_path: Path) -> None:
    policy = ResourcePolicy(max_tasktool_jobs=20, max_agent_slots=18)
    controller, _, _ = _controller(tmp_path, policy=policy)
    assert controller.resource_policy.max_tasktool_jobs == 16
    assert controller.resource_policy.max_agent_slots == 16


@pytest.mark.asyncio
async def test_abort_is_terminal_and_blocks_resume_next_advance(tmp_path: Path) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    controller.next(run_id, "agent")

    aborted = controller.abort(run_id, reason="stop")
    assert aborted["status"] == RunStatus.ABORTED.value
    assert store.get_run(run_id).status == RunStatus.ABORTED.value  # type: ignore[union-attr]
    task = store.get_task(run_id, "001")
    assert task is not None
    assert task.status == TaskStatus.BLOCKED.value

    resumed = await controller.resume(run_id)
    assert resumed["ok"] is False
    assert "terminal" in str(resumed.get("error", ""))

    claimed = controller.next(run_id, "agent")
    assert claimed["ok"] is False
    assert claimed["dispatches"] == []

    advanced = await controller.advance(run_id)
    assert advanced.get("idempotent") is True
    assert store.get_run(run_id).status == RunStatus.ABORTED.value  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_question_pause_blocks_next_and_unapproved_advance(tmp_path: Path) -> None:
    controller, store, _ = _controller(tmp_path, tasks=2)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent", limit=1)["dispatches"][0]
    question = (
        "```question\n"
        '{"need_input": true, "questions":['
        '{"id":"q1","question":"which?","options":["a","b"],"why":"need"}'
        "]}\n"
        "```\n"
    )
    _report(controller, tmp_path, worker, question)
    paused = await controller.advance(run_id)
    assert paused["status"] == RunStatus.PAUSED.value

    blocked_next = controller.next(run_id, "agent")
    assert blocked_next["ok"] is False
    assert blocked_next["dispatches"] == []

    blocked_advance = await controller.advance(run_id)
    assert blocked_advance.get("idempotent") is True

    questions = (
        controller.repo_root / ".harness" / "runs" / run_id / "pending" / "questions.json"
    )
    assert questions.is_file()
    assert store.get_run(run_id).status == RunStatus.PAUSED.value  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_frozen_plan_ignores_mutated_task_files(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    mutable = controller.repo_root / "tasks" / "task-001.md"
    mutable.write_text(
        mutable.read_text(encoding="utf-8").replace("src/task_1.py", "../../outside.py"),
        encoding="utf-8",
    )
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    assert dispatch["files_owned"] == ["src/task_1.py"]
    assert "../../outside.py" not in dispatch["files_owned"]
    frozen = Path(str(dispatch["source_document_paths"][0]))
    assert ".harness/runs" in str(frozen)
    assert "src/task_1.py" in frozen.read_text(encoding="utf-8")


def test_plan_artifact_rejects_absolute_and_dotdot_owned_paths() -> None:
    with pytest.raises(ContractError, match="relative"):
        normalize_owned_path("/etc/passwd")
    with pytest.raises(ContractError, match="\\.\\."):
        normalize_owned_path("../secret")
    with pytest.raises(ContractError):
        normalize_owned_path("")


def test_plan_artifact_rejects_cross_task_ownership_overlap() -> None:
    now = "2026-07-20T12:00:00+00:00"

    def task(task_id: str, owned: tuple[str, ...]) -> PlanTask:
        return PlanTask(
            task_id=task_id,
            role=Role.WORKER,
            stage=TaskStage.IMPLEMENT,
            prompt_path=f"/spool/{task_id}.md",
            result_path=f"/spool/{task_id}.json",
            model="auto",
            model_tier=ModelTier.STANDARD,
            repo="/repo",
            worktree=f"/wt/{task_id}",
            files_owned=owned,
            read_only_context=(),
            frozen_contracts=(),
            acceptance=("ok",),
            resource_class=ResourceClass.STANDARD,
            source_document_paths=("spec.md",),
        )

    with pytest.raises(ContractError, match="overlap"):
        PlanArtifact(
            run_id="r",
            goal="g",
            project="p",
            repo="/repo",
            base_branch="main",
            spec_sources=("PLAN.md",),
            tasks=(task("a", ("src/**",)), task("b", ("src/x.py",))),
            created_at=now,
            updated_at=now,
        )


@pytest.mark.asyncio
async def test_result_path_outside_spool_is_rejected(tmp_path: Path) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    envelope = store.get_dispatch(str(dispatch["dispatch_id"]))
    assert envelope is not None
    evil = tmp_path / "evil-result.json"
    # Force a path escape via raw SQL — dispatch rows are otherwise immutable.
    store._conn.execute(
        "UPDATE dispatches SET result_path=? WHERE dispatch_id=?",
        (str(evil), envelope.dispatch_id),
    )
    evil.write_text(
        json.dumps({"dispatch_id": envelope.dispatch_id, "final_text": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="under .harness/runs"):
        controller.report(envelope.dispatch_id, evil, "agent", ok=True)


@pytest.mark.asyncio
async def test_premerge_fix_requires_risk_approval_before_retry(tmp_path: Path) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    lifecycle.fail_full_gate = True
    paused = await controller.advance(run_id, approved_merge=True)
    assert paused["status"] == "paused"
    assert store.get_task(run_id, "001").status == TaskStatus.READY.value  # type: ignore[union-attr]
    assert controller.next(run_id, "agent")["ok"] is False

    resumed = await controller.resume(run_id)
    assert resumed.get("requires") == "risk_approval"

    approved = await controller.resume(run_id, approve_risk=True)
    assert approved["status"] == "running"
    pending = [d for d in approved["dispatches"] if d["status"] == "pending"]
    assert pending


@pytest.mark.asyncio
async def test_post_merge_fix_uses_lifecycle_repair_hook(tmp_path: Path) -> None:
    controller, store, lifecycle = _controller(tmp_path)

    async def failing_merge(run_id: str, task_id: str, *, approved: bool = False):
        task = store.get_task(run_id, task_id)
        assert task is not None
        store.transition_task(task, TaskStatus.POST_MERGE_FIX, note="gate failed")
        store.set_run_status(run_id, RunStatus.PAUSED.value)
        lifecycle.calls.append(("merge", task_id, approved))
        return _Result(
            "post_integration_gate_failed",
            TaskStatus.POST_MERGE_FIX.value,
            passed=False,
            detail="post gate red",
        )

    lifecycle.merge = failing_merge  # type: ignore[method-assign]
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    paused = await controller.advance(run_id, approved_merge=True)
    assert paused["status"] == "paused"
    assert store.get_task(run_id, "001").status == TaskStatus.POST_MERGE_FIX.value  # type: ignore[union-attr]

    recovered = await controller.resume(run_id, approve_risk=True)
    assert ("post_merge_repair", "001") in lifecycle.calls
    assert store.get_task(run_id, "001").status in {
        TaskStatus.READY.value,
        TaskStatus.RUNNING.value,
    }
    assert recovered["status"] == "running"


@pytest.mark.asyncio
async def test_approved_merge_does_not_consume_unrelated_succeeded_while_paused(
    tmp_path: Path,
) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    assert store.get_run(run_id).status == RunStatus.PAUSED.value  # type: ignore[union-attr]
    assert store.get_task(run_id, "001").status == TaskStatus.MERGE_QUEUE.value  # type: ignore[union-attr]

    # Inject an unrelated succeeded worker dispatch while paused.
    existing = store.list_dispatches(run_id)[0]
    unrelated = replace(
        existing,
        dispatch_id=f"{run_id}-unrelated-worker-9",
        task_id="001",
        stage=TaskStage.IMPLEMENT,
        status=DispatchStatus.SUCCEEDED,
        created_at=existing.created_at,
        updated_at=existing.updated_at,
    )
    # Bypass immutability only for this probe fixture via insert path.
    store.create_dispatch(unrelated)
    meta = controller._read_meta(run_id)
    kinds = dict(meta.get("dispatch_kinds") or {})
    kinds[unrelated.dispatch_id] = "worker"
    meta["dispatch_kinds"] = kinds
    controller._write_meta(run_id, meta)
    # Result file so consume could proceed if isolation failed.
    result_path = Path(unrelated.result_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"dispatch_id": unrelated.dispatch_id, "final_text": "HARNESS_DONE"}),
        encoding="utf-8",
    )

    before_finalize = sum(call[0] == "finalize" for call in lifecycle.calls)
    await controller.advance(run_id, approved_merge=True)
    after_finalize = sum(call[0] == "finalize" for call in lifecycle.calls)
    assert after_finalize == before_finalize
    assert not store.is_dispatch_applied(unrelated.dispatch_id)
    assert ("merge", "001", True) in lifecycle.calls


@pytest.mark.asyncio
async def test_approve_risk_does_not_bypass_questions_or_consume_work(
    tmp_path: Path,
) -> None:
    controller, store, lifecycle = _controller(tmp_path, tasks=2)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent", limit=1)["dispatches"][0]
    question = (
        "```question\n"
        '{"need_input": true, "questions":['
        '{"id":"q1","question":"which?","options":["a","b"],"why":"need"}'
        "]}\n"
        "```\n"
    )
    _report(controller, tmp_path, worker, question)
    await controller.advance(run_id)
    assert store.get_run(run_id).status == RunStatus.PAUSED.value  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="no persisted risk gate"):
        await controller.advance(run_id, approve_risk=True)
    questions = (
        controller.repo_root
        / ".harness"
        / "runs"
        / run_id
        / "pending"
        / "questions.json"
    )
    assert questions.is_file()
    assert controller.next(run_id, "agent")["ok"] is False


@pytest.mark.asyncio
async def test_plan_hash_rejects_ownership_mutation_with_stale_hash_field(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    plan_path = controller.repo_root / ".harness" / "runs" / run_id / "plan.json"
    plan = PlanArtifact.from_json(plan_path.read_text(encoding="utf-8"))
    stale_hash = plan.plan_hash
    mutated_tasks = []
    for task in plan.tasks:
        mutated_tasks.append(
            PlanTask(
                task_id=task.task_id,
                role=task.role,
                stage=task.stage,
                prompt_path=task.prompt_path,
                result_path=task.result_path,
                model=task.model,
                model_tier=task.model_tier,
                repo=task.repo,
                worktree=task.worktree,
                files_owned=("evil/owned.py",),
                read_only_context=task.read_only_context,
                frozen_contracts=task.frozen_contracts,
                acceptance=task.acceptance,
                resource_class=task.resource_class,
                source_document_paths=task.source_document_paths,
                dependencies=task.dependencies,
                spec_sha256=task.spec_sha256,
                frozen_spec_path=task.frozen_spec_path,
            )
        )
    mutated = PlanArtifact(
        run_id=plan.run_id,
        goal=plan.goal,
        project=plan.project,
        repo=plan.repo,
        base_branch=plan.base_branch,
        spec_sources=plan.spec_sources,
        tasks=tuple(mutated_tasks),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        protocol_version=plan.protocol_version,
        plan_hash=stale_hash,
    )
    plan_path.write_text(mutated.to_json(), encoding="utf-8")
    claimed = controller.next(run_id, "agent")
    assert claimed["ok"] is False
    assert "plan hash mismatch" in str(claimed.get("error", ""))


@pytest.mark.asyncio
async def test_advance_rejects_plan_mutation_before_any_lifecycle_side_effects(
    tmp_path: Path,
) -> None:
    """Mutating plan.json after claim must fail advance with zero state/event drift."""
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    dispatch_id = str(worker["dispatch_id"])
    _report(controller, tmp_path, worker, "HARNESS_DONE")

    plan_path = controller.repo_root / ".harness" / "runs" / run_id / "plan.json"
    plan = PlanArtifact.from_json(plan_path.read_text(encoding="utf-8"))
    stale_hash = plan.plan_hash
    mutated_tasks = []
    for task in plan.tasks:
        mutated_tasks.append(
            PlanTask(
                task_id=task.task_id,
                role=task.role,
                stage=task.stage,
                prompt_path=task.prompt_path,
                result_path=task.result_path,
                model=task.model,
                model_tier=task.model_tier,
                repo=task.repo,
                worktree=task.worktree,
                files_owned=("evil/owned.py",),
                read_only_context=task.read_only_context,
                frozen_contracts=task.frozen_contracts,
                acceptance=task.acceptance,
                resource_class=task.resource_class,
                source_document_paths=task.source_document_paths,
                dependencies=task.dependencies,
                spec_sha256=task.spec_sha256,
                frozen_spec_path=task.frozen_spec_path,
            )
        )
    mutated = PlanArtifact(
        run_id=plan.run_id,
        goal=plan.goal,
        project=plan.project,
        repo=plan.repo,
        base_branch=plan.base_branch,
        spec_sources=plan.spec_sources,
        tasks=tuple(mutated_tasks),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        protocol_version=plan.protocol_version,
        plan_hash=stale_hash,
    )
    plan_path.write_text(mutated.to_json(), encoding="utf-8")

    events_before = [(e.id, e.type, e.task_id, e.detail) for e in store.list_events(run_id)]
    task_before = store.get_task(run_id, "001")
    assert task_before is not None
    task_status_before = task_before.status
    run_status_before = store.get_run(run_id).status  # type: ignore[union-attr]
    finalize_before = sum(call[0] == "finalize" for call in lifecycle.calls)
    dispatches_before = [item.to_dict() for item in store.list_dispatches(run_id)]

    rejected = await controller.advance(run_id)

    assert rejected.get("ok") is False
    assert "plan hash mismatch" in str(rejected.get("error", ""))
    assert store.get_run(run_id).status == run_status_before  # type: ignore[union-attr]
    assert store.get_task(run_id, "001").status == task_status_before  # type: ignore[union-attr]
    assert [(e.id, e.type, e.task_id, e.detail) for e in store.list_events(run_id)] == (
        events_before
    )
    assert sum(call[0] == "finalize" for call in lifecycle.calls) == finalize_before
    assert not store.is_dispatch_applied(dispatch_id)
    assert [item.to_dict() for item in store.list_dispatches(run_id)] == dispatches_before
    # Immutable claim-time ownership must still be the original scope.
    envelope = store.get_dispatch(dispatch_id)
    assert envelope is not None
    assert list(envelope.files_owned) == ["src/task_1.py"]


@pytest.mark.asyncio
async def test_human_answers_injected_into_retry_and_questions_cleared(
    tmp_path: Path,
) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    question = (
        "```question\n"
        '{"need_input": true, "questions":['
        '{"id":"q1","question":"which?","options":["a","b"],"why":"need"}'
        "]}\n"
        "```\n"
    )
    _report(controller, tmp_path, worker, question)
    await controller.advance(run_id)
    pending = controller.repo_root / ".harness" / "runs" / run_id / "pending"
    assert (pending / "questions.json").is_file()

    resumed = await controller.resume(run_id, answers={"q1": "option-a"})
    assert resumed["status"] == "running"
    assert not (pending / "questions.json").exists()
    assert not (pending / "answers.json").exists()
    # Worker not yet reported; answers must land in the new worker prompt.
    prompts = list(
        (controller.repo_root / ".harness" / "runs" / run_id / "prompts").glob("*worker*")
    )
    assert any("option-a" in path.read_text(encoding="utf-8") for path in prompts)
    # No second question loop without answers.
    assert controller._pending_questions(run_id) is False
