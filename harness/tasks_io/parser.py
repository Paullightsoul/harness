"""Парсинг tasks/task-*.md (frontmatter + секции) и зависимостей из PLAN.md.

Формат задачи — как в tasks/_TEMPLATE.md: YAML-подобный frontmatter между '---'
и markdown-секции (# Контекст, # Файлы, # Acceptance criteria, ...).
Зависимости берутся из таблицы PLAN.md (колонка «зависит от»), а не выдумываются.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FIELD = re.compile(r'^([A-Za-z_]+):\s*"?(.*?)"?\s*(?:#.*)?$')


@dataclass
class ParsedTask:
    id: str
    title: str
    status: str
    attempts: int
    provides: str = ""
    complexity: str = "normal"  # normal|high (high стартует лестницу эскалации с kimi)
    depends_on: list[str] = field(default_factory=list)
    path: Path = field(default_factory=Path)


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER.search(text)
    fields: dict[str, str] = {}
    if not match:
        return fields
    for line in match.group(1).splitlines():
        m = _FIELD.match(line.strip())
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def parse_task_file(path: Path) -> ParsedTask:
    text = path.read_text(encoding="utf-8")
    fm = _parse_frontmatter(text)
    return ParsedTask(
        id=fm.get("id", path.stem.replace("task-", "")),
        title=fm.get("title", path.stem),
        status=fm.get("status", "todo"),
        attempts=int(fm.get("attempts", "0") or "0"),
        provides=read_section(text, "Provides") or fm.get("provides", ""),
        complexity=_norm_complexity(fm.get("complexity", "normal")),
        path=path,
    )


def _norm_complexity(raw: str) -> str:
    return "high" if raw.strip().lower() in {"high", "elevated", "hard"} else "normal"


def read_section(text: str, heading: str) -> str:
    """Возвращает тело markdown-секции '# <heading>' до следующего заголовка."""
    pattern = re.compile(
        rf"^#+\s*{re.escape(heading)}\s*\n(.*?)(?=^#+\s|\Z)",
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def parse_plan_dependencies(plan_path: Path) -> dict[str, list[str]]:
    """Извлекает граф зависимостей из markdown-таблицы PLAN.md.

    Ожидается строка вида: | 003 | название | 001, 002 | todo |
    Возвращает {task_id: [dep_id, ...]}. Прочерки и пустые ячейки игнорируются.
    """
    if not plan_path.exists():
        return {}
    deps: dict[str, list[str]] = {}
    for raw_line in plan_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        task_id = _norm_id(cells[0])
        if not task_id or task_id in {"id", "—", "-"}:
            continue
        raw_deps = cells[2]
        ids = [_norm_id(d) for d in re.split(r"[,\s]+", raw_deps) if d.strip()]
        deps[task_id] = [d for d in ids if d and d not in {"—", "-"}]
    return deps


def _norm_id(raw: str) -> str:
    """Нормализует id: 'task-003' / '#3' -> '003' при числовом, иначе как есть."""
    cleaned = raw.replace("task-", "").replace("#", "").strip()
    if cleaned.isdigit():
        return cleaned.zfill(3)
    return cleaned
