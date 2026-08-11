"""Контракт гейта качества. Гейт — машинный оракул истины: галлюцинация не пройдёт.

Pluggable: для Python — make check (ruff+mypy+pytest), для другого стека —
своя реализация Gate с тем же интерфейсом.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class GateRunResult:
    """One executed profile gate with durable evidence pointers."""

    gate_id: str
    cmd: str
    exit_code: int
    log_path: str = ""
    soft: bool = False
    skipped: bool = False


@dataclass
class GateResult:
    passed: bool
    output: str  # хвост вывода для фидбэка воркеру/ревьюеру
    gate_runs: list[GateRunResult] = field(default_factory=list)


@runtime_checkable
class Gate(Protocol):
    async def check(self, cwd: Path) -> GateResult: ...
