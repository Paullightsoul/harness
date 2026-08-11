"""ADR-0013: per-project work history (docs/history/) written by the harness."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_tasktool_controller import _controller, _report

from harness.domain.enums import EventType
from harness.journal import JournalEntry, list_recent_entries, render_entry, write_entry


def _entry(**overrides: object) -> JournalEntry:
    base: dict[str, object] = {
        "title": "001 — demo task",
        "source": "harness-task",
        "project": "demo",
        "run_id": "run-1",
        "task_id": "001",
        "decisions": ("review: approve",),
        "done": ("файлы: src/x.py", "Provides: API"),
        "iterations": ("попыток: 2", "доработка: fix lint"),
    }
    base.update(overrides)
    return JournalEntry(**base)  # type: ignore[arg-type]


def test_render_entry_sections_and_redaction() -> None:
    text = render_entry(
        _entry(dialog="Обсудили подход. token: sk-abcdefghijklmnopqrstuvwx")
    )
    assert text.startswith("---\n")
    assert "source: harness-task" in text
    assert 'task: "001"' in text
    assert "## Решения" in text
    assert "## Что сделано" in text
    assert "## Итерации" in text
    assert "## Диалог" in text
    assert "sk-abcdefghijklmnopqrstuvwx" not in text  # redacted


def test_write_entry_unique_files_and_listing(tmp_path: Path) -> None:
    moment = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    first = write_entry(tmp_path, _entry(), now=moment)
    second = write_entry(tmp_path, _entry(), now=moment)  # same second → suffix

    assert first.parent == tmp_path / "docs" / "history"
    assert first.name == "2026-07-28-120000-001.md"
    assert second.name == "2026-07-28-120000-001-2.md"
    recent = list_recent_entries(tmp_path, limit=5)
    assert recent[0].name >= recent[-1].name
    assert len(recent) == 2


@pytest.mark.asyncio
async def test_task_and_run_journal_written_on_done(tmp_path: Path) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "Provides:\n- API\n\nHARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    # Production TaskLifecycleService journals the verdict; the test stub does not.
    store.add_event(
        run_id,
        EventType.REVIEW_RESULT,
        task_id="001",
        detail={"verdict": "approve"},
    )
    await controller.advance(run_id)
    finished = await controller.advance(run_id, approved_merge=True)
    assert finished["status"] == "done"

    history = sorted((controller.repo_root / "docs" / "history").glob("*.md"))
    assert len(history) == 2  # task entry + run summary
    texts = [path.read_text(encoding="utf-8") for path in history]
    task_text = next(text for text in texts if "source: harness-task" in text)
    run_text = next(text for text in texts if "source: harness-run" in text)
    assert "review: approve" in task_text
    assert "попыток: 1" in task_text
    assert "src/task_1.py" in task_text
    assert "Run завершён: goal" in run_text
    assert "001 — task 1" in run_text


@pytest.mark.asyncio
async def test_journal_idempotent_and_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, store, _ = _controller(tmp_path)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    worker = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, worker, "HARNESS_DONE")
    await controller.advance(run_id)
    reviewer = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, reviewer, "VERDICT: APPROVE")
    await controller.advance(run_id)
    await controller.advance(run_id, approved_merge=True)

    task = store.get_task(run_id, "001")
    assert task is not None
    controller._maybe_write_journal(run_id, task)  # crash-replay: no duplicate
    history = list((controller.repo_root / "docs" / "history").glob("*.md"))
    assert len(history) == 2

    monkeypatch.setenv("HARNESS_PROJECT_JOURNAL", "0")
    disabled, _, _ = _controller(tmp_path / "off")
    started_off = await disabled.start(project="demo", goal="g", approve_plan=True)
    run_off = str(started_off["run_id"])
    worker_off = disabled.next(run_off, "agent")["dispatches"][0]
    _report(disabled, tmp_path / "off", worker_off, "HARNESS_DONE")
    await disabled.advance(run_off)
    reviewer_off = disabled.next(run_off, "agent")["dispatches"][0]
    _report(disabled, tmp_path / "off", reviewer_off, "VERDICT: APPROVE")
    await disabled.advance(run_off)
    finished = await disabled.advance(run_off, approved_merge=True)

    assert finished["status"] == "done"
    assert not (disabled.repo_root / "docs" / "history").exists()
