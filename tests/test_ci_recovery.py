"""v2-033: CI failure recovery — `gh pr checks` poll → fix-pass на CI-логах → re-push.

Формально задача зависит от ROADMAP 3.14 (GitHub PR-интеграция вместо локального
мержа), которая ещё не реализована — harness мержит задачи локально и пушит
`base` напрямую, PR на ветку `task/<id>` никто не создаёт автоматически. Поэтому
`Engine._maybe_run_ci_recovery` спроектирован как safe no-op seam: если PR для
ветки не найден (обычный случай сегодня) — тихо ничего не делает. Логика
poll→fix→re-push (`_run_ci_recovery`) и обёртка над `gh` (`GitHubCli`) полностью
рабочие и протестированы в изоляции — как только 3.14 начнёт создавать PR на
каждую задачу, весь путь заработает без изменений.
"""
from __future__ import annotations

import asyncio
import inspect
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.config import Settings
from harness.domain.enums import EventType
from harness.domain.models import Run, Task
from harness.integrations.github import CheckStatus, GitHubCli, PullRequest
from harness.runner.base import AgentResult
from harness.scheduler.engine import Engine
from harness.store.repository import Store
from harness.worktree.manager import WorktreeManager


class _FakeProc:
    def __init__(self, stdout: bytes = b"", rc: int = 0) -> None:
        self._out = stdout
        self.returncode = rc

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, b""


# ── GitHubCli: unit-тесты обёртки над `gh` ──────────────────────────────────

