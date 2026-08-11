"""Agent-in-agent expansion: sub-orchestrator decomposition of one plan task.

A ``large`` task first receives a *sub-orchestrator* dispatch (stage ``plan``).
Its result may contain a fenced ``json`` block that decomposes the task into
child subtasks. This module parses that block, validates the proposal against
the frozen parent ownership (children partition the parent, never widen it),
freezes child specs into the run spool, and materializes child ``PlanTask``
rows as a versioned ``ExpansionArtifact`` — the root ``PlanArtifact`` stays
frozen and untouched.

The root Cursor chat remains the only Task Tool dispatcher (ADR-0011); the
control plane only turns the sub-orchestrator's *plan* into durable child
dispatches.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from harness.domain.enums import ModelTier, ResourceClass, Role
from harness.tasktool.models import (
    TASKTOOL_PROTOCOL_VERSION,
    ContractError,
    PlanTask,
    TaskStage,
    normalize_owned_path,
    paths_overlap,
)

MAX_CHILDREN_DEFAULT = 8
_ALLOWED_CHILD_COMPLEXITY = {"trivial", "small", "normal", "medium"}
_FENCED_JSON = re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL)
_CHILD_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


@dataclass(frozen=True, slots=True)
class SubtaskBrief:
    """One proposed child task from the sub-orchestrator's decomposition."""

    child_id: str
    title: str
    summary: str
    files_owned: tuple[str, ...]
    depends_on: tuple[str, ...]
    acceptance: tuple[str, ...]
    complexity: str


@dataclass(frozen=True, slots=True)
class ExpansionDecision:
    """Parsed sub-orchestrator verdict: decompose with subtasks, or implement directly."""

    decompose: bool
    reason: str = ""
    subtasks: tuple[SubtaskBrief, ...] = ()


@dataclass(frozen=True, slots=True)
class ExpansionArtifact:
    """Frozen decomposition of one parent task — replayable like a plan."""

    run_id: str
    parent_task_id: str
    reason: str
    children: tuple[PlanTask, ...]
    created_at: str
    protocol_version: str = TASKTOOL_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "reason": self.reason,
            "children": [child.to_dict() for child in self.children],
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> Self:
        raw: object = json.loads(payload)
        if not isinstance(raw, dict):
            raise ContractError("expansion artifact must be a JSON object")
        children_raw = raw.get("children")
        if not isinstance(children_raw, list):
            raise ContractError("expansion children must be an array")
        return cls(
            run_id=str(raw.get("run_id") or ""),
            parent_task_id=str(raw.get("parent_task_id") or ""),
            reason=str(raw.get("reason") or ""),
            children=tuple(
                PlanTask.from_dict(item) for item in children_raw if isinstance(item, dict)
            ),
            created_at=str(raw.get("created_at") or ""),
            protocol_version=str(
                raw.get("protocol_version") or TASKTOOL_PROTOCOL_VERSION
            ),
        )


def parse_expansion(text: str) -> ExpansionDecision | None:
    """Extract the last valid decomposition JSON block from sub-orchestrator text.

    Returns ``None`` when no well-formed block exists (caller treats the result
    as malformed and retries with feedback instead of guessing).
    """
    decision: ExpansionDecision | None = None
    for match in _FENCED_JSON.finditer(text):
        try:
            raw: object = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict) or "decompose" not in raw:
            continue
        parsed = _decision_from_dict(raw)
        if parsed is not None:
            decision = parsed
    return decision


def _decision_from_dict(raw: dict[str, object]) -> ExpansionDecision | None:
    decompose = raw.get("decompose")
    if not isinstance(decompose, bool):
        return None
    reason = str(raw.get("reason") or "")
    if not decompose:
        return ExpansionDecision(decompose=False, reason=reason)
    subtasks_raw = raw.get("subtasks")
    if not isinstance(subtasks_raw, list) or not subtasks_raw:
        return None
    briefs: list[SubtaskBrief] = []
    for index, item in enumerate(subtasks_raw, start=1):
        if not isinstance(item, dict):
            return None
        briefs.append(
            SubtaskBrief(
                child_id=_slug(str(item.get("id") or f"s{index}")),
                title=str(item.get("title") or "").strip(),
                summary=str(item.get("summary") or "").strip(),
                files_owned=_str_tuple(item.get("files_owned")),
                depends_on=tuple(
                    _slug(value) for value in _str_tuple(item.get("depends_on"))
                ),
                acceptance=_str_tuple(item.get("acceptance")),
                complexity=str(item.get("complexity") or "small").strip().lower(),
            )
        )
    return ExpansionDecision(decompose=True, reason=reason, subtasks=tuple(briefs))


