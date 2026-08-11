"""v2-013: SCOPE_EXIT — детектор выхода воркера за список «Файлы:» из спеки.

Парсит секцию `# Файлы` / `# Files (owned)` из task-spec и сравнивает с реально
изменёнными файлами в ветке. Возвращает список путей, которые воркер тронул, но
они не заявлены в спеке (и не относятся к ним через glob). Детерминированная
проверка — не агент.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from harness.tasks_io.parser import read_section

# Строка пути в секции Файлы: может быть `- path/to/file.py` или `- path/**`.
_PATH_LINE = re.compile(r"^\s*[-*]\s+`?([^`\s]+)`?")
# Markdown table cell that looks like a repo path (optional backticks).
_TABLE_PATH = re.compile(r"^`?([A-Za-z0-9_./\-*{}\[\]]+)`?$")
_SECTION_ALIASES = (
    "Файлы",
    "Files",
    "Files (owned)",
    "Owned files",
    "Owned paths",
    "Write set",
)


def parse_spec_files(spec_text: str) -> list[str]:
    """Достать пути из секции ownership спеки.

    Поддерживает:
      - `# Файлы` / `# Files (owned)` markdown lists (`- path`, `- path/**`)
      - markdown tables with a Path / Owns column
      - prose lines that are bare repo-relative paths
    """
    body = ""
    for heading in _SECTION_ALIASES:
        body = read_section(spec_text, heading)
        if body:
            break
    if not body:
        # Some planner templates use `## Files (owned)` already covered by aliases;
        # also accept a fenced ownership block starting after "Files (owned)".
        match = re.search(
            r"(?is)^#{2,4}\s*files[^\n]*owned[^\n]*\n(.*?)(?=^#{1,4}\s|\Z)",
            spec_text,
            re.MULTILINE,
        )
        body = match.group(1).strip() if match else ""
    if not body:
        return []
    out: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not cells:
                continue
            headerish = cells[0].lower() in {
                "path",
                "paths",
                "file",
                "files",
                "owns",
                "own",
                "write",
                "action",
            }
            if headerish or re.match(r"^[-:]+$", cells[0]):
                continue
            candidate = cells[0].strip()
            # Keep only the path token inside backticks when present.
            tick = re.match(r"^`([^`]+)`", candidate)
            if tick:
                candidate = tick.group(1).strip()
            else:
                candidate = candidate.strip("`").split()[0] if candidate else ""
            if _looks_like_path(candidate):
                out.append(candidate)
            continue
        match = _PATH_LINE.match(stripped)
        if match:
            candidate = match.group(1).strip().rstrip(",")
            if _looks_like_path(candidate):
                out.append(candidate)
            continue
        # Bare path line (no bullet) used in some specs.
        bare = stripped.strip("`")
        if _looks_like_path(bare) and " " not in bare:
            out.append(bare)
    return list(dict.fromkeys(out))


def _looks_like_path(value: str) -> bool:
    if not value or value.lower() in {"optional", "prefer", "forbidden", "tbd", "—", "-"}:
        return False
    if value.startswith("http://") or value.startswith("https://"):
        return False
    # Require a path-ish token: slash, glob, or extension.
    return bool(
        "/" in value
        or "*" in value
        or value.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs", ".php", ".md", ".toml", ".yaml", ".yml", ".json"))
    )


def scope_violations(
    changed_files: list[str], spec_text: str, *, spec_path: str | None = None,
) -> list[str]:
    """Возвращает изменённые файлы, не покрытые списком «Файлы:» из спеки.

    Поддерживает glob (`**`, `*`) в путях спеки. Файлы `tests/spec/**` (anti-gaming
    зона) отдельно ловятся guard'ом — здесь мы не дублируем.
    `spec_path` — если задан, относительные пути спеки резолвятся относительно его
    родителя; иначе считаем пути уже relative to repo root.
    """
    patterns = parse_spec_files(spec_text)
    base = Path(spec_path).parent if spec_path else None
    violations: list[str] = []
    for changed in changed_files:
        matched = False
        for pat in patterns:
            candidate = pat
            if base is not None and not Path(pat).is_absolute():
                # Если спека лежит в tasks/task-NNN.md, пути в ней — relative to repo root,
                # не к спеке. Не резолвим.
                candidate = pat
            if _match_path(changed, candidate):
                matched = True
                break
        if not matched:
            violations.append(changed)
    return violations


def _match_path(path: str, pattern: str) -> bool:
    """Сопоставить путь с glob-паттерном (с поддержкой **)."""
    # Простейшее сравнение с fnmatch + ручная поддержка **
    if "**" in pattern:
        # fnmatch трактует ** как * — для нашего случая достаточно.
        return fnmatch.fnmatch(path, pattern)
    return fnmatch.fnmatch(path, pattern) or path.startswith(pattern.rstrip("*"))
