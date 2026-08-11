from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import RunStatus, TaskStatus
from harness.domain.models import Run, Task
from harness.gates.base import GateResult
from harness.store.repository import Store
from harness.tasktool.lifecycle import (
    TaskLifecycleService,
    owned_scope_violations,
    parse_review_verdict,
)
from harness.worktree.manager import WorktreeManager


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "base")
    profile = path / ".harness" / "project.toml"
    profile.parent.mkdir()
    profile.write_text('[[gates]]\nid = "ok"\ncmd = "true"\n', encoding="utf-8")
    _git(path, "add", ".harness/project.toml")
    _git(path, "commit", "-q", "-m", "profile")
    return path


def test_lifecycle_prepare_finalize_review_and_human_merge(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    root.mkdir()
    store = Store(root / "state.db")
    store.create_run(
        Run("r1", "p", "goal", RunStatus.RUNNING.value, base_branch="main")
    )
    store.upsert_task(
        Task("001", "r1", "task", str(root / "spec.md"), TaskStatus.PENDING.value)
    )
    settings = Settings(root=root, use_worktrees=True, ship_mode="merge")
    service = TaskLifecycleService(settings, store, repo)

    task = asyncio.run(service.prepare_task("r1", "001"))
    worktree = Path(task.worktree_path)
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
    result = asyncio.run(
        service.finalize_worker("r1", "001", "Provides:\nfeature.txt\n")
    )
    assert result.changed_files == ["feature.txt"]
    assert result.provides == "feature.txt"

    assert asyncio.run(service.run_gates("r1", "001")).passed
    reviewed = service.apply_review("r1", "001", "**VERDICT: APPROVE**")
    assert reviewed.task_status == TaskStatus.MERGE_QUEUE.value
    paused = asyncio.run(service.merge("r1", "001"))
    assert paused.action == "merge_required"
    assert (repo / "feature.txt").exists() is False

    merged = asyncio.run(service.merge("r1", "001", approved=True))
    assert merged.task_status == TaskStatus.DONE.value
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
    store.close()


def test_lifecycle_inplace_manual_ship_skips_worktree_and_merge(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    root.mkdir()
    store = Store(root / "state.db")
    store.create_run(
        Run("r1", "p", "goal", RunStatus.RUNNING.value, base_branch="main")
    )
    store.upsert_task(
        Task("001", "r1", "task", str(root / "spec.md"), TaskStatus.PENDING.value)
    )
    settings = Settings(root=root, use_worktrees=False, ship_mode="manual")
    service = TaskLifecycleService(settings, store, repo)

    task = asyncio.run(service.prepare_task("r1", "001"))
    assert Path(task.worktree_path) == repo.resolve()
    assert not (repo / ".worktrees").exists()
    (repo / "feature.txt").write_text("done\n", encoding="utf-8")
    result = asyncio.run(
        service.finalize_worker(
            "r1",
            "001",
            "Provides:\nfeature.txt\n",
            files_owned=("feature.txt",),
        )
    )
    assert result.changed_files == ["feature.txt"]
    assert asyncio.run(service.run_gates("r1", "001")).passed
    reviewed = service.apply_review("r1", "001", "**VERDICT: APPROVE**")
    assert reviewed.action == "manual_ship"
    assert reviewed.task_status == TaskStatus.DONE.value
    # Working tree left dirty for the human — no auto-commit/merge.
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "done\n"
    store.close()


def test_finalize_captures_real_diff_in_place(tmp_path: Path) -> None:
    """In-place mode has nothing committed, so new files still need to reach review."""
    repo = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    root.mkdir()
    store = Store(root / "state.db")
    store.create_run(
        Run("r1", "p", "goal", RunStatus.RUNNING.value, base_branch="main")
    )
    store.upsert_task(
        Task("001", "r1", "task", str(root / "spec.md"), TaskStatus.PENDING.value)
    )
    settings = Settings(root=root, use_worktrees=False, ship_mode="manual")
    service = TaskLifecycleService(settings, store, repo)

    asyncio.run(service.prepare_task("r1", "001"))
    (repo / "added.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    (repo / "README.md").write_text("base\nedited line\n", encoding="utf-8")
    result = asyncio.run(
        service.finalize_worker(
            "r1", "001", "done\n", files_owned=("added.py", "README.md")
        )
    )

    assert "+def hello():" in result.diff  # untracked file rendered as an addition
    assert "+edited line" in result.diff  # tracked file diffed against HEAD
    # In-place mode must not stage on the human's behalf.
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    assert staged.stdout.strip() == ""
    store.close()


def test_finalize_captures_diff_against_base_in_worktree_mode(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    root.mkdir()
    store = Store(root / "state.db")
    store.create_run(
        Run("r1", "p", "goal", RunStatus.RUNNING.value, base_branch="main")
    )
    store.upsert_task(
        Task("001", "r1", "task", str(root / "spec.md"), TaskStatus.PENDING.value)
    )
    settings = Settings(root=root, use_worktrees=True, ship_mode="merge")
    service = TaskLifecycleService(settings, store, repo)

    task = asyncio.run(service.prepare_task("r1", "001"))
    (Path(task.worktree_path) / "feature.txt").write_text("done\n", encoding="utf-8")
    result = asyncio.run(service.finalize_worker("r1", "001", "done\n"))

    assert "feature.txt" in result.diff
    assert "+done" in result.diff
    store.close()


def test_review_diff_truncation_is_announced(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo, repo / ".worktrees", "main")
    (repo / "big.txt").write_text("line\n" * 5_000, encoding="utf-8")

    diff = asyncio.run(manager.review_diff(repo, ["big.txt"], max_bytes=500))

    assert len(diff) < 1_000
    assert "diff truncated" in diff


def test_review_diff_ignores_generated_artifacts(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo, repo / ".worktrees", "main")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "mod.pyc").write_text("junk\n", encoding="utf-8")

    diff = asyncio.run(manager.review_diff(repo, ["__pycache__/mod.pyc"]))

    assert diff == ""


def test_finalize_worker_enforces_files_owned_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    root.mkdir()
    store = Store(root / "state.db")
    store.create_run(
        Run("r1", "p", "goal", RunStatus.RUNNING.value, base_branch="main")
    )
    store.upsert_task(
        Task("001", "r1", "task", str(root / "spec.md"), TaskStatus.PENDING.value)
    )
    settings = Settings(root=root, use_worktrees=True, ship_mode="merge")
    service = TaskLifecycleService(settings, store, repo)
    task = asyncio.run(service.prepare_task("r1", "001"))
    worktree = Path(task.worktree_path)
    (worktree / "allowed.py").write_text("ok\n", encoding="utf-8")
    (worktree / "evil.py").write_text("nope\n", encoding="utf-8")

    result = asyncio.run(
        service.finalize_worker(
            "r1",
            "001",
            "HARNESS_DONE",
            files_owned=("allowed.py",),
        )
    )

    assert result.action == "scope_violation"
    assert "evil.py" in result.violations
    assert store.get_task("r1", "001").status == TaskStatus.READY.value  # type: ignore[union-attr]
    store.close()


def test_review_parser_uses_last_well_formed_decision() -> None:
    assert parse_review_verdict("CHANGES first\n**APPROVE**") is not None
    assert parse_review_verdict("Final VERDICT: APPROVE") is not None
    assert parse_review_verdict("APROVE") is None


def test_missing_base_branch_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo, repo / ".worktrees", "missing")
    with pytest.raises(ValueError, match="base branch"):
        asyncio.run(manager.create("001"))


def test_merge_refuses_failed_base_checkout(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    manager = WorktreeManager(repo, repo / ".worktrees", "main")

    class FailingCheckoutGit:
        async def run(
            self,
            *args: str,
            cwd: Path | None = None,
        ) -> tuple[int, str]:
            assert args[:2] == ("checkout", "main")
            return 1, "checkout failed"

    manager._git = FailingCheckoutGit()  # type: ignore[assignment]  # test seam
    result = asyncio.run(manager.merge_to_base("task/001", "task"))

    assert result.merged is False
    assert result.conflict is False
    assert "checkout failed" in result.output


def test_post_integration_gate_failure_never_marks_done(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    root.mkdir()
    store = Store(root / "state.db")
    store.create_run(
        Run("r1", "p", "goal", RunStatus.RUNNING.value, base_branch="main")
    )
    store.upsert_task(
        Task("001", "r1", "task", str(root / "spec.md"), TaskStatus.READY.value)
    )
    service = TaskLifecycleService(
        Settings(root=root, use_worktrees=True, ship_mode="merge"), store, repo
    )
    task = asyncio.run(service.prepare_task("r1", "001"))
    (Path(task.worktree_path) / "feature.txt").write_text("done\n", encoding="utf-8")
    asyncio.run(service.finalize_worker("r1", "001", "HARNESS_DONE"))
    asyncio.run(service.run_gates("r1", "001"))
    service.apply_review("r1", "001", "VERDICT: APPROVE")

    checked: list[Path] = []

    class FailingGate:
        async def check(
            self,
            cwd: Path,
            *,
            task_id: str | None = None,
            full: bool = False,
        ) -> GateResult:
            checked.append(cwd)
            return GateResult(False, f"failed in {cwd}")

    service._gate = FailingGate()  # type: ignore[assignment]  # test seam
    result = asyncio.run(service.merge("r1", "001", approved=True))

    assert result.action == "post_integration_gate_failed"
    assert checked == [repo]
    assert store.get_task("r1", "001").status == TaskStatus.POST_MERGE_FIX.value  # type: ignore[union-attr]
    assert store.get_run("r1").status == RunStatus.PAUSED.value  # type: ignore[union-attr]


def test_premerge_full_gate_runs_in_task_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    root = tmp_path / "control"
    root.mkdir()
    store = Store(root / "state.db")
    store.create_run(
        Run("r1", "p", "goal", RunStatus.RUNNING.value, base_branch="main")
    )
    store.upsert_task(
        Task("001", "r1", "task", str(root / "spec.md"), TaskStatus.READY.value)
    )
    service = TaskLifecycleService(
        Settings(root=root, use_worktrees=True, ship_mode="merge"), store, repo
    )
    task = asyncio.run(service.prepare_task("r1", "001"))
    (Path(task.worktree_path) / "feature.txt").write_text("done\n", encoding="utf-8")
    asyncio.run(service.finalize_worker("r1", "001", "HARNESS_DONE"))
    asyncio.run(service.run_gates("r1", "001"))
    service.apply_review("r1", "001", "VERDICT: APPROVE")
    checked: list[Path] = []

    class RecordingGate:
        async def check(
            self,
            cwd: Path,
            *,
            task_id: str | None = None,
            full: bool = False,
        ) -> GateResult:
            checked.append(cwd)
            return GateResult(True, "passed")

    service._gate = RecordingGate()  # type: ignore[assignment]  # test seam
    result = asyncio.run(service.run_gates("r1", "001", full=True))

    assert result.passed is True
    assert checked == [Path(task.worktree_path)]


def test_owned_scope_exact_file_does_not_prefix_match() -> None:
    assert owned_scope_violations(["allowed.py"], ["allowed.py"]) == []
    assert owned_scope_violations(["allowed.py.evil"], ["allowed.py"]) == [
        "allowed.py.evil"
    ]
    assert owned_scope_violations(["src/pkg/a.py"], ["src/pkg/"]) == []
    assert owned_scope_violations(["src/pkg"], ["src/pkg/"]) == []
    assert owned_scope_violations(["src/other/a.py"], ["src/pkg/"]) == [
        "src/other/a.py"
    ]
    assert owned_scope_violations(["src/a.py"], ["src/*.py"]) == []
    assert owned_scope_violations(["src/nested/a.py"], ["src/*.py"]) == [
        "src/nested/a.py"
    ]
    assert owned_scope_violations(["src/pkg/a.py"], ["src/pkg/**"]) == []
