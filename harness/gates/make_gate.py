"""Гейт `make check` (ruff + mypy + pytest) — как в Makefile проекта."""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness.gates.base import GateResult

_TAIL_LINES = 60


class MakeGate:
    def __init__(self, target: str = "check") -> None:
        self._target = target

    async def check(self, cwd: Path) -> GateResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "make", self._target,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return GateResult(passed=False, output="make не найден в PATH")

        out_bytes, _ = await proc.communicate()
        out = out_bytes.decode("utf-8", errors="replace")
        tail = "\n".join(out.splitlines()[-_TAIL_LINES:])
        return GateResult(passed=proc.returncode == 0, output=tail)
