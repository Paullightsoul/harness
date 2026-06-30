"""Docker-песочница: команда исполняется в одноразовом контейнере.

Рабочий каталог (git worktree) монтируется в /work. По умолчанию сеть отключена
(`--network none`) — изоляция; образ должен содержать тулчейн гейтов (ruff/pytest,
node и т.п.). Образ и сеть задаются профилем проекта (`[sandbox]` в project.toml).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path


class DockerSandbox:
    def __init__(
        self, image: str, network: str = "none", extra_args: Sequence[str] = ()
    ) -> None:
        self._image = image
        self._network = network
        self._extra = list(extra_args)

    async def exec(self, cmd: str, cwd: Path) -> tuple[int, str]:
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", self._network,
            "-v", f"{cwd}:/work",
            "-w", "/work",
            *self._extra,
            self._image,
            "sh", "-lc", cmd,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return 1, "docker не найден в PATH"
        out_bytes, _ = await proc.communicate()
        return proc.returncode or 0, out_bytes.decode("utf-8", errors="replace")
