"""Парсинг tasks/task-*.md (frontmatter + секции) и зависимостей из PLAN.md.

Формат задачи — как в tasks/_TEMPLATE.md: YAML-подобный frontmatter между '---'
и markdown-секции (# Контекст, # Файлы, # Acceptance criteria, ...).
Зависимости берутся из PLAN.md — либо из таблицы (колонка «зависит от»), либо
из heading+bullet формата авто-плана (`### id: title` + `- depends_on: ...`,
см. `parse_plan_dependencies`) — а не выдумываются.
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


def resolve_plan_root(root: Path) -> Path:
    """Определяет каталог, где реально лежат актуальные PLAN.md + tasks/.

    `harness plan` (см. `interface/cli.py::_save_plan_to_project`) пишет план не
    в `root` напрямую, а в `root/.harness/runs/<run_id>/` и обновляет симлинк
    `root/.harness/runs/latest` на него. Поэтому ingest/verify должны читать
    именно оттуда, если такой прогон есть.

    Приоритет:
      0. ``root`` already is a concrete per-run surface (has PLAN.md under
         ``.harness/runs/<id>`` or ``meta.json``) — use as-is; never bounce to
         ``latest`` (that symlink is racy under multi-tenant starts).
      1. `root/.harness/runs/latest/` — если там есть `PLAN.md` (обычный путь
         после `harness plan`, symlink разыменовывается прозрачно).
      2. `root` — фоллбэк на плоскую раскладку (dogfood-конвенция: PLAN.md и
         tasks/ прямо в HARNESS_ROOT, как описано в GUIDE.md §8; так же
         устроены существующие юнит-тесты ingest/verify).
    """
    root = Path(root)
    posix = root.as_posix()
    concrete = (root / "PLAN.md").is_file() and (
        (root / "meta.json").is_file()
        or (root / "controller.json").is_file()
        or "/.harness/runs/" in posix
        or "/.harness/tasktool/" in posix
    )
    if concrete:
        return root
    latest = root / ".harness" / "runs" / "latest"
    if (latest / "PLAN.md").exists():
        return latest
    return root


def resolve_staging_plan_root(repo_root: Path) -> Path:
    """Plan source for a *new* TaskTool start / verify.

    Prefer flat ``repo_root/PLAN.md`` + ``tasks/task-*.md`` when present so a
    second concurrent goal that staged into the shared checkout is snapshotted
    correctly. ``latest`` is only a discoverability fallback (racy).
    """
    repo_root = Path(repo_root)
    shared_plan = repo_root / "PLAN.md"
    shared_tasks = repo_root / "tasks"
    if (
        shared_plan.is_file()
        and shared_tasks.is_dir()
        and any(shared_tasks.glob("task-*.md"))
    ):
        return repo_root
    return resolve_plan_root(repo_root)


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
        depends_on=parse_task_dependencies(text),
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
    """Возвращает тело markdown-секции '# <heading>' до следующего заголовка.

    Поддерживает заголовки с дополнительным текстом, например
    '# Acceptance criteria (машинно-проверяемые)'.
    """
    pattern = re.compile(
        rf"^#+\s*{re.escape(heading)}(?:[ \t]+[^\n]*)?\n(.*?)(?=^#+\s|\Z)",
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


_HEADING_TASK = re.compile(r"^#{2,4}\s*(\d+|[A-Za-z][\w-]*)\s*[:.]\s*\S.*$")
_DEPENDS_BULLET = re.compile(r"^[-*]\s*depends_on\s*:\s*(.+)$", re.IGNORECASE)


def parse_plan_dependencies(plan_path: Path) -> dict[str, list[str]]:
    """Извлекает граф зависимостей из PLAN.md.

    Поддерживает два формата (ровно один встречается в реальном PLAN.md):
      1. markdown-таблица (хендкрафченный/детальный план):
         | id | название | зависит от | status |                     (legacy, 4 cols)
         | id | фаза | название | depends_on | complexity | status |  (6 cols)
      2. heading + bullet-список (авто-план `harness plan`, см.
         `interface/cli.py::_save_plan_to_project`):
         ### 001: название
         - complexity: small
         - depends_on: 001, 002

    Возвращает {task_id: [dep_id, ...]}. Прочерки и пустые ячейки игнорируются.
    """
    if not plan_path.exists():
        return {}
    text = plan_path.read_text(encoding="utf-8")
    deps = _parse_table_dependencies(text)
    return deps if deps else _parse_heading_dependencies(text)


def _parse_table_dependencies(text: str) -> dict[str, list[str]]:
    """Extract deps only from tables that declare a depends-on column.

    Important: PLAN.md often has later tables whose first column is also a task
    id (e.g. single-writer partitions). Those must not overwrite real DAG deps
    with empty/garbage values from unrelated columns.
    """
    deps: dict[str, list[str]] = {}
    dep_col: int | None = None
    in_dep_table = False
    _dep_headers = {
        "depends_on",
        "depends on",
        "зависит",
        "зависит от",
        "deps",
        "dependency",
        "dependencies",
    }
    _id_headers = {"id", "task id", "task", "task_id", "task-id"}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            # Blank / non-table line ends the current table.
            in_dep_table = False
            dep_col = None
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if re.match(r"^[-:\s|]+$", line):
            continue  # separator |---|

        header_like = cells[0].lower().replace("_", "-") in {
            h.replace("_", "-") for h in _id_headers
        } or any(
            cell.lower().replace("_", " ") in _dep_headers for cell in cells
        )
        if header_like and not cells[0].isdigit():
            dep_col = None
            for index, cell in enumerate(cells):
                if cell.lower().replace("_", " ") in _dep_headers:
                    dep_col = index
                    break
            in_dep_table = dep_col is not None
            continue

        if not in_dep_table or dep_col is None:
            continue

        task_id = _norm_id(cells[0])
        if not task_id or not task_id.isdigit():
            continue
        if dep_col >= len(cells):
            deps.setdefault(task_id, [])
            continue
        raw_deps = cells[dep_col]
        ids = [_norm_id(part) for part in re.split(r"[,\s]+", raw_deps) if part.strip()]
        deps[task_id] = [
            item for item in ids if item and item not in {"—", "-", "–"} and item.isdigit()
        ]
    return deps


def _parse_heading_dependencies(text: str) -> dict[str, list[str]]:
    """Зависимости из heading-формата: `### <id>: ...` + `- depends_on: <ids>`.

    Bullet ищется только внутри текущей секции задачи (до следующего `###`).
    Задача без `depends_on:` bullet считается независимой (пустой список).
    """
    deps: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = _HEADING_TASK.match(line)
        if heading:
            current = _norm_id(heading.group(1))
            deps.setdefault(current, [])
            continue
        if current is None:
            continue
        bullet = _DEPENDS_BULLET.match(line)
        if not bullet:
            continue
        ids = [_norm_id(d) for d in re.split(r"[,\s]+", bullet.group(1)) if d.strip()]
        deps[current] = [d for d in ids if d and d not in {"—", "-", "–"} and d.isdigit()]
    return deps


_TASK_DEPENDS_LINE = re.compile(
    r"(?i)^\s*(?:\*\*)?(?:depends\s*on|зависит\s*от)(?:\*\*)?\s*[:：]\s*(.+?)\s*$"
)


def parse_task_dependencies(text: str) -> list[str]:
    """Extract `Depends on: 002, 003` (or bold/RU variants) from a task body."""
    found: list[str] = []
    for raw in text.splitlines():
        match = _TASK_DEPENDS_LINE.match(raw.strip())
        if not match:
            continue
        payload = match.group(1).replace("*", " ")
        ids = [_norm_id(part) for part in re.split(r"[,\s]+", payload) if part.strip()]
        found.extend(
            item for item in ids if item and item not in {"—", "-", "–"} and item.isdigit()
        )
    return list(dict.fromkeys(found))


def _norm_id(raw: str) -> str:
    """Нормализует id: 'task-003' / '#3' / '**002**' -> '003' при числовом, иначе как есть."""
    cleaned = raw.replace("task-", "").replace("#", "").replace("*", "").strip()
    if cleaned.isdigit():
        return cleaned.zfill(3)
    return cleaned
