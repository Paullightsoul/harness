"""Public contracts for TaskTool-first planning and dispatch."""

from typing import TYPE_CHECKING

from harness.domain.enums import DispatchStatus, ModelTier, ResourceClass
from harness.tasktool.models import (
    TASKTOOL_PROTOCOL_VERSION,
    ContractError,
    DispatchEnvelope,
    PlanArtifact,
    PlanTask,
    TaskStage,
)
from harness.tasktool.resources import (
    ResourceDecision,
    ResourceDisposition,
    ResourcePolicy,
    ResourceSnapshot,
)

if TYPE_CHECKING:
    from harness.tasktool.controller import TaskToolController

__all__ = [
    "TASKTOOL_PROTOCOL_VERSION",
    "ContractError",
    "DispatchEnvelope",
    "DispatchStatus",
    "ModelTier",
    "PlanArtifact",
    "PlanTask",
    "ResourceClass",
    "ResourceDecision",
    "ResourceDisposition",
    "ResourcePolicy",
    "ResourceSnapshot",
    "TaskToolController",
    "TaskStage",
]


def __getattr__(name: str) -> object:
    if name == "TaskToolController":
        from harness.tasktool.controller import TaskToolController  # noqa: PLC0415

        return TaskToolController
    raise AttributeError(name)
