from __future__ import annotations

import pytest

from harness.policy.budget import BudgetExceeded, CostGovernor
from harness.policy.escalation import EscalationLadder


def test_budget_unlimited_when_none() -> None:
    CostGovernor(None).check(10_000.0)  # не бросает


def test_budget_exceeded() -> None:
    gov = CostGovernor(10.0)
    gov.check(9.99)
    with pytest.raises(BudgetExceeded):
        gov.check(10.0)


def test_ladder_auto_gets_chances_before_chinese() -> None:
    # Китайские модели — НЕ сразу: auto держит первые escalate_after попыток.
    ladder = EscalationLadder(
        base_model="auto", rungs=["kimi-k2.5", "glm-5.2-high"], escalate_after=2
    )
    assert ladder.model_for_attempt(1) == "auto"
    assert ladder.model_for_attempt(2) == "auto"
    assert ladder.model_for_attempt(3) == "kimi-k2.5"
    assert ladder.model_for_attempt(4) == "glm-5.2-high"
    assert ladder.model_for_attempt(5) == "glm-5.2-high"  # clamp
    assert not ladder.is_escalated(1)
    assert not ladder.is_escalated(2)
    assert ladder.is_escalated(3)
    assert ladder.ladder == ["auto", "kimi-k2.5", "glm-5.2-high"]


def test_ladder_high_complexity_starts_at_kimi() -> None:
    # Повышенная сложность: auto пропускается, старт сразу с kimi, потом glm.
    ladder = EscalationLadder(
        base_model="auto", rungs=["kimi-k2.5", "glm-5.2-high"], escalate_after=2
    )
    assert ladder.model_for_attempt(1, start_index=1) == "kimi-k2.5"
    assert ladder.model_for_attempt(2, start_index=1) == "kimi-k2.5"
    assert ladder.model_for_attempt(3, start_index=1) == "glm-5.2-high"
    assert ladder.is_escalated(1, start_index=1)


def test_ladder_no_rungs_stays_on_base() -> None:
    ladder = EscalationLadder(base_model="auto", rungs=[], escalate_after=2)
    assert ladder.model_for_attempt(5) == "auto"
    assert not ladder.is_escalated(5)
