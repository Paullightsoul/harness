"""Pre-call budget governors (techdoc §4.4 / Phase 2 P2.3).

Checked in ``next`` / ``advance`` *before* claiming work. Solo-safe default:
``HARNESS_PRECALL_BUDGET=0`` → always allow. When enabled, predicates pause the
run with a clear ``budget_exhausted`` / ``stuck`` phase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from harness.policy.budget import BudgetExceeded, CostGovernor


@dataclass(frozen=True, slots=True)
class PrecallHit:
    predicate: str
    reason: str
    phase: str = "budget_exhausted"  # budget_exhausted | stuck
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicate": self.predicate,
            "reason": self.reason,
            "phase": self.phase,
            "detail": self.detail or {},
        }


def precall_budget_enabled() -> bool:
    return os.environ.get("HARNESS_PRECALL_BUDGET", "0") == "1"


def loop_detect_enabled() -> bool:
    # Observational stall detection — on by default; does not burn tokens alone.
    return os.environ.get("HARNESS_LOOP_DETECT", "1") == "1"


@dataclass(slots=True)
class PrecallLimits:
    max_steps: int | None = None
    max_wall_clock_sec: int | None = None
    max_tokens_est: int | None = None
    max_tool_hash_repeat: int = 3
    run_budget_credits: float | None = None

    @classmethod
    def from_env(cls, *, run_budget_credits: float | None = None) -> PrecallLimits:
        return cls(
            max_steps=_optional_int("HARNESS_MAX_STEPS", None),
            max_wall_clock_sec=_optional_int("HARNESS_MAX_WALL_CLOCK_SEC", None),
            max_tokens_est=_optional_int("HARNESS_MAX_TOKENS_EST", None),
            max_tool_hash_repeat=int(os.environ.get("HARNESS_TOOL_HASH_REPEAT", "3")),
            run_budget_credits=run_budget_credits,
        )


def _optional_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name, "")
    if raw.strip() == "":
        return default
    value = int(raw)
    return value if value > 0 else None


class PrecallGovernor:
    """Deterministic pre-call stop predicates (witness + breaker when enabled)."""

    def __init__(self, limits: PrecallLimits | None = None) -> None:
        self.limits = limits or PrecallLimits.from_env()
        self._cost = CostGovernor(self.limits.run_budget_credits)

    def check(
        self,
        *,
        steps: int = 0,
        started_at: str | None = None,
        tokens_est: int = 0,
        spent_credits: float = 0.0,
        tool_hash_hits: list[tuple[str, Any, int]] | None = None,
        force: bool = False,
    ) -> PrecallHit | None:
        """Return a hit to refuse/pause, or None to continue.

        When ``HARNESS_PRECALL_BUDGET`` is off, only ``tool_hash_repeat`` may
        fire (if ``HARNESS_LOOP_DETECT=1``), and cost budget still applies when
        ``run_budget_credits`` is set on the run. Pass ``force=True`` to evaluate
        all predicates regardless of the feature flag (tests).
        """
        enforce = force or precall_budget_enabled()

        if tool_hash_hits and loop_detect_enabled():
            threshold = self.limits.max_tool_hash_repeat
            over = [(n, inp, c) for n, inp, c in tool_hash_hits if c > threshold]
            if over:
                name, _inp, count = over[0]
                return PrecallHit(
                    predicate="tool_hash_repeat",
                    reason=f"tool {name!r} repeated {count}x (>{threshold})",
                    phase="stuck",
                    detail={"tool": name, "count": count, "threshold": threshold},
                )

        if not enforce:
            # Soft credit ceiling still honored when run carries a budget.
            if self.limits.run_budget_credits is not None:
                try:
                    self._cost.check(spent_credits)
                except BudgetExceeded as exc:
                    return PrecallHit(
                        predicate="max_credits",
                        reason=str(exc),
                        phase="budget_exhausted",
                        detail={"spent": exc.spent, "budget": exc.budget},
                    )
            return None

        if self.limits.max_steps is not None and steps >= self.limits.max_steps:
            return PrecallHit(
                predicate="max_steps",
                reason=f"steps {steps} >= max_steps {self.limits.max_steps}",
                phase="budget_exhausted",
                detail={"steps": steps, "max_steps": self.limits.max_steps},
            )

        if self.limits.max_wall_clock_sec is not None and started_at:
            elapsed = _elapsed_sec(started_at)
            if elapsed is not None and elapsed >= self.limits.max_wall_clock_sec:
                return PrecallHit(
                    predicate="max_wall_clock",
                    reason=(
                        f"wall clock {elapsed:.0f}s >= "
                        f"{self.limits.max_wall_clock_sec}s"
                    ),
                    phase="budget_exhausted",
                    detail={
                        "elapsed_sec": elapsed,
                        "max_wall_clock_sec": self.limits.max_wall_clock_sec,
                    },
                )

        if (
            self.limits.max_tokens_est is not None
            and tokens_est >= self.limits.max_tokens_est
        ):
            return PrecallHit(
                predicate="max_tokens_est",
                reason=(
                    f"tokens_est {tokens_est} >= "
                    f"{self.limits.max_tokens_est}"
                ),
                phase="budget_exhausted",
                detail={
                    "tokens_est": tokens_est,
                    "max_tokens_est": self.limits.max_tokens_est,
                },
            )

        if self.limits.run_budget_credits is not None:
            try:
                self._cost.check(spent_credits)
            except BudgetExceeded as exc:
                return PrecallHit(
                    predicate="max_credits",
                    reason=str(exc),
                    phase="budget_exhausted",
                    detail={"spent": exc.spent, "budget": exc.budget},
                )
        return None


def _elapsed_sec(started_at: str) -> float | None:
    try:
        raw = started_at.replace("Z", "+00:00")
        started = datetime.fromisoformat(raw)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return (datetime.now(tz=UTC) - started).total_seconds()
    except ValueError:
        return None
