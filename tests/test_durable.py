"""Live-тесты durable-плейна: требуют DBOS + поднятый Postgres (make pg-up).

Пропускаются автоматически, если dbos не установлен или база недоступна, — чтобы
обычный `pytest -q` оставался зелёным в окружениях без durable-инфры.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

dbos = pytest.importorskip("dbos")  # noqa: F841  (skip всего модуля без dbos)

from harness.domain.enums import TaskStatus  # noqa: E402
from harness.domain.models import Run, Task  # noqa: E402
from harness.durable.config import DurableConfig, system_db_url  # noqa: E402
from harness.durable.executor import ReviewStepResult, WorkerStepResult  # noqa: E402
from harness.durable.orchestrator import durable_session  # noqa: E402
from harness.store.repository import Store  # noqa: E402


def _pg_available() -> bool:
    try:
        import psycopg  # noqa: PLC0415
    except ModuleNotFoundError:
        try:
            import psycopg2  # noqa: F401, PLC0415
        except ModuleNotFoundError:
            return False
        return _ping_psycopg2()
    return _ping_psycopg(psycopg)


def _ping_psycopg(psycopg: object) -> bool:
    try:
        conn = psycopg.connect(system_db_url(), connect_timeout=2)  # type: ignore[attr-defined]
        conn.close()
    except Exception:  # noqa: BLE001  (любая ошибка соединения => база недоступна)
        return False
    return True


def _ping_psycopg2() -> bool:
    import psycopg2  # noqa: PLC0415

    try:
        conn = psycopg2.connect(system_db_url(), connect_timeout=2)
        conn.close()
    except Exception:  # noqa: BLE001
        return False
    return True


pytestmark = pytest.mark.skipif(not _pg_available(), reason="Postgres недоступен (make pg-up)")


class FakeExecutor:
    """Детерминированный исполнитель: задаёт сценарий gate/verdict по попыткам."""

    def __init__(self, *, gate_ok: list[bool], verdict: list[str], max_attempts: int = 3) -> None:
        self._gate_ok = gate_ok
        self._verdict = verdict
        self._max = max_attempts
        self.worker_calls: list[tuple[str, int]] = []

    @property
    def max_attempts(self) -> int:
        return self._max

    def setup(self, run_id: str, task_id: str) -> None:
        pass

    def worker(self, run_id: str, task_id: str, attempt: int, feedback: str) -> WorkerStepResult:
        self.worker_calls.append((task_id, attempt))
        return WorkerStepResult(ok=True)

    def gate(self, run_id: str, task_id: str) -> bool:
        idx = min(self._attempt_index(task_id), len(self._gate_ok) - 1)
        return self._gate_ok[idx]

    def review(
        self, run_id: str, task_id: str, attempt: int, gates_passed: bool
    ) -> ReviewStepResult:
        idx = min(attempt - 1, len(self._verdict) - 1)
        v = self._verdict[idx]
        return ReviewStepResult(verdict=v, feedback="" if v == "approve" else "fix it")

    def merge(self, run_id: str, task_id: str) -> bool:
        return True

    def teardown(self, run_id: str, task_id: str, *, merged: bool) -> None:
        pass

    def _attempt_index(self, task_id: str) -> int:
        return sum(1 for tid, _ in self.worker_calls if tid == task_id) - 1


def _seed(store: Store, run_id: str, deps: dict[str, list[str]]) -> None:
    store.create_run(Run(id=run_id, project="p", goal="g", status="running"))
    for task_id, dep in deps.items():
        store.upsert_task(
            Task(id=task_id, run_id=run_id, title=task_id, spec_path="x",
                 status="pending", depends_on=dep)
        )


def _cfg() -> DurableConfig:
    return DurableConfig(app_name=f"harness-test-{uuid.uuid4().hex[:8]}")


def test_happy_path_linear_dag(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    _seed(store, run_id, {"001": [], "002": ["001"], "003": ["002"]})
    executor = FakeExecutor(gate_ok=[True], verdict=["approve"])

    with durable_session(store, executor, _cfg(), instance_name=run_id) as orch:
        result = orch.run(run_id)

    assert result == {"001": "done", "002": "done", "003": "done"}


def test_retry_then_pass(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    _seed(store, run_id, {"001": []})
    executor = FakeExecutor(gate_ok=[False, True], verdict=["changes", "approve"])

    with durable_session(store, executor, _cfg(), instance_name=run_id) as orch:
        result = orch.run(run_id)

    assert result["001"] == TaskStatus.DONE
    assert len(executor.worker_calls) == 2


def test_exhaust_escalates_to_blocked(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    _seed(store, run_id, {"001": []})
    executor = FakeExecutor(gate_ok=[True], verdict=["changes"], max_attempts=2)

    with durable_session(store, executor, _cfg(), instance_name=run_id) as orch:
        result = orch.run(run_id)

    assert result["001"] == TaskStatus.BLOCKED
    assert len(executor.worker_calls) == 2


def test_workflow_id_dedup_is_idempotent(tmp_path: Path) -> None:
    """Повторный enqueue с тем же workflow-id не перезапускает шаги (durable-чекпоинт)."""
    store = Store(tmp_path / "state.db")
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    _seed(store, run_id, {"001": []})
    executor = FakeExecutor(gate_ok=[True], verdict=["approve"])

    from dbos import SetWorkflowID  # noqa: PLC0415

    with durable_session(store, executor, _cfg(), instance_name=run_id) as orch:
        wid = f"{run_id}:001"
        with SetWorkflowID(wid):
            orch._queue.enqueue(orch.process_task, run_id, "001").get_result()
        calls_after_first = len(executor.worker_calls)
        with SetWorkflowID(wid):
            orch._queue.enqueue(orch.process_task, run_id, "001").get_result()

    assert calls_after_first == 1
    assert len(executor.worker_calls) == 1  # второй enqueue дедуплицирован, шаги не переигрывались
