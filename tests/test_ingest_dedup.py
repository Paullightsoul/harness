"""v2-005: idempotent ingest — повторный вызов с тем же (project, goal, base)
возвращает существующий не-терминальный Run; --force создаёт новый; терминальный
Run (done/failed) не мешает создать новый.
"""
from __future__ import annotations

from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType, RunStatus
from harness.ingest import _goal_hash, ingest_run
from harness.store.repository import Store


def _settings_with_tasks(tmp_path: Path) -> Settings:
    """Settings с минимальными PLAN.md и tasks/task-001.md."""
    tasks = tmp_path / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "task-001.md").write_text(
        "---\nid: \"001\"\ntitle: \"stub\"\nstatus: \"todo\"\nattempts: 0\n---\n# spec\n",
        encoding="utf-8",
    )
    (tmp_path / "PLAN.md").write_text("# plan stub\n", encoding="utf-8")
    return Settings(root=tmp_path)


def test_goal_hash_deterministic() -> None:
    h1 = _goal_hash("api", "добавить X", "main")
    h2 = _goal_hash("api", "добавить X", "main")
    assert h1 == h2
    assert h1 != _goal_hash("api", "добавить Y", "main")
    assert h1 != _goal_hash("web", "добавить X", "main")
    assert h1 != _goal_hash("api", "добавить X", "develop")


def test_ingest_dedup_returns_same_run(tmp_path: Path) -> None:
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    first = ingest_run(settings, store, project="api", goal="цель", base_branch="main")
    second = ingest_run(settings, store, project="api", goal="цель", base_branch="main")
    assert first == second, "повторный ingest должен вернуть тот же run_id"

    # RUN_REUSED event записан
    events = store.list_events(first)
    assert any(e.type == EventType.RUN_REUSED for e in events)


def test_ingest_force_creates_new_run(tmp_path: Path) -> None:
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    first = ingest_run(settings, store, project="api", goal="цель", base_branch="main")
    second = ingest_run(
        settings, store, project="api", goal="цель", base_branch="main", force=True,
    )
    assert first != second, "--force должен создать новый Run"


def test_ingest_after_terminal_creates_new(tmp_path: Path) -> None:
    """Терминальный Run (done) не мешает создать новый с тем же goal."""
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    first = ingest_run(settings, store, project="api", goal="цель", base_branch="main")
    store.set_run_status(first, RunStatus.DONE.value)
    second = ingest_run(settings, store, project="api", goal="цель", base_branch="main")
    assert first != second


def test_ingest_different_goal_creates_new(tmp_path: Path) -> None:
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    a = ingest_run(settings, store, project="api", goal="цель A", base_branch="main")
    b = ingest_run(settings, store, project="api", goal="цель B", base_branch="main")
    assert a != b


def test_run_carries_goal_hash(tmp_path: Path) -> None:
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="api", goal="цель", base_branch="main")
    run = store.get_run(run_id)
    assert run is not None and run.goal_hash
    assert run.goal_hash == _goal_hash("api", "цель", "main")