def validate_expansion(  # noqa: PLR0912 — one linear checklist, split hurts readability
    decision: ExpansionDecision,
    *,
    parent_files_owned: Sequence[str],
    existing_task_ids: Sequence[str],
    parent_task_id: str,
    max_children: int = MAX_CHILDREN_DEFAULT,
) -> list[str]:
    """Deterministic checks; returns human-readable errors (empty = valid)."""
    errors: list[str] = []
    subtasks = decision.subtasks
    if not decision.decompose:
        return errors
    if not subtasks:
        return ["decompose=true requires a non-empty subtasks array"]
    if len(subtasks) < 2:
        errors.append("decomposition needs at least 2 subtasks; otherwise decompose=false")
    if len(subtasks) > max_children:
        errors.append(f"too many subtasks: {len(subtasks)} > {max_children}")

    parent_patterns = [pattern.strip() for pattern in parent_files_owned if pattern.strip()]
    seen_ids: set[str] = set()
    taken = set(existing_task_ids)
    for brief in subtasks:
        label = f"subtask {brief.child_id or '?'}"
        if not _CHILD_ID.match(brief.child_id):
            errors.append(f"{label}: id must match [a-z0-9-] (got {brief.child_id!r})")
            continue
        if brief.child_id in seen_ids:
            errors.append(f"{label}: duplicate subtask id")
        seen_ids.add(brief.child_id)
        full_id = child_task_id(parent_task_id, brief.child_id)
        if full_id in taken:
            errors.append(f"{label}: task id {full_id} already exists in the run")
        if not brief.title:
            errors.append(f"{label}: title is required")
        if brief.complexity not in _ALLOWED_CHILD_COMPLEXITY:
            errors.append(
                f"{label}: complexity {brief.complexity!r} not allowed for children "
                f"(use one of {sorted(_ALLOWED_CHILD_COMPLEXITY)}; no recursive large)"
            )
        if not brief.files_owned:
            errors.append(f"{label}: files_owned must be non-empty")
        for pattern in brief.files_owned:
            try:
                normalized = normalize_owned_path(pattern)
            except ContractError as exc:
                errors.append(f"{label}: {exc}")
                continue
            if parent_patterns and not any(
                pattern_covered_by(normalized, parent) for parent in parent_patterns
            ):
                errors.append(
                    f"{label}: owned path {normalized!r} is outside parent ownership "
                    f"{parent_patterns}"
                )

    # Pairwise single-writer among children.
    owned: list[tuple[str, str]] = []
    for brief in subtasks:
        for pattern in brief.files_owned:
            for other_id, other_pattern in owned:
                if other_id != brief.child_id and paths_overlap(pattern, other_pattern):
                    errors.append(
                        f"subtask {brief.child_id} and {other_id} overlap: "
                        f"{pattern!r} vs {other_pattern!r}"
                    )
            owned.append((brief.child_id, pattern))

    errors.extend(_dag_errors(subtasks))
    return errors


def pattern_covered_by(child: str, parent: str) -> bool:
    """True when the parent ownership pattern fully covers the child pattern.

    Conservative: exact match, fnmatch coverage by the parent glob, or the
    child sitting under a parent ``dir/**`` subtree. Anything else is treated
    as escaping parent ownership.
    """
    if child == parent:
        return True
    if fnmatch.fnmatch(child, parent):
        return True
    if parent.endswith("/**"):
        prefix = parent[:-3]
        return child == prefix or child.startswith(prefix + "/")
    return False


def child_task_id(parent_task_id: str, child_id: str) -> str:
    return f"{parent_task_id}-{child_id}"


def build_child_plan_tasks(
    decision: ExpansionDecision,
    *,
    parent: PlanTask,
    run_id: str,
    spool: Path,
    model_for_child: Callable[[str], tuple[str, ModelTier, ResourceClass]],
) -> tuple[PlanTask, ...]:
    """Freeze child specs under the spool and return replayable child PlanTasks."""
    specs_dir = spool / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    children: list[PlanTask] = []
    for brief in decision.subtasks:
        task_id = child_task_id(parent.task_id, brief.child_id)
        spec_path = specs_dir / f"{task_id}.md"
        spec_text = render_child_spec(brief, parent_task_id=parent.task_id, task_id=task_id)
        _atomic_write(spec_path, spec_text)
        digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        model, tier, resource_class = model_for_child(brief.complexity)
        children.append(
            PlanTask(
                task_id=task_id,
                role=Role.WORKER,
                stage=TaskStage.IMPLEMENT,
                prompt_path=str(spool / "prompts" / f"{task_id}.md"),
                result_path=str(spool / "results" / f"{task_id}.json"),
                model=model,
                model_tier=tier,
                repo=parent.repo,
                worktree=parent.worktree,
                files_owned=tuple(
                    normalize_owned_path(pattern) for pattern in brief.files_owned
                ),
                read_only_context=(str(spec_path),),
                frozen_contracts=parent.frozen_contracts,
                acceptance=brief.acceptance,
                resource_class=resource_class,
                source_document_paths=(str(spec_path),),
                dependencies=tuple(
                    child_task_id(parent.task_id, dep) for dep in brief.depends_on
                ),
                spec_sha256=digest,
                frozen_spec_path=str(spec_path),
            )
        )
    return tuple(children)


