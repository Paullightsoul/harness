from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

import harness.runner.factory as runner_factory
from harness.config import Settings
from harness.domain.enums import DispatchStatus, TaskStatus, Verdict
from harness.store.repository import Store
from harness.tasktool.controller import TaskToolController
from harness.tasktool.lifecycle import parse_review_verdict
from harness.tasktool.resources import GIB, ResourcePolicy, ResourceSnapshot

_WORKER_DIFF = (
    "--- a/src/task_1.py\n"
    "+++ b/src/task_1.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def handler():\n"
    "+    return SENTINEL_VALUE\n"
)


@dataclass
class _Result:
    action: str
    task_status: str
    passed: bool = True
    violations: tuple[str, ...] = ()
    detail: str = ""
    changed_files: list[str] | None = None
    diff: str = ""

    def __post_init__(self) -> None:
        if self.changed_files is None:
            self.changed_files = []


class _Lifecycle:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[tuple[object, ...]] = []
        self.fail_full_gate = False

    async def prepare_task(self, run_id: str, task_id: str) -> _Result:
        task = self._task(run_id, task_id)
        task.worktree_path = str(Path(task.spec_path).parent.parent)
        self.store.update_task_fields(task)
        self.store.transition_task(task, TaskStatus.RUNNING)
        self.calls.append(("prepare", task_id))
        return _Result("worker_required", TaskStatus.RUNNING.value)

    async def finalize_worker(
        self, run_id: str, task_id: str, final_text: str, *, files_owned=None
    ) -> _Result:
        task = self._task(run_id, task_id)
        self.store.transition_task(task, TaskStatus.GATING)
        self.calls.append(("finalize", task_id, final_text))
        return _Result(
            "gates_required",
            TaskStatus.GATING.value,
            changed_files=["src/task_1.py"],
            diff=_WORKER_DIFF,
        )

    async def run_gates(
        self, run_id: str, task_id: str, *, full: bool = False
    ) -> _Result:
        task = self._task(run_id, task_id)
        if not full:
            self.store.transition_task(task, TaskStatus.REVIEW)
        self.calls.append(("gates", task_id, full))
        return _Result(
            "full_gate_failed" if full and self.fail_full_gate else "review_required",
            self._task(run_id, task_id).status,
            passed=not (full and self.fail_full_gate),
            detail="full suite failed" if full and self.fail_full_gate else "",
        )

    def apply_review(self, run_id: str, task_id: str, review_text: str) -> _Result:
        task = self._task(run_id, task_id)
        verdict = parse_review_verdict(review_text)
        if verdict is Verdict.APPROVE:
            self.store.transition_task(task, TaskStatus.MERGE_QUEUE)
            self.calls.append(("review", task_id, review_text))
            return _Result("merge_required", TaskStatus.MERGE_QUEUE.value)
        self.store.transition_task(task, TaskStatus.READY, note=review_text)
        self.calls.append(("review", task_id, review_text))
        return _Result("changes_required", TaskStatus.READY.value)

    async def merge(
        self, run_id: str, task_id: str, *, approved: bool = False
    ) -> _Result:
        task = self._task(run_id, task_id)
        self.store.transition_task(task, TaskStatus.DONE)
        self.calls.append(("merge", task_id, approved))
        return _Result("merged", TaskStatus.DONE.value)

    async def prepare_post_merge_repair(self, run_id: str, task_id: str) -> _Result:
        task = self._task(run_id, task_id)
        if task.status == TaskStatus.POST_MERGE_FIX.value:
            self.store.transition_task(task, TaskStatus.READY)
        self.calls.append(("post_merge_repair", task_id))
        return _Result("post_merge_repair_ready", TaskStatus.READY.value)

    def recover(self, run_id: str) -> None:
        self.calls.append(("recover", run_id))

    def _task(self, run_id: str, task_id: str):
        task = self.store.get_task(run_id, task_id)
        assert task is not None
        return task


