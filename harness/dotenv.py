"""Минимальный zero-dep загрузчик .env (KEY=VALUE). Не перетирает уже заданные env.

Вызывается из CLI перед чтением Settings, чтобы harness был turnkey: положил .env —
и переменные подхватились без python-dotenv.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> int:
    """Грузит KEY=VALUE из path в os.environ (existing не трогает). Возврат: сколько задано."""
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Пустые значения не задаём: пустая строка-заглушка не должна перетирать
        # реальное значение (ни из окружения, ни заданное ниже в этом же файле).
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
