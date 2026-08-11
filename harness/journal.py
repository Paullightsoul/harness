"""Per-project work history: append-only journal entries inside the target repo.

After every completed task (and at run completion) the harness records — into
the project itself, not only into ``brain/`` — what was decided, what was done
and how many iterations it took. Entries are one-file-per-event under
``<repo>/docs/history/`` (configurable), so parallel tasks never fight over a
single append-only file and the history is trivially greppable / reviewable
in git.

Dialog-driven entries (outside harness runs) use the same format — see the
workspace rule ``project-history.mdc`` and the stop-hook in
``/home/.cursor/hooks.json``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness.tasktool.context import redact_secrets

DEFAULT_JOURNAL_DIR = "docs/history"
_SLUG_RE = re.compile(r"[^a-z0-9а-яё-]+")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One completed unit of work: decisions, outcome, iteration trail."""

    title: str
    source: str  # "harness-task" | "harness-run" | "dialog"
    project: str
    run_id: str = ""
    task_id: str = ""
    decisions: tuple[str, ...] = ()
    done: tuple[str, ...] = ()
    iterations: tuple[str, ...] = ()
    dialog: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def render_entry(entry: JournalEntry) -> str:
    lines = [
        "---",
        f"date: {entry.created_at}",
        f"source: {entry.source}",
        f"project: {entry.project}",
    ]
    if entry.run_id:
        lines.append(f"run: {entry.run_id}")
    if entry.task_id:
        lines.append(f'task: "{entry.task_id}"')
    lines += ["---", "", f"# {entry.title}", ""]

    def section(name: str, items: tuple[str, ...]) -> None:
        if not items:
            return
        lines.append(f"## {name}")
        lines.extend(f"- {redact_secrets(item)}" for item in items)
        lines.append("")

    section("Решения", entry.decisions)
    section("Что сделано", entry.done)
    section("Итерации", entry.iterations)
    if entry.dialog:
        lines += ["## Диалог", redact_secrets(entry.dialog.strip()), ""]
    return "\n".join(lines).rstrip() + "\n"


def journal_dir(repo_root: Path, rel_dir: str = DEFAULT_JOURNAL_DIR) -> Path:
    return Path(repo_root) / rel_dir


def write_entry(
    repo_root: Path,
    entry: JournalEntry,
    *,
    rel_dir: str = DEFAULT_JOURNAL_DIR,
    now: datetime | None = None,
) -> Path:
    """Atomically persist the entry as its own timestamped markdown file."""
    moment = now or datetime.now(UTC)
    directory = journal_dir(repo_root, rel_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{moment.strftime('%Y-%m-%d-%H%M%S')}-"
        f"{_slug(entry.task_id or entry.title) or entry.source}"
    )
    path = directory / f"{stem}.md"
    counter = 1
    while path.exists():
        counter += 1
        path = directory / f"{stem}-{counter}.md"
    _atomic_write(path, render_entry(entry))
    return path


def list_recent_entries(
    repo_root: Path,
    *,
    rel_dir: str = DEFAULT_JOURNAL_DIR,
    limit: int = 5,
) -> list[Path]:
    """Newest journal entries first — feed for context packs / planners."""
    directory = journal_dir(repo_root, rel_dir)
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.md"), key=lambda p: p.name, reverse=True)
    return files[:limit]


def _slug(value: str) -> str:
    cleaned = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return cleaned[:48]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
