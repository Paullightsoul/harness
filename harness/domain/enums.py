"""Перечисления домена. Значения — строки: пишутся как есть в SQLite и события."""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    """Состояния узла DAG. Переходы заданы в state_machine.py."""

    PENDING = "pending"          # создана, зависимости ещё не готовы
    READY = "ready"              # зависимости выполнены, можно брать в работу
    RUNNING = "running"          # воркер пишет код в своём worktree
    GATING = "gating"            # гоняется make check
    REVIEW = "review"            # ревьюер выносит вердикт
    MERGE_QUEUE = "merge_queue"  # ждёт сериализованного мержа в base
    DONE = "done"                # влита в base
    BLOCKED = "blocked"          # нужен человек (или re-plan не помог)
    FAILED = "failed"            # технический сбой попытки
    ESCALATE = "escalate"        # вернулась оркестратору на переразбиение
    RECOVERING = "recovering"    # была in-flight на момент падения процесса -> сброс в READY


class RunStatus(StrEnum):
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"            # сработал human-gate или потолок бюджета
    DONE = "done"
    FAILED = "failed"


class Role(StrEnum):
    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"
    REVIEWER = "reviewer"


class RunnerKind(StrEnum):
    """Чем исполняется роль. SDK тарифицируется из пула API, CLI — из подписки."""

    SDK = "sdk"
    CLI = "cli"


class Verdict(StrEnum):
    APPROVE = "approve"
    CHANGES = "changes"


class EventType(StrEnum):
    """Типы записей append-only журнала. detail хранит произвольный JSON."""

    RUN_CREATED = "run_created"
    TASK_CREATED = "task_created"
    TASK_TRANSITION = "task_transition"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    GATE_RESULT = "gate_result"
    REVIEW_RESULT = "review_result"
    MERGED = "merged"
    ESCALATED = "escalated"
    REPLANNED = "replanned"
    BLOCKED = "blocked"
    RECOVERED = "recovered"
    BUDGET_SPENT = "budget_spent"
    BUDGET_EXCEEDED = "budget_exceeded"
    HUMAN_GATE_WAIT = "human_gate_wait"
    GUARD_VIOLATION = "guard_violation"
    ERROR = "error"
