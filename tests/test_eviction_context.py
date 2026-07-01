"""v2-029: merge queue with eviction context (Ralphinho "Merge Queue with Eviction").

На merge conflict раньше был слепой retry: `note="merge conflict"`, feedback
пустой. Теперь `WorktreeManager.merge_to_base` перед откатом (`merge --abort`)
собирает конфликтующие файлы + diff с conflict-маркерами (`MergeOutcome`), движок
пишет их на диск (`_record_merge_eviction`) и событие `MERGE_EVICTED`; первая
попытка следующего захода читает их (`_consume_eviction_context`, one-shot) как
стартовый feedback воркеру.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import EventType
from harness.domain.models import Run, Task
from harness.gates.base import GateResult
from harness.scheduler.engine import Engine, _format_eviction_context
from harness.store.repository import Store
from harness.worktree.manager import MergeOutcome, WorktreeManager


def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


class _StubWorktreesConflict:
    """Мок WorktreeManager.merge_to_base — всегда конфликт с заданным outcome."""

    def __init__(self, outcome: MergeOutcome) -> None:
        self.outcome = outcome
        self.merge_calls: list[str] = []

    async def merge_to_base(self, branch: str, title: str) -> MergeOutcome:
        self.merge_calls.append(branch)
        return self.outcome


# ── MergeOutcome / _format_eviction_context (pure) ──────────────────────────

def test_merge_outcome_default_eviction_fields_empty() -> None:
    out = MergeOutcome(merged=True, conflict=False, output="ok")
    assert out.conflicting_files == []
    assert out.conflict_diff == ""


def test_format_eviction_context_includes_files_and_diff() -> None:
    outcome = MergeOutcome(
        merged=False, conflict=True, output="CONFLICT",
        conflicting_files=["src/a.py", "src/b.py"],
        conflict_diff="<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> task/x\n",
    )
    text = _format_eviction_context(outcome)
    assert "=== MERGE CONFLICT" in text
    assert "src/a.py" in text
    assert "src/b.py" in text
    assert "<<<<<<< HEAD" in text
    assert ">>>>>>> task/x" in text


def test_format_eviction_context_no_files_placeholder() -> None:
    outcome = MergeOutcome(merged=False, conflict=True, output="x")
    text = _format_eviction_context(outcome)
    assert "(не определены)" in text


def test_format_eviction_context_truncates_long_diff() -> None:
    big_diff = "\n".join(f"line {i}" for i in range(500))
    outcome = MergeOutcome(
        merged=False, conflict=True, output="x",
        conflicting_files=["f.py"], conflict_diff=big_diff,
    )
    text = _format_eviction_context(outcome)
    assert "обрезано" in text
    assert "line 499" in text  # хвост сохранён
    assert "line 0" not in text  # начало обрезано


# ── Engine._record_merge_eviction / _consume_eviction_context ──────────────

@pytest.mark.asyncio
async def test_merge_conflict_writes_eviction_file_and_event(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    engine._store.upsert_task(Task(
        id="001", run_id="r1", title="t", spec_path="x", status="review",
    ))
    outcome = MergeOutcome(
        merged=False, conflict=True, output="CONFLICT (content): Merge conflict in x.py",
        conflicting_files=["x.py"], conflict_diff="<<<<<<< HEAD\na\n=======\nb\n>>>>>>> t\n",
    )
    stub = _StubWorktreesConflict(outcome)
    engine._worktrees = stub  # type: ignore[assignment]

    await engine._merge("r1", "001", "task/001")

    task = engine._store.get_task("r1", "001")
    assert task is not None
    assert task.status == "ready"
    assert task.note == "merge conflict"

    events = [e for e in engine._store.list_events("r1") if e.type == EventType.MERGE_EVICTED]
    assert events
    assert events[0].detail["conflicting_files"] == ["x.py"]

    evict_path = engine._eviction_context_path("001")
    assert evict_path.exists()
    content = evict_path.read_text(encoding="utf-8")
    assert "MERGE CONFLICT" in content
    assert "x.py" in content


@pytest.mark.asyncio
async def test_consume_eviction_context_reads_and_deletes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    path = engine._eviction_context_path("001")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("=== MERGE CONFLICT ===\nsomething", encoding="utf-8")

    text = engine._consume_eviction_context("001")
    assert "MERGE CONFLICT" in text
    assert not path.exists()

    # Второй вызов — файла уже нет, пусто.
    assert engine._consume_eviction_context("001") == ""


def test_worker_prompt_carries_eviction_feedback_forward(tmp_path: Path) -> None:
    """Acceptance: feedback (из eviction context) доходит до worker-промпта."""
    engine = _engine(tmp_path)
    spec = tmp_path / "task-001.md"
    spec.write_text("# spec\n", encoding="utf-8")
    task = Task(id="001", run_id="r1", title="t", spec_path=str(spec), status="ready")

    outcome = MergeOutcome(
        merged=False, conflict=True, output="x",
        conflicting_files=["src/auth.py"],
        conflict_diff="<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> task/001\n",
    )
    eviction_feedback = _format_eviction_context(outcome)
    prompt = engine._worker_prompt(task, feedback=eviction_feedback)

    assert "=== MERGE CONFLICT" in prompt
    assert "src/auth.py" in prompt


@pytest.mark.asyncio
async def test_successful_merge_does_not_write_eviction_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    engine._store.upsert_task(Task(
        id="001", run_id="r1", title="t", spec_path="x", status="review",
    ))

    class _StubWorktreesOk:
        async def merge_to_base(self, branch: str, title: str) -> MergeOutcome:
            return MergeOutcome(merged=True, conflict=False, output="ok")

        async def remove(self, task_id: str) -> None:
            pass

        async def push_base(self, remote: str) -> object:
            raise AssertionError("push disabled by default")

    engine._worktrees = _StubWorktreesOk()  # type: ignore[assignment]

    class _OkGate:
        async def check(self, cwd: Path) -> GateResult:
            return GateResult(passed=True, output="ok")

    engine._gate = _OkGate()  # type: ignore[assignment]

    await engine._merge("r1", "001", "task/001")

    assert not engine._eviction_context_path("001").exists()
    events = [e for e in engine._store.list_events("r1") if e.type == EventType.MERGE_EVICTED]
    assert events == []


# ── Регресс: wiring в _process_task / approve_merge ─────────────────────────

def test_process_task_consumes_eviction_context_for_initial_feedback() -> None:
    src = inspect.getsource(Engine._process_task)
    assert "_consume_eviction_context" in src
    feedback_line_idx = src.index("feedback = self._consume_eviction_context")
    loop_idx = src.index("for attempt_no in range")
    assert feedback_line_idx < loop_idx


def test_approve_merge_also_records_eviction_on_conflict() -> None:
    src = inspect.getsource(Engine.approve_merge)
    assert "_record_merge_eviction" in src


# ── Интеграция с реальным git: настоящий конфликт двух веток ────────────────

def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@harness.local")
    _run_git(repo, "config", "user.name", "harness-test")
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")


@pytest.mark.asyncio
async def test_real_git_merge_conflict_populates_eviction_fields(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    wm = WorktreeManager(repo_root=repo, worktrees_dir=repo / ".worktrees", base_branch="main")

    # task/a и task/b создаются ДО любого мержа — обе от одной и той же точки main,
    # поэтому независимые правки одной строки дадут настоящий конфликт при мерже
    # второй ветки (первая уже продвинула base).
    wt_a, branch_a = await wm.create("a")
    wt_b, branch_b = await wm.create("b")

    (wt_a / "f.txt").write_text("from A\n", encoding="utf-8")
    await wm.commit_all(wt_a, "task a change")

    (wt_b / "f.txt").write_text("from B\n", encoding="utf-8")
    await wm.commit_all(wt_b, "task b change")

    outcome_a = await wm.merge_to_base(branch_a, "merge a")
    assert outcome_a.merged is True

    outcome_b = await wm.merge_to_base(branch_b, "merge b")
    assert outcome_b.merged is False
    assert outcome_b.conflict is True
    assert "f.txt" in outcome_b.conflicting_files
    assert "<<<<<<<" in outcome_b.conflict_diff
    assert "from A" in outcome_b.conflict_diff or "from B" in outcome_b.conflict_diff

    # merge --abort реально выполнен: конфликтующих (unmerged) файлов в base не осталось.
    unmerged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(repo), check=True,
        capture_output=True, text=True,
    )
    assert unmerged.stdout.strip() == ""
    assert (repo / "f.txt").read_text(encoding="utf-8") == "from A\n"
