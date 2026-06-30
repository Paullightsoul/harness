"""Контракт гейта качества. Гейт — машинный оракул истины: галлюцинация не пройдёт.

Pluggable: для Python — make check (ruff+mypy+pytest), для другого стека —
своя реализация Gate с тем же интерфейсом.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class GateResult:
    passed: bool
    output: str  # хвост вывода для фидбэка воркеру/ревьюеру


@runtime_checkable
class Gate(Protocol):
    async def check(self, cwd: Path) -> GateResult: ...
