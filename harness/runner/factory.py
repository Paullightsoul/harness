"""Сборка нужного исполнителя по конфигу роли."""

from __future__ import annotations

import os

from harness.domain.enums import RunnerKind
from harness.runner.base import AgentRunner
from harness.runner.cli_runner import CliRunner
from harness.runner.sdk_runner import SdkRunner


def build_runner(kind: RunnerKind) -> AgentRunner:
    if kind == RunnerKind.SDK:
        return SdkRunner()
    return CliRunner(extra_flags=os.environ.get("CURSOR_FLAGS", ""))
