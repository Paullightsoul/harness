"""Гейт поверх профиля проекта: гоняет команды из `.harness/project.toml`.

Каждый гейт — команда оболочки, exit code которой и есть вердикт. Проверки идут
последовательно и fail-fast: первый красный гейт останавливает прогон, его хвост
вывода уходит воркеру/ревьюеру как фидбэк. Это языко-независимая замена MakeGate
(ось 3, ADR-0003) — оракул истины теперь работает для любого стека.
"""

from __future__ import annotations

from pathlib import Path

from harness.gates.base import GateResult
from harness.profile import ProjectProfile, load_profile
from harness.sandbox.base import Sandbox
from harness.sandbox.local import LocalSandbox

_TAIL_LINES = 60


class ProfileGate:
    def __init__(self, profile: ProjectProfile, sandbox: Sandbox | None = None) -> None:
        self._profile = profile
        self._sandbox: Sandbox = sandbox or LocalSandbox()

    async def check(self, cwd: Path) -> GateResult:
        for gate in self._profile.gates:
            code, out = await self._sandbox.exec(gate.cmd, cwd)
            if code != 0:
                tail = "\n".join(out.splitlines()[-_TAIL_LINES:])
                return GateResult(passed=False, output=f"[gate FAIL: {gate.id}] {gate.cmd}\n{tail}")
        return GateResult(passed=True, output="all gates passed")


def build_gate(repo_root: Path, sandbox: Sandbox | None = None) -> ProfileGate:
    """Фабрика: грузит профиль из репозитория и возвращает гейт по нему."""
    return ProfileGate(load_profile(repo_root), sandbox)