@pytest.mark.asyncio
async def test_pr_for_branch_parses_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        assert cmd[:2] == ("gh", "pr")
        return _FakeProc(b'{"number": 42, "url": "https://github.com/x/y/pull/42"}', 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    pr = await gh.pr_for_branch("task/001")

    assert pr is not None
    assert pr.number == 42
    assert "pull/42" in pr.url


@pytest.mark.asyncio
async def test_pr_for_branch_none_when_gh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обычный случай сегодня: PR для ветки задачи не существует (нет 3.14)."""
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        return _FakeProc(b"no pull requests found for branch", 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    assert await gh.pr_for_branch("task/001") is None


@pytest.mark.asyncio
async def test_pr_for_branch_none_on_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        return _FakeProc(b"not json", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    assert await gh.pr_for_branch("task/001") is None


@pytest.mark.asyncio
async def test_check_status_all_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        return _FakeProc(b"lint\tpass\t1s\thttps://x/actions/runs/1/job/1\n", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    status = await gh.check_status(42)

    assert status.all_passed is True
    assert status.failed_run_ids == []


@pytest.mark.asyncio
async def test_check_status_detects_failed_run_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = (
        b"lint\tpass\t1s\thttps://github.com/x/y/actions/runs/111/job/1\n"
        b"test\tfail\t3s\thttps://github.com/x/y/actions/runs/222/job/2\n"
    )

    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        return _FakeProc(output, 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    status = await gh.check_status(42)

    assert status.all_passed is False
    assert "222" in status.failed_run_ids
    assert "111" not in status.failed_run_ids


@pytest.mark.asyncio
async def test_failed_run_log_returns_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        assert "view" in cmd and "--log-failed" in cmd
        return _FakeProc(b"AssertionError: expected 1, got 2", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    log = await gh.failed_run_log("222")
    assert "AssertionError" in log


@pytest.mark.asyncio
async def test_create_pr_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        calls.append(cmd)
        if cmd[1] == "create":
            return _FakeProc(b"https://github.com/x/y/pull/5\n", 0)
        return _FakeProc(b'{"number": 5, "url": "https://github.com/x/y/pull/5"}', 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    pr = await gh.create_pr("task/001", "main", "task(001): title")

    assert pr is not None
    assert pr.number == 5


@pytest.mark.asyncio
async def test_create_pr_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        return _FakeProc(b"not authenticated", 1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    assert await gh.create_pr("task/001", "main", "t") is None


@pytest.mark.asyncio
async def test_gh_not_installed_is_handled_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    gh = GitHubCli(tmp_path)

    assert await gh.pr_for_branch("task/001") is None


# ── Engine._run_ci_recovery: poll → fix → re-push цикл ──────────────────────

def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    engine = Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    spec = tmp_path / "task-001.md"
    spec.write_text("# spec\n", encoding="utf-8")
    engine._store.upsert_task(Task(
        id="001", run_id="r1", title="t", spec_path=str(spec), status="done",
    ))
    return engine


class _StubGh:
    """Мок GitHubCli.check_status/failed_run_log — сценарий по очереди из `statuses`."""

    def __init__(self, statuses: list[CheckStatus]) -> None:
        self._statuses = list(statuses)
        self.check_calls = 0
        self.log_calls: list[str] = []

    async def check_status(self, pr_number: int) -> CheckStatus:
        idx = min(self.check_calls, len(self._statuses) - 1)
        self.check_calls += 1
        return self._statuses[idx]

    async def failed_run_log(self, run_id: str) -> str:
        self.log_calls.append(run_id)
        return f"log for run {run_id}: AssertionError boom"


class _StubWorktrees:
    def __init__(self) -> None:
        self.commit_calls: list[str] = []
        self.push_calls: list[tuple[str, str]] = []

    async def commit_all(self, worktree: Path, message: str) -> None:
        self.commit_calls.append(message)

    async def push_branch(self, branch: str, remote: str) -> None:
        self.push_calls.append((branch, remote))


class _StubWorkerRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(
        self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None,
    ) -> AgentResult:
        self.calls.append(prompt)
        return AgentResult(ok=True, text="fixed")


@pytest.mark.asyncio
async def test_recovery_returns_immediately_when_checks_pass(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    gh = _StubGh([CheckStatus(all_passed=True, failed_run_ids=[], raw="all green")])
    worker = _StubWorkerRunner()
    engine._worker_runner = worker  # type: ignore[assignment]
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    await engine._run_ci_recovery(
        "r1", "001", gh, PullRequest(number=1), "task/001", tmp_path, max_retries=2,
    )

    assert worker.calls == []
    events = [e for e in engine._store.list_events("r1") if e.type == EventType.CI_CHECK_RESULT]
    assert len(events) == 1
    assert events[0].detail["passed"] is True
    assert not [
        e for e in engine._store.list_events("r1")
        if e.type == EventType.CI_RECOVERY_EXHAUSTED
    ]


@pytest.mark.asyncio
async def test_recovery_fix_pass_then_succeeds(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    gh = _StubGh([
        CheckStatus(all_passed=False, failed_run_ids=["222"], raw="test fail"),
        CheckStatus(all_passed=True, failed_run_ids=[], raw="all green"),
    ])
    worker = _StubWorkerRunner()
    engine._worker_runner = worker  # type: ignore[assignment]
    worktrees = _StubWorktrees()
    engine._worktrees = worktrees  # type: ignore[assignment]

    await engine._run_ci_recovery(
        "r1", "001", gh, PullRequest(number=7), "task/001", tmp_path, max_retries=2,
    )

    assert len(worker.calls) == 1
    assert "CI FAILED" in worker.calls[0]
    assert "AssertionError boom" in worker.calls[0]
    assert len(worktrees.commit_calls) == 1
    assert worktrees.push_calls == [("task/001", "origin")]
    events = [e for e in engine._store.list_events("r1") if e.type == EventType.CI_CHECK_RESULT]
    assert len(events) == 2
    assert events[0].detail["passed"] is False
    assert events[1].detail["passed"] is True
    assert not [
        e for e in engine._store.list_events("r1")
        if e.type == EventType.CI_RECOVERY_EXHAUSTED
    ]


@pytest.mark.asyncio
async def test_recovery_exhausts_after_max_retries(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    gh = _StubGh([CheckStatus(all_passed=False, failed_run_ids=["1"], raw="still red")])
    worker = _StubWorkerRunner()
    engine._worker_runner = worker  # type: ignore[assignment]
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    await engine._run_ci_recovery(
        "r1", "001", gh, PullRequest(number=3), "task/001", tmp_path, max_retries=2,
    )

    assert len(worker.calls) == 2  # ровно max_retries фикс-попыток
    exhausted = [
        e for e in engine._store.list_events("r1")
        if e.type == EventType.CI_RECOVERY_EXHAUSTED
    ]
    assert len(exhausted) == 1
    assert exhausted[0].detail["retries"] == 2


# ── Engine._maybe_run_ci_recovery: gating + safe no-op seam ─────────────────

@pytest.mark.asyncio
async def test_maybe_recovery_skips_when_push_after_merge_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    assert engine._s.push_after_merge is False  # дефолт
    monkeypatch.setattr(
        "harness.scheduler.engine.GitHubCli",
        MagicMock(side_effect=AssertionError("GitHubCli не должен создаваться")),
    )
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    await engine._maybe_run_ci_recovery("r1", "001", "task/001")  # не должно упасть


@pytest.mark.asyncio
async def test_maybe_recovery_skips_when_ci_retry_max_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    engine._s.push_after_merge = True
    monkeypatch.setenv("CI_RETRY_MAX", "0")
    monkeypatch.setattr(
        "harness.scheduler.engine.GitHubCli",
        MagicMock(side_effect=AssertionError("GitHubCli не должен создаваться")),
    )
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    await engine._maybe_run_ci_recovery("r1", "001", "task/001")


@pytest.mark.asyncio
async def test_maybe_recovery_noop_when_no_pr_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обычный случай сегодня (нет ROADMAP 3.14): PR для ветки не существует."""
    engine = _engine(tmp_path)
    engine._s.push_after_merge = True
    monkeypatch.setenv("CI_RETRY_MAX", "1")

    class _NoPrGh:
        def __init__(self, cwd: Path) -> None:
            pass

        async def pr_for_branch(self, branch: str) -> None:
            return None

    monkeypatch.setattr("harness.scheduler.engine.GitHubCli", _NoPrGh)
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    await engine._maybe_run_ci_recovery("r1", "001", "task/001")

    assert not [
        e for e in engine._store.list_events("r1") if e.type == EventType.CI_CHECK_RESULT
    ]


@pytest.mark.asyncio
async def test_maybe_recovery_noop_when_worktree_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    engine._s.push_after_merge = True
    monkeypatch.setenv("CI_RETRY_MAX", "1")

    class _HasPrGh:
        def __init__(self, cwd: Path) -> None:
            pass

        async def pr_for_branch(self, branch: str) -> PullRequest:
            return PullRequest(number=9)

    monkeypatch.setattr("harness.scheduler.engine.GitHubCli", _HasPrGh)
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]
    # tmp_path/.worktrees/task-001 намеренно не создан

    await engine._maybe_run_ci_recovery("r1", "001", "task/001")

    assert not [
        e for e in engine._store.list_events("r1") if e.type == EventType.CI_CHECK_RESULT
    ]


