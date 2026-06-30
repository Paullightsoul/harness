"""Локальная песочница: команда исполняется прямо на хосте (поведение по умолчанию)."""

from __future__ import annotations

import asyncio
from pathlib import Path


class LocalSandbox:
    async def exec(self, cmd: str, cwd: Path) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return 1, f"не удалось запустить '{cmd}': {exc}"
        out_bytes, _ = await proc.communicate()
        return proc.returncode or 0, out_bytes.decode("utf-8", errors="replace")
