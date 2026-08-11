"""Budgeted ContextPack for each TaskTool dispatch.

Packs only task-local contracts, dependency Provides, a canon pointer, recent
project lessons, and compact checkpoint/feedback — never whole-repo dumps or secrets.

Phase 0.5 pointer-only policy (``HARNESS_CONTEXT_POINTER_ONLY=1``): brain /
AI_MEMORY / ADR bodies are never dumped — only path pointers + short titles.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from harness.domain.models import Run, Task
from harness.profile import ProjectProfile, load_profile
from harness.store.repository import Store
from harness.tasks_io.parser import read_section

DEFAULT_CHAR_BUDGET = 30_000
DEFAULT_LESSON_LIMIT = 5

# Per-section caps are fractions of the pack budget so a single knob
# (``HARNESS_CONTEXT_CHAR_BUDGET``) resizes everything coherently. Floors keep a
# small budget from starving a section to uselessness; ``_render_budgeted`` still
# enforces the overall cap afterwards.
_CHECKPOINT_FRACTION = 0.15
_FEEDBACK_FRACTION = 0.20
_BRIEF_FRACTION = 0.13
_PROVIDES_FRACTION = 0.05
_LESSON_FRACTION = 0.04

_SECRET_LINE = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token|authorization|private[_-]?key)"
    r"\s*[:=]\s*\S+"
)
_SECRET_VALUE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9]{20,}"
    r"|ghp_[A-Za-z0-9]{36}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z\-_]{35}"
    r")\b"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Forbidden dump markers under pointer-only policy.
_DUMP_MARKERS = (
    "AI_MEMORY",
    "/home/brain/",
    "brain/decisions/",
    "brain/incidents/",
    "## Full standards",
    "BEGIN DUMP",
)


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Compact, budgeted context injected into a dispatch prompt."""

    acceptance: tuple[str, ...]
    dependency_provides: tuple[tuple[str, str], ...]
    canon_pointer: str
    lessons: tuple[str, ...]
    checkpoint: str
    feedback: str
    rendered: str
    project_brief: str = ""
    char_budget: int = DEFAULT_CHAR_BUDGET
    pointer_only: bool = False
    truncations: int = 0

    @property
    def contracts_block(self) -> str:
        lines = [f"- {item}" for item in self.acceptance]
        for dep_id, provides in self.dependency_provides:
            compact = " ".join(provides.split())
            lines.append(f"- depends:{dep_id} provides: {compact}")
        return "\n".join(lines)


def resolve_brain_root(explicit: Path | str | None = None) -> Path | None:
    """Return a usable brain root from argument or ``HARNESS_BRAIN_ROOT``."""
    raw = explicit if explicit is not None else os.environ.get("HARNESS_BRAIN_ROOT", "")
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return None
    return path if path.exists() else None


def pointer_only_enabled(explicit: bool | None = None) -> bool:
    """Resolve pointer-only policy from argument or ``HARNESS_CONTEXT_POINTER_ONLY``."""
    if explicit is not None:
        return explicit
    return os.environ.get("HARNESS_CONTEXT_POINTER_ONLY", "1") == "1"


def redact_secrets(text: str) -> str:
    """Strip secret-like material before packing context."""
    cleaned = _PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", text)
    cleaned = _SECRET_VALUE.sub("[REDACTED]", cleaned)
    lines: list[str] = []
    for line in cleaned.splitlines():
        if _SECRET_LINE.search(line):
            lines.append("[REDACTED_SECRET_LINE]")
        else:
            lines.append(line)
    return "\n".join(lines)


