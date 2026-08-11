"""v2-025 / V4 Phase 3: lessons-learned extractor.

После терминального статуса задачи (DONE/BLOCKED) оркестратор пишет lesson в
`brain-agents/lessons/<project>/<task_id>.md` (``HARNESS_BRAIN_ROOT``).
Human canon `/home/brain` is never auto-written. `cmd_plan` / ContextPack
читают последние N lessons (pointer-only when policy on).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def lessons_dir(brain_root: Path, project: str) -> Path:
    """Каталог lessons для проекта: `<brain_root>/lessons/<project>/`."""
    d = brain_root / "lessons" / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_lesson_file(
    brain_root: Path, project: str, task_id: str, content: str,
) -> Path:
    """Записать lesson в `<brain_root>/lessons/<project>/task-<id>-<timestamp>.md`.

    Refuses human canon ``/home/brain`` (and equivalent path) — agents write only
    to ``HARNESS_BRAIN_ROOT`` (default ``/home/brain-agents``).
    """
    from harness.brain_agents.sync import is_human_canon  # noqa: PLC0415

    if is_human_canon(brain_root):
        raise PermissionError(
            "refusing write to human brain canon; set HARNESS_BRAIN_ROOT=/home/brain-agents"
        )
    d = lessons_dir(brain_root, project)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = d / f"task-{task_id}-{ts}.md"
    path.write_text(content, encoding="utf-8")
    return path


def list_recent_lessons(
    brain_root: Path, project: str, limit: int = 5,
) -> list[Path]:
    """Последние N lesson-файлов для проекта, новые сверху."""
    d = brain_root / "lessons" / project
    if not d.exists():
        return []
    files = sorted(d.glob("task-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def format_lessons_for_plan(lessons: list[Path], char_limit: int = 2_000) -> str:
    """Форматировать lessons для инжекта в промпт оркестратора.

    Возвращает блок `=== PAST LESSONS ===\n<содержимое каждого lesson>`.
    Пусто если lessons нет. Вызывающий (ContextPack) режет ещё раз под свой
    бюджет, поэтому здесь потолок должен быть не жёстче его — иначе урок
    обрезается дважды и теряет суть до того, как бюджет вообще применён.
    """
    if not lessons:
        return ""
    lines = ["=== PAST LESSONS (учти историю) ==="]
    for p in lessons:
        text = p.read_text(encoding="utf-8").strip()
        if len(text) > char_limit:
            text = text[:char_limit] + "..."
        lines.append(f"--- {p.name} ---\n{text}\n")
    return "\n".join(lines) + "\n"