def _repo(
    tmp_path: Path,
    *,
    tasks: int = 1,
    chain: bool = False,
    complexity: str = "small",
) -> Path:
    repo = tmp_path / "repo"
    (repo / "tasks").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads" / "main").write_text(
        "0" * 40 + "\n", encoding="utf-8"
    )
    headings: list[str] = ["# Plan", ""]
    for index in range(1, tasks + 1):
        task_id = f"{index:03d}"
        dependency = "001" if chain and index == 2 else "-"
        headings.extend(
            [f"### {task_id}: task {index}", f"- depends_on: {dependency}", ""]
        )
        (repo / "tasks" / f"task-{task_id}.md").write_text(
            "\n".join(
                [
                    "---",
                    f'id: "{task_id}"',
                    f'title: "task {index}"',
                    'status: "todo"',
                    f'complexity: "{complexity}"',
                    "attempts: 0",
                    "---",
                    "# Файлы",
                    f"- src/task_{index}.py",
                    "# Acceptance criteria",
                    "- `pytest -q`",
                    "# Provides",
                    "- public API",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    (repo / "PLAN.md").write_text("\n".join(headings), encoding="utf-8")
    return repo


def _controller(
    tmp_path: Path,
    *,
    tasks: int = 1,
    chain: bool = False,
    policy: ResourcePolicy | None = None,
    complexity: str = "small",
    settings: Settings | None = None,
) -> tuple[TaskToolController, Store, _Lifecycle]:
    repo = _repo(tmp_path, tasks=tasks, chain=chain, complexity=complexity)
    base = Settings(
        root=repo,
        base_branch="main",
        # Controller unit tests use a stub lifecycle that still models merge-queue;
        # keep ship/merge semantics unless a test opts into the production defaults.
        ship_mode="merge",
        use_worktrees=True,
        # Unit tests assert single-active refuse; multi-tenant is covered elsewhere.
        multi_tenant=False,
    )
    if settings is not None:
        base = replace(
            base,
            review_min_complexity=settings.review_min_complexity,
            dag_unlock=settings.dag_unlock,
            ship_mode=settings.ship_mode,
            use_worktrees=settings.use_worktrees,
            root=repo,
            base_branch="main",
        )
    settings = base
    store = Store(tmp_path / "state.db")
    lifecycle = _Lifecycle(store)
    # Healthy host snapshot above soft MemAvailable (16 GiB) so admission allows.
    snapshot = ResourceSnapshot(64 * GIB, 0.1, 8)
    controller = TaskToolController(
        settings,
        store,
        repo,
        lifecycle=lifecycle,
        resource_policy=policy
        or ResourcePolicy(
            max_tasktool_jobs=6,
            max_agent_slots=6,
            max_heavy_jobs=2,
            max_gates=1,
            soft_mem_available_bytes=16 * GIB,
            hard_mem_available_bytes=8 * GIB,
            load_per_cpu_threshold=0.75,
        ),
        snapshot=snapshot,
    )
    return controller, store, lifecycle


def _report(
    controller: TaskToolController,
    tmp_path: Path,
    dispatch: dict[str, object],
    text: str,
    *,
    agent: str = "agent",
    worker: str = "",
) -> dict[str, object]:
    dispatch_id = str(dispatch["dispatch_id"])
    result = tmp_path / f"{dispatch_id}.json"
    result.write_text(
        json.dumps({"dispatch_id": dispatch_id, "final_text": text}),
        encoding="utf-8",
    )
    # One dispatcher holds every lease; each Task Tool job is its own executor.
    return controller.report(
        dispatch_id,
        result,
        agent,
        ok=True,
        worker_id=worker or f"worker-{dispatch_id}",
    )


@pytest.mark.asyncio
async def test_full_pull_flow_never_constructs_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_factory,
        "build_runner",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("runner constructed")),
    )
    controller, _, lifecycle = _controller(tmp_path)
    started = await controller.start(
        project="demo", goal="ship task", base_branch="main", approve_plan=True
    )
    run_id = str(started["run_id"])

    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "Provides:\n- API\n\nHARNESS_DONE")
    await controller.advance(run_id)

    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    assert reviewer["stage"] == "review"
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    paused = await controller.advance(run_id)
    assert paused["status"] == "paused"

    finished = await controller.advance(run_id, approved_merge=True)
    assert finished["status"] == "done"
    assert ("merge", "001", True) in lifecycle.calls
    assert ("gates", "001", False) in lifecycle.calls
    assert ("gates", "001", True) in lifecycle.calls


@pytest.mark.asyncio
async def test_restart_and_report_are_idempotent(tmp_path: Path) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    first = _report(controller, tmp_path, dispatch, "HARNESS_DONE")

    rebuilt = TaskToolController(
        controller.settings,
        store,
        controller.repo_root,
        lifecycle=lifecycle,
        snapshot=ResourceSnapshot(64 * GIB, 0.1, 8),
    )
    second = _report(rebuilt, tmp_path, dispatch, "HARNESS_DONE")
    await rebuilt.advance(run_id)
    await rebuilt.advance(run_id)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert sum(call[0] == "finalize" for call in lifecycle.calls) == 1


