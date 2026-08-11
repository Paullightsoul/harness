from __future__ import annotations

import asyncio
from pathlib import Path

from harness.gates.profile_gate import ProfileGate
from harness.profile import GateSpec, ProjectProfile


class RecordingSandbox:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec(self, command: str, cwd: Path) -> tuple[int, str]:
        self.commands.append(command)
        return 0, ""


def test_task_scoped_gates_and_full_mode(tmp_path: Path) -> None:
    sandbox = RecordingSandbox()
    gate = ProfileGate(
        ProjectProfile(
            gates=(
                GateSpec("all", "all"),
                GateSpec("one", "one", task_ids=("001",)),
                GateSpec("two", "two", task_ids=("002",)),
            )
        ),
        sandbox,
    )

    asyncio.run(gate.check(tmp_path, task_id="001"))
    assert sandbox.commands == ["all", "one"]

    sandbox.commands.clear()
    asyncio.run(gate.check(tmp_path, task_id="001", full=True))
    assert sandbox.commands == ["all", "one", "two"]


def test_unspecified_task_preserves_all_gate_behavior(tmp_path: Path) -> None:
    sandbox = RecordingSandbox()
    gate = ProfileGate(
        ProjectProfile(gates=(GateSpec("one", "one", task_ids=("001",)),)),
        sandbox,
    )
    asyncio.run(gate.check(tmp_path))
    assert sandbox.commands == ["one"]
