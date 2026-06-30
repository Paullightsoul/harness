"""Исполнитель через cursor-agent CLI. Тарифицируется из подписки Cursor.

Эквивалент run_agent() из scripts/lib.sh, но асинхронный — чтобы движок мог
гонять несколько воркеров параллельно.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

from harness.runner.base import AgentResult


class CliRunner:
    def __init__(self, extra_flags: str = "") -> None:
        self._extra_flags = shlex.split(extra_flags) if extra_flags else []

    async def run(
        self,
        prompt: str,
        *,
        model: str,
        cwd: Path,
        log_path: Path | None = None,
    ) -> AgentResult:
        cmd = [
            "cursor-agent", "-p", prompt,
            "--model", model,
            "--output-format", "text",
            "--force",
            *self._extra_flags,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return AgentResult(ok=False, text="", error="cursor-agent не найден в PATH")

        out_bytes, _ = await proc.communicate()
        text = out_bytes.decode("utf-8", errors="replace")
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(text, encoding="utf-8")
        # CLI не отдаёт стоимость машиночитаемо — стоимость считаем отдельно по политике.
        return AgentResult(ok=proc.returncode == 0, text=text)