@pytest.mark.asyncio
async def test_gate_contention_replays_without_refinalizing_worker(tmp_path: Path) -> None:
    controller, _, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, dispatch, "HARNESS_DONE")
    lock = controller.settings.root / ".harness" / "tasktool-gate.lock"
    lock.write_text(f"{os.getpid()}:other:task\n", encoding="utf-8")

    contended = await controller.advance(run_id)
    assert contended["tasks"][0]["status"] == TaskStatus.GATING.value
    assert sum(call[0] == "finalize" for call in lifecycle.calls) == 1

    lock.unlink()
    replayed = await controller.advance(run_id)

    assert replayed["tasks"][0]["status"] == TaskStatus.REVIEW.value
    assert sum(call[0] == "finalize" for call in lifecycle.calls) == 1
    assert sum(call[0] == "gates" for call in lifecycle.calls) == 1


@pytest.mark.asyncio
async def test_stale_gate_lock_is_recovered(tmp_path: Path) -> None:
    controller, _, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, dispatch, "HARNESS_DONE")
    lock = controller.settings.root / ".harness" / "tasktool-gate.lock"
    lock.write_text("999999999:dead:task\n", encoding="utf-8")

    advanced = await controller.advance(run_id)

    assert advanced["tasks"][0]["status"] == TaskStatus.REVIEW.value
    assert not lock.exists()
    assert sum(call[0] == "gates" for call in lifecycle.calls) == 1


@pytest.mark.asyncio
async def test_plan_draft_requires_explicit_approval(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)

    draft = await controller.start(project="demo", goal="goal")
    resumed_draft = await controller.resume(str(draft["run_id"]))
    approved = await controller.start(project="demo", goal="goal", approve_plan=True)

    assert draft["status"] == "plan_draft"
    assert draft["requires"] == "approve_plan"
    assert resumed_draft["status"] == "plan_draft"
    assert resumed_draft["requires"] == "approve_plan"
    assert approved["run_id"] == draft["run_id"]
    assert approved["status"] == "running"


@pytest.mark.asyncio
async def test_plan_draft_blocks_second_run_for_same_repo(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    await controller.start(project="demo", goal="first")

    with pytest.raises(RuntimeError, match="already has active TaskTool run"):
        await controller.start(project="demo", goal="second")


@pytest.mark.asyncio
async def test_report_canonicalizes_result_already_at_target(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    result_path = Path(str(dispatch["result_path"]))
    result_path.write_text(' { "text" : "HARNESS_DONE" } \n', encoding="utf-8")

    reported = controller.report(
        str(dispatch["dispatch_id"]),
        result_path,
        "agent",
        ok=True,
    )
    repeated = controller.report(
        str(dispatch["dispatch_id"]),
        result_path,
        "agent",
        ok=True,
    )

    assert reported["ok"] is True
    assert repeated["idempotent"] is True
    canonical = json.loads(result_path.read_text(encoding="utf-8"))
    assert canonical["dispatch_id"] == dispatch["dispatch_id"]
    assert canonical["final_text"] == "HARNESS_DONE"


@pytest.mark.asyncio
async def test_done_dependency_activates_next_worker(tmp_path: Path) -> None:
    controller, store, _ = _controller(tmp_path, tasks=2, chain=True)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)

    after_merge = await controller.advance(run_id, approved_merge=True)

    task_two = store.get_task(run_id, "002")
    assert task_two is not None
    assert task_two.status == TaskStatus.RUNNING.value
    pending_task_ids = {
        str(item["task_id"])
        for item in after_merge["dispatches"]
        if item["status"] == "pending"
    }
    assert "002" in pending_task_ids


@pytest.mark.asyncio
async def test_failed_premerge_full_gate_pauses_without_merge(tmp_path: Path) -> None:
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
    assert not any(call[0] == "merge" for call in lifecycle.calls)
    risk = (
        controller._run_dir(run_id)  # noqa: SLF001
        / "pending"
        / "risk_gate.json"
    )
    assert risk.is_file()
    assert "pre-merge" in risk.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_resume_does_not_bypass_ship_approval(tmp_path: Path) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)

    resumed = await controller.resume(run_id)

    assert resumed["status"] == "paused"
    assert store.get_task(run_id, "001").status == TaskStatus.MERGE_QUEUE.value  # type: ignore[union-attr]
    assert not any(call[0] == "merge" for call in lifecycle.calls)