@pytest.mark.asyncio
async def test_maybe_recovery_runs_when_pr_and_worktree_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    engine._s.push_after_merge = True
    monkeypatch.setenv("CI_RETRY_MAX", "1")
    worktree_dir = tmp_path / ".worktrees" / "task-001"
    worktree_dir.mkdir(parents=True)

    class _HasPrGh:
        def __init__(self, cwd: Path) -> None:
            pass

        async def pr_for_branch(self, branch: str) -> PullRequest:
            return PullRequest(number=9)

        async def check_status(self, pr_number: int) -> CheckStatus:
            return CheckStatus(all_passed=True, failed_run_ids=[], raw="ok")

    monkeypatch.setattr("harness.scheduler.engine.GitHubCli", _HasPrGh)
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    await engine._maybe_run_ci_recovery("r1", "001", "task/001")

    events = [e for e in engine._store.list_events("r1") if e.type == EventType.CI_CHECK_RESULT]
    assert len(events) == 1
    assert events[0].detail["passed"] is True


def test_complete_merge_calls_ci_recovery_seam() -> None:
    """Регресс: _complete_merge зовёт _maybe_run_ci_recovery ПОСЛЕ push, ДО remove."""
    src = inspect.getsource(Engine._complete_merge)
    push_idx = src.index("_push_base_if_enabled")
    recovery_idx = src.index("_maybe_run_ci_recovery")
    remove_idx = src.index("_worktrees.remove")
    assert push_idx < recovery_idx < remove_idx


# ── WorktreeManager.push_branch: реальный git (push_base делегирует) ────────

def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


@pytest.mark.asyncio
async def test_push_branch_pushes_arbitrary_branch_to_remote(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _run_git(remote, "init", "-q", "--bare", "-b", "main")

    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "t@t.local")
    _run_git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")
    _run_git(repo, "remote", "add", "origin", str(remote))
    _run_git(repo, "push", "-q", "origin", "main")

    wm = WorktreeManager(repo_root=repo, worktrees_dir=repo / ".worktrees", base_branch="main")
    worktree, branch = await wm.create("001")
    (worktree / "f.txt").write_text("from task\n", encoding="utf-8")
    await wm.commit_all(worktree, "task change")

    outcome = await wm.push_branch(branch, "origin")

    assert outcome.pushed is True
    branches = subprocess.run(
        ["git", "branch", "-r"], cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout
    assert "origin/task/001" in branches