_STACK_FRAME = re.compile(r"(?m)^(\s*File \".+\", line \d+.*)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_HUGE_BASE64 = re.compile(r"[A-Za-z0-9+/]{400,}={0,2}")


def mask_observation(text: str, *, max_chars: int | None = None) -> str:
    """Phase 2 observation masking: cap noisy feedback before ContextPack.

    Strips ANSI, collapses huge base64 blobs, truncates long stack traces,
    then applies ``max_chars`` (from ``HARNESS_OBSERVATION_MASK_CHARS``).
    """
    if not text:
        return text
    cleaned = _ANSI.sub("", text)
    cleaned = _HUGE_BASE64.sub("[masked:large-blob]", cleaned)
    # Keep at most ~40 stack frames worth of File lines context.
    frames = list(_STACK_FRAME.finditer(cleaned))
    if len(frames) > 40:
        # Drop middle frames.
        keep = {m.group(0) for m in frames[:20] + frames[-20:]}
        lines_out: list[str] = []
        for line in cleaned.splitlines():
            if _STACK_FRAME.match(line) and line not in keep:
                continue
            lines_out.append(line)
        cleaned = "\n".join(lines_out)
        cleaned += "\n[masked:stack-trace-middle]"
    limit = max_chars
    if limit is None:
        raw = os.environ.get("HARNESS_OBSERVATION_MASK_CHARS", "2400")
        try:
            limit = int(raw)
        except ValueError:
            limit = 2400
    if limit > 0:
        cleaned = _truncate_text(cleaned, limit)
    return cleaned


def assert_pointer_only(text: str) -> tuple[str, int]:
    """Strip dump markers; return (cleaned, truncation_count).

    Under pointer-only policy we refuse to ship whole AI_MEMORY / brain dumps.
    Matching lines are replaced with a pointer hint.
    """
    if not text:
        return text, 0
    truncations = 0
    out: list[str] = []
    for line in text.splitlines():
        if any(marker in line for marker in _DUMP_MARKERS) and len(line) > 80:
            truncations += 1
            out.append("[pointer-only: dump redacted — use path pointers]")
        else:
            out.append(line)
    return "\n".join(out), truncations


def canon_pointer_for(profile: ProjectProfile, repo_root: Path | None = None) -> str:
    """One relevant canon pointer — never a full standards dump."""
    canon = (profile.canon or "").strip() or "python-backend"
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(repo_root / ".harness" / "project.toml")
    shared = Path("/home/shared-context")
    for name in (f"{canon}.md", "coding-standards.md"):
        candidates.append(shared / name)
    for path in candidates:
        if path.is_file():
            return f"{path}#canon={canon}"
    return f"canon:{canon}"


def build_context_pack(
    *,
    run: Run,
    task: Task,
    store: Store,
    repo_root: Path,
    acceptance: Sequence[str] | None = None,
    feedback: str = "",
    checkpoint_text: str = "",
    brain_root: Path | str | None = None,
    profile: ProjectProfile | None = None,
    project_brief: str = "",
    char_budget: int = DEFAULT_CHAR_BUDGET,
    lesson_limit: int = DEFAULT_LESSON_LIMIT,
    pointer_only: bool | None = None,
) -> ContextPack:
    """Assemble a deterministic, budgeted ContextPack for one dispatch."""
    use_pointers = pointer_only_enabled(pointer_only)
    truncations = 0
    resolved_profile = profile or load_profile(repo_root)
    ac = tuple(
        _clean_line(item)
        for item in (acceptance if acceptance is not None else _acceptance_from_spec(task))
        if _clean_line(item)
    )
    provides = _dependency_provides(
        store, run.id, task, limit=_section_cap(char_budget, _PROVIDES_FRACTION, 400)
    )
    pointer = canon_pointer_for(resolved_profile, repo_root)
    if use_pointers:
        lessons, lesson_trunc = _load_lesson_pointers(
            resolve_brain_root(brain_root),
            run.project,
            limit=lesson_limit,
        )
        truncations += lesson_trunc
        brief_raw = _pointer_brief(project_brief, repo_root=repo_root, brain_root=brain_root)
    else:
        lessons = _load_lessons(
            resolve_brain_root(brain_root),
            run.project,
            limit=lesson_limit,
            char_limit=_section_cap(char_budget, _LESSON_FRACTION, 400),
        )
        brief_raw = project_brief
    brief_cleaned, brief_trunc = (
        assert_pointer_only(brief_raw) if use_pointers else (brief_raw, 0)
    )
    truncations += brief_trunc
    safe_checkpoint = _truncate_text(
        redact_secrets(checkpoint_text.strip()),
        _section_cap(char_budget, _CHECKPOINT_FRACTION, 1_200),
    )
    safe_feedback = _truncate_text(
        mask_observation(redact_secrets(feedback.strip())),
        _section_cap(char_budget, _FEEDBACK_FRACTION, 800),
    )
    safe_brief = _truncate_text(
        redact_secrets(brief_cleaned.strip()),
        _section_cap(char_budget, _BRIEF_FRACTION, 1_600),
    )
    rendered = _render_budgeted(
        acceptance=ac,
        dependency_provides=provides,
        canon_pointer=pointer,
        lessons=lessons,
        checkpoint=safe_checkpoint,
        feedback=safe_feedback,
        project_brief=safe_brief,
        char_budget=char_budget,
        pointer_only=use_pointers,
    )
    if len(rendered) > char_budget:
        truncations += 1
    return ContextPack(
        acceptance=ac,
        dependency_provides=provides,
        canon_pointer=pointer,
        lessons=lessons,
        checkpoint=safe_checkpoint,
        feedback=safe_feedback,
        rendered=rendered,
        project_brief=safe_brief,
        char_budget=char_budget,
        pointer_only=use_pointers,
        truncations=truncations,
    )


def _acceptance_from_spec(task: Task) -> list[str]:
    try:
        text = Path(task.spec_path).read_text(encoding="utf-8")
    except OSError:
        return []
    section = read_section(text, "Acceptance criteria")
    return [
        line.lstrip("-* [xX]").strip(" `")
        for line in section.splitlines()
        if line.strip().startswith(("-", "*"))
    ]


def _section_cap(char_budget: int, fraction: float, floor: int) -> int:
    return max(floor, int(char_budget * fraction))


def _dependency_provides(
    store: Store, run_id: str, task: Task, *, limit: int = 400
) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for dep_id in task.depends_on:
        dep = store.get_task(run_id, dep_id)
        if dep is None:
            continue
        provides = redact_secrets((dep.provides or "").strip())
        if not provides:
            continue
        items.append((dep_id, _truncate_text(provides, limit)))
    return tuple(items)


def _pointer_brief(
    project_brief: str,
    *,
    repo_root: Path,
    brain_root: Path | str | None,
) -> str:
    """Collapse brief to pointers; never ship AI_MEMORY / full brain bodies."""
    cleaned, _ = assert_pointer_only(project_brief)
    # Keep short bullet lines; drop long prose that looks like a dump.
    kept: list[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) > 240 and not stripped.startswith("-"):
            continue
        kept.append(stripped if stripped.startswith("-") else f"- {stripped}")
    brain = resolve_brain_root(brain_root)
    pointers = [
        f"- project_context: {repo_root / '.harness' / 'context'}",
        f"- shared_standards: /home/shared-context (pointer only)",
    ]
    if brain is not None:
        pointers.append(f"- brain_root: {brain} (query cards; do not dump)")
        pointers.extend(_brain_card_pointers(brain, limit=3))
    canon = Path("/home/AI_MEMORY.md")
    if canon.is_file():
        pointers.append(f"- AI_MEMORY: {canon} (pointer only — do not inline)")
    # Prefer brief bullets that already look like pointers; else fall back.
    if kept and all(len(x) < 200 for x in kept[:12]):
        return "\n".join([*pointers, *kept[:12]])
    return "\n".join(pointers)


def _brain_card_pointers(brain_root: Path, *, limit: int = 3) -> list[str]:
    """Inject up to N card pointers from brain-agents index (never bodies)."""
    index_path = brain_root / "index.json"
    if not index_path.is_file():
        cards_dir = brain_root / "cards"
        if not cards_dir.is_dir():
            return []
        lines: list[str] = []
        for path in sorted(cards_dir.glob("*.json"))[:limit]:
            lines.append(f"- brain_card: {path} (pointer only)")
        return lines
    try:
        import json  # noqa: PLC0415

        raw: object = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    cards = raw.get("cards") or []
    lines = []
    for item in cards[:limit]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") or "card"
        path = item.get("path") or ""
        title = item.get("title") or item.get("id") or ""
        if path:
            lines.append(f"- brain_card:{kind}: {path} — {title}")
    return lines


def _load_lesson_pointers(
    brain_root: Path | None, project: str, *, limit: int
) -> tuple[tuple[str, ...], int]:
    """Pointer-only lessons: path + title, no body dump."""
    if brain_root is None or not project:
        return (), 0
    try:
        from harness.lessons.extractor import list_recent_lessons  # noqa: PLC0415

        files = list_recent_lessons(brain_root, project, limit=limit)
    except (OSError, ImportError, ValueError):
        return (), 0
    pointers: list[str] = []
    truncations = 0
    for path in files:
        try:
            head = path.read_text(encoding="utf-8")[:200]
        except OSError:
            continue
        title = next(
            (ln.lstrip("# ").strip() for ln in head.splitlines() if ln.strip()),
            path.stem,
        )
        pointers.append(f"lesson:{path} — {title[:80]}")
        truncations += 1  # body intentionally omitted
    return tuple(pointers[:limit]), truncations


def _load_lessons(
    brain_root: Path | None, project: str, *, limit: int, char_limit: int = 400
) -> tuple[str, ...]:
    if brain_root is None or not project:
        return ()
    try:
        from harness.lessons.extractor import (  # noqa: PLC0415
            format_lessons_for_plan,
            list_recent_lessons,
        )

        files = list_recent_lessons(brain_root, project, limit=limit)
        block = format_lessons_for_plan(files).strip()
    except (OSError, ImportError, ValueError):
        return ()
    if not block:
        return ()
    # Keep each lesson compact and secret-free; drop the banner line.
    chunks: list[str] = []
    for chunk in block.split("--- ")[1:]:
        body = redact_secrets(chunk.strip())
        if body:
            chunks.append(_truncate_text(body, char_limit))
    return tuple(chunks[:limit])


def _render_budgeted(
    *,
    acceptance: tuple[str, ...],
    dependency_provides: tuple[tuple[str, str], ...],
    canon_pointer: str,
    lessons: tuple[str, ...],
    checkpoint: str,
    feedback: str,
    char_budget: int,
    project_brief: str = "",
    pointer_only: bool = False,
) -> str:
    contracts = _contracts_text(acceptance, dependency_provides)
    lessons_text = "\n\n".join(lessons)
    checkpoint_block = checkpoint
    feedback_block = feedback
    brief_block = project_brief

    def assemble(
        contracts_body: str,
        checkpoint_body: str,
        feedback_body: str,
        lessons_body: str,
        brief_body: str,
    ) -> str:
        parts = [
            "## Context pack",
            f"Canon: {canon_pointer}",
            f"Policy: {'pointer-only' if pointer_only else 'budgeted'}",
            "### Acceptance & dependency contracts",
            contracts_body or "(none)",
        ]
        if brief_body:
            parts.extend(["### Project brief", brief_body])
        if checkpoint_body:
            parts.extend(["### Checkpoint", checkpoint_body])
        if feedback_body:
            parts.extend(["### Feedback", feedback_body])
        if lessons_body:
            label = "### Lesson pointers" if pointer_only else "### Recent lessons"
            parts.extend([label, lessons_body])
        return "\n".join(parts).strip() + "\n"

    def render() -> str:
        return assemble(
            contracts, checkpoint_block, feedback_block, lessons_text, brief_block
        )

    rendered = render()
    if len(rendered) <= char_budget:
        return rendered

    # Priority: contracts+acceptance > checkpoint/feedback > project brief > lessons.
    while lessons and len(rendered) > char_budget:
        lessons = lessons[:-1]
        lessons_text = "\n\n".join(lessons)
        rendered = render()

    if len(rendered) > char_budget and brief_block:
        brief_block = _truncate_text(brief_block, max(120, char_budget // 8))
        rendered = render()

    if len(rendered) > char_budget and feedback_block:
        feedback_block = _truncate_text(
            feedback_block, max(80, char_budget // 8)
        )
        rendered = render()

    if len(rendered) > char_budget and checkpoint_block:
        checkpoint_block = _truncate_text(
            checkpoint_block, max(80, char_budget // 6)
        )
        rendered = render()

    if len(rendered) > char_budget:
        # Last resort: shrink contracts from the end while keeping at least one line.
        contracts = _truncate_text(contracts, max(120, char_budget // 2))
        rendered = render()

    if len(rendered) > char_budget:
        rendered = _truncate_text(rendered, char_budget)
    return rendered if rendered.endswith("\n") else rendered + "\n"


def _contracts_text(
    acceptance: tuple[str, ...],
    dependency_provides: tuple[tuple[str, str], ...],
) -> str:
    lines = [f"- {item}" for item in acceptance]
    for dep_id, provides in dependency_provides:
        compact = " ".join(provides.split())
        lines.append(f"- depends:{dep_id} provides: {compact}")
    return "\n".join(lines)


def _clean_line(value: str) -> str:
    return redact_secrets(value.strip())


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."
