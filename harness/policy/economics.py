"""Phase 3 — multi-agent economics: breadth gate + solo/team fan-out profiles.

Aggressive checks are behind ``HARNESS_ECONOMICS=1`` (default off). Solo-safe
defaults keep current ADR-0014 limits unless the flag is on; then the active
``HARNESS_FANOUT_PROFILE`` (solo|team) clamps parallelism and requires
disjoint ownership before fan-out.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from harness.tasktool.expansion import ExpansionDecision
from harness.tasktool.models import paths_overlap

FanoutProfileName = Literal["solo", "team"]

DENY_CODE = "DECOMPOSE_DENIED"


@dataclass(frozen=True, slots=True)
class FanoutProfile:
    """Resource + decompose caps for one economics profile."""

    name: FanoutProfileName
    max_tasktool_jobs: int
    max_agent_slots: int
    max_heavy_jobs: int
    max_gates: int
    decompose_max_children: int
    # When economics is on, solo prefers fewer parallel agents.
    prefer_direct_under: int = 2  # children < this → treat as not worth fan-out


PROFILES: dict[str, FanoutProfile] = {
    "solo": FanoutProfile(
        name="solo",
        max_tasktool_jobs=4,
        max_agent_slots=4,
        max_heavy_jobs=2,
        max_gates=2,
        decompose_max_children=4,
        prefer_direct_under=2,
    ),
    "team": FanoutProfile(
        name="team",
        max_tasktool_jobs=12,
        max_agent_slots=12,
        max_heavy_jobs=5,
        max_gates=4,
        decompose_max_children=12,
        prefer_direct_under=2,
    ),
}


@dataclass(frozen=True, slots=True)
class BreadthVerdict:
    """Result of the pre-fan-out breadth / isolation check."""

    ok: bool
    code: str = ""
    reason: str = ""
    errors: tuple[str, ...] = ()

    @property
    def denied(self) -> bool:
        return not self.ok


def economics_enabled(explicit: bool | None = None) -> bool:
    """Resolve aggressive economics flag (default OFF — solo-safe)."""
    if explicit is not None:
        return explicit
    return os.environ.get("HARNESS_ECONOMICS", "0") == "1"


def resolve_fanout_profile(
    name: str | None = None,
) -> FanoutProfile:
    """Return solo|team profile; unknown names fall back to solo."""
    raw = (name or os.environ.get("HARNESS_FANOUT_PROFILE", "solo")).strip().lower()
    return PROFILES.get(raw, PROFILES["solo"])


def assess_breadth(
    decision: ExpansionDecision,
    *,
    parent_files_owned: Sequence[str] | None = None,
    max_children: int | None = None,
    force: bool | None = None,
) -> BreadthVerdict:
    """Breadth check before multi-agent fan-out.

    When economics is off, always OK (validation still runs via
    ``validate_expansion``). When on: deny overlapping ownership, identical
    ownership sets, or fan-out that does not buy isolation.
    """
    if not economics_enabled(force):
        return BreadthVerdict(ok=True)
    if not decision.decompose:
        return BreadthVerdict(ok=True)

    errors: list[str] = []
    subtasks = decision.subtasks
    if len(subtasks) < 2:
        errors.append("fan-out needs ≥2 isolated subtasks; otherwise implement directly")
    profile = resolve_fanout_profile()
    cap = max_children if max_children is not None else profile.decompose_max_children
    if len(subtasks) > cap:
        errors.append(f"too many subtasks for profile {profile.name}: {len(subtasks)} > {cap}")

    # Pairwise disjoint ownership (same rule as validate_expansion, re-stated
    # so economics can deny without relying on retry feedback alone).
    owned: list[tuple[str, str]] = []
    for brief in subtasks:
        for pattern in brief.files_owned:
            for other_id, other_pattern in owned:
                if other_id != brief.child_id and paths_overlap(pattern, other_pattern):
                    errors.append(
                        f"not disjoint: {brief.child_id} ({pattern!r}) overlaps "
                        f"{other_id} ({other_pattern!r})"
                    )
            owned.append((brief.child_id, pattern))

    # Identical ownership sets → multi-agent tax without isolation.
    ownership_sets = [frozenset(b.files_owned) for b in subtasks if b.files_owned]
    if len(ownership_sets) >= 2 and len(set(ownership_sets)) == 1:
        errors.append("identical files_owned across children — no isolation benefit")

    # All children inherit the full parent glob without narrowing → deny.
    parent = [p.strip() for p in (parent_files_owned or ()) if p.strip()]
    if parent and subtasks:
        parent_set = frozenset(parent)
        if all(frozenset(b.files_owned) == parent_set for b in subtasks):
            errors.append(
                "children mirror parent ownership wholesale — partition before fan-out"
            )

    if errors:
        return BreadthVerdict(
            ok=False,
            code=DENY_CODE,
            reason="; ".join(errors[:6]),
            errors=tuple(errors),
        )
    return BreadthVerdict(ok=True)


def effective_resource_limits(
    *,
    profile: FanoutProfile | None = None,
    force_economics: bool | None = None,
    baseline_jobs: int = 12,
    baseline_slots: int = 12,
    baseline_heavy: int = 5,
    baseline_gates: int = 4,
    baseline_children: int = 12,
) -> tuple[int, int, int, int, int]:
    """Return (jobs, slots, heavy, gates, max_children) after economics profile.

    When economics is off, baseline (env/Settings) wins unchanged.
    """
    if not economics_enabled(force_economics):
        return (
            baseline_jobs,
            baseline_slots,
            baseline_heavy,
            baseline_gates,
            baseline_children,
        )
    p = profile or resolve_fanout_profile()
    return (
        min(baseline_jobs, p.max_tasktool_jobs),
        min(baseline_slots, p.max_agent_slots),
        min(baseline_heavy, p.max_heavy_jobs),
        min(baseline_gates, p.max_gates),
        min(baseline_children, p.decompose_max_children),
    )
