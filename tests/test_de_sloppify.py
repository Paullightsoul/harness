"""v2-028: de-sloppify — отдельный cleanup-pass после зелёных гейтов, перед reviewer.

Отдельный focused-агент (дешёвая модель) чистит diff воркера в своём контексте
(debug-print, мёртвый код, over-defensive checks) до того, как ревьюер его увидит.
Пропускается для trivial tier, на красных гейтах, при пустом diff, через
HARNESS_DESLOPPIFY=0. Если cleanup ломает гейты — откат.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import EventType
from harness.domain.models import Run, Task
from harness.gates.base import GateResult
from harness.runner.base import AgentResult
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator", "de-sloppify"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


class _StubWorktrees:
    """Мок WorktreeManager: diff/uncommitted_diff по сценарию, счётчики вызовов."""

    def __init__(
        self, *, diff: str = "diff --git a/x.py b/x.py\n+print(1)\n",
        cleanup_diff: str = "", commit_calls: list[str] | None = None,
    ) -> None:
        self.diff = diff
        self.cleanup_diff = cleanup_diff
        self.commit_calls: list[str] = commit_calls if commit_calls is not None else []
        self.discard_calls = 0

    async def diff_against_base(self, branch: str) -> str:
        return self.diff

    async def uncommitted_diff(self, worktree: Path) -> str:
        return self.cleanup_diff

    async def commit_all(self, worktree: Path, message: str) -> None:
        self.commit_calls.append(message)

    async def discard_uncommitted(self, worktree: Path) -> None:
        self.discard_calls += 1


class _StubGate:
    """Мок ProfileGate: отдаёт результаты по очереди из `results`."""

    def __init__(self, results: list[GateResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def check(self, cwd: Path) -> GateResult:
        self.calls += 1
        idx = min(self.calls - 1, len(self._results) - 1)
        return self._results[idx]


class _StubRunner:
    def __init__(self, result: AgentResult | None = None) -> None:
        self.result = result or AgentResult(ok=True, text="De-sloppify: removed print()")
        self.calls: list[dict[str, object]] = []

    async def run(
        self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None,
    ) -> AgentResult:
        self.calls.append({"prompt": prompt, "model": model, "cwd": cwd})
        return self.result


def _task(complexity: str = "small") -> Task:
    return Task(id="001", run_id="r1", title="t", spec_path="x", status="gating",
                complexity=complexity)


def test_event_type_registered() -> None:
    assert EventType.DE_SLOPPIFIED.value == "de_sloppified"


@pytest.mark.asyncio
async def test_skips_trivial_tier(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))

    worktrees = _StubWorktrees()
    engine._worktrees = worktrees  # type: ignore[assignment]
    runner = _StubRunner()
    engine._worker_runner = runner  # type: ignore[assignment]

    gate = GateResult(passed=True, output="ok")
    out = await engine._run_de_sloppify("r1", "001", _task("trivial"), tmp_path, "task/001", gate)

    assert out is gate
    assert runner.calls == []


@pytest.mark.asyncio
async def test_skips_when_gates_failed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    runner = _StubRunner()
    engine._worker_runner = runner  # type: ignore[assignment]
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    gate = GateResult(passed=False, output="lint FAIL")
    out = await engine._run_de_sloppify("r1", "001", _task(), tmp_path, "task/001", gate)

    assert out is gate
    assert runner.calls == []


@pytest.mark.asyncio
async def test_skips_when_disabled_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_DESLOPPIFY", "0")
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    runner = _StubRunner()
    engine._worker_runner = runner  # type: ignore[assignment]
    engine._worktrees = _StubWorktrees()  # type: ignore[assignment]

    gate = GateResult(passed=True, output="ok")
    out = await engine._run_de_sloppify("r1", "001", _task(), tmp_path, "task/001", gate)

    assert out is gate
    assert runner.calls == []


@pytest.mark.asyncio
async def test_skips_when_diff_empty(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    runner = _StubRunner()
    engine._worker_runner = runner  # type: ignore[assignment]
    engine._worktrees = _StubWorktrees(diff="")  # type: ignore[assignment]

    gate = GateResult(passed=True, output="ok")
    out = await engine._run_de_sloppify("r1", "001", _task(), tmp_path, "task/001", gate)

    assert out is gate
    assert runner.calls == []


@pytest.mark.asyncio
async def test_applies_cleanup_and_commits_when_gates_stay_green(tmp_path: Path) -> None:
    """Воркер оставил print() — de-sloppify снёс, гейты остались зелёными → commit."""
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    runner = _StubRunner(AgentResult(ok=True, text="De-sloppify: removed print()"))
    engine._worker_runner = runner  # type: ignore[assignment]
    worktrees = _StubWorktrees(cleanup_diff="diff --git a/x.py b/x.py\n-print(1)\n")
    engine._worktrees = worktrees  # type: ignore[assignment]
    engine._gate = _StubGate([GateResult(passed=True, output="ok")])  # type: ignore[assignment]

    gate = GateResult(passed=True, output="ok")
    out = await engine._run_de_sloppify("r1", "001", _task(), tmp_path, "task/001", gate)

    assert out.passed is True
    assert len(worktrees.commit_calls) == 1
    assert "de-sloppify" in worktrees.commit_calls[0]
    assert worktrees.discard_calls == 0
    events = [e for e in engine._store.list_events("r1") if e.type == EventType.DE_SLOPPIFIED]
    assert events
    assert events[0].detail["applied"] is True
    assert events[0].detail["reverted"] is False


@pytest.mark.asyncio
async def test_reverts_when_cleanup_breaks_gates(tmp_path: Path) -> None:
    """Cleanup сломал гейты → откат, задача продолжает с исходным (зелёным) gate."""
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    runner = _StubRunner(AgentResult(ok=True, text="De-sloppify: removed helper"))
    engine._worker_runner = runner  # type: ignore[assignment]
    worktrees = _StubWorktrees(cleanup_diff="diff --git a/x.py b/x.py\n-def used(): ...\n")
    engine._worktrees = worktrees  # type: ignore[assignment]
    engine._gate = _StubGate([GateResult(passed=False, output="test FAIL")])  # type: ignore[assignment]

    original_gate = GateResult(passed=True, output="ok")
    out = await engine._run_de_sloppify("r1", "001", _task(), tmp_path, "task/001", original_gate)

    assert out is original_gate  # откат — задача продолжает как будто ничего не было
    assert worktrees.discard_calls == 1
    assert worktrees.commit_calls == []
    events = [e for e in engine._store.list_events("r1") if e.type == EventType.DE_SLOPPIFIED]
    assert events
    assert events[0].detail["applied"] is False
    assert events[0].detail["reverted"] is True


@pytest.mark.asyncio
async def test_no_op_when_agent_makes_no_changes(tmp_path: Path) -> None:
    """Агент отработал, но ничего не поменял (cleanup_diff пуст) — no-op, событие есть."""
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    runner = _StubRunner(AgentResult(ok=True, text="De-sloppify: без изменений"))
    engine._worker_runner = runner  # type: ignore[assignment]
    worktrees = _StubWorktrees(cleanup_diff="")
    engine._worktrees = worktrees  # type: ignore[assignment]

    gate = GateResult(passed=True, output="ok")
    out = await engine._run_de_sloppify("r1", "001", _task(), tmp_path, "task/001", gate)

    assert out is gate
    assert worktrees.commit_calls == []
    assert worktrees.discard_calls == 0
    events = [e for e in engine._store.list_events("r1") if e.type == EventType.DE_SLOPPIFIED]
    assert events
    assert events[0].detail["applied"] is False


@pytest.mark.asyncio
async def test_prompt_includes_diff_and_gates(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine._store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    runner = _StubRunner()
    engine._worker_runner = runner  # type: ignore[assignment]
    worktrees = _StubWorktrees(diff="diff --git a/x.py b/x.py\n+print('debug')\n")
    engine._worktrees = worktrees  # type: ignore[assignment]

    gate = GateResult(passed=True, output="ok")
    await engine._run_de_sloppify("r1", "001", _task(), tmp_path, "task/001", gate)

    assert runner.calls, "agent должен был быть вызван"
    prompt = str(runner.calls[0]["prompt"])
    assert "print('debug')" in prompt
    assert "=== GIT DIFF" in prompt
    assert "ГЕЙТЫ ПРОЕКТА" in prompt
    assert runner.calls[0]["model"] == "auto"


def test_engine_source_calls_de_sloppify_before_guard() -> None:
    """Регресс: _run_de_sloppify зовётся после GATE_RESULT и до diff_against_base/changed_files."""
    src = inspect.getsource(Engine._process_task)
    gate_idx = src.index("GATE_RESULT")
    desloppify_idx = src.index("_run_de_sloppify")
    guard_idx = src.index("protected_violations")
    assert gate_idx < desloppify_idx < guard_idx


def test_de_sloppify_prompt_file_exists() -> None:
    text = (Path(__file__).resolve().parent.parent / "prompts" / "de-sloppify.md").read_text(
        encoding="utf-8",
    )
    assert "де-слоппиф" in text.lower() or "de-sloppify" in text.lower()
    assert "VERDICT" not in text  # это не reviewer — вердикт не выносит
