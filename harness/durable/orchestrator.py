"""Durable-оркестратор поверх DBOS: per-task workflow со steps + очередь.

Идея (ADR-0003, L1): детерминированный workflow реплеится из чекпоинтов DBOS,
а побочные эффекты (worker/gate/review/merge) — это DBOS-steps. Если процесс
падает в середине задачи, DBOS восстанавливает workflow с последнего
завершённого шага — без нашего ручного reset (ср. Engine._recover_interrupted).

Драйвер DAG — обычный цикл: волнами кладёт готовые задачи в очередь с лимитом
параллелизма. Идентификатор workflow детерминирован (`run:task`), поэтому повторный
enqueue после рестарта дедуплицируется DBOS и не запускает задачу дважды.

Шаги идемпотентны: переход статуса делается, только если задача ещё в исходном
состоянии (повторный прогон незавершённого шага не ломает конечный автомат).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from dbos import DBOS, DBOSConfig, DBOSConfiguredInstance, Queue, SetWorkflowID

from harness.domain.enums import TaskStatus, Verdict
from harness.domain.models import Task
from harness.durable.config import DurableConfig
from harness.durable.executor import ReviewStepResult, TaskExecutor, WorkerStepResult
from harness.store.repository import Store

# Очередь регистрируется в глобальном реестре DBOS один раз на процесс
# (повторное Queue(name) бросает "already declared"). Кэшируем по имени.
_QUEUES: dict[str, Queue] = {}


def _get_queue(name: str, concurrency: int) -> Queue:
    queue = _QUEUES.get(name)
    if queue is None:
        queue = Queue(name, concurrency=concurrency)
        _QUEUES[name] = queue
    return queue


@DBOS.dbos_class()
class DurableOrchestrator(DBOSConfiguredInstance):
    def __init__(
        self,
        config: DurableConfig,
        store: Store,
        executor: TaskExecutor,
        instance_name: str = "harness-durable",
    ) -> None:
        super().__init__(instance_name)
        self._store = store
        self._executor = executor
        self._queue = _get_queue(config.queue_name, config.max_parallel)

    # ── driver: волнами по DAG ───────────────────────────────────────────────
    def run(self, run_id: str) -> dict[str, str]:
        while True:
            tasks = self._store.list_tasks(run_id)
            status = {t.id: TaskStatus(t.status) for t in tasks}
            if all(s == TaskStatus.DONE for s in status.values()):
                break
            actionable = self._actionable(tasks, status)
            if not actionable:
                break  # остались blocked/escalate — ничего не готово
            handles = []
            for task_id in actionable:
                # Детерминированный id: повторный enqueue после рестарта дедуплицируется
                # DBOS и переподхватывает восстановленный (in-flight на момент краха) workflow.
                with SetWorkflowID(f"{run_id}:{task_id}"):
                    handles.append(self._queue.enqueue(self.process_task, run_id, task_id))
            for handle in handles:
                handle.get_result()
        return {t.id: t.status for t in self._store.list_tasks(run_id)}

    @staticmethod
    def _actionable(tasks: list[Task], status: dict[str, TaskStatus]) -> list[str]:
        """Задачи, которые можно (до)исполнить: не терминальные, deps готовы.

        Включает in-flight статусы (RUNNING/GATING/REVIEW/MERGE_QUEUE/RECOVERING) —
        чтобы после рестарта переподхватить и дождаться восстановленный DBOS-workflow,
        а не считать задачу «зависшей» по SQLite-статусу.
        """
        stuck = {TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.ESCALATE}
        out: list[str] = []
        for task in tasks:
            if status[task.id] in stuck:
                continue
            if all(status.get(dep) == TaskStatus.DONE for dep in task.depends_on):
                out.append(task.id)
        return out

    # ── durable workflow одной задачи ────────────────────────────────────────
    @DBOS.workflow()
    def process_task(self, run_id: str, task_id: str) -> str:
        self._s_begin(run_id, task_id)
        feedback = ""
        max_attempts = self._executor.max_attempts
        for attempt in range(1, max_attempts + 1):
            self._s_worker(run_id, task_id, attempt, feedback)
            gates = self._s_gate(run_id, task_id)
            review = self._s_review(run_id, task_id, attempt, gates)

            if review.verdict == Verdict.APPROVE.value and gates:
                if self._s_merge(run_id, task_id):
                    return "done"
                feedback = "merge conflict: переразреши и повтори"
                continue

            feedback = review.feedback
            if attempt < max_attempts:
                self._s_changes(run_id, task_id)
            else:
                self._s_escalate(run_id, task_id)
                return "escalated"
        return "exhausted"

    # ── steps (побочные эффекты + переходы автомата) ─────────────────────────
    @DBOS.step()
    def _s_begin(self, run_id: str, task_id: str) -> None:
        task = self._require(run_id, task_id)
        if TaskStatus(task.status) == TaskStatus.PENDING:
            self._store.transition_task(task, TaskStatus.READY)
        self._executor.setup(run_id, task_id)

    @DBOS.step()
    def _s_worker(self, run_id: str, task_id: str, attempt: int, feedback: str) -> WorkerStepResult:
        self._move(run_id, task_id, TaskStatus.READY, TaskStatus.RUNNING, note=f"attempt {attempt}")
        return self._executor.worker(run_id, task_id, attempt, feedback)

    @DBOS.step()
    def _s_gate(self, run_id: str, task_id: str) -> bool:
        self._move(run_id, task_id, TaskStatus.RUNNING, TaskStatus.GATING)
        return self._executor.gate(run_id, task_id)

    @DBOS.step()
    def _s_review(
        self, run_id: str, task_id: str, attempt: int, gates_passed: bool
    ) -> ReviewStepResult:
        self._move(run_id, task_id, TaskStatus.GATING, TaskStatus.REVIEW)
        return self._executor.review(run_id, task_id, attempt, gates_passed)

    @DBOS.step()
    def _s_merge(self, run_id: str, task_id: str) -> bool:
        self._move(run_id, task_id, TaskStatus.REVIEW, TaskStatus.MERGE_QUEUE)
        merged = self._executor.merge(run_id, task_id)
        if merged:
            self._move(run_id, task_id, TaskStatus.MERGE_QUEUE, TaskStatus.DONE)
            self._executor.teardown(run_id, task_id, merged=True)
        else:
            self._move(run_id, task_id, TaskStatus.MERGE_QUEUE, TaskStatus.READY, note="conflict")
        return merged

    @DBOS.step()
    def _s_changes(self, run_id: str, task_id: str) -> None:
        self._move(run_id, task_id, TaskStatus.REVIEW, TaskStatus.READY, note="changes")

    @DBOS.step()
    def _s_escalate(self, run_id: str, task_id: str) -> None:
        self._move(run_id, task_id, TaskStatus.REVIEW, TaskStatus.ESCALATE, note="exhausted")
        self._move(run_id, task_id, TaskStatus.ESCALATE, TaskStatus.BLOCKED, note="need re-plan")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _require(self, run_id: str, task_id: str) -> Task:
        task = self._store.get_task(run_id, task_id)
        if task is None:
            raise ValueError(f"task {task_id} не найдена в run {run_id}")
        return task

    def _move(
        self, run_id: str, task_id: str, src: TaskStatus, dst: TaskStatus, note: str = ""
    ) -> None:
        """Идемпотентный переход: если шаг переигрывается, повторный no-op (уже в dst)."""
        task = self._require(run_id, task_id)
        if TaskStatus(task.status) == dst:
            return
        self._store.transition_task(task, dst, note=note)


@contextmanager
def durable_session(
    store: Store,
    executor: TaskExecutor,
    config: DurableConfig | None = None,
    instance_name: str = "harness-durable",
) -> Iterator[DurableOrchestrator]:
    """Поднять DBOS, создать оркестратор, запустить, гарантированно погасить."""
    cfg = config or DurableConfig()
    DBOS(config=DBOSConfig(name=cfg.app_name, system_database_url=cfg.system_database_url))
    orchestrator = DurableOrchestrator(cfg, store, executor, instance_name)
    DBOS.launch()
    try:
        yield orchestrator
    finally:
        DBOS.destroy()
