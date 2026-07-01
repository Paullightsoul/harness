"""v2-034: live end-to-end тесты `EngineTaskExecutor` через `DurableOrchestrator`
на настоящем DBOS+Postgres.

Отдельный файл (не `test_durable_executor.py`): `pytest.importorskip` без `dbos`
скипает ВЕСЬ модуль на этапе импорта (collection error), а не только тесты после
него — если положить это в один файл с обычными unit-тестами `EngineTaskExecutor`,
они тоже перестанут собираться. Тот же паттерн разделения, что и у
`tests/test_durable.py` (тоже отдельный файл, тоже live).

Пропускается автоматически без `dbos` или недоступного Postgres (`make pg-up`).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from durable_executor_helpers import (
    StubReviewerRunner,
    StubWorkerRunner,
    init_repo,
    make_executor,
    seed_task,
)

from harness.domain.models import Run, Task
from harness.store.repository import Store

dbos = pytest.importorskip("dbos")  # noqa: F841  (skip всего модуля без dbos)

from harness.durable.config import DurableConfig, system_db_url  # noqa: E402
from harness.durable.executor import EngineTaskExecutor  # noqa: E402
from harness.durable.orchestrator import durable_session  # noqa: E402


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


def _cfg() -> DurableConfig:
    return DurableConfig(app_name=f"harness-executor-test-{uuid.uuid4().hex[:8]}")


def test_live_two_tasks_end_to_end_with_real_executor(tmp_path: Path) -> None:
    """Acceptance: 2 задачи end-to-end с реальным EngineTaskExecutor (не Fake)."""
    repo = tmp_path / "repo"
    init_repo(repo)
    executor: EngineTaskExecutor = make_executor(
        tmp_path, repo,
        worker=StubWorkerRunner(edit="live e2e\n"),
        reviewer=StubReviewerRunner("VERDICT: APPROVE"),
    )
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    store: Store = executor._store  # noqa: SLF001
    store.create_run(Run(id=run_id, project="p", goal="g", status="running"))
    for tid, deps in (("001", []), ("002", ["001"])):
        spec = tmp_path / f"task-{tid}.md"
        spec.write_text("# spec\n", encoding="utf-8")
        store.upsert_task(Task(
            id=tid, run_id=run_id, title=tid, spec_path=str(spec),
            status="pending", depends_on=deps,
        ))

    with durable_session(store, executor, _cfg(), instance_name=run_id) as orch:
        result = orch.run(run_id)

    assert result == {"001": "done", "002": "done"}
    assert (repo / "f.txt").read_text(encoding="utf-8") == "live e2e\n"


def test_live_crash_resume_continues_from_last_step(tmp_path: Path) -> None:
    """Повторный enqueue с тем же workflow-id после «краха» не переигрывает уже
    завершённые шаги — с РЕАЛЬНЫМ executor (не `FakeExecutor` из test_durable.py)."""
    from dbos import SetWorkflowID  # noqa: PLC0415

    repo = tmp_path / "repo"
    init_repo(repo)
    worker = StubWorkerRunner(edit="crash resume\n")
    executor: EngineTaskExecutor = make_executor(
        tmp_path, repo, worker=worker, reviewer=StubReviewerRunner("VERDICT: APPROVE"),
    )
    run_id = f"r-{uuid.uuid4().hex[:8]}"
    store: Store = executor._store  # noqa: SLF001
    seed_task(executor, run_id, "001", tmp_path)

    with durable_session(store, executor, _cfg(), instance_name=run_id) as orch:
        wid = f"{run_id}:001"
        with SetWorkflowID(wid):
            orch._queue.enqueue(orch.process_task, run_id, "001").get_result()  # noqa: SLF001
        calls_after_first = len(worker.calls)
        # «Крах и рестарт»: тот же workflow-id — DBOS не переигрывает завершённые
        # шаги, EngineTaskExecutor.worker второй раз не зовётся.
        with SetWorkflowID(wid):
            orch._queue.enqueue(orch.process_task, run_id, "001").get_result()  # noqa: SLF001

    assert calls_after_first == 1
    assert len(worker.calls) == 1
    task = store.get_task(run_id, "001")
    assert task is not None
    assert task.status == "done"
