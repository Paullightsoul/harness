"""SQLite-состояние прогона: статусы задач, запуски агентов, события.

Даёт восстановление после краша и источник данных для `status`-дашборда.
Соединение открывается на каждую операцию (безопасно при доступе из потоков
через asyncio.to_thread). Используется busy_timeout для file-lock SQLite.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    worker_type TEXT,
    layer       INTEGER,
    depends_on  TEXT,
    file_globs  TEXT,
    status      TEXT,
    attempts    INTEGER,
    parent      TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT,
    attempt    INTEGER,
    role       TEXT,
    verdict    TEXT,
    gates_ok   INTEGER,
    started_at TEXT,
    ended_at   TEXT,
    log_path   TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT,
    task_id TEXT,
    kind    TEXT,
    message TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def upsert_task(
        self,
        task_id: str,
        title: str,
        worker_type: str,
        layer: int,
        depends_on: List[str],
        file_globs: List[str],
        status: str,
        attempts: int,
        parent: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, title, worker_type, layer, depends_on,
                                   file_globs, status, attempts, parent, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    worker_type=excluded.worker_type,
                    layer=excluded.layer,
                    depends_on=excluded.depends_on,
                    file_globs=excluded.file_globs,
                    parent=excluded.parent,
                    updated_at=excluded.updated_at
                """,
                (
                    task_id,
                    title,
                    worker_type,
                    layer,
                    json.dumps(depends_on),
                    json.dumps(file_globs),
                    status,
                    attempts,
                    parent,
                    _now(),
                ),
            )

    def set_status(self, task_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status, _now(), task_id),
            )

    def set_attempts(self, task_id: str, attempts: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE tasks SET attempts=?, updated_at=? WHERE id=?",
                (attempts, _now(), task_id),
            )

    def get_status(self, task_id: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return str(row["status"]) if row else "todo"

    def statuses(self) -> Dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id, status FROM tasks").fetchall()
            return {str(r["id"]): str(r["status"]) for r in rows}

    def record_run(
        self,
        task_id: str,
        attempt: int,
        role: str,
        verdict: str,
        gates_ok: bool,
        started_at: str,
        log_path: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO runs (task_id, attempt, role, verdict, gates_ok,
                                  started_at, ended_at, log_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    attempt,
                    role,
                    verdict,
                    1 if gates_ok else 0,
                    started_at,
                    _now(),
                    log_path,
                ),
            )

    def log_event(self, task_id: str, kind: str, message: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events (ts, task_id, kind, message) VALUES (?, ?, ?, ?)",
                (_now(), task_id, kind, message),
            )

    def snapshot(self) -> List[Dict[str, object]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, title, worker_type, layer, status, attempts, updated_at "
                "FROM tasks ORDER BY layer, id"
            ).fetchall()
            return [dict(r) for r in rows]
