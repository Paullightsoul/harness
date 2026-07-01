"""Подключение к SQLite и инициализация схемы."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = Path(__file__).with_name("schema.sql")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: durable-плейн (DBOS) исполняет шаги в worker-потоках и
    # делит один Store. Атомарность многошаговых операций обеспечивает RLock в Store,
    # WAL — конкурентные чтения, busy_timeout — короткие гонки на запись.
    conn = sqlite3.connect(
        str(db_path), isolation_level=None, check_same_thread=False
    )  # autocommit; транзакции вручную
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Добавляем колонки к существующим таблицам (CREATE TABLE IF NOT EXISTS не делает этого).

    Идемпотентно: проверяем PRAGMA table_info перед ALTER. Новые колонки — NULL/DEFAULT,
    чтобы старые строки не ломались.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "goal_hash" not in cols:
        conn.execute("ALTER TABLE runs ADD COLUMN goal_hash TEXT NOT NULL DEFAULT ''")

    # v2-003: cost-учёт для attempts
    att_cols = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}
    if "cost_kind" not in att_cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN cost_kind TEXT NOT NULL DEFAULT 'estimate'")
    if "tokens_in" not in att_cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN tokens_in INTEGER NOT NULL DEFAULT 0")
    if "tokens_out" not in att_cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN tokens_out INTEGER NOT NULL DEFAULT 0")

    # v2-021: completion signals counter
    task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "completion_signals" not in task_cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN completion_signals INTEGER NOT NULL DEFAULT 0"
        )