@pytest.mark.asyncio
async def test_start_validates_branch_existence_not_checkout(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    git = controller.repo_root / ".git"
    (git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (git / "refs" / "heads" / "feature").write_text("1" * 40 + "\n", encoding="utf-8")

    started = await controller.start(
        project="demo",
        goal="goal",
        base_branch="main",
        approve_plan=True,
    )

    assert started["ok"] is True
    other, _, _ = _controller(tmp_path / "missing")
    with pytest.raises(ValueError, match="base branch does not exist"):
        await other.start(
            project="demo",
            goal="goal",
            base_branch="missing",
            approve_plan=True,
        )


@pytest.mark.asyncio
async def test_next_throttles_at_durable_active_limit(tmp_path: Path) -> None:
    policy = ResourcePolicy(max_tasktool_jobs=1, max_agent_slots=2)
    controller, _, _ = _controller(tmp_path, tasks=2, policy=policy)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])

    first = controller.next(run_id, "agent-1", limit=1)
    second = controller.next(run_id, "agent-2", limit=1)

    assert len(first["dispatches"]) == 1
    assert second["status"] == "throttle"
    assert second["dispatches"] == []


@pytest.mark.asyncio
async def test_next_passes_run_scope_to_atomic_store_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    original = store.claim_pending_dispatches
    scopes: list[str | None] = []

    def claim(**kwargs: object):
        scope = kwargs.get("run_id")
        scopes.append(scope if isinstance(scope, str) else None)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "claim_pending_dispatches", claim)

    controller.next(run_id, "agent")

    assert scopes == [run_id]


@pytest.mark.asyncio
async def test_goal_judge_only_for_large_not_medium(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Decompose off: this test pins the goal-judge pipeline, not agent-in-agent.
    monkeypatch.setenv("HARNESS_DECOMPOSE", "0")
    medium, _, _ = _controller(tmp_path / "medium", complexity="medium")
    started_m = await medium.start(project="demo", goal="goal", approve_plan=True)
    run_m = str(started_m["run_id"])
    worker_m = medium.next(run_m, "agent")["dispatches"][0]
    _report(medium, tmp_path / "medium", worker_m, "HARNESS_DONE")
    await medium.advance(run_m)
    review_m = medium.next(run_m, "agent")["dispatches"][0]
    assert "goal-judge" not in str(review_m["dispatch_id"])
    assert review_m["stage"] == "review"

    large, _, _ = _controller(tmp_path / "large", complexity="large")
    started_l = await large.start(project="demo", goal="goal", approve_plan=True)
    run_l = str(started_l["run_id"])
    worker_l = large.next(run_l, "agent")["dispatches"][0]
    _report(large, tmp_path / "large", worker_l, "HARNESS_DONE")
    await large.advance(run_l)
    judge = large.next(run_l, "agent")["dispatches"][0]
    assert "goal-judge" in str(judge["dispatch_id"])
    prompt = Path(str(judge["prompt_path"])).read_text(encoding="utf-8")
    assert "Task Brief" in prompt
    assert "Worktree absolute path:" in prompt


@pytest.mark.asyncio
async def test_checkpoint_written_and_used_on_resume(tmp_path: Path) -> None:
    controller, store, lifecycle = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)

    checkpoint = controller._checkpoints.read(run_id, "001")
    assert checkpoint is not None
    assert checkpoint.stage in {"worker", "gate"}
    assert "src/task_1.py" in checkpoint.changed_files

    rebuilt = TaskToolController(
        controller.settings,
        store,
        controller.repo_root,
        lifecycle=lifecycle,
        snapshot=ResourceSnapshot(64 * GIB, 0.1, 8),
    )
    await rebuilt.resume(run_id)
    reviewer = rebuilt.next(run_id, "agent")["dispatches"][0]
    prompt = Path(str(reviewer["prompt_path"])).read_text(encoding="utf-8")
    assert "ReviewBundle" in prompt
    assert "src/task_1.py" in prompt
    assert "path:line:severity: message" in prompt


