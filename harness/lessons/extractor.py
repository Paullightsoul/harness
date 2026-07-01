"""v2-025: lessons-learned extractor.

После терминального статуса задачи (DONE/BLOCKED) оркестратор пишет lesson в
`brain/lessons/<project>/<task_id>.md`. `cmd_plan` читает последние N lessons
и инжектит в промпт оркестратора — план учитывает историю.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def lessons_dir(brain_root: Path, project: str) -> Path:
    """Каталог lessons для проекта: `brain/lessons/<project>/`."""
    d = brain_root / "lessons" / project
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_lesson_file(
    brain_root: Path, project: str, task_id: str, content: str,
) -> Path:
    """Записать lesson в `brain/lessons/<project>/task-<id>-<timestamp>.md`."""
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


def format_lessons_for_plan(lessons: list[Path]) -> str:
    """Форматировать lessons для инжекта в промпт оркестратора.

    Возвращает блок `=== PAST LESSONS ===\n<содержимое каждого lesson, первые 500 символов>`.
    Пусто если lessons нет.
    """
    if not lessons:
        return ""
    lines = ["=== PAST LESSONS (учти историю) ==="]
    for p in lessons:
        text = p.read_text(encoding="utf-8").strip()
        if len(text) > 500:
            text = text[:500] + "..."
        lines.append(f"--- {p.name} ---\n{text}\n")
    return "\n".join(lines) + "\n"
