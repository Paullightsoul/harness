"""Deterministic project context for planners, sub-orchestrators and workers.

Collects a compact, secret-free brief about the target repository and its
knowledge layer (``brain/``): identity and gates from ``.harness/project.toml``,
top-level layout, dependency manifests, README head, graphify graph hint,
architecture note, related ADR/incident titles and lesson count.

The result is cached under ``<repo>/.harness/context/`` keyed by git HEAD, so
repeated dispatch fan-out does not rescan the tree. Never dumps whole files —
budgeted extracts only.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.profile import ProjectProfile, load_profile
from harness.tasktool.context import redact_secrets

CACHE_MAX_AGE = timedelta(hours=24)
_SKIP_DIRS = {
    ".git",
    ".harness",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".worktrees",
    ".idea",
    ".vscode",
    "graphify-out",
}
_LAYOUT_CAP = 40
_DEPS_CAP = 15
_ADR_CAP = 6
_INCIDENT_CAP = 4
# Monorepos keep the real manifests one or two levels down (backend/, frontend/),
# while the root often holds only a scaffold stub. Reporting the root alone told
# agents a FastAPI+React repo had no dependencies at all.
_MANIFEST_NAMES = ("pyproject.toml", "package.json", "go.mod", "Cargo.toml")
_MANIFEST_CAP = 10
_MANIFEST_DEPTH = 2
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """Budgeted, deterministic snapshot of one target project."""

    project: str
    repo_root: str
    head_sha: str
    language: str
    canon: str
    gates: tuple[str, ...]
    layout: tuple[str, ...]
    manifests: tuple[str, ...]
    readme_head: str
    graphify_hint: str
    brain_architecture: str
    brain_adrs: tuple[str, ...]
    brain_incidents: tuple[str, ...]
    lessons_count: int
    generated_at: str

    def to_markdown(self) -> str:
        layout_lines = [f"- `{entry}`" for entry in self.layout] or ["- (empty)"]
        lines = [
            f"# Project context — {self.project}",
            "",
            f"- repo: `{self.repo_root}`",
            f"- HEAD: `{self.head_sha or 'unknown'}`",
            f"- language/canon: {self.language} / {self.canon}",
            f"- gates: {', '.join(self.gates) or '(default make check)'}",
            f"- generated: {self.generated_at}",
            "",
            "## Layout (top levels)",
            *layout_lines,
        ]
        if self.manifests:
            lines += ["", "## Dependencies", *(f"- {item}" for item in self.manifests)]
        if self.readme_head:
            lines += ["", "## README (head)", self.readme_head]
        if self.graphify_hint:
            lines += [
                "",
                "## Knowledge graph",
                "Architectural questions go to graphify first:",
                f"`{self.graphify_hint}`",
            ]
        brain_lines: list[str] = []
        if self.brain_architecture:
            brain_lines += ["### Architecture note (head)", self.brain_architecture]
        if self.brain_adrs:
            brain_lines += ["### Related ADRs", *(f"- {item}" for item in self.brain_adrs)]
        if self.brain_incidents:
            brain_lines += [
                "### Related incidents",
                *(f"- {item}" for item in self.brain_incidents),
            ]
        if self.lessons_count:
            brain_lines.append(f"### Lessons: {self.lessons_count} recorded for this project")
        if brain_lines:
            lines += ["", "## Brain (knowledge layer)", *brain_lines]
        return "\n".join(lines).rstrip() + "\n"

    def brief(self, char_budget: int = 4_000) -> str:
        """Compact single-block summary for per-dispatch context packs."""
        parts = [
            f"{self.project} ({self.language}/{self.canon})",
            f"gates: {', '.join(self.gates) or 'make check'}",
        ]
        if self.layout:
            parts.append("layout: " + ", ".join(self.layout[:25]))
        if self.manifests:
            parts.append("deps: " + "; ".join(self.manifests))
        if self.graphify_hint:
            parts.append(f"graph: {self.graphify_hint}")
        if self.brain_adrs:
            parts.append(
                "ADRs: " + "; ".join(item.split(" — ")[0] for item in self.brain_adrs)
            )
        if self.brain_incidents:
            parts.append(f"incidents: {len(self.brain_incidents)} related in brain/incidents")
        if self.lessons_count:
            parts.append(f"lessons: {self.lessons_count} in brain/lessons")
        text = "\n".join(f"- {part}" for part in parts)
        if len(text) > char_budget:
            text = text[: char_budget - 3].rstrip() + "..."
        return text


def collect_project_context(
    repo_root: Path,
    project: str,
    *,
    brain_root: Path | None = None,
    profile: ProjectProfile | None = None,
) -> ProjectContext:
    """Assemble the context from scratch (no cache)."""
    repo = Path(repo_root).resolve()
    resolved = profile or load_profile(repo)
    return ProjectContext(
        project=project,
        repo_root=str(repo),
        head_sha=_head_sha(repo),
        language=resolved.language,
        canon=resolved.canon,
        gates=tuple(f"{gate.id}: {gate.cmd}" for gate in resolved.gates),
        layout=_layout(repo),
        manifests=_manifests(repo),
        readme_head=_readme_head(repo),
        graphify_hint=_graphify_hint(repo, project),
        brain_architecture=_brain_architecture(brain_root, project),
        brain_adrs=_brain_titles(brain_root, "decisions", project, cap=_ADR_CAP),
        brain_incidents=_brain_titles(brain_root, "incidents", project, cap=_INCIDENT_CAP),
        lessons_count=_lessons_count(brain_root, project),
        generated_at=datetime.now(UTC).isoformat(),
    )


def load_or_build(
    repo_root: Path,
    project: str,
    *,
    brain_root: Path | None = None,
    refresh: bool = False,
) -> tuple[ProjectContext, Path]:
    """Return cached context when HEAD matches and cache is fresh; else rebuild.

    Also writes the markdown twin next to the JSON cache so agents can read a
    single stable path: ``<repo>/.harness/context/project-context.md``.
    """
    repo = Path(repo_root).resolve()
    cache_dir = repo / ".harness" / "context"
    json_path = cache_dir / "project-context.json"
    md_path = cache_dir / "project-context.md"
    if not refresh:
        cached = _read_cache(json_path)
        if cached is not None and _cache_fresh(cached, repo) and md_path.is_file():
            return cached, md_path
    context = collect_project_context(repo, project, brain_root=brain_root)
    _atomic_write(
        json_path,
        json.dumps(asdict(context), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    _atomic_write(md_path, context.to_markdown())
    return context, md_path


def _read_cache(path: Path) -> ProjectContext | None:
    if not path.is_file():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return ProjectContext(
            project=str(raw["project"]),
            repo_root=str(raw["repo_root"]),
            head_sha=str(raw.get("head_sha") or ""),
            language=str(raw.get("language") or "python"),
            canon=str(raw.get("canon") or "python-backend"),
            gates=tuple(raw.get("gates") or ()),
            layout=tuple(raw.get("layout") or ()),
            manifests=tuple(raw.get("manifests") or ()),
            readme_head=str(raw.get("readme_head") or ""),
            graphify_hint=str(raw.get("graphify_hint") or ""),
            brain_architecture=str(raw.get("brain_architecture") or ""),
            brain_adrs=tuple(raw.get("brain_adrs") or ()),
            brain_incidents=tuple(raw.get("brain_incidents") or ()),
            lessons_count=int(raw.get("lessons_count") or 0),
            generated_at=str(raw.get("generated_at") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _cache_fresh(cached: ProjectContext, repo: Path) -> bool:
    if cached.head_sha != _head_sha(repo):
        return False
    try:
        generated = datetime.fromisoformat(cached.generated_at)
    except ValueError:
        return False
    return datetime.now(UTC) - generated < CACHE_MAX_AGE


def _head_sha(repo: Path) -> str:
    git = repo / ".git"
    if git.is_file():
        marker = _safe_read(git).strip()
        if marker.startswith("gitdir:"):
            target = Path(marker.partition(":")[2].strip())
            git = target if target.is_absolute() else (repo / target).resolve()
    head = _safe_read(git / "HEAD").strip()
    if head.startswith("ref:"):
        ref = head.partition(":")[2].strip()
        direct = _safe_read(git / ref).strip()
        if direct:
            return direct[:12]
        packed = _safe_read(git / "packed-refs")
        for line in packed.splitlines():
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0][:12]
        return ""
    return head[:12]


def _layout(repo: Path) -> tuple[str, ...]:
    entries: list[str] = []
    try:
        top = sorted(repo.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return ()
    for item in top:
        if item.name.startswith(".") or item.name in _SKIP_DIRS:
            continue
        if item.is_dir():
            entries.append(f"{item.name}/")
            try:
                subdirs = sorted(
                    child.name
                    for child in item.iterdir()
                    if child.is_dir()
                    and not child.name.startswith(".")
                    and child.name not in _SKIP_DIRS
                )
            except OSError:
                subdirs = []
            entries.extend(f"{item.name}/{name}/" for name in subdirs[:6])
        elif item.suffix in {".md", ".toml", ".json", ".yaml", ".yml", ".mod", ".txt"}:
            entries.append(item.name)
        if len(entries) >= _LAYOUT_CAP:
            break
    return tuple(entries[:_LAYOUT_CAP])


def _manifests(repo: Path) -> tuple[str, ...]:
    lines: list[str] = []
    for path in _find_manifests(repo):
        lines.extend(_describe_manifest(path, path.relative_to(repo).as_posix()))
        if len(lines) >= _MANIFEST_CAP:
            break
    return tuple(lines[:_MANIFEST_CAP])


def _find_manifests(repo: Path) -> list[Path]:
    """Manifests at the repo root and in sub-project dirs, shallowest first."""
    found: list[Path] = []
    level = [repo]
    for depth in range(_MANIFEST_DEPTH + 1):
        for directory in level:
            found.extend(
                directory / name
                for name in _MANIFEST_NAMES
                if (directory / name).is_file()
            )
        if depth == _MANIFEST_DEPTH:
            break
        level = [
            child
            for directory in level
            for child in _child_dirs(directory)
        ]
    return found


def _child_dirs(directory: Path) -> list[Path]:
    try:
        return sorted(
            child
            for child in directory.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name not in _SKIP_DIRS
        )
    except OSError:
        return []


def _describe_manifest(path: Path, rel: str) -> list[str]:
    if path.name == "pyproject.toml":
        return _describe_pyproject(path, rel)
    if path.name == "package.json":
        return _describe_package_json(path, rel)
    if path.name == "go.mod":
        first = _safe_read(path).splitlines()[:1]
        return [f"{rel}: {first[0].strip()}"] if first else []
    if path.name == "Cargo.toml":
        try:
            data = tomllib.loads(_safe_read(path))
        except tomllib.TOMLDecodeError:
            return []
        package = data.get("package")
        name = package.get("name", "?") if isinstance(package, dict) else "?"
        deps = data.get("dependencies")
        names = sorted(deps)[:_DEPS_CAP] if isinstance(deps, dict) else []
        return [f"{rel}: {name} — {', '.join(names) or 'no deps'}"]
    return []


def _describe_pyproject(path: Path, rel: str) -> list[str]:
    try:
        data = tomllib.loads(_safe_read(path))
    except tomllib.TOMLDecodeError:
        return []
    lines: list[str] = []
    project = data.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        names = _dep_names(deps if isinstance(deps, list) else [])
        lines.append(f"{rel}: {project.get('name', '?')} — {', '.join(names) or 'no deps'}")
    poetry = _nested(data, "tool", "poetry")
    if isinstance(poetry, dict):
        deps = poetry.get("dependencies")
        if isinstance(deps, dict):
            names = [name for name in deps if name.lower() != "python"][:_DEPS_CAP]
            lines.append(f"{rel} (poetry): {', '.join(names) or '(none)'}")
    return lines


def _describe_package_json(path: Path, rel: str) -> list[str]:
    try:
        raw: object = json.loads(_safe_read(path))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, dict):
        return []
    deps = raw.get("dependencies")
    names = sorted(deps)[:_DEPS_CAP] if isinstance(deps, dict) else []
    return [f"{rel}: {raw.get('name', '?')} — {', '.join(names) or 'no deps'}"]


def _dep_names(deps: list[object]) -> list[str]:
    names: list[str] = []
    for item in deps[:_DEPS_CAP]:
        text = str(item)
        names.append(re.split(r"[<>=!~\[ ]", text, maxsplit=1)[0])
    return names


def _readme_head(repo: Path) -> str:
    for name in ("README.md", "Readme.md", "readme.md"):
        path = repo / name
        if path.is_file():
            text = redact_secrets(_safe_read(path)).strip()
            return _truncate(text, 1_500)
    return ""


def _graphify_hint(repo: Path, project: str) -> str:
    candidates = [repo / "graphify-out" / "graph.json"]
    if project:
        candidates.append(Path("/home/projects") / project / "graphify-out" / "graph.json")
        candidates.append(Path("/home") / project / "graphify-out" / "graph.json")
    for graph in candidates:
        if graph.is_file():
            return f'graphify query "<вопрос>" --graph {graph} --budget 4000'
    return ""


def _brain_architecture(brain_root: Path | None, project: str) -> str:
    if brain_root is None or not project:
        return ""
    path = brain_root / "architecture" / f"{project}.md"
    if not path.is_file():
        return ""
    return _truncate(redact_secrets(_safe_read(path)).strip(), 900)


def _brain_titles(
    brain_root: Path | None,
    section: str,
    project: str,
    *,
    cap: int,
) -> tuple[str, ...]:
    """Titles of brain notes mentioning the project (filename or heading/body head)."""
    if brain_root is None or not project:
        return ()
    directory = brain_root / section
    if not directory.is_dir():
        return ()
    needle = project.lower()
    matches: list[str] = []
    for path in sorted(directory.glob("*.md")):
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:2_000]
        except OSError:
            continue
        if needle not in path.name.lower() and needle not in head.lower():
            continue
        title_match = _TITLE_RE.search(head)
        title = title_match.group(1) if title_match else path.stem
        matches.append(f"{path.stem} — {title}" if title != path.stem else path.stem)
        if len(matches) >= cap:
            break
    return tuple(matches)


def _lessons_count(brain_root: Path | None, project: str) -> int:
    if brain_root is None or not project:
        return 0
    directory = brain_root / "lessons" / project
    if not directory.is_dir():
        return 0
    return sum(1 for _ in directory.glob("task-*.md"))


def _nested(data: dict[str, object], *keys: str) -> object:
    node: object = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
