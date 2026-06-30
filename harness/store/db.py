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
