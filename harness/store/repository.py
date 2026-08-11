"""Репозиторий: CRUD домена + append-only журнал событий.

Любой переход статуса задачи идёт через transition_task(), который:
  1) проверяет допустимость по конечному автомату,
  2) пишет новый статус,
  3) добавляет событие TASK_TRANSITION.
Так состояние и журнал не расходятся, а Run можно поднять после сбоя.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from harness.domain.enums import (
    DispatchStatus,
    EventType,
    ModelTier,
    ResourceClass,
    Role,
    TaskStatus,
)
from harness.domain.models import AgentEvent, Attempt, Event, Review, Run, Task
from harness.domain.state_machine import require_transition
from harness.store.db import connect, init_db
from harness.tasktool.models import DispatchEnvelope, TaskStage

_DEFAULT_LEASE_MINUTES = 60


def default_lease_duration() -> timedelta:
    """Lease TTL for a claimed dispatch.

    A Task Tool job routinely runs far longer than the old hard-coded 5 minutes,
    and there is no heartbeat from inside the job — so a short TTL made
    ``recover_stale_dispatches`` reset live work back to ``pending`` and hand the
    same task (and the same ``files_owned``) to a second agent. The dispatcher's
    own pull loop renews leases via :meth:`Store.renew_leases`, so this value only
    has to outlive the gap between two ``next`` calls plus the slowest job.
    """
    raw = (os.environ.get("HARNESS_DISPATCH_LEASE_MINUTES") or "").strip()
    try:
        minutes = int(raw) if raw else _DEFAULT_LEASE_MINUTES
    except ValueError:
        minutes = _DEFAULT_LEASE_MINUTES
    return timedelta(minutes=max(1, minutes))


_DISPATCH_CREATED = "dispatch_created"
_DISPATCH_CLAIMED = "dispatch_claimed"
_DISPATCH_COMPLETED = "dispatch_completed"
_DISPATCH_FAILED = "dispatch_failed"
_DISPATCH_RECOVERED = "dispatch_recovered"

APPLICATION_APPLYING = "applying"
APPLICATION_APPLIED = "applied"


@dataclass(frozen=True, slots=True)
class DispatchApplication:
    """Durable receipt that a succeeded dispatch was applied exactly once."""

    dispatch_id: str
    run_id: str
    task_id: str
    status: str
    stage: str
    detail: dict[str, Any]
    created_at: str
    updated_at: str


class DispatchConflictError(RuntimeError):
    """A dispatch update conflicts with its durable state."""


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

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Keep multi-statement writes atomic while retaining autocommit elsewhere."""
        self._conn.execute("SAVEPOINT store_write")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK TO SAVEPOINT store_write")
            self._conn.execute("RELEASE SAVEPOINT store_write")
            raise
        else:
            self._conn.execute("RELEASE SAVEPOINT store_write")

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
            "SELECT * FROM runs WHERE goal_hash=? AND status NOT IN "
            "('done','failed','aborted') "
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

    # ── TaskTool dispatches ───────────────────────────────────────────────────
    def create_dispatch(self, dispatch: DispatchEnvelope) -> None:
        """Persist one immutable transport envelope and journal its creation."""
        with self._lock, self._transaction():
            self._insert_dispatch(dispatch)
            self.add_event(
                dispatch.run_id,
                _DISPATCH_CREATED,
                task_id=dispatch.task_id,
                detail={"dispatch_id": dispatch.dispatch_id, "stage": dispatch.stage.value},
            )

    def create_or_assert_dispatch(self, dispatch: DispatchEnvelope) -> DispatchEnvelope:
        """Create a dispatch or no-op when an identical envelope already exists.

        Status/ownership/path/model/contract mutations for an existing ID raise
        ``DispatchConflictError``. Lifecycle transitions must use claim/finish APIs.
        """
        with self._lock, self._transaction():
            row = self._conn.execute(
                "SELECT * FROM dispatches WHERE dispatch_id=?",
                (dispatch.dispatch_id,),
            ).fetchone()
            if row is None:
                self._insert_dispatch(dispatch)
                self.add_event(
                    dispatch.run_id,
                    _DISPATCH_CREATED,
                    task_id=dispatch.task_id,
                    detail={
                        "dispatch_id": dispatch.dispatch_id,
                        "stage": dispatch.stage.value,
                    },
                )
                return dispatch
            existing = _row_to_dispatch(row)
            if not _dispatch_identity_equal(existing, dispatch):
                raise DispatchConflictError(
                    f"dispatch {dispatch.dispatch_id} is immutable; "
                    "refusing to rewrite status/ownership/path/model/contracts"
                )
            return existing

    def upsert_dispatch(self, dispatch: DispatchEnvelope) -> None:
        """Replay-safe create-or-assert; identical replay is a no-op."""
        self.create_or_assert_dispatch(dispatch)

    def _insert_dispatch(self, dispatch: DispatchEnvelope) -> None:
        self._conn.execute(
            "INSERT INTO dispatches("
            "dispatch_id,run_id,task_id,protocol_version,role,stage,status,prompt_path,"
            "result_path,model,model_tier,repo,worktree,files_owned,read_only_context,"
            "frozen_contracts,acceptance,source_document_paths,dependencies,skill_paths,"
            "resource_class,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _dispatch_values(dispatch),
        )

    def get_dispatch(self, dispatch_id: str) -> DispatchEnvelope | None:
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        return _row_to_dispatch(row) if row else None

    def list_dispatches(
        self,
        run_id: str | None = None,
        *,
        task_id: str | None = None,
        status: DispatchStatus | str | None = None,
    ) -> list[DispatchEnvelope]:
        clauses: list[str] = []
        values: list[str] = []
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(run_id)
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(task_id)
        if status is not None:
            clauses.append("status=?")
            values.append(status.value if isinstance(status, DispatchStatus) else status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM dispatches{where} ORDER BY created_at, dispatch_id",
            values,
        ).fetchall()
        return [_row_to_dispatch(row) for row in rows]

    def claim_pending_dispatches(
        self,
        *,
        agent_id: str,
        limit: int,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        lease_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> list[DispatchEnvelope]:
        """Atomically claim the oldest pending dispatches, optionally within one run.

        ``dispatch_id`` pins the claim to one specific row. Admission decides
        against a candidate's ``resource_class``; without pinning, a concurrent
        claimer could take that row first and this call would silently claim a
        different one — admitting, say, a HEAVY job under a decision made for a
        STANDARD one. An empty result then means "that one is gone", not "the
        queue is empty".
        """
        if not agent_id:
            raise ValueError("agent_id must be non-empty")
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []
        current = _timestamp(now)
        ttl = lease_duration if lease_duration is not None else default_lease_duration()
        lease_expires = _timestamp(_as_datetime(current) + ttl)
        run_clause = " AND run_id=?" if run_id is not None else ""
        if dispatch_id is not None:
            run_clause += " AND dispatch_id=?"
        selection_values: list[object] = [DispatchStatus.PENDING.value]
        if run_id is not None:
            selection_values.append(run_id)
        if dispatch_id is not None:
            selection_values.append(dispatch_id)
        selection_values.append(limit)
        with self._lock, self._transaction():
            rows = self._conn.execute(
                "UPDATE dispatches SET status=?,agent_id=?,claimed_at=?,lease_expires_at=?,"
                "error=NULL,updated_at=? WHERE dispatch_id IN ("
                f"SELECT dispatch_id FROM dispatches WHERE status=?{run_clause} "
                "ORDER BY created_at,dispatch_id LIMIT ?"
                ") AND status=? RETURNING *",
                (
                    DispatchStatus.CLAIMED.value,
                    agent_id,
                    current,
                    lease_expires,
                    current,
                    *selection_values,
                    DispatchStatus.PENDING.value,
                ),
            ).fetchall()
            rows.sort(key=lambda row: (str(row["created_at"]), str(row["dispatch_id"])))
            for row in rows:
                self.add_event(
                    str(row["run_id"]),
                    _DISPATCH_CLAIMED,
                    task_id=str(row["task_id"]),
                    detail={"dispatch_id": row["dispatch_id"], "agent_id": agent_id},
                )
        return [_row_to_dispatch(row) for row in rows]

    def claim_dispatches(
        self,
        *,
        agent_id: str,
        limit: int,
        run_id: str | None = None,
        lease_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> list[DispatchEnvelope]:
        return self.claim_pending_dispatches(
            agent_id=agent_id,
            limit=limit,
            run_id=run_id,
            lease_duration=lease_duration,
            now=now,
        )

    def mark_dispatch_running(
        self,
        dispatch_id: str,
        *,
        agent_id: str,
        lease_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> DispatchEnvelope:
        current = _timestamp(now)
        with self._lock, self._transaction():
            row = self._require_dispatch_row(dispatch_id)
            status = DispatchStatus(row["status"])
            if status is DispatchStatus.RUNNING and row["agent_id"] == agent_id:
                return _row_to_dispatch(row)
            if status is not DispatchStatus.CLAIMED or row["agent_id"] != agent_id:
                raise DispatchConflictError(
                    f"dispatch {dispatch_id} cannot be marked running by {agent_id}"
                )
            lease_expires = row["lease_expires_at"]
            if lease_duration is not None:
                lease_expires = _timestamp(_as_datetime(current) + lease_duration)
            updated = self._conn.execute(
                "UPDATE dispatches SET status=?,lease_expires_at=?,updated_at=? "
                "WHERE dispatch_id=? AND status=? AND agent_id=? RETURNING *",
                (
                    DispatchStatus.RUNNING.value,
                    lease_expires,
                    current,
                    dispatch_id,
                    DispatchStatus.CLAIMED.value,
                    agent_id,
                ),
            ).fetchone()
            if updated is None:
                raise DispatchConflictError(f"dispatch {dispatch_id} changed while starting")
        return _row_to_dispatch(updated)

    def complete_dispatch(
        self,
        dispatch_id: str,
        *,
        agent_id: str,
        result_path: str,
        now: datetime | None = None,
    ) -> DispatchEnvelope:
        if not result_path:
            raise ValueError("result_path must be non-empty")
        return self._finish_dispatch(
            dispatch_id,
            status=DispatchStatus.SUCCEEDED,
            agent_id=agent_id,
            result_path=result_path,
            error=None,
            event_type=_DISPATCH_COMPLETED,
            now=now,
        )

    def fail_dispatch(
        self,
        dispatch_id: str,
        *,
        agent_id: str,
        error: str,
        now: datetime | None = None,
    ) -> DispatchEnvelope:
        return self._finish_dispatch(
            dispatch_id,
            status=DispatchStatus.FAILED,
            agent_id=agent_id,
            result_path=None,
            error=error,
            event_type=_DISPATCH_FAILED,
            now=now,
        )

    def cancel_dispatch(
        self,
        dispatch_id: str,
        *,
        error: str = "",
        now: datetime | None = None,
    ) -> DispatchEnvelope:
        current = _timestamp(now)
        with self._lock, self._transaction():
            row = self._require_dispatch_row(dispatch_id)
            status = DispatchStatus(row["status"])
            if status is DispatchStatus.CANCELLED and (row["error"] or "") == error:
                return _row_to_dispatch(row)
            if status in {
                DispatchStatus.SUCCEEDED,
                DispatchStatus.FAILED,
                DispatchStatus.CANCELLED,
            }:
                raise DispatchConflictError(f"dispatch {dispatch_id} is already {status.value}")
            updated = self._conn.execute(
                "UPDATE dispatches SET status=?,error=?,lease_expires_at=NULL,updated_at=? "
                "WHERE dispatch_id=? AND status=? RETURNING *",
                (
                    DispatchStatus.CANCELLED.value,
                    error or None,
                    current,
                    dispatch_id,
                    status.value,
                ),
            ).fetchone()
            if updated is None:
                raise DispatchConflictError(f"dispatch {dispatch_id} changed while cancelling")
            self.add_event(
                str(updated["run_id"]),
                _DISPATCH_FAILED,
                task_id=str(updated["task_id"]),
                detail={"dispatch_id": dispatch_id, "status": DispatchStatus.CANCELLED.value},
            )
        return _row_to_dispatch(updated)

    def renew_leases(
        self,
        *,
        agent_id: str,
        run_id: str | None = None,
        lease_duration: timedelta | None = None,
        now: datetime | None = None,
    ) -> list[DispatchEnvelope]:
        """Push back the lease on live work still held by ``agent_id``.

        Task Tool jobs cannot heartbeat from the inside, so the dispatcher's pull
        loop is the liveness signal: every ``next`` call proves the dispatcher is
        alive and renews its in-flight leases. If the chat dies, renewals stop and
        ``recover_stale_dispatches`` reclaims the work as designed.
        """
        if not agent_id:
            raise ValueError("agent_id must be non-empty")
        current = _timestamp(now)
        ttl = lease_duration if lease_duration is not None else default_lease_duration()
        lease_expires = _timestamp(_as_datetime(current) + ttl)
        run_clause = " AND run_id=?" if run_id is not None else ""
        values: list[object] = [lease_expires, current, agent_id]
        if run_id is not None:
            values.append(run_id)
        with self._lock, self._transaction():
            rows = self._conn.execute(
                "UPDATE dispatches SET lease_expires_at=?,updated_at=? "
                f"WHERE agent_id=?{run_clause} AND status IN (?,?) RETURNING *",
                (
                    *values,
                    DispatchStatus.CLAIMED.value,
                    DispatchStatus.RUNNING.value,
                ),
            ).fetchall()
        return [_row_to_dispatch(row) for row in rows]

    def recover_stale_dispatches(
        self,
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> list[DispatchEnvelope]:
        """Return expired claimed/running work to FIFO pending state.

        ``run_id`` scopes recovery to one run; without it a single dispatcher's
        pull loop would also reset leases belonging to other runs in the store.
        """
        current = _timestamp(now)
        run_clause = " AND run_id=?" if run_id is not None else ""
        scope: list[object] = [run_id] if run_id is not None else []
        with self._lock, self._transaction():
            rows = self._conn.execute(
                "UPDATE dispatches SET status=?,agent_id=NULL,error=NULL,claimed_at=NULL,"
                f"lease_expires_at=NULL,updated_at=? WHERE status IN (?,?){run_clause} "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=? RETURNING *",
                (
                    DispatchStatus.PENDING.value,
                    current,
                    DispatchStatus.CLAIMED.value,
                    DispatchStatus.RUNNING.value,
                    *scope,
                    current,
                ),
            ).fetchall()
            rows.sort(key=lambda row: (str(row["created_at"]), str(row["dispatch_id"])))
            for row in rows:
                self.add_event(
                    str(row["run_id"]),
                    _DISPATCH_RECOVERED,
                    task_id=str(row["task_id"]),
                    detail={"dispatch_id": row["dispatch_id"]},
                )
        return [_row_to_dispatch(row) for row in rows]

    def get_dispatch_application(self, dispatch_id: str) -> DispatchApplication | None:
        row = self._conn.execute(
            "SELECT * FROM dispatch_applications WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        return _row_to_application(row) if row else None

    def list_dispatch_applications(
        self,
        run_id: str,
        *,
        status: str | None = None,
    ) -> list[DispatchApplication]:
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_applications WHERE run_id=? "
                "ORDER BY created_at, dispatch_id",
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM dispatch_applications WHERE run_id=? AND status=? "
                "ORDER BY created_at, dispatch_id",
                (run_id, status),
            ).fetchall()
        return [_row_to_application(row) for row in rows]

    def begin_dispatch_application(
        self,
        *,
        dispatch_id: str,
        run_id: str,
        task_id: str,
        stage: str = "",
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> DispatchApplication:
        """Mark a succeeded dispatch as applying. Idempotent for applying/applied."""
        current = _timestamp(now)
        payload = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        with self._lock, self._transaction():
            existing = self._conn.execute(
                "SELECT * FROM dispatch_applications WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if existing is not None:
                return _row_to_application(existing)
            self._conn.execute(
                "INSERT INTO dispatch_applications("
                "dispatch_id,run_id,task_id,status,stage,detail,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    dispatch_id,
                    run_id,
                    task_id,
                    APPLICATION_APPLYING,
                    stage,
                    payload,
                    current,
                    current,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM dispatch_applications WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            assert row is not None
            return _row_to_application(row)

    def complete_dispatch_application(
        self,
        dispatch_id: str,
        *,
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> DispatchApplication:
        """Flip applying -> applied. Already-applied is a no-op."""
        current = _timestamp(now)
        with self._lock, self._transaction():
            row = self._conn.execute(
                "SELECT * FROM dispatch_applications WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown dispatch application: {dispatch_id}")
            if str(row["status"]) == APPLICATION_APPLIED:
                return _row_to_application(row)
            payload = (
                json.dumps(detail, ensure_ascii=False, sort_keys=True)
                if detail is not None
                else str(row["detail"])
            )
            updated = self._conn.execute(
                "UPDATE dispatch_applications SET status=?, detail=?, updated_at=? "
                "WHERE dispatch_id=? AND status=? RETURNING *",
                (
                    APPLICATION_APPLIED,
                    payload,
                    current,
                    dispatch_id,
                    APPLICATION_APPLYING,
                ),
            ).fetchone()
            if updated is None:
                refreshed = self._conn.execute(
                    "SELECT * FROM dispatch_applications WHERE dispatch_id=?",
                    (dispatch_id,),
                ).fetchone()
                assert refreshed is not None
                return _row_to_application(refreshed)
            return _row_to_application(updated)

    def is_dispatch_applied(self, dispatch_id: str) -> bool:
        row = self._conn.execute(
            "SELECT status FROM dispatch_applications WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        return row is not None and str(row["status"]) == APPLICATION_APPLIED

    def _finish_dispatch(
        self,
        dispatch_id: str,
        *,
        status: DispatchStatus,
        agent_id: str,
        result_path: str | None,
        error: str | None,
        event_type: str,
        now: datetime | None,
    ) -> DispatchEnvelope:
        current = _timestamp(now)
        with self._lock, self._transaction():
            row = self._require_dispatch_row(dispatch_id)
            current_status = DispatchStatus(row["status"])
            same_result = result_path is None or row["result_path"] == result_path
            same_error = error is None or (row["error"] or "") == error
            if (
                current_status is status
                and row["agent_id"] == agent_id
                and same_result
                and same_error
            ):
                return _row_to_dispatch(row)
            if current_status in {
                DispatchStatus.SUCCEEDED,
                DispatchStatus.FAILED,
                DispatchStatus.CANCELLED,
            }:
                raise DispatchConflictError(
                    f"dispatch {dispatch_id} is already {current_status.value}"
                )
            if current_status not in {DispatchStatus.CLAIMED, DispatchStatus.RUNNING}:
                raise DispatchConflictError(
                    f"dispatch {dispatch_id} cannot finish from {current_status.value}"
                )
            if row["agent_id"] != agent_id:
                raise DispatchConflictError(
                    f"dispatch {dispatch_id} is leased to another agent"
                )
            final_result = result_path if result_path is not None else str(row["result_path"])
            updated = self._conn.execute(
                "UPDATE dispatches SET status=?,result_path=?,error=?,lease_expires_at=NULL,"
                "updated_at=? WHERE dispatch_id=? AND status=? AND agent_id=? RETURNING *",
                (
                    status.value,
                    final_result,
                    error,
                    current,
                    dispatch_id,
                    current_status.value,
                    agent_id,
                ),
            ).fetchone()
            if updated is None:
                raise DispatchConflictError(f"dispatch {dispatch_id} changed while finishing")
            self.add_event(
                str(updated["run_id"]),
                event_type,
                task_id=str(updated["task_id"]),
                detail={
                    "dispatch_id": dispatch_id,
                    "agent_id": agent_id,
                    "result_path": final_result,
                    "error": error,
                },
            )
        return _row_to_dispatch(updated)

    def _require_dispatch_row(self, dispatch_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown dispatch: {dispatch_id}")
        return cast(sqlite3.Row, row)

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
        type_: EventType | str,
        *,
        task_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events(run_id,type,task_id,detail,at) VALUES(?,?,?,?,?)",
            (
                run_id,
                type_.value if isinstance(type_, EventType) else type_,
                task_id,
                json.dumps(detail or {}, ensure_ascii=False),
                _now(),
            ),
        )

    # ── Read helpers for reporting surfaces ──────────────────────────────────
    # Doctor, dashboard, bot, meta and skills used to reach into ``store._conn``
    # with raw SQL. Owning these queries here keeps the schema in one module, so
    # a column rename is a change in one place rather than a hunt across seven.

    def list_run_ids(
        self,
        *,
        project: str | None = None,
        limit: int | None = None,
        created_since: str | None = None,
        oldest_first: bool = False,
    ) -> list[str]:
        """Run ids, newest first by default."""
        clauses: list[str] = []
        values: list[object] = []
        if project is not None:
            clauses.append("project=?")
            values.append(project)
        if created_since is not None:
            clauses.append("created_at >= ?")
            values.append(created_since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if oldest_first else "DESC"
        sql = f"SELECT id FROM runs{where} ORDER BY created_at {order}"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        rows = self._conn.execute(sql, values).fetchall()
        return [str(row["id"]) for row in rows]

    def latest_run_id(self, *, project: str | None = None) -> str | None:
        ids = self.list_run_ids(project=project, limit=1)
        return ids[0] if ids else None

    def list_runs(self, *, limit: int = 100) -> list[Run]:
        """Recent runs, newest first — for dashboards and status surfaces."""
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def list_event_types(self, run_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT type FROM events WHERE run_id=?", (run_id,)
        ).fetchall()
        return [str(row["type"]) for row in rows]

    def list_event_details(self, run_id: str, event_type: str) -> list[dict[str, Any]]:
        """Parsed ``detail`` payloads for one event type; unparsable rows → {}."""
        rows = self._conn.execute(
            "SELECT detail FROM events WHERE run_id=? AND type=?",
            (run_id, event_type),
        ).fetchall()
        details: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["detail"] or "{}")
            except json.JSONDecodeError:
                value = {}
            details.append(value if isinstance(value, dict) else {})
        return details

    def count_agent_events_matching(self, pattern: str) -> int:
        """Agent-event rows whose payload contains ``pattern`` (SQL LIKE)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM agent_events WHERE payload LIKE ?",
            (f"%{pattern}%",),
        ).fetchone()
        return int(row["c"]) if row else 0

    def count_dispatches_with_skill(self, pattern: str) -> int:
        """Dispatches whose ``skill_paths`` mention ``pattern`` (SQL LIKE)."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM dispatches WHERE skill_paths LIKE ?",
            (f"%{pattern}%",),
        ).fetchone()
        return int(row["c"]) if row else 0

    def count_rows_for_run(self, run_id: str) -> dict[str, int]:
        """How many rows each table holds for ``run_id`` — for prune previews."""
        counts: dict[str, int] = {}
        for table in ("tasks", "dispatches", "events", "agent_events", "attempts"):
            row = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE run_id=?", (run_id,)
            ).fetchone()
            counts[table] = int(row["c"]) if row else 0
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM reviews WHERE attempt_id IN "
            "(SELECT id FROM attempts WHERE run_id=?)",
            (run_id,),
        ).fetchone()
        counts["reviews"] = int(row["c"]) if row else 0
        return counts

    def delete_run(self, run_id: str) -> dict[str, int]:
        """Delete a run and every row that belongs to it. Returns rows removed.

        ``events``, ``agent_events``, ``attempts`` and ``reviews`` have **no**
        foreign key to ``runs`` — deleting the run row alone would orphan exactly
        the four largest tables. Order matters: reviews hang off attempts, so they
        go first.
        """
        removed = self.count_rows_for_run(run_id)
        with self._lock, self._transaction():
            self._conn.execute(
                "DELETE FROM reviews WHERE attempt_id IN "
                "(SELECT id FROM attempts WHERE run_id=?)",
                (run_id,),
            )
            for table in ("attempts", "agent_events", "events"):
                self._conn.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
            # dispatch_applications / dispatches / tasks cascade from runs, but be
            # explicit so the counts are honest even if PRAGMA foreign_keys is off.
            self._conn.execute(
                "DELETE FROM dispatch_applications WHERE run_id=?", (run_id,)
            )
            self._conn.execute("DELETE FROM dispatches WHERE run_id=?", (run_id,))
            self._conn.execute("DELETE FROM tasks WHERE run_id=?", (run_id,))
            self._conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        removed["runs"] = 1
        return removed

    def vacuum(self) -> None:
        """Reclaim file space after deletes (SQLite keeps freed pages otherwise)."""
        self._conn.execute("VACUUM")

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


def _dispatch_values(dispatch: DispatchEnvelope) -> tuple[object, ...]:
    return (
        dispatch.dispatch_id,
        dispatch.run_id,
        dispatch.task_id,
        dispatch.protocol_version,
        dispatch.role.value,
        dispatch.stage.value,
        dispatch.status.value,
        dispatch.prompt_path,
        dispatch.result_path,
        dispatch.model,
        dispatch.model_tier.value,
        dispatch.repo,
        dispatch.worktree,
        json.dumps(dispatch.files_owned, ensure_ascii=False),
        json.dumps(dispatch.read_only_context, ensure_ascii=False),
        json.dumps(dispatch.frozen_contracts, ensure_ascii=False),
        json.dumps(dispatch.acceptance, ensure_ascii=False),
        json.dumps(dispatch.source_document_paths, ensure_ascii=False),
        json.dumps(dispatch.dependencies, ensure_ascii=False),
        json.dumps(dispatch.skill_paths, ensure_ascii=False),
        dispatch.resource_class.value,
        dispatch.created_at,
        dispatch.updated_at,
    )


def _dispatch_identity_equal(left: DispatchEnvelope, right: DispatchEnvelope) -> bool:
    """Compare immutable dispatch identity (status/ownership/paths/model/contracts)."""
    return (
        left.dispatch_id == right.dispatch_id
        and left.run_id == right.run_id
        and left.task_id == right.task_id
        and left.protocol_version == right.protocol_version
        and left.role == right.role
        and left.stage == right.stage
        and left.status == right.status
        and left.prompt_path == right.prompt_path
        and left.result_path == right.result_path
        and left.model == right.model
        and left.model_tier == right.model_tier
        and left.repo == right.repo
        and left.worktree == right.worktree
        and left.files_owned == right.files_owned
        and left.read_only_context == right.read_only_context
        and left.frozen_contracts == right.frozen_contracts
        and left.acceptance == right.acceptance
        and left.source_document_paths == right.source_document_paths
        and left.dependencies == right.dependencies
        and left.skill_paths == right.skill_paths
        and left.resource_class == right.resource_class
        and left.created_at == right.created_at
    )


def _row_to_dispatch(r: sqlite3.Row) -> DispatchEnvelope:
    return DispatchEnvelope(
        dispatch_id=r["dispatch_id"],
        run_id=r["run_id"],
        task_id=r["task_id"],
        protocol_version=r["protocol_version"],
        role=Role(r["role"]),
        stage=TaskStage(r["stage"]),
        status=DispatchStatus(r["status"]),
        prompt_path=r["prompt_path"],
        result_path=r["result_path"],
        model=r["model"],
        model_tier=ModelTier(r["model_tier"]),
        repo=r["repo"],
        worktree=r["worktree"],
        files_owned=tuple(json.loads(r["files_owned"])),
        read_only_context=tuple(json.loads(r["read_only_context"])),
        frozen_contracts=tuple(json.loads(r["frozen_contracts"])),
        acceptance=tuple(json.loads(r["acceptance"])),
        resource_class=ResourceClass(r["resource_class"]),
        source_document_paths=tuple(json.loads(r["source_document_paths"])),
        dependencies=tuple(json.loads(r["dependencies"])),
        skill_paths=(
            tuple(json.loads(r["skill_paths"]))
            if "skill_paths" in r.keys()
            else ()
        ),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def _row_to_application(r: sqlite3.Row) -> DispatchApplication:
    raw_detail = r["detail"]
    try:
        parsed: object = json.loads(raw_detail) if raw_detail else {}
    except json.JSONDecodeError:
        parsed = {}
    detail = parsed if isinstance(parsed, dict) else {}
    return DispatchApplication(
        dispatch_id=str(r["dispatch_id"]),
        run_id=str(r["run_id"]),
        task_id=str(r["task_id"]),
        status=str(r["status"]),
        stage=str(r["stage"] or ""),
        detail=cast(dict[str, Any], detail),
        created_at=str(r["created_at"]),
        updated_at=str(r["updated_at"]),
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


def _timestamp(value: datetime | None) -> str:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.astimezone(UTC).isoformat()


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
