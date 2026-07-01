"""Репозиторий: CRUD домена + append-only журнал событий.

Любой переход статуса задачи идёт через transition_task(), который:
  1) проверяет допустимость по конечному автомату,
  2) пишет новый статус,
  3) добавляет событие TASK_TRANSITION.
Так состояние и журнал не расходятся, а Run можно поднять после сбоя.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.domain.enums import EventType, TaskStatus
from harness.domain.models import AgentEvent, Attempt, Event, Review, Run, Task
from harness.domain.state_machine import require_transition
from harness.store.db import connect, init_db


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, db_path: Path) -> None:
        self._conn = connect(db_path)
        init_db(self._conn)
        # Сериализует многошаговые записи (UPDATE+INSERT события) при параллельных
        # durable-шагах в разных потоках. Reentrant: вложенные вызовы безопасны.
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    # ── Runs ────────────────────────────────────────────────────────────────
    def create_run(self, run: Run) -> None:
        self._conn.execute(
            "INSERT INTO runs(id,project,goal,status,base_branch,budget_credits,"
            "spent_credits,goal_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run.id, run.project, run.goal, run.status, run.base_branch,
             run.budget_credits, run.spent_credits, run.goal_hash,
             run.created_at, run.updated_at),
        )
        self.add_event(run.id, EventType.RUN_CREATED, detail={"goal": run.goal})

    def get_run(self, run_id: str) -> Run | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def find_active_run_by_goal_hash(self, goal_hash: str) -> Run | None:
        """v2-005: найти существующий не-терминальный Run с тем же goal_hash.

        Терминальные ('done', 'failed') пропускаем — пользователь явно перезапускает.
        Возвращает первый попавшийся активный (planning/running/paused) или None.
        """
        if not goal_hash:
            return None
        row = self._conn.execute(
            "SELECT * FROM runs WHERE goal_hash=? AND status NOT IN ('done','failed') "
            "ORDER BY created_at DESC LIMIT 1",
            (goal_hash,),
        ).fetchone()
        return _row_to_run(row) if row else None

    def set_run_status(self, run_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, _now(), run_id)
        )

    def add_spend(self, run_id: str, credits: float, *, cost_kind: str = "estimate") -> float:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET spent_credits = spent_credits + ?, updated_at=? WHERE id=?",
                (credits, _now(), run_id),
            )
            row = self._conn.execute(
                "SELECT spent_credits FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            spent = float(row["spent_credits"]) if row else 0.0
            self.add_event(
                run_id, EventType.BUDGET_SPENT,
                detail={"credits": credits, "total": spent, "cost_kind": cost_kind},
            )
            return spent

    # ── Tasks ─────────────────────────────────────────────────────────────────
    def upsert_task(self, task: Task) -> None:
        self._conn.execute(
            "INSERT INTO tasks(id,run_id,title,spec_path,status,depends_on,provides,"
            "complexity,attempts,completion_signals,branch,worktree_path,note,"
            "created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,id) DO UPDATE SET title=excluded.title,"
            "spec_path=excluded.spec_path,depends_on=excluded.depends_on,"
            "provides=excluded.provides,complexity=excluded.complexity,"
            "updated_at=excluded.updated_at",
            (task.id, task.run_id, task.title, task.spec_path, task.status,
             json.dumps(task.depends_on), task.provides, task.complexity, task.attempts,
             task.completion_signals, task.branch, task.worktree_path, task.note,
             task.created_at, task.updated_at),
        )

    def get_task(self, run_id: str, task_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id=? AND id=?", (run_id, task_id)
        ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(self, run_id: str) -> list[Task]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        return [_row_to_task(r) for r in rows]

    def transition_task(self, task: Task, dst: TaskStatus, note: str = "") -> Task:
        """Единственный способ сменить статус задачи. Валидирует по автомату."""
        src = TaskStatus(task.status)
        require_transition(src, dst)
        now = _now()
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, note=?, updated_at=? WHERE run_id=? AND id=?",
                (dst.value, note or task.note, now, task.run_id, task.id),
            )
            self.add_event(
                task.run_id, EventType.TASK_TRANSITION, task_id=task.id,
                detail={"from": src.value, "to": dst.value, "note": note},
            )
        task.status = dst.value
        task.note = note or task.note
        task.updated_at = now
        return task

    def update_task_fields(self, task: Task) -> None:
        self._conn.execute(
            "UPDATE tasks SET attempts=?, completion_signals=?, branch=?, "
            "worktree_path=?, provides=?, updated_at=? WHERE run_id=? AND id=?",
            (task.attempts, task.completion_signals, task.branch, task.worktree_path,
             task.provides, _now(), task.run_id, task.id),
        )

    # ── Attempts / reviews ────────────────────────────────────────────────────
    def start_attempt(self, run_id: str, task_id: str, number: int, model: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO attempts(run_id,task_id,number,model,started_at) VALUES(?,?,?,?,?)",
            (run_id, task_id, number, model, _now()),
        )
        attempt_id = int(cur.lastrowid or 0)
        self.add_event(
            run_id, EventType.ATTEMPT_STARTED, task_id=task_id,
            detail={"attempt": number, "model": model},
        )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        run_id: str,
        task_id: str,
        worker_output: str,
        gates_passed: bool | None,
        verdict: str | None,
        cost_credits: float,
        cost_kind: str = "estimate",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        self._conn.execute(
            "UPDATE attempts SET worker_output=?, gates_passed=?, verdict=?, "
            "cost_credits=?, cost_kind=?, tokens_in=?, tokens_out=?, finished_at=? WHERE id=?",
            (worker_output, _bool_to_int(gates_passed), verdict, cost_credits,
             cost_kind, tokens_in, tokens_out, _now(), attempt_id),
        )
        self.add_event(
            run_id, EventType.ATTEMPT_FINISHED, task_id=task_id,
            detail={"attempt_id": attempt_id, "gates": gates_passed, "verdict": verdict},
        )

    def save_review(self, review: Review) -> None:
        self._conn.execute(
            "INSERT INTO reviews(attempt_id,task_id,verdict,report,feedback,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (review.attempt_id, review.task_id, review.verdict, review.report,
             review.feedback, review.created_at),
        )

    # ── Attempts (read) ───────────────────────────────────────────────────────
    def last_attempt(self, run_id: str, task_id: str) -> Attempt | None:
        """v2-011: последняя завершённая попытка задачи — для token-usage в status.

        Берёт запись с максимальным `number` (не id, т.к. id — autoincrement через
        все задачи run'а). Возвращает None если попыток не было.
        """
        row = self._conn.execute(
            "SELECT * FROM attempts WHERE run_id=? AND task_id=? "
            "ORDER BY number DESC LIMIT 1",
            (run_id, task_id),
        ).fetchone()
        return _row_to_attempt(row) if row else None

    def list_attempts(self, run_id: str, task_id: str) -> list[Attempt]:
        """Все попытки задачи, по возрастанию number."""
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE run_id=? AND task_id=? ORDER BY number",
            (run_id, task_id),
        ).fetchall()
        return [_row_to_attempt(r) for r in rows]

    # ── Events ────────────────────────────────────────────────────────────────
    def add_event(
        self,
        run_id: str,
        type_: EventType,
        *,
        task_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events(run_id,type,task_id,detail,at) VALUES(?,?,?,?,?)",
            (run_id, type_.value, task_id, json.dumps(detail or {}, ensure_ascii=False), _now()),
        )

    def list_events(self, run_id: str, after_id: int = 0) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id)
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    # ── Agent events (v2-009: транскрипт) ─────────────────────────────────────
    def add_agent_events(
        self, run_id: str, task_id: str, attempt: int, events: list[AgentEvent],
    ) -> None:
        """Bulk-insert агент-событий после прогона (пост-прогонный транскрипт)."""
        if not events:
            return
        rows = [
            (run_id, task_id, attempt, e.kind, e.payload_json(), e.at) for e in events
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO agent_events(run_id,task_id,attempt,kind,payload,at) "
                "VALUES(?,?,?,?,?,?)",
                rows,
            )

    def list_agent_events(
        self, run_id: str, task_id: str | None = None, after_id: int = 0,
    ) -> list[AgentEvent]:
        """Транскрипт по задаче (или всему run'у), по возрастанию id."""
        if task_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM agent_events WHERE run_id=? AND task_id=? AND id>? "
                "ORDER BY id",
                (run_id, task_id, after_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM agent_events WHERE run_id=? AND id>? ORDER BY id",
                (run_id, after_id),
            ).fetchall()
        return [_row_to_agent_event(r) for r in rows]

    # ── Metrics (v2-030: pass@k / pass^k) ──────────────────────────────────────
    def _tasks_for_metrics(self, run_id: str) -> list[Task]:
        """Задачи, участвующие в pass@k/pass^k — trivial исключены.

        Trivial tier auto-approve'ится на первых зелёных гейтах без ревьюера
        (v2-019), поэтому его "попытки" не несут сигнала "справился ли агент с
        ревью/фидбэком" — единственное, что измеряют эти метрики.
        """
        return [t for t in self.list_tasks(run_id) if t.complexity != "trivial"]

    def pass_at_k(self, run_id: str, k: int) -> float | None:
        """Доля задач, у которых хотя бы одна из первых `k` попыток получила
        `verdict == "approve"` (ECC `skills/eval-harness`: "справился ли агент
        хотя бы за k попыток"). Задачи без попыток не учитываются в знаменателе.
        `None`, если учитывать нечего (нет задач с попытками).
        """
        considered = 0
        passed = 0
        for task in self._tasks_for_metrics(run_id):
            attempts = self.list_attempts(run_id, task.id)
            if not attempts:
                continue
            considered += 1
            if any(a.verdict == "approve" for a in attempts[:k]):
                passed += 1
        return (passed / considered) if considered else None

    def pass_all_k(self, run_id: str, k: int) -> float | None:
        """Доля задач, у которых ВСЕ первые `k` попыток (или все имеющиеся, если
        их меньше `k`) прошли гейты — "pass^k", строгая версия pass@k (там любая
        одна из k, тут все).

        Использует `gates_passed` (детерминированный "оракул истины"), а не
        `verdict`: в последовательном escalation-цикле approve возможен максимум
        на одной (последней, финальной) попытке задачи — "все k попыток
        approve" был бы вырожденным (≈0 при k>1). Gates — сигнал, который может
        повторяться много раз подряд, поэтому pass^k осмысленно измеряет
        стабильность («код рабочий с самого начала», а не «в итоге дожали»).
        """
        considered = 0
        passed = 0
        for task in self._tasks_for_metrics(run_id):
            attempts = self.list_attempts(run_id, task.id)
            if not attempts:
                continue
            considered += 1
            if all(a.gates_passed for a in attempts[:k]):
                passed += 1
        return (passed / considered) if considered else None


# ── мапперы строк ─────────────────────────────────────────────────────────────
def _row_to_run(r: sqlite3.Row) -> Run:
    cols = r.keys()
    goal_hash = r["goal_hash"] if "goal_hash" in cols else ""
    return Run(
        id=r["id"], project=r["project"], goal=r["goal"], status=r["status"],
        base_branch=r["base_branch"], budget_credits=r["budget_credits"],
        spent_credits=r["spent_credits"], goal_hash=goal_hash,
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _row_to_task(r: sqlite3.Row) -> Task:
    cols = r.keys()
    return Task(
        id=r["id"], run_id=r["run_id"], title=r["title"], spec_path=r["spec_path"],
        status=r["status"], depends_on=json.loads(r["depends_on"]), provides=r["provides"],
        complexity=r["complexity"] if "complexity" in cols else "normal",
        attempts=r["attempts"],
        completion_signals=r["completion_signals"] if "completion_signals" in cols else 0,
        branch=r["branch"], worktree_path=r["worktree_path"],
        note=r["note"], created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _row_to_event(r: sqlite3.Row) -> Event:
    return Event(
        id=r["id"], run_id=r["run_id"], type=r["type"], task_id=r["task_id"],
        detail=json.loads(r["detail"]), at=r["at"],
    )


def _row_to_attempt(r: sqlite3.Row) -> Attempt:
    """Маппер attempts-строки. Новые колонки (cost_kind, tokens) могут
    отсутствовать в старых БД до миграции — getattr-безопасно."""
    cols = r.keys()
    return Attempt(
        id=r["id"], task_id=r["task_id"], run_id=r["run_id"], number=r["number"],
        model=r["model"], worker_output=r["worker_output"],
        gates_passed=_int_to_bool(r["gates_passed"]) if "gates_passed" in cols else None,
        verdict=r["verdict"], cost_credits=r["cost_credits"],
        started_at=r["started_at"],
        finished_at=r["finished_at"] if "finished_at" in cols else "",
        cost_kind=r["cost_kind"] if "cost_kind" in cols else "estimate",
        tokens_in=r["tokens_in"] if "tokens_in" in cols else 0,
        tokens_out=r["tokens_out"] if "tokens_out" in cols else 0,
    )


def _int_to_bool(v: int | None) -> bool | None:
    if v is None:
        return None
    return bool(v)


def _row_to_agent_event(r: sqlite3.Row) -> AgentEvent:
    return AgentEvent(
        kind=r["kind"],
        payload=json.loads(r["payload"]) if r["payload"] else {},
        at=r["at"],
        id=r["id"],
        task_id=r["task_id"],
    )


def _bool_to_int(value: bool | None) -> int | None:
    return None if value is None else int(value)
