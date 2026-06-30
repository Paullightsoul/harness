"""Демо durable-recovery: падение в середине шага → перезапуск → DBOS дочинивает.

Запуск (нужен `make pg-up`):
    python scripts/durable_demo.py crash     # упадёт внутри gate-шага (os._exit)
    python scripts/durable_demo.py resume     # перезапуск: DBOS восстановит workflow

Маркер-файл .harness/demo_marker.txt показывает, какие шаги выполнялись. Шаг worker,
завершившийся ДО падения, при восстановлении НЕ переигрывается (записан один раз) —
это и есть durable-чекпоинт. Шаг gate, прерванный на полпути, выполнится заново.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from harness.domain.models import Run, Task
from harness.durable.config import DurableConfig
from harness.durable.executor import ReviewStepResult, WorkerStepResult
from harness.durable.orchestrator import durable_session
from harness.store.repository import Store

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / ".harness" / "demo.db"
MARKER = ROOT / ".harness" / "demo_marker.txt"
SESSION = ROOT / ".harness" / "demo_session.txt"
APP = "harness-demo"


def _run_id() -> str:
    return f"demo-{SESSION.read_text(encoding='utf-8').strip()}"


def _mark(line: str) -> None:
    with MARKER.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


class DemoExecutor:
    max_attempts = 2

    def setup(self, run_id: str, task_id: str) -> None:
        _mark("setup")

    def worker(self, run_id: str, task_id: str, attempt: int, feedback: str) -> WorkerStepResult:
        _mark(f"worker:attempt{attempt}")
        return WorkerStepResult(ok=True)

    def gate(self, run_id: str, task_id: str) -> bool:
        _mark("gate")
        if os.environ.get("DEMO_CRASH") == "1":
            _mark("CRASH (os._exit before gate returns)")
            sys.stdout.flush()
            os._exit(137)  # имитация падения процесса в середине шага
        return True

    def review(
        self, run_id: str, task_id: str, attempt: int, gates_passed: bool
    ) -> ReviewStepResult:
        _mark("review")
        return ReviewStepResult(verdict="approve")

    def merge(self, run_id: str, task_id: str) -> bool:
        _mark("merge")
        return True

    def teardown(self, run_id: str, task_id: str, *, merged: bool) -> None:
        _mark("teardown")


def _seed(run_id: str) -> Store:
    DB.parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB)
    store.create_run(Run(id=run_id, project="demo", goal="durable demo", status="running"))
    store.upsert_task(Task(id="001", run_id=run_id, title="t", spec_path="x", status="pending"))
    return store


def cmd_crash() -> None:
    DB.unlink(missing_ok=True)
    MARKER.unlink(missing_ok=True)
    SESSION.parent.mkdir(parents=True, exist_ok=True)
    SESSION.write_text(uuid.uuid4().hex[:8], encoding="utf-8")  # свежая сессия => свежий workflow-id
    run_id = _run_id()
    store = _seed(run_id)
    os.environ["DEMO_CRASH"] = "1"
    cfg = DurableConfig(app_name=APP)
    with durable_session(store, DemoExecutor(), cfg, instance_name=run_id) as orch:
        orch.run(run_id)  # умрёт внутри gate-шага


def cmd_resume() -> None:
    os.environ.pop("DEMO_CRASH", None)
    run_id = _run_id()
    store = Store(DB)
    cfg = DurableConfig(app_name=APP)
    with durable_session(store, DemoExecutor(), cfg, instance_name=run_id) as orch:
        result = orch.run(run_id)
    print("FINAL STATUS:", result)
    print("MARKER:")
    print(MARKER.read_text(encoding="utf-8"))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "resume"
    {"crash": cmd_crash, "resume": cmd_resume}[mode]()
