"""Deterministic TaskTool model and resource selection for pull dispatches.

TaskTool routing is independent of legacy SDK/CLI ``ORCH_MODEL`` /
``WORKER_MODEL`` / ``REVIEWER_MODEL`` on :class:`~harness.config.Settings`.
Overrides use ``TASKTOOL_*_MODEL`` only and must be exact Cursor Task Tool
slugs from :data:`AVAILABLE_TASKTOOL_MODELS`.

Default policy (2026-08-05): **Grok-first** — orch / worker / reviewer /
research aliases all default to ``cursor-grok-4.5-high``. See
``docs/GROK-DEFAULTS.md``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from harness.config import Settings
from harness.domain.enums import ModelTier, ResourceClass, Role
from harness.tasktool.models import TaskStage

# Exact Cursor Task Tool model slugs (no legacy SDK/CLI aliases).
AVAILABLE_TASKTOOL_MODELS: Final[frozenset[str]] = frozenset(
    {
        "claude-4.6-opus-high-thinking",
        "claude-fable-5-thinking-high",
        "claude-opus-4-7-thinking-xhigh",
        "claude-opus-4-8-thinking-high",
        "claude-opus-5-thinking-high",
        "claude-sonnet-5-thinking-max",
        "composer-2.5",
        "composer-2.5-fast",
        "cursor-grok-4.5-high",
        "glm-5.2-high",
        "gpt-5.6-sol-medium",
        "gpt-5.6-terra-medium",
        "kimi-k2.7-code",
        "kimi-k3-max",
    }
)

# Grok-first defaults (env TASKTOOL_*_MODEL still overrides).
_GROK: Final[str] = "cursor-grok-4.5-high"
DEFAULT_TASKTOOL_ORCH_MODEL: Final[str] = _GROK
DEFAULT_TASKTOOL_WORKER_MODEL: Final[str] = _GROK
DEFAULT_TASKTOOL_REVIEWER_MODEL: Final[str] = _GROK

_ENV_ORCH: Final[str] = "TASKTOOL_ORCH_MODEL"
_ENV_WORKER: Final[str] = "TASKTOOL_WORKER_MODEL"
_ENV_REVIEWER: Final[str] = "TASKTOOL_REVIEWER_MODEL"

# Planner / root → orch model (Grok by default).
_ORCH_KEYS: Final[frozenset[str]] = frozenset(
    {
        "orchestrator",
        "planner",
        "root",
        Role.ORCHESTRATOR.value,
    }
)
# Worker / leaf / implement / sub-orchestrator / research → worker model (Grok).
# Sub-orchestrator is read-only research/decompose, not the GPT planner seat.
_WORKER_KEYS: Final[frozenset[str]] = frozenset(
    {
        "worker",
        "leaf",
        "implement",
        "de-sloppify",
        "de_sloppify",
        "sub-orchestrator",
        "sub_orchestrator",
        "research",
        "researching",
        Role.WORKER.value,
    }
)
# Reviewer / goal-judge → reviewer model (Grok by default).
_REVIEWER_KEYS: Final[frozenset[str]] = frozenset(
    {"reviewer", "goal-judge", "goal_judge", Role.REVIEWER.value}
)


def _normalize_key(value: Role | str) -> str:
    if isinstance(value, Role):
        return value.value
    return str(value).strip().lower().replace(" ", "-")


def tasktool_role_models(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return TaskTool default/override slugs for orch/worker/reviewer.

    Reads ``TASKTOOL_*_MODEL`` from ``environ`` (or ``os.environ``). Does not
    consult legacy ``ORCH_MODEL`` / ``WORKER_MODEL`` / ``REVIEWER_MODEL``.
    """
    env = os.environ if environ is None else environ

    def pick(var: str, default: str) -> str:
        raw = (env.get(var) or "").strip()
        if not raw:
            return default
        if raw not in AVAILABLE_TASKTOOL_MODELS:
            raise ValueError(
                f"{var}={raw!r} is not an available TaskTool model slug; "
                f"expected one of {sorted(AVAILABLE_TASKTOOL_MODELS)}"
            )
        return raw

    return {
        "orchestrator": pick(_ENV_ORCH, DEFAULT_TASKTOOL_ORCH_MODEL),
        "worker": pick(_ENV_WORKER, DEFAULT_TASKTOOL_WORKER_MODEL),
        "reviewer": pick(_ENV_REVIEWER, DEFAULT_TASKTOOL_REVIEWER_MODEL),
    }


def resolve_tasktool_model(
    role: Role | str,
    *,
    kind: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Map a TaskTool role/kind alias to a concrete model slug.

    Planner/orchestrator/root use the orch default; reviewer/goal-judge use
    the reviewer default; worker/leaf/implement/sub-orchestrator/research use
    the worker default. ``kind`` wins over ``role`` when both are provided.
    """
    models = tasktool_role_models(environ)
    key = _normalize_key(kind if kind is not None else role)
    if key in _ORCH_KEYS:
        return models["orchestrator"]
    if key in _REVIEWER_KEYS:
        return models["reviewer"]
    if key in _WORKER_KEYS:
        return models["worker"]
    # Unknown aliases: fall back by Role enum when possible.
    if isinstance(role, Role):
        if role is Role.ORCHESTRATOR:
            return models["orchestrator"]
        if role is Role.REVIEWER:
            return models["reviewer"]
        return models["worker"]
    raise ValueError(f"unknown TaskTool model role/kind: {key!r}")


def tier_for_complexity(complexity: str) -> ModelTier:
    """Map task complexity to a transport tier (independent of model name)."""
    normalized = complexity.lower().strip()
    if normalized in {"large", "high"}:
        return ModelTier.HEAVY
    if normalized == "trivial":
        return ModelTier.LIGHT
    return ModelTier.STANDARD


def model_for(
    settings: Settings,
    role: Role,
    complexity: str,
    *,
    kind: str | None = None,
) -> tuple[str, ModelTier]:
    """Return the TaskTool model plus a stable transport tier.

    ``settings`` is retained for call-site compatibility with the controller;
    model selection does **not** read ``settings.roles`` / legacy env names.
    Optional ``kind`` (e.g. ``goal-judge``, ``implement``) refines routing.
    """
    _ = settings  # API compat; TaskTool routing ignores Settings.roles.
    model = resolve_tasktool_model(role, kind=kind)
    return model, tier_for_complexity(complexity)


def resource_for(stage: TaskStage, tier: ModelTier) -> ResourceClass:
    """Map transport stages to admission-control classes."""
    if stage is TaskStage.GATE:
        return ResourceClass.GATE
    if stage in {TaskStage.PLAN, TaskStage.SPRINT_CONTRACT}:
        # Sub-orchestrator / sprint-contract are read-only research: must not
        # consume the scarce heavy slots reserved for large implement work.
        return ResourceClass.STANDARD
    if tier is ModelTier.HEAVY:
        return ResourceClass.HEAVY
    if tier is ModelTier.LIGHT:
        return ResourceClass.LIGHT
    return ResourceClass.STANDARD
