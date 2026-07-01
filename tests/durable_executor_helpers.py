"""Общие фикстуры для test_durable_executor.py и test_durable_executor_live.py.

Не `test_*.py` — pytest не пытается собрать этот файл как тесты, поэтому его можно
безопасно импортировать из обоих (иначе `from tests.test_durable_executor import
...` рискует зарегистрировать файл под двумя разными именами модуля в pytest
rootless import mode — `test_durable_executor` и `tests.test_durable_executor`).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from harness.config import Settings
from harness.domain.models import Run, Task
from harness.durable.executor import EngineTaskExecutor
from harness.gates.base import GateResult
from harness.runner.base import AgentResult
from harness.store.repository import Store


def run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "test@harness.local")
    run_git(repo, "config", "user.name", "harness-test")
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")


def git_log(repo_or_worktree: Path) -> str:
    return subprocess.run(
        ["git", "log", "--oneline"], cwd=str(repo_or_worktree), check=True,
        capture_output=True, text=True,
    ).stdout


class StubWorkerRunner:
    def __init__(self, text: str = "done", edit: str | None = "from worker\n") -> None:
        self.text = text
        self.edit = edit
        self.calls: list[str] = []

    async def run(
        self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None,
    ) -> AgentResult:
        self.calls.append(prompt)
        if self.edit is not None:
            (cwd / "f.txt").write_text(self.edit, encoding="utf-8")
        return AgentResult(ok=True, text=self.text, cost_credits=0.5)


class StubReviewerRunner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []

    async def run(
        self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None,
    ) -> AgentResult:
        self.calls.append(prompt)
        return AgentResult(ok=True, text=self.text)


class StubGate:
    def __init__(self, passed: bool = True) -> None:
        self.passed = passed
        self.calls = 0

    async def check(self, cwd: Path) -> GateResult:
        self.calls += 1
        return GateResult(passed=self.passed, output="ok" if self.passed else "FAIL")


def make_executor(
    tmp_path: Path, repo: Path, *, worker: object = None, reviewer: object = None,
    gate: object = None,
) -> EngineTaskExecutor:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "worker.md").write_text("# worker role\n", encoding="utf-8")
    (prompts / "reviewer.md").write_text("# reviewer role\n", encoding="utf-8")
    settings = Settings(root=tmp_path)
    store = Store(tmp_path / "state.db")
    return EngineTaskExecutor(
        settings, store, repo_root=repo, base_branch="main",
        worker_runner=worker or StubWorkerRunner(),  # type: ignore[arg-type]
        reviewer_runner=reviewer or StubReviewerRunner("VERDICT: APPROVE"),  # type: ignore[arg-type]
        gate=gate or StubGate(passed=True),  # type: ignore[arg-type]
    )


def seed_task(executor: EngineTaskExecutor, run_id: str, task_id: str, tmp_path: Path) -> None:
    store: Store = executor._store  # noqa: SLF001 (тестовый хелпер — прямой доступ)
    store.create_run(Run(id=run_id, project="p", goal="g", status="running"))
    spec = tmp_path / f"task-{task_id}.md"
    spec.write_text("# Контекст\nтестовая задача\n", encoding="utf-8")
    store.upsert_task(Task(
        id=task_id, run_id=run_id, title="t", spec_path=str(spec), status="pending",
    ))
