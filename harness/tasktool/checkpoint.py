"""Atomic per-task progress checkpoints for TaskTool resume/context."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Compact durable progress snapshot — never a full agent transcript dump."""

    stage: str
    status: str
    evidence_summary: str
    changed_files: tuple[str, ...]
    next_action: str
    updated_at: str

    def to_markdown(self) -> str:
        files = "\n".join(f"- `{path}`" for path in self.changed_files) or "- (none)"
        evidence = self.evidence_summary.strip() or "(none)"
        if len(evidence) > 800:
            evidence = evidence[:797].rstrip() + "..."
        return (
            f"# TaskTool checkpoint\n\n"
            f"## stage\n{self.stage}\n\n"
            f"## status\n{self.status}\n\n"
            f"## evidence_summary\n{evidence}\n\n"
            f"## changed_files\n{files}\n\n"
            f"## next_action\n{self.next_action.strip() or '(none)'}\n\n"
            f"## updated_at\n{self.updated_at}\n"
        )

    @classmethod
    def from_markdown(cls, text: str) -> Checkpoint:
        sections = _parse_sections(text)
        files: list[str] = []
        for line in sections.get("changed_files", "").splitlines():
            stripped = line.strip().lstrip("-* ").strip().strip("`")
            if stripped and stripped != "(none)":
                files.append(stripped)
        return cls(
            stage=sections.get("stage", "").strip(),
            status=sections.get("status", "").strip(),
            evidence_summary=sections.get("evidence_summary", "").strip(),
            changed_files=tuple(files),
            next_action=sections.get("next_action", "").strip(),
            updated_at=sections.get("updated_at", "").strip(),
        )


class CheckpointWriter:
    """Persists ``.harness/tasktool/<run>/tasks/<task>/progress.md`` atomically."""

    def __init__(self, repo_root: Path) -> None:
        self._root = Path(repo_root).resolve() / ".harness" / "tasktool"

    def path(self, run_id: str, task_id: str) -> Path:
        return self._root / run_id / "tasks" / task_id / "progress.md"

    def write(
        self,
        run_id: str,
        task_id: str,
        *,
        stage: str,
        status: str,
        evidence_summary: str = "",
        changed_files: tuple[str, ...] | list[str] = (),
        next_action: str = "",
        updated_at: str | None = None,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            stage=stage,
            status=status,
            evidence_summary=_compact_evidence(evidence_summary),
            changed_files=tuple(dict.fromkeys(changed_files)),
            next_action=next_action.strip(),
            updated_at=updated_at or _now(),
        )
        path = self.path(run_id, task_id)
        _atomic_write(path, checkpoint.to_markdown())
        return checkpoint

    def read(self, run_id: str, task_id: str) -> Checkpoint | None:
        path = self.path(run_id, task_id)
        if not path.is_file():
            return None
        try:
            return Checkpoint.from_markdown(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def summary_for_context(self, run_id: str, task_id: str) -> str:
        checkpoint = self.read(run_id, task_id)
        if checkpoint is None:
            return ""
        files = ", ".join(checkpoint.changed_files[:12])
        parts = [
            f"stage={checkpoint.stage}",
            f"status={checkpoint.status}",
            f"next={checkpoint.next_action}" if checkpoint.next_action else "",
            f"files={files}" if files else "",
            f"evidence={checkpoint.evidence_summary}" if checkpoint.evidence_summary else "",
        ]
        return "; ".join(part for part in parts if part)


def _compact_evidence(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) > 800:
        return compact[:797].rstrip() + "..."
    return compact


def _parse_sections(text: str) -> dict[str, str]:
    matches = list(_HEADING.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip().lower()] = text[start:end].strip()
    return sections


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()
