"""v2-024: SHARED_TASK_NOTES.md — мост между попытками воркера.

Воркер читает notes в начале попытки (через промпт), пишет обновление в output
через `=== NOTES UPDATE ===` маркер. Движок append'ит в worktree/SHARED_TASK_NOTES.md.
Следующая попытка видит прогресс — не слепой retry.
"""
from __future__ import annotations

from pathlib import Path

from harness.config import Settings
from harness.domain.models import Task
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


def test_worker_prompt_includes_notes_block(tmp_path: Path) -> None:
    """Если передать notes — в промпте есть SHARED TASK NOTES блок."""
    engine = _engine(tmp_path)
    spec = tmp_path / "task-001.md"
    spec.write_text("# spec\n", encoding="utf-8")
    task = Task(id="001", run_id="r1", title="t", spec_path=str(spec), status="ready")
    prompt = engine._worker_prompt(task, feedback="", notes="## Attempt 1\nпрогресс X")
    assert "=== SHARED TASK NOTES" in prompt
    assert "прогресс X" in prompt


def test_worker_prompt_without_notes_omits_block(tmp_path: Path) -> None:
    """Без notes — блока нет."""
    engine = _engine(tmp_path)
    spec = tmp_path / "task-001.md"
    spec.write_text("# spec\n", encoding="utf-8")
    task = Task(id="001", run_id="r1", title="t", spec_path=str(spec), status="ready")
    prompt = engine._worker_prompt(task, feedback="", notes="")
    assert "SHARED TASK NOTES" not in prompt


def test_update_shared_notes_appends_to_file(tmp_path: Path) -> None:
    """=== NOTES UPDATE === блок append'ится в SHARED_TASK_NOTES.md."""
    engine = _engine(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    output = "done\n=== NOTES UPDATE ===\nсделал X, осталось Y\n=== irrelevant ===\n"
    engine._update_shared_notes(worktree, "001", 1, output)
    notes = worktree / "SHARED_TASK_NOTES.md"
    assert notes.exists()
    content = notes.read_text(encoding="utf-8")
    assert "## Attempt 1" in content
    assert "сделал X, осталось Y" in content
    # === irrelevant === обрезано
    assert "irrelevant" not in content


def test_update_shared_notes_no_marker_no_file(tmp_path: Path) -> None:
    """Без маркера === NOTES UPDATE === — файл не создаётся."""
    engine = _engine(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    engine._update_shared_notes(worktree, "001", 1, "воркер просто текст без notes update")
    notes = worktree / "SHARED_TASK_NOTES.md"
    assert not notes.exists()


def test_update_shared_notes_appends_across_attempts(tmp_path: Path) -> None:
    """Вторая попытка — append к существующему файлу."""
    engine = _engine(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    engine._update_shared_notes(worktree, "001", 1, "=== NOTES UPDATE ===\nattempt 1 notes")
    engine._update_shared_notes(worktree, "001", 2, "=== NOTES UPDATE ===\nattempt 2 notes")
    content = (worktree / "SHARED_TASK_NOTES.md").read_text(encoding="utf-8")
    assert "## Attempt 1" in content
    assert "attempt 1 notes" in content
    assert "## Attempt 2" in content
    assert "attempt 2 notes" in content


def test_worker_prompt_read_section_in_template() -> None:
    """prompts/worker.md упоминает SHARED_TASK_NOTES.md."""
    text = (Path(__file__).resolve().parent.parent / "prompts" / "worker.md").read_text(
        encoding="utf-8",
    )
    assert "SHARED_TASK_NOTES.md" in text
