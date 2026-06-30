"""Парсинг задач из tasks/*.md, построение DAG и разворачивание fan-out.

Формат задачи — markdown с YAML-подобным frontmatter между двумя `---`.
Парсер минимальный (stdlib-only): поддерживает скаляры и инлайн-списки
в JSON-стиле (`["001", "002"]`, `[]`). Этого достаточно для наших полей.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


@dataclass
class Task:
    id: str
    title: str = ""
    worker_type: str = "default"
    layer: int = 0
    depends_on: List[str] = field(default_factory=list)
    file_globs: List[str] = field(default_factory=list)
    status: str = "todo"  # todo | in_progress | done | blocked
    attempts: int = 0
    fanout: List[str] = field(default_factory=list)
    parent: str = ""
    body: str = ""
    path: Path = field(default_factory=lambda: Path("."))

    @property
    def globs(self) -> List[str]:
        return self.file_globs


def _strip_inline_comment(value: str) -> str:
    # Срезаем " # ..." только если решётка не внутри строки/списка.
    in_quote = False
    out: List[str] = []
    for ch in value:
        if ch == '"':
            in_quote = not in_quote
        if ch == "#" and not in_quote:
            break
        out.append(ch)
    return "".join(out).strip()


def _parse_scalar_or_list(value: str) -> object:
    """Возвращает str | int | list[str] из значения frontmatter."""
    value = _strip_inline_comment(value)
    if value == "":
        return ""
    if value.startswith("[") and value.endswith("]"):
        # Пытаемся как JSON; иначе грубо режем по запятым.
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            inner = value[1:-1].strip()
            if not inner:
                return []
            return [p.strip().strip('"').strip("'") for p in inner.split(",")]
    # Скаляр: снимаем кавычки.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value


def parse_frontmatter(text: str) -> "tuple[Dict[str, object], str]":
    """Разбирает frontmatter; возвращает (поля, тело-после-frontmatter)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: Dict[str, object] = {}
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = _parse_scalar_or_list(value.strip())
    body = "\n".join(lines[end + 1 :]) if end != -1 else text
    return fields, body


def _as_str(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(_as_str(value, str(default)))
    except ValueError:
        return default


def _as_list(value: object) -> List[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def task_from_file(path: Path) -> Task:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    return Task(
        id=_as_str(fm.get("id")) or path.stem.replace("task-", ""),
        title=_as_str(fm.get("title")),
        worker_type=_as_str(fm.get("worker_type"), "default") or "default",
        layer=_as_int(fm.get("layer"), 0),
        depends_on=_as_list(fm.get("depends_on")),
        file_globs=_as_list(fm.get("file_globs")),
        status=_as_str(fm.get("status"), "todo") or "todo",
        attempts=_as_int(fm.get("attempts"), 0),
        fanout=_as_list(fm.get("fanout")),
        body=body,
        path=path,
    )


def load_tasks(tasks_dir: Path) -> List[Task]:
    files = sorted(tasks_dir.glob("task-*.md"))
    return [task_from_file(p) for p in files]


def expand_fanout(tasks: List[Task]) -> List[Task]:
    """Разворачивает fan-out задачи в N однотипных детей с подстановкой {item}.

    Дети наследуют worker_type/layer/depends_on родителя; их file_globs и тело
    получаются подстановкой `{item}`. Зависимости других задач от родителя
    переписываются на зависимость от всех его детей.
    """
    expanded: List[Task] = []
    parent_to_children: Dict[str, List[str]] = {}

    for t in tasks:
        if not t.fanout:
            expanded.append(t)
            continue
        children_ids: List[str] = []
        for item in t.fanout:
            child_id = f"{t.id}--{item}"
            children_ids.append(child_id)
            expanded.append(
                Task(
                    id=child_id,
                    title=f"{t.title} [{item}]",
                    worker_type=t.worker_type,
                    layer=t.layer,
                    depends_on=list(t.depends_on),
                    file_globs=[g.replace("{item}", item) for g in t.file_globs],
                    status="todo",
                    fanout=[],
                    parent=t.id,
                    body=t.body.replace("{item}", item),
                    path=t.path,
                )
            )
        parent_to_children[t.id] = children_ids

    if parent_to_children:
        for t in expanded:
            new_deps: List[str] = []
            for dep in t.depends_on:
                new_deps.extend(parent_to_children.get(dep, [dep]))
            t.depends_on = new_deps

    return expanded


def globs_conflict(a: List[str], b: List[str]) -> bool:
    """Пересекаются ли файловые наборы (консервативно, в сторону сериализации)."""
    for x in a:
        for y in b:
            if x == y or fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x):
                return True
    return False


def union_globs(globs_lists: "List[List[str]]") -> List[str]:
    out: List[str] = []
    for g in globs_lists:
        out.extend(g)
    return out
