"""v2-015: pre-flight understanding-brief + NEEDS_CLARIFICATION.

Воркер перед кодом возвращает машиночитанный brief (JSON в fenced-блоке).
Если `missing_context` непустой → задача в `NEEDS_CLARIFICATION`, движок ждёт.
Opt-in через `HARNESS_PREFLIGHT=1` (для trivial-задач — overhead).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import EventType, TaskStatus
from harness.domain.models import Run, Task
from harness.domain.state_machine import RECOVERABLE_STATUSES, can_transition
from harness.runner.base import AgentResult
from harness.scheduler.engine import Engine
from harness.store.repository import Store
from harness.tasks_io.brief import parse_brief
from harness.worktree.manager import WorktreeManager

# ── parse_brief ───────────────────────────────────────────────────────────────

def test_parse_brief_fenced_block() -> None:
    out = (
        "Размышления...\n```brief\n"
        '{"understand": "add JWT auth", "files_i_will_touch": ["src/auth.py"], '
        '"acceptance_i_will_satisfy": ["AC1"], "assumptions": ["HS256"], '
        '"missing_context": ["secret location"]}\n'
        "```\ndone"
    )
    b = parse_brief(out)
    assert b.parsed
    assert b.understand == "add JWT auth"
    assert b.files_i_will_touch == ["src/auth.py"]
    assert b.missing_context == ["secret location"]
    assert b.needs_clarification


def test_parse_brief_empty_missing_context_no_clarification() -> None:
    out = (
        "```brief\n"
        '{"understand": "ok", "files_i_will_touch": ["x.py"], '
        '"acceptance_i_will_satisfy": ["AC1"], "assumptions": [], "missing_context": []}\n'
        "```"
    )
    b = parse_brief(out)
    assert b.parsed
    assert not b.needs_clarification


def test_parse_brief_fallback_raw_json() -> None:
    """Без fenced-блока — пробуем сырой JSON."""
    out = (
        'вот brief: {"understand": "x", "files_i_will_touch": [], '
        '"acceptance_i_will_satisfy": [], "assumptions": [], "missing_context": ["y"]}'
    )
    b = parse_brief(out)
    assert b.parsed
    assert b.missing_context == ["y"]


def test_parse_brief_unparseable_returns_not_parsed() -> None:
    b = parse_brief("воркер ничего не выдал, просто текст")
    assert not b.parsed
    assert not b.needs_clarification


def test_parse_brief_malformed_json() -> None:
    b = parse_brief("```brief\n{not valid json}\n```")
    assert not b.parsed


def test_parse_brief_ignores_unknown_fields() -> None:
    out = '```brief\n{"understand": "x", "unknown_field": 42, "missing_context": []}\n```'
    b = parse_brief(out)
    assert b.parsed
    assert b.understand == "x"


# ── state machine ─────────────────────────────────────────────────────────────

def test_needs_clarification_transitions() -> None:
    """READY → NEEDS_CLARIFICATION → READY (после ответа) → RUNNING."""
    assert can_transition(TaskStatus.READY, TaskStatus.NEEDS_CLARIFICATION)
    assert can_transition(TaskStatus.NEEDS_CLARIFICATION, TaskStatus.READY)
    assert can_transition(TaskStatus.NEEDS_CLARIFICATION, TaskStatus.BLOCKED)


def test_needs_clarification_is_recoverable() -> None:
    """Если процесс упал в NEEDS_CLARIFICATION — восстановится в READY."""
    assert TaskStatus.NEEDS_CLARIFICATION in RECOVERABLE_STATUSES


# ── engine integration ────────────────────────────────────────────────────────

def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


@pytest.mark.asyncio
async def test_preflight_emits_brief_event_and_clarification_on_missing_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HARNESS_PREFLIGHT=1 + brief с missing_context → NEEDS_CLARIFICATION + event."""
    monkeypatch.setenv("HARNESS_PREFLIGHT", "1")
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    spec = tmp_path / "task-001.md"
    spec.write_text("# spec\n", encoding="utf-8")
    store.upsert_task(
        Task(id="001", run_id="r1", title="t", spec_path=str(spec), status="ready"),
    )

    # Мок worker_runner — возвращает brief с missing_context.
    captured: list[str] = []

    class _MockRunner:
        async def run(self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None):
            captured.append(prompt)
            return AgentResult(
                ok=True,
                text='```brief\n{"understand":"x","files_i_will_touch":["a.py"],'
                     '"acceptance_i_will_satisfy":["AC1"],"assumptions":[],'
                     '"missing_context":["где лежит JWT secret?"]}\n```',
                cost_credits=0.5, cost_kind="estimated",
            )

    engine._worker_runner = _MockRunner()  # type: ignore[assignment]
    # worktree manager mock
    engine._worktrees = WorktreeManager(  # type: ignore[assignment]
        repo_root=tmp_path, worktrees_dir=tmp_path / ".wt", base_branch="main",
    )

    await engine._run_preflight("r1", "001", store.get_task("r1", "001"), tmp_path)

    # UNDERSTANDING_BRIEF event записан
    brief_events = [e for e in store.list_events("r1") if e.type == EventType.UNDERSTANDING_BRIEF]
    assert brief_events
    assert brief_events[0].detail["parsed"] is True
    assert "JWT secret" in brief_events[0].detail["missing_context"][0]

    # Задача переведена в NEEDS_CLARIFICATION
    t = store.get_task("r1", "001")
    assert t is not None
    assert t.status == TaskStatus.NEEDS_CLARIFICATION.value


@pytest.mark.asyncio
async def test_preflight_no_clarification_when_missing_context_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """brief с пустым missing_context → задача остаётся ready, no clarification."""
    monkeypatch.setenv("HARNESS_PREFLIGHT", "1")
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    spec = tmp_path / "task-001.md"
    spec.write_text("# spec\n", encoding="utf-8")
    store.upsert_task(
        Task(id="001", run_id="r1", title="t", spec_path=str(spec), status="ready"),
    )


    class _MockRunner:
        async def run(self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None):
            return AgentResult(
                ok=True,
                text='```brief\n{"understand":"ok","files_i_will_touch":["a.py"],'
                     '"acceptance_i_will_satisfy":["AC1"],"assumptions":[],'
                     '"missing_context":[]}\n```',
                cost_credits=0.3, cost_kind="estimated",
            )

    engine._worker_runner = _MockRunner()  # type: ignore[assignment]
    engine._worktrees = WorktreeManager(  # type: ignore[assignment]
        repo_root=tmp_path, worktrees_dir=tmp_path / ".wt", base_branch="main",
    )

    result = await engine._run_preflight("r1", "001", store.get_task("r1", "001"), tmp_path)
    assert result is None  # clarification не нужно
    t = store.get_task("r1", "001")
    assert t is not None
    assert t.status == TaskStatus.READY.value  # осталась в ready


def test_understanding_brief_event_type_in_enum() -> None:
    assert EventType.UNDERSTANDING_BRIEF.value == "understanding_brief"
    assert EventType.CLARIFICATION_ANSWERED.value == "clarification_answered"
