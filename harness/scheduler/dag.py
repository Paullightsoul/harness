"""Граф задач: топологическая сортировка и вычисление готовых к запуску узлов.

Заменяет наивный `ls | sort` — порядок и параллелизм определяются зависимостями
из PLAN.md, а не алфавитом имени файла.
"""

from __future__ import annotations

from harness.domain.enums import TaskStatus
from harness.domain.models import Task


class CycleError(RuntimeError):
    def __init__(self, remaining: list[str]) -> None:
        super().__init__(f"цикл в зависимостях задач: {', '.join(sorted(remaining))}")
        self.remaining = remaining


class TaskGraph:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = {t.id: t for t in tasks}
        self._deps = {t.id: set(t.depends_on) for t in tasks}
        self._validate()

    def _validate(self) -> None:
        # все зависимости существуют
        for tid, deps in self._deps.items():
            missing = deps - self._tasks.keys()
            if missing:
                raise ValueError(f"task {tid}: неизвестные зависимости {sorted(missing)}")
        self.topo_order()  # бросит CycleError при цикле

    def topo_order(self) -> list[str]:
        indeg = {tid: len(deps) for tid, deps in self._deps.items()}
        queue = sorted(tid for tid, d in indeg.items() if d == 0)
        order: list[str] = []
        while queue:
            node = queue.pop(0)
            order.append(node)
            for tid, deps in self._deps.items():
                if node in deps:
                    indeg[tid] -= 1
                    if indeg[tid] == 0:
                        queue.append(tid)
            queue.sort()
        if len(order) != len(self._tasks):
            done = set(order)
            raise CycleError([t for t in self._tasks if t not in done])
        return order

    def ready(self, statuses: dict[str, TaskStatus]) -> list[str]:
        """Задачи, чьи зависимости все DONE и кто сам ждёт исполнения."""
        result: list[str] = []
        for tid, deps in self._deps.items():
            if statuses.get(tid) not in (TaskStatus.PENDING, TaskStatus.READY):
                continue
            if all(statuses.get(d) == TaskStatus.DONE for d in deps):
                result.append(tid)
        return sorted(result)

    def all_done(self, statuses: dict[str, TaskStatus]) -> bool:
        return all(s == TaskStatus.DONE for s in statuses.values())

    def provides_of(self, task_id: str) -> list[str]:
        return sorted(self._deps.get(task_id, set()))

    @property
    def tasks(self) -> dict[str, Task]:
        return self._tasks
