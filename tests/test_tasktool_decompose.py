"""Agent-in-agent: sub-orchestrator decomposition inside the TaskTool pull plane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import TaskStatus, Verdict
from harness.store.repository import Store
from harness.tasktool.controller import TaskToolController
from harness.tasktool.lifecycle import parse_review_verdict
from harness.tasktool.resources import GIB, ResourcePolicy, ResourceSnapshot


@dataclass
class _Result:
    action: str
    task_status: str
    passed: bool = True
    violations: tuple[str, ...] = ()
    detail: str = ""
    changed_files: list[str] | None = None

    def __post_init__(self) -> None:
        if self.changed_files is None:
            self.changed_files = []


class _Lifecycle:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[tuple[object, ...]] = []

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
        self.calls.append(("finalize", task_id))
        return _Result(
            "gates_required",
            TaskStatus.GATING.value,
            changed_files=[f"src/feature/{task_id}.py"],
        )

    async def run_gates(
        self, run_id: str, task_id: str, *, full: bool = False
    ) -> _Result:
        task = self._task(run_id, task_id)
        if not full:
            self.store.transition_task(task, TaskStatus.REVIEW)
        self.calls.append(("gates", task_id, full))
        return _Result("review_required", self._task(run_id, task_id).status)

    def apply_review(self, run_id: str, task_id: str, review_text: str) -> _Result:
        task = self._task(run_id, task_id)
        if parse_review_verdict(review_text) is Verdict.APPROVE:
            self.store.transition_task(task, TaskStatus.MERGE_QUEUE)
            return _Result("merge_required", TaskStatus.MERGE_QUEUE.value)
        self.store.transition_task(task, TaskStatus.READY, note=review_text)
        return _Result("changes_required", TaskStatus.READY.value)

    async def merge(
        self, run_id: str, task_id: str, *, approved: bool = False
    ) -> _Result:
        task = self._task(run_id, task_id)
        self.store.transition_task(task, TaskStatus.DONE)
        self.calls.append(("merge", task_id))
        return _Result("merged", TaskStatus.DONE.value)

    async def prepare_post_merge_repair(self, run_id: str, task_id: str) -> _Result:
        return _Result("post_merge_repair_ready", TaskStatus.READY.value)

    def recover(self, run_id: str) -> None:
        self.calls.append(("recover", run_id))

    def _task(self, run_id: str, task_id: str):
        task = self.store.get_task(run_id, task_id)
        assert task is not None
        return task


def _repo(tmp_path: Path, *, spec_extra: str = "") -> Path:
    repo = tmp_path / "repo"
    (repo / "tasks").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads" / "main").write_text(
        "0" * 40 + "\n", encoding="utf-8"
    )
    (repo / "PLAN.md").write_text(
        "# Plan\n\n### 001: big feature\n- depends_on: -\n", encoding="utf-8"
    )
    (repo / "tasks" / "task-001.md").write_text(
        "\n".join(
            [
                "---",
                'id: "001"',
                'title: "big feature"',
                'status: "todo"',
                'complexity: "large"',
                "attempts: 0",
                "---",
                "# Контекст",
                "Большая фича: модели + API.",
                spec_extra,
                "# Файлы",
                "- src/feature/**",
                "# Acceptance criteria",
                "- `pytest -q`",
                "# Provides",
                "- public API",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return repo


def _controller(
    tmp_path: Path, *, spec_extra: str = ""
) -> tuple[TaskToolController, Store, _Lifecycle]:
    repo = _repo(tmp_path, spec_extra=spec_extra)
    settings = Settings(
        root=repo,
        base_branch="main",
        ship_mode="merge",
        use_worktrees=True,
    )
    store = Store(tmp_path / "state.db")
    lifecycle = _Lifecycle(store)
    controller = TaskToolController(
        settings,
        store,
        repo,
        lifecycle=lifecycle,
        resource_policy=ResourcePolicy(),
        snapshot=ResourceSnapshot(64 * GIB, 0.1, 24),
    )
    return controller, store, lifecycle


def _report(
    controller: TaskToolController,
    tmp_path: Path,
    dispatch: dict[str, object],
    text: str,
) -> None:
    dispatch_id = str(dispatch["dispatch_id"])
    result = tmp_path / f"{dispatch_id}.json"
    result.write_text(
        json.dumps({"dispatch_id": dispatch_id, "final_text": text}),
        encoding="utf-8",
    )
    # One dispatcher holds every lease; each Task Tool job is its own executor.
    controller.report(
        dispatch_id, result, "agent", ok=True, worker_id=f"worker-{dispatch_id}"
    )


_EXPANSION = (
    "Изучил репо.\n\n"
    "```json\n"
    + json.dumps(
        {
            "decompose": True,
            "reason": "models and api are independent",
            "subtasks": [
                {
                    "id": "models",
                    "title": "Data models",
                    "summary": "ORM models for the feature",
                    "files_owned": ["src/feature/models/**"],
                    "depends_on": [],
                    "acceptance": ["pytest tests/test_models.py -q"],
                    "complexity": "small",
                },
                {
                    "id": "api",
                    "title": "API layer",
                    "summary": "Routers over models",
                    "files_owned": ["src/feature/api/**"],
                    "depends_on": ["models"],
                    "acceptance": ["pytest tests/test_api.py -q"],
                    "complexity": "small",
                },
            ],
        }
    )
    + "\n```\n"
)


async def _drive_child_to_done(
    controller: TaskToolController,
    tmp_path: Path,
    run_id: str,
    task_id: str,
) -> None:
    worker = _dispatch_for(controller, run_id, task_id, "worker")
    _report(controller, tmp_path, worker, "Provides:\n- API\n\nHARNESS_DONE")
    await controller.advance(run_id)
    reviewer = _dispatch_for(controller, run_id, task_id, "reviewer")
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    await controller.advance(run_id, approved_merge=True)


def _dispatch_for(
    controller: TaskToolController,
    run_id: str,
    task_id: str,
    kind: str,
) -> dict[str, object]:
    claimed = controller.next(run_id, "agent", limit=8)["dispatches"]
    matches = [
        item
        for item in claimed
        if item["task_id"] == task_id and kind in str(item["dispatch_id"])
    ]
    assert matches, f"no {kind} dispatch for {task_id}: {claimed}"
    return matches[0]


@pytest.mark.asyncio
async def test_large_task_starts_with_sub_orchestrator(tmp_path: Path) -> None:
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="big", approve_plan=True)
    run_id = str(started["run_id"])

    claimed = controller.next(run_id, "agent")["dispatches"]

    assert len(claimed) == 1
    dispatch = claimed[0]
    assert "sub-orchestrator" in str(dispatch["dispatch_id"])
    assert dispatch["stage"] == "plan"
    assert dispatch["resource_class"] == "standard"
    prompt = Path(str(dispatch["prompt_path"])).read_text(encoding="utf-8")
    assert "sub-orchestrator" in prompt
    assert '"decompose"' in prompt
    assert "## Context pack" in prompt


@pytest.mark.asyncio
async def test_expansion_creates_children_and_parent_integrates(
    tmp_path: Path,
) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="big", approve_plan=True)
    run_id = str(started["run_id"])
    sub = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, sub, _EXPANSION)

    await controller.advance(run_id)

    parent = store.get_task(run_id, "001")
    assert parent is not None
    assert parent.status == TaskStatus.PENDING.value
    assert set(parent.depends_on) >= {"001-models", "001-api"}
    status = controller.status(run_id)
    assert status["expansions"] == {"001": ["001-models", "001-api"]}
    models = store.get_task(run_id, "001-models")
    api = store.get_task(run_id, "001-api")
    assert models is not None and api is not None
    assert models.status == TaskStatus.RUNNING.value  # prepared + worker enqueued
    assert api.status == TaskStatus.PENDING.value  # waits for models

    await _drive_child_to_done(controller, tmp_path, run_id, "001-models")
    await _drive_child_to_done(controller, tmp_path, run_id, "001-api")

    integration = _dispatch_for(controller, run_id, "001", "worker")
    prompt = Path(str(integration["prompt_path"])).read_text(encoding="utf-8")
    assert "depends:001-models provides" in prompt
    assert "depends:001-api provides" in prompt

    _report(controller, tmp_path, integration, "Provides:\n- API\n\nHARNESS_DONE")
    await controller.advance(run_id)
    judge = _dispatch_for(controller, run_id, "001", "goal-judge")
    _report(controller, tmp_path, judge, "VERDICT: APPROVE")
    await controller.advance(run_id)
    reviewer = _dispatch_for(controller, run_id, "001", "reviewer")
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    finished = await controller.advance(run_id, approved_merge=True)

    assert finished["status"] == "done"
    tasks = {item["task_id"]: item["status"] for item in finished["tasks"]}
    assert tasks == {
        "001": "done",
        "001-models": "done",
        "001-api": "done",
    }


@pytest.mark.asyncio
async def test_decompose_false_hands_analysis_to_direct_worker(
    tmp_path: Path,
) -> None:
    controller, _, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="big", approve_plan=True)
    run_id = str(started["run_id"])
    sub = controller.next(run_id, "agent")["dispatches"][0]
    verdict = (
        "Ключевые файлы: src/feature/core.py.\n\n"
        '```json\n{"decompose": false, "reason": "single cohesive change"}\n```\n'
    )
    _report(controller, tmp_path, sub, verdict)

    await controller.advance(run_id)

    worker = _dispatch_for(controller, run_id, "001", "worker")
    prompt = Path(str(worker["prompt_path"])).read_text(encoding="utf-8")
    assert "single cohesive change" in prompt
    assert "Ключевые файлы" in prompt


@pytest.mark.asyncio
async def test_invalid_expansion_retries_sub_orchestrator_with_feedback(
    tmp_path: Path,
) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="big", approve_plan=True)
    run_id = str(started["run_id"])
    sub = controller.next(run_id, "agent")["dispatches"][0]
    escape = (
        "```json\n"
        + json.dumps(
            {
                "decompose": True,
                "reason": "bad split",
                "subtasks": [
                    {
                        "id": "a",
                        "title": "A",
                        "summary": "…",
                        "files_owned": ["src/other/**"],
                        "depends_on": [],
                        "acceptance": [],
                        "complexity": "small",
                    },
                    {
                        "id": "b",
                        "title": "B",
                        "summary": "…",
                        "files_owned": ["src/feature/b/**"],
                        "depends_on": [],
                        "acceptance": [],
                        "complexity": "small",
                    },
                ],
            }
        )
        + "\n```"
    )
    _report(controller, tmp_path, sub, escape)

    await controller.advance(run_id)

    assert store.get_task(run_id, "001-a") is None
    retry = _dispatch_for(controller, run_id, "001", "sub-orchestrator-2")
    prompt = Path(str(retry["prompt_path"])).read_text(encoding="utf-8")
    assert "outside parent ownership" in prompt


@pytest.mark.asyncio
async def test_spec_opt_out_and_env_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opted_out, _, _ = _controller(tmp_path / "optout", spec_extra="Decompose: no")
    started = await opted_out.start(project="demo", goal="big", approve_plan=True)
    first = opted_out.next(str(started["run_id"]), "agent")["dispatches"][0]
    assert "worker" in str(first["dispatch_id"])

    monkeypatch.setenv("HARNESS_DECOMPOSE", "0")
    disabled, _, _ = _controller(tmp_path / "disabled")
    started_d = await disabled.start(project="demo", goal="big", approve_plan=True)
    first_d = disabled.next(str(started_d["run_id"]), "agent")["dispatches"][0]
    assert "worker" in str(first_d["dispatch_id"])
