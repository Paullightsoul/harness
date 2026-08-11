"""TaskTool model routing: defaults, env overrides, roles, complexity tiers."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import ModelTier, ResourceClass, Role
from harness.tasktool.model_map import (
    AVAILABLE_TASKTOOL_MODELS,
    DEFAULT_TASKTOOL_ORCH_MODEL,
    DEFAULT_TASKTOOL_REVIEWER_MODEL,
    DEFAULT_TASKTOOL_WORKER_MODEL,
    model_for,
    resolve_tasktool_model,
    resource_for,
    tasktool_role_models,
    tier_for_complexity,
)
from harness.tasktool.models import TaskStage

_TASKTOOL_ENVS = (
    "TASKTOOL_ORCH_MODEL",
    "TASKTOOL_WORKER_MODEL",
    "TASKTOOL_REVIEWER_MODEL",
)
_LEGACY_ENVS = ("ORCH_MODEL", "WORKER_MODEL", "REVIEWER_MODEL")


@pytest.fixture(autouse=True)
def _isolate_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*_TASKTOOL_ENVS, *_LEGACY_ENVS):
        monkeypatch.delenv(key, raising=False)


def test_available_slugs_include_tasktool_defaults() -> None:
    assert DEFAULT_TASKTOOL_ORCH_MODEL in AVAILABLE_TASKTOOL_MODELS
    assert DEFAULT_TASKTOOL_WORKER_MODEL in AVAILABLE_TASKTOOL_MODELS
    assert DEFAULT_TASKTOOL_REVIEWER_MODEL in AVAILABLE_TASKTOOL_MODELS
    assert DEFAULT_TASKTOOL_ORCH_MODEL == "cursor-grok-4.5-high"
    assert DEFAULT_TASKTOOL_REVIEWER_MODEL == "cursor-grok-4.5-high"
    assert DEFAULT_TASKTOOL_WORKER_MODEL == "cursor-grok-4.5-high"


def test_defaults_independent_of_legacy_settings(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)

    orch, orch_tier = model_for(settings, Role.ORCHESTRATOR, "small")
    worker, worker_tier = model_for(settings, Role.WORKER, "small")
    reviewer, reviewer_tier = model_for(settings, Role.REVIEWER, "small")

    assert orch == DEFAULT_TASKTOOL_ORCH_MODEL
    assert worker == DEFAULT_TASKTOOL_WORKER_MODEL
    assert reviewer == DEFAULT_TASKTOOL_REVIEWER_MODEL
    assert orch_tier is ModelTier.STANDARD
    assert worker_tier is ModelTier.STANDARD
    assert reviewer_tier is ModelTier.STANDARD
    # Legacy Settings defaults remain untouched (config.py / ORCH_MODEL etc.).
    assert settings.role(Role.ORCHESTRATOR).model == "claude-opus-4-8"
    assert settings.role(Role.WORKER).model == "kimi-k2.5"
    assert settings.role(Role.REVIEWER).model == "glm-5.2"


def test_legacy_env_does_not_affect_tasktool_routing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ORCH_MODEL", "claude-opus-4-8-thinking-high")
    monkeypatch.setenv("WORKER_MODEL", "auto")
    monkeypatch.setenv("REVIEWER_MODEL", "glm-5.2-high")
    settings = Settings(root=tmp_path)

    assert settings.role(Role.ORCHESTRATOR).model == "claude-opus-4-8-thinking-high"
    assert settings.role(Role.WORKER).model == "auto"
    assert settings.role(Role.REVIEWER).model == "glm-5.2-high"

    assert model_for(settings, Role.ORCHESTRATOR, "medium")[0] == DEFAULT_TASKTOOL_ORCH_MODEL
    assert model_for(settings, Role.WORKER, "medium")[0] == DEFAULT_TASKTOOL_WORKER_MODEL
    assert model_for(settings, Role.REVIEWER, "medium")[0] == DEFAULT_TASKTOOL_REVIEWER_MODEL


def test_tasktool_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TASKTOOL_ORCH_MODEL", "gpt-5.6-terra-medium")
    monkeypatch.setenv("TASKTOOL_WORKER_MODEL", "composer-2.5-fast")
    monkeypatch.setenv("TASKTOOL_REVIEWER_MODEL", "claude-sonnet-5-thinking-max")
    settings = Settings(root=tmp_path)

    assert model_for(settings, Role.ORCHESTRATOR, "small")[0] == "gpt-5.6-terra-medium"
    assert model_for(settings, Role.WORKER, "small")[0] == "composer-2.5-fast"
    assert model_for(settings, Role.REVIEWER, "small")[0] == "claude-sonnet-5-thinking-max"
    assert tasktool_role_models() == {
        "orchestrator": "gpt-5.6-terra-medium",
        "worker": "composer-2.5-fast",
        "reviewer": "claude-sonnet-5-thinking-max",
    }


def test_invalid_tasktool_env_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKTOOL_WORKER_MODEL", "auto")
    with pytest.raises(ValueError, match="not an available TaskTool model slug"):
        tasktool_role_models()


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("planner", DEFAULT_TASKTOOL_ORCH_MODEL),
        ("orchestrator", DEFAULT_TASKTOOL_ORCH_MODEL),
        ("root", DEFAULT_TASKTOOL_ORCH_MODEL),
        (Role.ORCHESTRATOR, DEFAULT_TASKTOOL_ORCH_MODEL),
        ("worker", DEFAULT_TASKTOOL_WORKER_MODEL),
        ("leaf", DEFAULT_TASKTOOL_WORKER_MODEL),
        ("sub-orchestrator", DEFAULT_TASKTOOL_WORKER_MODEL),
        ("research", DEFAULT_TASKTOOL_WORKER_MODEL),
        ("researching", DEFAULT_TASKTOOL_WORKER_MODEL),
        ("implement", DEFAULT_TASKTOOL_WORKER_MODEL),
        (Role.WORKER, DEFAULT_TASKTOOL_WORKER_MODEL),
        ("reviewer", DEFAULT_TASKTOOL_REVIEWER_MODEL),
        ("goal-judge", DEFAULT_TASKTOOL_REVIEWER_MODEL),
        (Role.REVIEWER, DEFAULT_TASKTOOL_REVIEWER_MODEL),
    ],
)
def test_resolve_role_and_kind_aliases(alias: Role | str, expected: str) -> None:
    assert resolve_tasktool_model(alias) == expected


def test_kind_overrides_role_for_goal_judge_and_implement(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)
    # Controller passes REVIEWER for goal-judge; kind may refine explicitly.
    model, _ = model_for(settings, Role.REVIEWER, "large", kind="goal-judge")
    assert model == DEFAULT_TASKTOOL_REVIEWER_MODEL
    model, _ = model_for(settings, Role.WORKER, "small", kind="implement")
    assert model == DEFAULT_TASKTOOL_WORKER_MODEL
    # kind wins when it disagrees with role (docs/tests mapping).
    assert (
        resolve_tasktool_model(Role.WORKER, kind="goal-judge")
        == DEFAULT_TASKTOOL_REVIEWER_MODEL
    )
    assert (
        resolve_tasktool_model(Role.REVIEWER, kind="sub-orchestrator")
        == DEFAULT_TASKTOOL_WORKER_MODEL
    )


@pytest.mark.parametrize(
    ("complexity", "tier"),
    [
        ("trivial", ModelTier.LIGHT),
        ("small", ModelTier.STANDARD),
        ("medium", ModelTier.STANDARD),
        ("normal", ModelTier.STANDARD),
        ("large", ModelTier.HEAVY),
        ("high", ModelTier.HEAVY),
        ("LARGE", ModelTier.HEAVY),
    ],
)
def test_complexity_tiers_not_tied_to_model_name(
    complexity: str,
    tier: ModelTier,
    tmp_path: Path,
) -> None:
    settings = Settings(root=tmp_path)
    model, got = model_for(settings, Role.WORKER, complexity)
    assert model == DEFAULT_TASKTOOL_WORKER_MODEL
    assert got is tier
    assert tier_for_complexity(complexity) is tier


def test_resource_for_maps_stage_and_tier() -> None:
    assert resource_for(TaskStage.GATE, ModelTier.STANDARD) is ResourceClass.GATE
    assert resource_for(TaskStage.IMPLEMENT, ModelTier.HEAVY) is ResourceClass.HEAVY
    assert resource_for(TaskStage.IMPLEMENT, ModelTier.LIGHT) is ResourceClass.LIGHT
    assert resource_for(TaskStage.REVIEW, ModelTier.STANDARD) is ResourceClass.STANDARD


def test_controller_compatible_signature(tmp_path: Path) -> None:
    """Preserve ``model_for(settings, role, complexity)`` call shape."""
    settings = Settings(root=tmp_path)
    model, tier = model_for(settings, Role.WORKER, "medium")
    assert model == DEFAULT_TASKTOOL_WORKER_MODEL
    assert tier is ModelTier.STANDARD
    model, tier = model_for(settings, Role.REVIEWER, "large")
    assert model == DEFAULT_TASKTOOL_REVIEWER_MODEL
    assert tier is ModelTier.HEAVY
