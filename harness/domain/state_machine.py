"""Конечный автомат задачи. Единственное место, где разрешены переходы статусов.

Диаграмма:
    PENDING -> READY -> RUNNING -> GATING -> REVIEW
    RUNNING -> PENDING (agent-in-agent: sub-orchestrator декомпозировал задачу,
        родитель ждёт дочерние; активируется обратно через deps как обычно)
    REVIEW  -> MERGE_QUEUE (approve & gates pass; ship_mode=merge)
            -> DONE         (ship_mode=manual — human commits/merges later)
            -> READY        (changes, есть попытки)
            -> ESCALATE      (попытки исчерпаны)
    ESCALATE -> PENDING (re-plan) | READY (уточнение) | BLOCKED (нужен человек)
    MERGE_QUEUE -> DONE | READY (конфликт/pre-merge gate) | POST_MERGE_FIX
    POST_MERGE_FIX -> READY (approved repair dispatch)
    BLOCKED/FAILED -> READY (человек разблокировал / ретрай)
    {RUNNING,GATING,REVIEW,MERGE_QUEUE,POST_MERGE_FIX} -> RECOVERING -> READY
        (resume: задача была in-flight на момент падения процесса)
    Abort: nonterminal -> BLOCKED | FAILED
"""

from __future__ import annotations

from harness.domain.enums import TaskStatus

_T = TaskStatus

# In-flight статусы: если процесс упал на них, при рестарте задача уходит в
# RECOVERING -> READY (см. Engine._recover_interrupted). Иначе бы «зависла».
RECOVERABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        _T.RUNNING,
        _T.GATING,
        _T.REVIEW,
        _T.MERGE_QUEUE,
        _T.NEEDS_CLARIFICATION,
        _T.POST_MERGE_FIX,
    }
)

_ALLOWED: dict[TaskStatus, frozenset[TaskStatus]] = {
    _T.PENDING: frozenset({_T.READY, _T.BLOCKED, _T.FAILED}),
    _T.READY: frozenset(
        # PENDING: decompose recovery — родитель распался на детей после resume.
        {_T.RUNNING, _T.NEEDS_CLARIFICATION, _T.BLOCKED, _T.FAILED, _T.PENDING}
    ),
    _T.RUNNING: frozenset(
        {
            _T.GATING,
            _T.FAILED,
            _T.BLOCKED,
            _T.RECOVERING,
            _T.NEEDS_CLARIFICATION,
            _T.READY,
            # Decompose wait: родитель уходит в PENDING до завершения детей.
            _T.PENDING,
        }
    ),
    _T.GATING: frozenset({_T.REVIEW, _T.READY, _T.FAILED, _T.BLOCKED, _T.RECOVERING}),
    _T.REVIEW: frozenset(
        {
            _T.MERGE_QUEUE,
            _T.DONE,
            _T.READY,
            _T.ESCALATE,
            _T.RECOVERING,
            _T.BLOCKED,
            _T.FAILED,
        }
    ),
    _T.MERGE_QUEUE: frozenset(
        {_T.DONE, _T.READY, _T.POST_MERGE_FIX, _T.RECOVERING, _T.BLOCKED, _T.FAILED}
    ),
    _T.POST_MERGE_FIX: frozenset({_T.READY, _T.RUNNING, _T.BLOCKED, _T.FAILED, _T.RECOVERING}),
    _T.ESCALATE: frozenset({_T.PENDING, _T.READY, _T.BLOCKED, _T.FAILED}),
    _T.BLOCKED: frozenset({_T.READY}),
    _T.FAILED: frozenset({_T.READY, _T.BLOCKED}),
    _T.RECOVERING: frozenset({_T.READY}),
    _T.NEEDS_CLARIFICATION: frozenset({_T.READY, _T.BLOCKED, _T.FAILED, _T.RECOVERING}),
    _T.DONE: frozenset(),
}

TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({_T.DONE})


class InvalidTransition(RuntimeError):
    def __init__(self, src: TaskStatus, dst: TaskStatus) -> None:
        super().__init__(f"недопустимый переход {src.value} -> {dst.value}")
        self.src = src
        self.dst = dst

    def __reduce__(self) -> tuple[type[InvalidTransition], tuple[TaskStatus, TaskStatus]]:
        # Восстановление из pickle (durable-плейн сериализует исключения шагов в DBOS).
        return (InvalidTransition, (self.src, self.dst))


def can_transition(src: TaskStatus, dst: TaskStatus) -> bool:
    return dst in _ALLOWED.get(src, frozenset())


def require_transition(src: TaskStatus, dst: TaskStatus) -> None:
    """Бросает InvalidTransition, если переход запрещён. Вызывается перед записью в store."""
    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES
