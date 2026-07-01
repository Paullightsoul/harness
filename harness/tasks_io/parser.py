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
    """Нормализует complexity (v2-019: tiered pipeline depth).

    Поддерживает 4 tier'а + обратную совместимость со старыми:
      - trivial — worker→gate, без reviewer (дешёвые задачи)
      - small   — +reviewer (текущая схема normal)
      - medium  — зарезервировано под multi-stage (research+plan+PRD-review+review-fix)
      - large   — зарезервировано под +final-review
    Старые: `normal` → `small`, `high` → `medium` (мягкая миграция).
    """
    v = raw.strip().lower()
    if v in {"trivial", "small", "medium", "large"}:
        return v
    if v in {"high", "elevated", "hard"}:
        return "medium"  # было high → medium
    return "small"  # было normal → small; неизвестные → small (безопасный default)


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

    Поддерживает форматы:
      | id | название | зависит от | status |          (legacy, 4 cols)
      | id | фаза | название | depends_on | complexity | status |  (6 cols)

    Возвращает {task_id: [dep_id, ...]}. Прочерки и пустые ячейки игнорируются.
    """
    if not plan_path.exists():
        return {}
    deps: dict[str, list[str]] = {}
    dep_col: int | None = None
    _dep_headers = {"depends_on", "зависит", "зависит от", "deps", "depends on"}

    for raw_line in plan_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        # Строка заголовка — определяем колонку зависимостей.
        if cells[0].lower() in {"id", "—", "-"}:
            for i, cell in enumerate(cells):
                if cell.lower().replace("_", " ") in _dep_headers or cell.lower() == "depends_on":
                    dep_col = i
                    break
            continue
        if re.match(r"^[-:\s|]+$", line):
            continue  # separator |---|

        task_id = _norm_id(cells[0])
        if not task_id or not task_id.isdigit():
            continue

        col = dep_col if dep_col is not None else (3 if len(cells) >= 5 else 2)
        if col >= len(cells):
            deps[task_id] = []
            continue
        raw_deps = cells[col]
        ids = [_norm_id(d) for d in re.split(r"[,\s]+", raw_deps) if d.strip()]
        deps[task_id] = [d for d in ids if d and d not in {"—", "-"} and d.isdigit()]
    return deps


def _norm_id(raw: str) -> str:
    """Нормализует id: 'task-003' / '#3' -> '003' при числовом, иначе как есть."""
    cleaned = raw.replace("task-", "").replace("#", "").strip()
    if cleaned.isdigit():
        return cleaned.zfill(3)
    return cleaned
