"""Deterministic ReviewBundle from lifecycle changed_files (never worker prose)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReviewGroup:
    """Related files grouped by package/directory for reviewer focus."""

    key: str
    files: tuple[str, ...]
    impact_hint: str = ""


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    """Authoritative review scope derived from worktree/lifecycle file lists."""

    changed_files: tuple[str, ...]
    groups: tuple[ReviewGroup, ...]
    graphify_hint: str = ""
    diff: str = ""

    def render(self) -> str:
        lines = [
            "## ReviewBundle (authoritative; ignore worker-claimed paths)",
            f"Changed files ({len(self.changed_files)}):",
        ]
        if not self.changed_files:
            lines.append("- (none)")
        else:
            lines.extend(f"- `{path}`" for path in self.changed_files)
        lines.append("Groups:")
        if not self.groups:
            lines.append("- (none)")
        else:
            for group in self.groups:
                hint = f" — {group.impact_hint}" if group.impact_hint else ""
                lines.append(f"- `{group.key}` ({len(group.files)} files){hint}")
                lines.extend(f"  - `{path}`" for path in group.files)
        if self.graphify_hint:
            lines.extend(
                [
                    "On-demand graphify (do not run automatically):",
                    f"`{self.graphify_hint}`",
                ]
            )
        if self.diff.strip():
            lines.extend(
                [
                    "",
                    "### Diff (captured at worker finalize; authoritative)",
                    "```diff",
                    self.diff.rstrip("\n"),
                    "```",
                ]
            )
        else:
            lines.append(
                "No diff captured — read the changed files listed above before judging."
            )
        lines.append(
            "Findings must use `path:line:severity: message` (one finding per line)."
        )
        return "\n".join(lines) + "\n"


def build_review_bundle(
    changed_files: Sequence[str],
    *,
    repo_root: Path | None = None,
    project: str = "",
    diff: str = "",
) -> ReviewBundle:
    """Group exact changed paths deterministically; add graphify hint only if graph exists."""
    files = tuple(
        sorted({_normalize_path(item) for item in changed_files if _normalize_path(item)})
    )
    buckets: dict[str, list[str]] = {}
    for path in files:
        key = _group_key(path)
        buckets.setdefault(key, []).append(path)
    groups = tuple(
        ReviewGroup(
            key=key,
            files=tuple(buckets[key]),
            impact_hint=_impact_hint(buckets[key]),
        )
        for key in sorted(buckets)
    )
    hint = _graphify_hint(repo_root=repo_root, project=project)
    return ReviewBundle(
        changed_files=files, groups=groups, graphify_hint=hint, diff=diff
    )


def _normalize_path(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def _group_key(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "."
    if len(parts) == 1:
        return "."
    # File directly under a top-level dir → group by that dir.
    if "." in parts[1] and not parts[1].startswith("."):
        return parts[0]
    # Prefer package-ish prefixes: harness/foo, src/pkg, app/services.
    if parts[0] in {"src", "app", "lib", "pkg", "packages", "harness", "tests"}:
        return "/".join(parts[:2])
    return parts[0]


def _impact_hint(files: Sequence[str]) -> str:
    suffixes = sorted({Path(path).suffix or "(noext)" for path in files})
    kinds = ", ".join(suffixes[:4])
    return f"{len(files)} file(s); types: {kinds}"


def _graphify_hint(*, repo_root: Path | None, project: str) -> str:
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(Path(repo_root).resolve() / "graphify-out" / "graph.json")
    project_name = project.strip()
    if project_name:
        candidates.append(Path("/home/projects") / project_name / "graphify-out" / "graph.json")
        candidates.append(Path("/home") / project_name / "graphify-out" / "graph.json")
    for graph in candidates:
        if graph.is_file():
            return (
                f'graphify query "<review focus>" --graph {graph} --budget 4000'
            )
    return ""
