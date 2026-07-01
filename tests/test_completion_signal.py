"""v2-021: completion signal — воркер выдает `HARNESS_DONE` magic-phrase,
N=3 consecutive с зелёными гейтами → auto-APPROVE без reviewer. CHANGES или
отсутствие signal сбрасывает counter.
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType
from harness.domain.models import Run, Task
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


def test_completion_signal_event_type_in_enum() -> None:
    assert EventType.COMPLETION_SIGNAL.value == "completion_signal"


def test_task_model_has_completion_signals_field(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    t = store.get_task("r1", "001")
    assert t is not None
    assert t.completion_signals == 0


def test_update_task_fields_persists_completion_signals(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    t = store.get_task("r1", "001")
    assert t is not None
    t.completion_signals = 2
    store.update_task_fields(t)
    t2 = store.get_task("r1", "001")
    assert t2 is not None
    assert t2.completion_signals == 2


def test_migrate_adds_completion_signals_column(tmp_path: Path) -> None:
    """Старая БД без completion_signals — миграция добавляет колонку."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
    CREATE TABLE runs (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL,
        status TEXT NOT NULL, base_branch TEXT NOT NULL DEFAULT 'main',
        budget_credits REAL, spent_credits REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE tasks (
        id TEXT NOT NULL, run_id TEXT NOT NULL, title TEXT NOT NULL,
        spec_path TEXT NOT NULL, status TEXT NOT NULL,
        depends_on TEXT NOT NULL DEFAULT '[]', provides TEXT NOT NULL DEFAULT '',
        complexity TEXT NOT NULL DEFAULT 'normal', attempts INTEGER NOT NULL DEFAULT 0,
        branch TEXT NOT NULL DEFAULT '', worktree_path TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (run_id, id)
    );
    """)
    conn.close()
    # init_db должна мигрировать
    store = Store(db)
    cols = {row["name"] for row in store._conn.execute("PRAGMA table_info(tasks)")}  # noqa: SLF001
    assert "completion_signals" in cols


def test_engine_source_has_harness_done_detection() -> None:
    """Регресс: engine._process_task детектит HARNESS_DONE и threshold."""
    src = inspect.getsource(Engine._process_task)
    assert "HARNESS_DONE" in src
    assert "HARNESS_COMPLETION_THRESHOLD" in src
    assert "completion_signals" in src
    assert "signals_ok" in src


def test_engine_source_resets_counter_on_no_signal() -> None:
    """Если воркер не выдал HARNESS_DONE — counter сбрасывается."""
    src = inspect.getsource(Engine._process_task)
    # Сброс в else-ветке
    assert "completion_signals = 0" in src
