from __future__ import annotations

import pytest

from harness.domain.enums import TaskStatus
from harness.domain.state_machine import (
    InvalidTransition,
    can_transition,
    is_terminal,
    require_transition,
)


def test_happy_path_transitions() -> None:
    chain = [
        TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING,
        TaskStatus.GATING, TaskStatus.REVIEW, TaskStatus.MERGE_QUEUE, TaskStatus.DONE,
    ]
    for src, dst in zip(chain, chain[1:], strict=False):
        assert can_transition(src, dst), f"{src}->{dst}"


def test_review_can_loop_back_to_ready() -> None:
    assert can_transition(TaskStatus.REVIEW, TaskStatus.READY)
    assert can_transition(TaskStatus.REVIEW, TaskStatus.ESCALATE)


def test_invalid_transition_raises() -> None:
    with pytest.raises(InvalidTransition):
        require_transition(TaskStatus.PENDING, TaskStatus.DONE)


def test_done_is_terminal() -> None:
    assert is_terminal(TaskStatus.DONE)
    assert not is_terminal(TaskStatus.BLOCKED)
