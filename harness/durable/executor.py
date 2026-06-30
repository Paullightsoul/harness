"""Контракт исполнителя задачи для durable-плейна.

Durable-workflow детерминирован и состоит из шагов (DBOS steps). Все побочные
эффекты (запуск агента, гейты, git-merge) и обновления домена живут за этим
протоколом — так workflow можно тестировать с фейковым исполнителем, а в
проде подменять на реальный (worktree + runner + ProfileGate).

Методы синхронные: каждый DBOS-шаг исполняется в своём worker-потоке, и реальная
реализация может мостить async-вызовы через asyncio.run внутри шага.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class WorkerStepResult:
    ok: bool
    cost_credits: float = 0.0


@dataclass
class ReviewStepResult:
    verdict: str  # значение Verdict ("approve" | "changes")
    feedback: str = ""


@runtime_checkable
class TaskExecutor(Protocol):
    """Гранулярные операции одной задачи. Возвраты — простые сериализуемые данные."""

    @property
    def max_attempts(self) -> int: ...

    def setup(self, run_id: str, task_id: str) -> None:
        """Подготовить изоляцию (worktree/ветку) перед первой попыткой."""

    def worker(self, run_id: str, task_id: str, attempt: int, feedback: str) -> WorkerStepResult:
        """Прогнать воркера на попытке attempt с фидбэком прошлой итерации."""

    def gate(self, run_id: str, task_id: str) -> bool:
        """Машинные гейты (computational sensors). True = зелёные."""

    def review(
        self, run_id: str, task_id: str, attempt: int, gates_passed: bool
    ) -> ReviewStepResult:
        """LLM-ревью. Возвращает вердикт и фидбэк на доработку."""

    def merge(self, run_id: str, task_id: str) -> bool:
        """Слить ветку задачи в base. True = слито (False = конфликт)."""

    def teardown(self, run_id: str, task_id: str, *, merged: bool) -> None:
        """Снять worktree после успешного мержа."""
