"""Сборка нужного исполнителя по конфигу роли."""

from __future__ import annotations

import os
from pathlib import Path

from harness.domain.enums import Role, RunnerKind
from harness.runner.base import AgentRunner
from harness.runner.cli_runner import CliRunner
from harness.runner.cursor_task_runner import CursorTaskRunner
from harness.runner.sdk_runner import SdkRunner


def build_runner(kind: RunnerKind, role: Role | None = None) -> AgentRunner:
    """`role` — опционально, нужен только `CursorTaskRunner` (v2-022c): попадает
    в `BridgeRequest.role`, чтобы root chat знал протокол обработки (живые
    вопросы+resume для orchestrator vs прозрачная трансляция для worker/reviewer).
    SDK/CLI раннеры игнорируют — они не знают о bridge вообще.
    """
    if kind == RunnerKind.SDK:
        return SdkRunner()
    if kind == RunnerKind.CURSOR_TASK:  # v2-022
        if "HARNESS_CHAT_BRIDGE" in os.environ:
            bridge: str | None = os.environ["HARNESS_CHAT_BRIDGE"] or None
        else:
            root = Path(os.environ.get("HARNESS_ROOT") or Path.cwd())
            bridge = str((root / ".harness" / "bridge").resolve())
        return CursorTaskRunner(bridge_path=bridge, role=role)
    return CliRunner(extra_flags=os.environ.get("CURSOR_FLAGS", ""))
