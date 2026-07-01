"""v2-022: CursorTaskRunner — runner-adapter для chat-режима (Task tool в IDE).

Stub-реализация: без bridge возвращает понятную ошибку. Architectural seam
(RunnerKind.CURSOR_TASK + factory + AgentRunner contract) закрыт. Live-интеграция
через IDE-bridge — follow-up v2-022b.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.domain.enums import RunnerKind
from harness.runner.base import AgentResult, AgentRunner
from harness.runner.cursor_task_runner import CursorTaskRunner
from harness.runner.factory import build_runner


@pytest.mark.asyncio
async def test_cursor_task_runner_stub_without_bridge(tmp_path: Path) -> None:
    """Без bridge — понятная ошибка, не падает."""
    runner = CursorTaskRunner(bridge_path=None)
    res = await runner.run("prompt", model="auto", cwd=tmp_path)
    assert isinstance(res, AgentResult)
    assert not res.ok
    assert "IDE-bridge" in res.error or "bridge" in res.error


@pytest.mark.asyncio
async def test_cursor_task_runner_stub_with_bridge_path(tmp_path: Path) -> None:
    """С bridge path — возвращает заглушку-ошибку v2-022b (live integration)."""
    runner = CursorTaskRunner(bridge_path="/tmp/bridge.sock")
    res = await runner.run("prompt", model="auto", cwd=tmp_path)
    assert not res.ok
    assert "v2-022b" in res.error or "follow-up" in res.error


def test_cursor_task_runner_satisfies_agent_runner_protocol() -> None:
    """CursorTaskRunner соответствует AgentRunner Protocol (runtime checkable)."""
    runner = CursorTaskRunner(bridge_path=None)
    assert isinstance(runner, AgentRunner)


def test_runner_kind_cursor_task_in_enum() -> None:
    assert RunnerKind.CURSOR_TASK.value == "cursor_task"


def test_build_runner_returns_cursor_task_for_kind() -> None:
    """factory.build_runner(CURSOR_TASK) → CursorTaskRunner."""
    runner = build_runner(RunnerKind.CURSOR_TASK)
    assert isinstance(runner, CursorTaskRunner)


def test_build_runner_with_env_bridge() -> None:
    """HARNESS_CHAT_BRIDGE env передаётся в CursorTaskRunner."""
    old = os.environ.get("HARNESS_CHAT_BRIDGE")
    os.environ["HARNESS_CHAT_BRIDGE"] = "/tmp/test-bridge.sock"
    try:
        runner = build_runner(RunnerKind.CURSOR_TASK)
        assert isinstance(runner, CursorTaskRunner)
        assert runner._bridge == "/tmp/test-bridge.sock"  # noqa: SLF001
    finally:
        if old is None:
            os.environ.pop("HARNESS_CHAT_BRIDGE", None)
        else:
            os.environ["HARNESS_CHAT_BRIDGE"] = old
