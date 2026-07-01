"""v2-013: SCOPE_EXIT — детектор выхода воркера за список «Файлы:» из спеки.

Парсит секцию `# Файлы` из task-spec и сравнивает с реально изменёнными файлами
в ветке. Возвращает список путей, которые воркер тронул, но они не заявлены в
спеке (и не относятся к ним через glob). Детерминированная проверка — не агент.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from harness.tasks_io.parser import read_section

# Строка пути в секции Файлы: может быть `- path/to/file.py` или `- path/**`.
_PATH_LINE = re.compile(r"^\s*[-*]\s+([^\s]+)")


def parse_spec_files(spec_text: str) -> list[str]:
    """Достать пути из секции `# Файлы` спеки.

    Поддерживает markdown-список (`- path`), glob-паттерны (`path/**`).
    Возвращает список raw-строк (как в спеке), без нормализации.
    """
    body = read_section(spec_text, "Файлы")
    if not body:
        return []
    out: list[str] = []
    for line in body.splitlines():
        m = _PATH_LINE.match(line)
        if m:
            out.append(m.group(1).strip())
    return out


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
