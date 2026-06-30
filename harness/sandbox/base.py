"""Контракт песочницы: выполнить shell-команду в каталоге и вернуть (код, вывод)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Sandbox(Protocol):
    async def exec(self, cmd: str, cwd: Path) -> tuple[int, str]:
        """Запустить cmd с рабочим каталогом cwd. Возврат: (exit_code, combined_output)."""
        ...
