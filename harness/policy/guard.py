"""Anti-gaming: защита зоны спеков/acceptance от правок воркера (ROADMAP 3.7).

Воркер не должен «подгонять» приёмку под себя — править тесты-спеки, эталоны,
acceptance-критерии. Эту зону пишет оркестратор/ревьюер. Guard — детерминированный
сенсор (L5): если воркер тронул защищённые пути, попытка автоматически уходит на
доработку, без обращения к ревьюеру.

Дефолтная зона — `tests/spec/**`. Настраивается через PROTECTED_PATHS.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable


def is_protected(path: str, patterns: Iterable[str]) -> bool:
    """Попадает ли путь в защищённую зону. Поддерживает glob и префикс каталога."""
    normalized = path.strip().lstrip("./")
    for pattern in patterns:
        base = pattern.rstrip("*").rstrip("/")
        if base and (normalized == base or normalized.startswith(base + "/")):
            return True
        if fnmatch.fnmatch(normalized, pattern):
            return True
    return False


def protected_violations(changed_paths: Iterable[str], patterns: Iterable[str]) -> list[str]:
    """Список изменённых файлов, попавших в защищённую зону (отсортирован)."""
    pats = list(patterns)
    if not pats:
        return []
    return sorted({p for p in changed_paths if p and is_protected(p, pats)})
