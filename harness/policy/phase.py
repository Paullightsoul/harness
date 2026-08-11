"""Loop / UX phase tracking for Harness V4 Phase 2.

Task FSM stays authoritative; these phases are an orthogonal governor/UX
overlay (techdoc §5 + §11.3). Solo defaults never invent a fake readiness %.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class LoopPhase(StrEnum):
    """Internal Plan→…→Budget overlay (techdoc §5.1)."""

    PLAN = "plan"
    ACT = "act"
    SENSE = "sense"
    EVIDENCE = "evidence"
    JUDGE = "judge"
    BUDGET = "budget"


class UxPhase(StrEnum):
    """First-class status/dashboard phases (user-visible)."""

    RESEARCHING = "researching"
    CODING = "coding"
    TESTING = "testing"
    STUCK = "stuck"
    WAITING_ON_HUMAN = "waiting-on-human"
    BUDGET_EXHAUSTED = "budget_exhausted"
    IDLE = "idle"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class PhaseSnapshot:
    ux_phase: UxPhase
    loop_phase: LoopPhase | None
    stall_reason: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.ux_phase.value,
            "loop_phase": self.loop_phase.value if self.loop_phase else None,
            "stall_reason": self.stall_reason,
            "detail": self.detail,
        }


def derive_phase(
    *,
    run_status: str,
    task_statuses: list[str],
    active_kinds: list[str] | None = None,
    recent_event_types: list[str] | None = None,
    stall_override: str | None = None,
    phase_override: str | None = None,
) -> PhaseSnapshot:
    """Derive UX phase + stall_reason from durable run/task/event signals."""
    kinds = [k.lower() for k in (active_kinds or [])]
    events = [e.lower() for e in (recent_event_types or [])]
    statuses = [s.lower() for s in task_statuses]

    if phase_override:
        try:
            ux = UxPhase(phase_override)
        except ValueError:
            ux = UxPhase.STUCK
        return PhaseSnapshot(
            ux_phase=ux,
            loop_phase=_loop_for_ux(ux),
            stall_reason=stall_override or ("none" if ux not in {
                UxPhase.STUCK, UxPhase.WAITING_ON_HUMAN, UxPhase.BUDGET_EXHAUSTED
            } else stall_override or ux.value),
        )

    if stall_override == "budget_exhausted" or "budget_predicate_hit" in events or (
        run_status == "paused" and "budget_exceeded" in events
    ):
        return PhaseSnapshot(
            UxPhase.BUDGET_EXHAUSTED,
            LoopPhase.BUDGET,
            stall_override or "budget:predicate",
        )

    if "loop_stuck" in events or stall_override and stall_override.startswith("stuck"):
        return PhaseSnapshot(
            UxPhase.STUCK,
            LoopPhase.ACT,
            stall_override or "stuck:tool_hash_repeat",
        )

    if run_status in {"done"}:
        return PhaseSnapshot(UxPhase.DONE, LoopPhase.BUDGET, "none")

    if run_status in {"aborted", "failed"}:
        return PhaseSnapshot(
            UxPhase.STUCK, LoopPhase.BUDGET, stall_override or f"run:{run_status}"
        )

    waiting_signals = (
        "human_gate_wait" in events
        or "input_requested" in events
        or any(s in {"blocked", "needs_clarification", "merge_queue"} for s in statuses)
        or run_status == "paused"
    )
    if waiting_signals and not any(s in {"running", "gating"} for s in statuses):
        reason = stall_override or _waiting_reason(events, statuses)
        return PhaseSnapshot(
            UxPhase.WAITING_ON_HUMAN, LoopPhase.BUDGET, reason
        )

    if any(s == "gating" for s in statuses) or "gate" in kinds:
        return PhaseSnapshot(UxPhase.TESTING, LoopPhase.SENSE, stall_override or "none")

    if any(k in {"reviewer", "goal-judge"} for k in kinds) or any(
        s == "review" for s in statuses
    ):
        return PhaseSnapshot(UxPhase.TESTING, LoopPhase.JUDGE, stall_override or "none")

    if any(k in {"sub-orchestrator", "sprint-contract"} for k in kinds) or run_status in {
        "planning",
        "plan_draft",
    }:
        return PhaseSnapshot(
            UxPhase.RESEARCHING, LoopPhase.PLAN, stall_override or "none"
        )

    if any(s == "running" for s in statuses) or any(k == "worker" for k in kinds):
        return PhaseSnapshot(UxPhase.CODING, LoopPhase.ACT, stall_override or "none")

    if any(s == "ready" for s in statuses):
        return PhaseSnapshot(UxPhase.IDLE, LoopPhase.BUDGET, stall_override or "none")

    if waiting_signals:
        return PhaseSnapshot(
            UxPhase.WAITING_ON_HUMAN,
            LoopPhase.BUDGET,
            stall_override or _waiting_reason(events, statuses),
        )

    return PhaseSnapshot(UxPhase.IDLE, None, stall_override or "none")


def _loop_for_ux(ux: UxPhase) -> LoopPhase | None:
    return {
        UxPhase.RESEARCHING: LoopPhase.PLAN,
        UxPhase.CODING: LoopPhase.ACT,
        UxPhase.TESTING: LoopPhase.SENSE,
        UxPhase.STUCK: LoopPhase.ACT,
        UxPhase.WAITING_ON_HUMAN: LoopPhase.BUDGET,
        UxPhase.BUDGET_EXHAUSTED: LoopPhase.BUDGET,
        UxPhase.DONE: LoopPhase.BUDGET,
        UxPhase.IDLE: LoopPhase.BUDGET,
    }.get(ux)


def _waiting_reason(events: list[str], statuses: list[str]) -> str:
    if "human_gate_wait" in events:
        return "waiting_human:ship"
    if "input_requested" in events or "needs_clarification" in statuses:
        return "waiting_human:clarification"
    if "blocked" in statuses:
        return "waiting_human:blocked"
    if "merge_queue" in statuses:
        return "waiting_human:merge"
    return "waiting_human"
