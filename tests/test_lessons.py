"""v2-025: lessons-learned — extractor + multi-run memory в cmd_plan.

После терминального статуса (DONE/BLOCKED) для medium/large задач оркестратор
пишет lesson в `brain/lessons/<project>/`. `cmd_plan` читает последние N и
инжектит в промпт оркестратора.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import RunStatus
from harness.domain.models import Run, Task
from harness.lessons.extractor import (
    format_lessons_for_plan,
    lessons_dir,
    list_recent_lessons,
    write_lesson_file,
)
from harness.runner.base import AgentResult
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def test_write_and_list_lessons(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_lesson_file(brain, "api", "001", "## Pattern\nX\n## What worked\nY\n")
    write_lesson_file(brain, "api", "002", "## Pattern\nZ\n")
    files = list_recent_lessons(brain, "api", limit=5)
    assert len(files) == 2
    names = [f.name for f in files]
    assert any("001" in n for n in names)
    assert any("002" in n for n in names)


def test_list_recent_lessons_empty(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    assert list_recent_lessons(brain, "nope") == []


def test_list_recent_lessons_limit(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    for i in range(5):
        write_lesson_file(brain, "api", f"00{i}", f"## Pattern\n{i}\n")
    files = list_recent_lessons(brain, "api", limit=2)
    assert len(files) == 2


def test_format_lessons_for_plan_includes_content(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    write_lesson_file(brain, "api", "001", "## Pattern\nauth bug\n## What worked\nrefresh tokens\n")
    files = list_recent_lessons(brain, "api")
    block = format_lessons_for_plan(files)
    assert "=== PAST LESSONS" in block
    assert "auth bug" in block
    assert "refresh tokens" in block


def test_format_lessons_empty_returns_empty() -> None:
    assert format_lessons_for_plan([]) == ""


def test_format_lessons_truncates_long_content(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    long = "## Pattern\n" + "X" * 800
    write_lesson_file(brain, "api", "001", long)
    block = format_lessons_for_plan(list_recent_lessons(brain, "api"))
    assert "..." in block
    assert len(block) < 1000  # обрезано


def test_lessons_dir_creates_path(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    d = lessons_dir(brain, "newproject")
    assert d.exists()
    assert d.parent.name == "lessons"


@pytest.mark.asyncio
async def test_engine_extract_lesson_skips_trivial(tmp_path: Path) -> None:
    """trivial задача — lesson не извлекается (шум)."""

    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator", "lesson"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    engine = Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status=RunStatus.RUNNING.value))
    store.upsert_task(Task(
        id="001", run_id="r1", title="t", spec_path=str(tmp_path / "s.md"),
        status="done", complexity="trivial",
    ))
    (tmp_path / "s.md").write_text("# spec\n", encoding="utf-8")

    called: list[str] = []

    class _MockOrch:
        async def run(self, prompt, *, model, cwd, log_path=None):
            called.append("called")
            return AgentResult(ok=True, text="## Pattern\nlesson\n")

    engine._orchestrator_runner = _MockOrch()  # type: ignore[assignment]
    await engine._extract_lesson("r1", "001", outcome="DONE")
    assert called == []  # trivial — не звали
