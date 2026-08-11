"""Versioned, replayable contracts for the pull-based TaskTool transport."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self, TypeVar

from harness.domain.enums import DispatchStatus, ModelTier, ResourceClass, Role

TASKTOOL_PROTOCOL_VERSION = "3.0"
EnumT = TypeVar("EnumT", bound=StrEnum)


class ContractError(ValueError):
    """A TaskTool artifact is structurally invalid or cannot be replayed safely."""


class TaskStage(StrEnum):
    PLAN = "plan"
    SPRINT_CONTRACT = "sprint_contract"  # V4 Phase 2: freeze AC before coding
    IMPLEMENT = "implement"
    GATE = "gate"
    REVIEW = "review"
    INTEGRATE = "integrate"


def normalize_owned_path(path: str) -> str:
    """Normalize a single-writer owned path; reject escapes and absolute roots."""
    raw = path.strip()
    if not raw or raw in {".", "/"}:
        raise ContractError(f"owned path must be a non-empty relative path: {path!r}")
    candidate = PurePosixPath(raw.replace("\\", "/"))
    if candidate.is_absolute():
        raise ContractError(f"owned path must be relative: {path!r}")
    parts = candidate.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ContractError(f"owned path must not contain '.' or '..': {path!r}")
    normalized = candidate.as_posix()
    if normalized.startswith("/") or normalized == "" or ".." in normalized.split("/"):
        raise ContractError(f"owned path escapes repository root: {path!r}")
    return normalized


def _has_glob_meta(pattern: str) -> bool:
    return any(char in pattern for char in "*?[")


def _static_root(pattern: str) -> str:
    """Longest concrete path prefix before the first glob metacharacter."""
    end = len(pattern)
    for index, char in enumerate(pattern):
        if char in "*?[":
            end = index
            break
    return pattern[:end].rstrip("/")


def _roots_compatible(left: str, right: str) -> bool:
    """True when static roots share a namespace (conservative overlap signal)."""
    if not left or not right:
        return True
    if left == right:
        return True
    if left.startswith(right + "/") or right.startswith(left + "/"):
        return True
    left_parent = str(PurePosixPath(left).parent)
    right_parent = str(PurePosixPath(right).parent)
    # Same non-root directory with globs (src/a* vs src/*b) → conservative overlap.
    # Parents of top-level roots are "." and must not collapse src/** vs tests/**.
    return (
        left_parent == right_parent
        and left_parent not in {"", "."}
    )


def paths_overlap(left: str, right: str) -> bool:  # noqa: PLR0911
    """True when ownership patterns may cover the same path.

    Exact matches and fnmatch coverage always overlap. Directory ``/**`` covers
    descendants. Intersecting globs such as ``src/a*.py`` vs ``src/*b.py`` are
    rejected via conservative static-root analysis — false-positive serialization
    is safer than concurrent writers.
    """
    if left == right:
        return True
    if fnmatch.fnmatch(left, right) or fnmatch.fnmatch(right, left):
        return True
    left_prefix = left[:-3] if left.endswith("/**") else left.rstrip("/")
    right_prefix = right[:-3] if right.endswith("/**") else right.rstrip("/")
    if left.endswith("/**") and (
        right == left_prefix or right.startswith(left_prefix + "/")
    ):
        return True
    if right.endswith("/**") and (
        left == right_prefix or left.startswith(right_prefix + "/")
    ):
        return True
    left_glob = _has_glob_meta(left)
    right_glob = _has_glob_meta(right)
    if not left_glob and not right_glob:
        return False
    left_root = _static_root(left)
    right_root = _static_root(right)
    # Distinct concrete directory trees (src/pkg/** vs src/other/**) are disjoint.
    if left.endswith("/**") or right.endswith("/**"):
        return _roots_compatible(left_root, right_root) and (
            left_root == right_root
            or left_root.startswith(right_root + "/")
            or right_root.startswith(left_root + "/")
            or not left_root
            or not right_root
        )
    return _roots_compatible(left_root, right_root)


def _required_str(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty string")
    return value


def _optional_str(data: dict[str, object], name: str) -> str:
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    return value


def _string_tuple(data: dict[str, object], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{name} must be an array of non-empty strings")
    return tuple(value)


def _enum_value(
    data: dict[str, object],
    name: str,
    enum_type: type[EnumT],
) -> EnumT:
    value = _required_str(data, name)
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ContractError(f"unsupported {name}: {value}") from exc


def _validate_timestamp(value: str, name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name} must include a timezone")


def _validate_protocol(value: str) -> None:
    if value != TASKTOOL_PROTOCOL_VERSION:
        raise ContractError(
            f"unsupported protocol_version {value!r}; expected {TASKTOOL_PROTOCOL_VERSION!r}"
        )


def _load_json(payload: str) -> dict[str, object]:
    try:
        value: object = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ContractError("artifact JSON must be an object")
    return value


@dataclass(frozen=True, slots=True)
class PlanTask:
    """One immutable DAG node embedded in a plan artifact."""

    task_id: str
    role: Role
    stage: TaskStage
    prompt_path: str
    result_path: str
    model: str
    model_tier: ModelTier
    repo: str
    worktree: str
    files_owned: tuple[str, ...]
    read_only_context: tuple[str, ...]
    frozen_contracts: tuple[str, ...]
    acceptance: tuple[str, ...]
    resource_class: ResourceClass
    source_document_paths: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    spec_sha256: str = ""
    frozen_spec_path: str = ""

    def __post_init__(self) -> None:
        for name in ("task_id", "prompt_path", "result_path", "model", "repo", "worktree"):
            if not getattr(self, name):
                raise ContractError(f"{name} must be non-empty")
        normalized = tuple(normalize_owned_path(path) for path in self.files_owned)
        object.__setattr__(self, "files_owned", normalized)
        if len(set(self.files_owned)) != len(self.files_owned):
            raise ContractError(f"task {self.task_id}: files_owned contains duplicates")
        for left_index, left in enumerate(self.files_owned):
            for right in self.files_owned[left_index + 1 :]:
                if paths_overlap(left, right):
                    raise ContractError(
                        f"task {self.task_id}: files_owned patterns overlap: "
                        f"{left!r} vs {right!r}"
                    )
        overlap = set(self.files_owned) & {
            path for path in self.read_only_context if not path.startswith("/")
        }
        if overlap:
            raise ContractError(
                f"task {self.task_id}: owned and read-only paths overlap: {sorted(overlap)}"
            )
        if self.task_id in self.dependencies:
            raise ContractError(f"task {self.task_id}: a task cannot depend on itself")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_id": self.task_id,
            "role": self.role.value,
            "stage": self.stage.value,
            "prompt_path": self.prompt_path,
            "result_path": self.result_path,
            "model": self.model,
            "model_tier": self.model_tier.value,
            "repo": self.repo,
            "worktree": self.worktree,
            "files_owned": list(self.files_owned),
            "read_only_context": list(self.read_only_context),
            "frozen_contracts": list(self.frozen_contracts),
            "acceptance": list(self.acceptance),
            "resource_class": self.resource_class.value,
            "source_document_paths": list(self.source_document_paths),
            "dependencies": list(self.dependencies),
        }
        if self.spec_sha256:
            payload["spec_sha256"] = self.spec_sha256
        if self.frozen_spec_path:
            payload["frozen_spec_path"] = self.frozen_spec_path
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        return cls(
            task_id=_required_str(data, "task_id"),
            role=_enum_value(data, "role", Role),
            stage=_enum_value(data, "stage", TaskStage),
            prompt_path=_required_str(data, "prompt_path"),
            result_path=_required_str(data, "result_path"),
            model=_required_str(data, "model"),
            model_tier=_enum_value(data, "model_tier", ModelTier),
            repo=_required_str(data, "repo"),
            worktree=_required_str(data, "worktree"),
            files_owned=_string_tuple(data, "files_owned"),
            read_only_context=_string_tuple(data, "read_only_context"),
            frozen_contracts=_string_tuple(data, "frozen_contracts"),
            acceptance=_string_tuple(data, "acceptance"),
            resource_class=_enum_value(data, "resource_class", ResourceClass),
            source_document_paths=_string_tuple(data, "source_document_paths"),
            dependencies=_string_tuple(data, "dependencies"),
            spec_sha256=_optional_str(data, "spec_sha256"),
            frozen_spec_path=_optional_str(data, "frozen_spec_path"),
        )


@dataclass(frozen=True, slots=True)
class PlanArtifact:
    """Frozen run plan; validation makes replay independent of planner state."""

    run_id: str
    goal: str
    project: str
    repo: str
    base_branch: str
    spec_sources: tuple[str, ...]
    tasks: tuple[PlanTask, ...]
    created_at: str
    updated_at: str
    protocol_version: str = TASKTOOL_PROTOCOL_VERSION
    plan_hash: str = ""

    def __post_init__(self) -> None:
        _validate_protocol(self.protocol_version)
        for name in ("run_id", "goal", "project", "repo", "base_branch"):
            if not getattr(self, name):
                raise ContractError(f"{name} must be non-empty")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.updated_at, "updated_at")
        self._validate_dag()
        self._validate_single_writer()

    def _validate_dag(self) -> None:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ContractError("plan contains duplicate task IDs")
        known = set(task_ids)
        for task in self.tasks:
            missing = set(task.dependencies) - known
            if missing:
                raise ContractError(
                    f"task {task.task_id}: missing dependencies {sorted(missing)}"
                )

        dependencies = {task.task_id: task.dependencies for task in self.tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ContractError(f"dependency cycle includes task {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)

    def _validate_single_writer(self) -> None:
        owned: list[tuple[str, str]] = []
        for task in self.tasks:
            for path in task.files_owned:
                for other_task, other_path in owned:
                    if paths_overlap(path, other_path):
                        raise ContractError(
                            f"files_owned overlap between tasks {other_task} and "
                            f"{task.task_id}: {other_path!r} vs {path!r}"
                        )
                owned.append((task.task_id, path))

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "goal": self.goal,
            "project": self.project,
            "repo": self.repo,
            "base_branch": self.base_branch,
            "spec_sources": list(self.spec_sources),
            "tasks": [task.to_dict() for task in self.tasks],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.plan_hash:
            payload["plan_hash"] = self.plan_hash
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list) or any(not isinstance(task, dict) for task in raw_tasks):
            raise ContractError("tasks must be an array of objects")
        tasks = tuple(PlanTask.from_dict(task) for task in raw_tasks if isinstance(task, dict))
        return cls(
            protocol_version=_required_str(data, "protocol_version"),
            run_id=_required_str(data, "run_id"),
            goal=_required_str(data, "goal"),
            project=_required_str(data, "project"),
            repo=_required_str(data, "repo"),
            base_branch=_required_str(data, "base_branch"),
            spec_sources=_string_tuple(data, "spec_sources"),
            tasks=tasks,
            created_at=_required_str(data, "created_at"),
            updated_at=_required_str(data, "updated_at"),
            plan_hash=_optional_str(data, "plan_hash"),
        )

    @classmethod
    def from_json(cls, payload: str) -> Self:
        return cls.from_dict(_load_json(payload))


@dataclass(frozen=True, slots=True)
class DispatchEnvelope:
    """A queue item that points to prompt/result files instead of carrying prompts."""

    run_id: str
    dispatch_id: str
    task_id: str
    role: Role
    stage: TaskStage
    status: DispatchStatus
    prompt_path: str
    result_path: str
    model: str
    model_tier: ModelTier
    repo: str
    worktree: str
    files_owned: tuple[str, ...]
    read_only_context: tuple[str, ...]
    frozen_contracts: tuple[str, ...]
    acceptance: tuple[str, ...]
    resource_class: ResourceClass
    source_document_paths: tuple[str, ...]
    dependencies: tuple[str, ...]
    created_at: str
    updated_at: str
    protocol_version: str = TASKTOOL_PROTOCOL_VERSION
    # Phase 0.5: 1–3 curated skill paths injected into the worker/reviewer prompt.
    skill_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_protocol(self.protocol_version)
        for name in (
            "run_id",
            "dispatch_id",
            "task_id",
            "prompt_path",
            "result_path",
            "model",
            "repo",
            "worktree",
        ):
            if not getattr(self, name):
                raise ContractError(f"{name} must be non-empty")
        _validate_timestamp(self.created_at, "created_at")
        _validate_timestamp(self.updated_at, "updated_at")
        normalized = tuple(normalize_owned_path(path) for path in self.files_owned)
        object.__setattr__(self, "files_owned", normalized)
        if set(self.files_owned) & {
            path for path in self.read_only_context if not path.startswith("/")
        }:
            raise ContractError("files_owned and read_only_context must not overlap")
        if len(self.skill_paths) > 3:
            raise ContractError("skill_paths must contain at most 3 entries")
        for path in self.skill_paths:
            if not path or not isinstance(path, str):
                raise ContractError("skill_paths entries must be non-empty strings")

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "dispatch_id": self.dispatch_id,
            "task_id": self.task_id,
            "role": self.role.value,
            "stage": self.stage.value,
            "status": self.status.value,
            "prompt_path": self.prompt_path,
            "result_path": self.result_path,
            "model": self.model,
            "model_tier": self.model_tier.value,
            "repo": self.repo,
            "worktree": self.worktree,
            "files_owned": list(self.files_owned),
            "read_only_context": list(self.read_only_context),
            "frozen_contracts": list(self.frozen_contracts),
            "acceptance": list(self.acceptance),
            "resource_class": self.resource_class.value,
            "source_document_paths": list(self.source_document_paths),
            "dependencies": list(self.dependencies),
            "skill_paths": list(self.skill_paths),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        return cls(
            protocol_version=_required_str(data, "protocol_version"),
            run_id=_required_str(data, "run_id"),
            dispatch_id=_required_str(data, "dispatch_id"),
            task_id=_required_str(data, "task_id"),
            role=_enum_value(data, "role", Role),
            stage=_enum_value(data, "stage", TaskStage),
            status=_enum_value(data, "status", DispatchStatus),
            prompt_path=_required_str(data, "prompt_path"),
            result_path=_required_str(data, "result_path"),
            model=_required_str(data, "model"),
            model_tier=_enum_value(data, "model_tier", ModelTier),
            repo=_required_str(data, "repo"),
            worktree=_required_str(data, "worktree"),
            files_owned=_string_tuple(data, "files_owned"),
            read_only_context=_string_tuple(data, "read_only_context"),
            frozen_contracts=_string_tuple(data, "frozen_contracts"),
            acceptance=_string_tuple(data, "acceptance"),
            resource_class=_enum_value(data, "resource_class", ResourceClass),
            source_document_paths=_string_tuple(data, "source_document_paths"),
            dependencies=_string_tuple(data, "dependencies"),
            created_at=_required_str(data, "created_at"),
            updated_at=_required_str(data, "updated_at"),
            skill_paths=_optional_string_tuple(data, "skill_paths"),
        )

    @classmethod
    def from_json(cls, payload: str) -> Self:
        return cls.from_dict(_load_json(payload))


def _optional_string_tuple(data: dict[str, object], name: str) -> tuple[str, ...]:
    """Backward-compatible optional string list (missing → empty)."""
    if name not in data or data[name] is None:
        return ()
    return _string_tuple(data, name)
