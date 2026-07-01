"""v2-007: prompt всегда сохраняется в файл для аудита; опция shell-mode
скрывает его из `ps` через "$(cat file)".

cursor-agent CLI не поддерживает --prompt-file/stdin (подтверждено
docs.cursor.com + gsd-core#669), поэтому полностью убрать argv-передачу нельзя.
Реалистичный объём дыры #9: закрыть «нечитаемо в логах» (теперь есть файл) +
опционально «попадает в ps» (HARNESS_PROMPT_VIA_SHELL=1).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harness.runner.cli_runner import CliRunner


class _FakeProc:
    """Мок subprocess: завершается сразу с заданным stdout/rc."""

    def __init__(self, stdout: bytes = b"ok\n", rc: int = 0) -> None:
        self._out = stdout
        self.returncode = rc

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, b""


@pytest.mark.asyncio
async def test_prompt_saved_to_file_for_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Именованный (с log_path) промпт сохраняется в .harness/prompts/<stem>.md."""
    log_path = tmp_path / "logs" / "worker-008-a1.log"
    captured: dict[str, object] = {}

    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _FakeProc(b"done\n", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("HARNESS_PROMPT_VIA_SHELL", raising=False)

    runner = CliRunner()
    res = await runner.run(
        "BIG PROMPT " * 100, model="auto", cwd=tmp_path, log_path=log_path,
    )

    assert res.ok
    prompt_file = tmp_path / ".harness" / "prompts" / "worker-008-a1.md"
    assert prompt_file.exists()
    assert "BIG PROMPT" in prompt_file.read_text(encoding="utf-8")
    # argv-режим по умолчанию — prompt в cmd
    assert "-p" in captured["cmd"]


@pytest.mark.asyncio
async def test_shell_mode_hides_prompt_from_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HARNESS_PROMPT_VIA_SHELL=1 — shell + "$(cat file)", prompt НЕ в argv."""
    log_path = tmp_path / "logs" / "worker-008-a1.log"
    captured: dict[str, str] = {}

    async def fake_shell(cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _FakeProc(b"done\n", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        MagicMock(side_effect=AssertionError("exec не должен зваться в shell-mode")),
    )
    monkeypatch.setenv("HARNESS_PROMPT_VIA_SHELL", "1")

    runner = CliRunner()
    secret = "SECRET-PROMPT-DO-NOT-LEAK-" + "x" * 200
    res = await runner.run(secret, model="auto", cwd=tmp_path, log_path=log_path)

    assert res.ok
    assert "SECRET-PROMPT-DO-NOT-LEAK" not in captured["cmd"]
    assert "$(cat" in captured["cmd"]
    prompt_file = tmp_path / ".harness" / "prompts" / "worker-008-a1.md"
    assert secret in prompt_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_default_exec_mode_passes_prompt_in_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """По умолчанию (обратная совместимость) — exec с -p prompt в argv."""
    captured: dict[str, object] = {}

    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        captured["cmd"] = cmd
        return _FakeProc(b"ok\n", 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("HARNESS_PROMPT_VIA_SHELL", raising=False)

    runner = CliRunner()
    await runner.run(
        "hello", model="auto", cwd=tmp_path, log_path=tmp_path / "logs" / "w.log",
    )

    cmd = captured["cmd"]
    assert "hello" in cmd
    assert "cursor-agent" in cmd
    assert "--model" in cmd and "auto" in cmd


@pytest.mark.asyncio
async def test_file_not_found_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        raise FileNotFoundError("cursor-agent")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("HARNESS_PROMPT_VIA_SHELL", raising=False)

    runner = CliRunner()
    res = await runner.run("x", model="auto", cwd=tmp_path, log_path=None)
    assert not res.ok
    assert "cursor-agent" in (res.error or "")
