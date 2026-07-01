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
    NEEDS_CLARIFICATION = "needs_clarification"  # v2-015: pre-flight brief выявил missing_context


class RunStatus(StrEnum):
    PLANNING = "planning"
    PLAN_DRAFT = "plan_draft"  # v2-023: ingest создал Run, ждёт подтверждения плана
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
    CURSOR_TASK = "cursor_task"  # v2-022: chat-режим, Task tool в Cursor IDE


class Verdict(StrEnum):
    APPROVE = "approve"
    CHANGES = "changes"


class EventType(StrEnum):
    """Типы записей append-only журнала. detail хранит произвольный JSON."""

    RUN_CREATED = "run_created"
    RUN_REUSED = "run_reused"  # v2-005: ingest повторно с той же (project, goal, base)
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
    # v2-004: воркер не заявил Provides для задачи с dependents → CHANGES без ревьюера
    PROVIDES_MISSING = "provides_missing"
    VERDICT_UNPARSED = "verdict_unparsed"  # ревьюер не выдал VERDICT → эскалация
    # v2-009: агент-события (tool_call / assistant_msg / file_edit / usage / error) —
    # пост-прогонный транскрипт; real-time стриминг через async-bridge — follow-up.
    AGENT_EVENT = "agent_event"
    # v2-013: контекст-монитор — warnings в event-log.
    CONTEXT_LOW = "context_low"          # context_window_remaining < threshold
    SCOPE_EXIT = "scope_exit"            # воркер тронул файлы вне списка «Файлы:» из спеки
    LOOP_STUCK = "loop_stuck"            # один tool-call > threshold раз за попытку
    # v2-015: pre-flight understanding-brief.
    UNDERSTANDING_BRIEF = "understanding_brief"  # brief записан (ok или с missing_context)
    CLARIFICATION_ANSWERED = "clarification_answered"  # пользователь ответил на вопрос
    # v2-020: question-protocol — оркестратор/воркер вернул maшиночитимый question-блок.
    COMPLETION_SIGNAL = "completion_signal"  # v2-021: воркер выдал HARNESS_DONE magic-phrase
    INPUT_REQUESTED = "input_requested"      # движок поставил NEEDS_CLARIFICATION, ждёт /answer
    GIT_PUSH = "git_push"
    GIT_PUSH_FAILED = "git_push_failed"
    ERROR = "error"


class AgentEventKind(StrEnum):
    """Тип агент-события в транскрипте (v2-009). Тэг для фильтрации в tail/дашборде."""

    TOOL_CALL = "tool_call"
    ASSISTANT_MSG = "assistant_msg"
    FILE_EDIT = "file_edit"
    USAGE = "usage"
    ERROR = "error"
    OTHER = "other"