@pytest.mark.asyncio
async def test_reviewer_prompt_carries_the_captured_diff(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)

    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    prompt = Path(str(reviewer["prompt_path"])).read_text(encoding="utf-8")

    assert "```diff" in prompt
    assert "SENTINEL_VALUE" in prompt
    # The diff is persisted so a later dispatch reviews the same snapshot.
    patch = controller._run_dir(run_id) / "diffs" / "001.patch"  # noqa: SLF001
    assert patch.is_file()
    assert "SENTINEL_VALUE" in patch.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_goal_judge_prompt_carries_the_diff_not_just_worker_claims(
    tmp_path: Path,
) -> None:
    """A large task gets a goal-judge; it must grade code, not the worker's prose."""
    controller, _, _ = _controller(tmp_path, complexity="large")
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])

    seen_goal_judge = False
    for _ in range(6):
        dispatches = controller.next(run_id, "agent")["dispatches"]
        if not dispatches:
            break
        dispatch = dispatches[0]
        prompt = Path(str(dispatch["prompt_path"])).read_text(encoding="utf-8")
        if "goal-judge" in prompt.split("\n", 1)[0]:
            seen_goal_judge = True
            assert "```diff" in prompt
            assert "SENTINEL_VALUE" in prompt
            assert "Worker's own claims (unverified)" in prompt
            break
        if "sub-orchestrator" in prompt.split("\n", 1)[0]:
            _report(
                controller, tmp_path, dispatch,
                '```json\n{"decompose": false, "reason": "small enough"}\n```\n',
            )
        else:
            _report(controller, tmp_path, dispatch, "HARNESS_DONE")
        await controller.advance(run_id)

    assert seen_goal_judge


@pytest.mark.asyncio
async def test_lesson_best_effort_on_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    brain.mkdir()
    monkeypatch.setenv("HARNESS_BRAIN_ROOT", str(brain))
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    finished = await controller.advance(run_id, approved_merge=True)
    assert finished["status"] == "done"
    lessons = list((brain / "lessons" / "demo").glob("task-*.md"))
    assert len(lessons) == 1
    assert "outcome: DONE" in lessons[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_lesson_missing_brain_does_not_fail_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARNESS_BRAIN_ROOT", raising=False)
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    finished = await controller.advance(run_id, approved_merge=True)
    assert finished["status"] == "done"


@pytest.mark.asyncio
async def test_report_separates_lease_identity_from_worker_identity(
    tmp_path: Path,
) -> None:
    """ADR-0011 keeps one dispatcher, so the lease id cannot be the executor id.

    Regression: ``report`` used to stamp ``worker_id = agent_id``, which made the
    documented single-root-chat flow indistinguishable from orch==worker cosplay
    and left runs stuck at ``paused``.
    """
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "root-chat")["dispatches"][0]
    dispatch_id = str(dispatch["dispatch_id"])
    result = tmp_path / "r.json"
    result.write_text(
        json.dumps({"dispatch_id": dispatch_id, "final_text": "HARNESS_DONE"}),
        encoding="utf-8",
    )

    out = controller.report(
        dispatch_id, result, "root-chat", ok=True, worker_id="task-tool-job-7"
    )

    assert out["worker_id"] == "task-tool-job-7"
    assert out["agent_id"] == "root-chat"
    # The lease still belongs to the dispatcher that claimed it.
    assert store.get_dispatch(dispatch_id).status is DispatchStatus.SUCCEEDED
    meta = json.loads(
        (controller._run_dir(run_id) / "controller.json").read_text(encoding="utf-8")
    )
    recorded = meta["dispatch_agents"][dispatch_id]
    assert recorded["worker_id"] == "task-tool-job-7"
    assert recorded["agent_id"] == "root-chat"
    snapshot = controller._cosplay_snapshot(run_id)
    assert snapshot["distinct_worker_ids"] == ["task-tool-job-7"]


@pytest.mark.asyncio
async def test_report_falls_back_to_lease_identity_without_worker_id(
    tmp_path: Path,
) -> None:
    """Legacy callers that pass no --worker-id keep the old behaviour."""
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "root-chat")["dispatches"][0]
    dispatch_id = str(dispatch["dispatch_id"])
    result = tmp_path / "r.json"
    result.write_text(
        json.dumps({"dispatch_id": dispatch_id, "final_text": "HARNESS_DONE"}),
        encoding="utf-8",
    )

    out = controller.report(dispatch_id, result, "root-chat", ok=True)

    assert out["worker_id"] == "root-chat"
    assert controller._cosplay_snapshot(run_id)["distinct_worker_ids"] == ["root-chat"]