def render_child_spec(brief: SubtaskBrief, *, parent_task_id: str, task_id: str) -> str:
    """Markdown spec in the same shape task parsers/verifiers already understand."""
    files = "\n".join(f"- {pattern}" for pattern in brief.files_owned)
    acceptance = "\n".join(f"- {item}" for item in brief.acceptance) or "- HARNESS_DONE"
    depends = (
        ", ".join(child_task_id(parent_task_id, dep) for dep in brief.depends_on)
        or "-"
    )
    return (
        "---\n"
        f'id: "{task_id}"\n'
        f'title: "{brief.title}"\n'
        'status: "todo"\n'
        f'complexity: "{brief.complexity}"\n'
        "attempts: 0\n"
        "---\n"
        f"# Контекст\n{brief.summary or brief.title}\n\n"
        f"Родительская задача: {parent_task_id}. Depends on: {depends}\n\n"
        "Decompose: no\n\n"
        f"# Файлы\n{files}\n\n"
        f"# Acceptance criteria\n{acceptance}\n\n"
        "# Provides\n- (заполни интерфейсы для зависимых задач)\n"
    )


@dataclass(frozen=True, slots=True)
class ExpansionStore:
    """Reads/writes frozen expansion artifacts under the run spool."""

    spool: Path

    @property
    def directory(self) -> Path:
        return self.spool / "expansions"

    def path_for(self, parent_task_id: str) -> Path:
        return self.directory / f"{parent_task_id}.json"

    def write(self, artifact: ExpansionArtifact) -> tuple[Path, str]:
        path = self.path_for(artifact.parent_task_id)
        payload = artifact.to_json() + "\n"
        _atomic_write(path, payload)
        return path, hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def read(self, parent_task_id: str) -> ExpansionArtifact | None:
        path = self.path_for(parent_task_id)
        if not path.is_file():
            return None
        return ExpansionArtifact.from_json(path.read_text(encoding="utf-8"))

    def read_all(self) -> tuple[ExpansionArtifact, ...]:
        if not self.directory.is_dir():
            return ()
        artifacts: list[ExpansionArtifact] = []
        for path in sorted(self.directory.glob("*.json")):
            artifacts.append(ExpansionArtifact.from_json(path.read_text(encoding="utf-8")))
        return tuple(artifacts)


@dataclass(frozen=True)
class _Node:
    ident: str
    deps: tuple[str, ...] = field(default_factory=tuple)


def _dag_errors(subtasks: Sequence[SubtaskBrief]) -> list[str]:
    known = {brief.child_id for brief in subtasks}
    errors: list[str] = []
    for brief in subtasks:
        missing = set(brief.depends_on) - known
        if missing:
            errors.append(
                f"subtask {brief.child_id}: unknown depends_on {sorted(missing)}"
            )
        if brief.child_id in brief.depends_on:
            errors.append(f"subtask {brief.child_id}: depends on itself")
    if errors:
        return errors
    deps = {brief.child_id: brief.depends_on for brief in subtasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return False
        if node in visited:
            return True
        visiting.add(node)
        for dep in deps[node]:
            if not visit(dep):
                return False
        visiting.remove(node)
        visited.add(node)
        return True

    for brief in subtasks:
        if not visit(brief.child_id):
            errors.append(f"dependency cycle includes subtask {brief.child_id}")
            break
    return errors


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    return cleaned[:32]


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_artifact(
    *,
    run_id: str,
    parent_task_id: str,
    reason: str,
    children: tuple[PlanTask, ...],
) -> ExpansionArtifact:
    return ExpansionArtifact(
        run_id=run_id,
        parent_task_id=parent_task_id,
        reason=reason,
        children=children,
        created_at=_now(),
    )
