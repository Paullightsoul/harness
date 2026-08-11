"""v2-022c: `harness mode` — переключатель run/chat одной командой +
`HARNESS_RUNNER` master switch на все роли в `Settings`.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from harness.config import Settings, _env_runner
from harness.domain.enums import RunnerKind
from harness.interface import cli as cli_module


@pytest.fixture(autouse=True)
def _clean_runner_env() -> None:
    """Изолируем тесты от реального окружения (ORCH_RUNNER и т.п. из .env)."""
    keys = ["HARNESS_RUNNER", "ORCH_RUNNER", "WORKER_RUNNER", "REVIEWER_RUNNER"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k, v in saved.items():
        if v is not None:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def test_env_runner_defaults_without_any_env() -> None:
    assert _env_runner("WORKER_RUNNER", "sdk") == "sdk"


def test_env_runner_master_switch_applies_to_unset_role() -> None:
    os.environ["HARNESS_RUNNER"] = "cursor_task"
    assert _env_runner("WORKER_RUNNER", "sdk") == "cursor_task"
    assert _env_runner("REVIEWER_RUNNER", "sdk") == "cursor_task"


def test_env_runner_explicit_role_wins_over_master_switch() -> None:
    os.environ["HARNESS_RUNNER"] = "cursor_task"
    os.environ["REVIEWER_RUNNER"] = "sdk"
    assert _env_runner("REVIEWER_RUNNER", "sdk") == "sdk"


def test_settings_roles_use_cursor_task_when_master_switch_set() -> None:
    os.environ["HARNESS_RUNNER"] = "cursor_task"
    settings = Settings()
    from harness.domain.enums import Role  # noqa: PLC0415

    assert settings.role(Role.WORKER).runner == RunnerKind.CURSOR_TASK
    assert settings.role(Role.REVIEWER).runner == RunnerKind.CURSOR_TASK
    assert settings.role(Role.ORCHESTRATOR).runner == RunnerKind.CURSOR_TASK


def test_settings_roles_default_to_sdk_without_master_switch() -> None:
    settings = Settings()
    from harness.domain.enums import Role  # noqa: PLC0415

    assert settings.role(Role.WORKER).runner == RunnerKind.SDK


def test_cmd_mode_chat_writes_master_switch(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)
    rc = cli_module.cmd_mode(settings, argparse.Namespace(mode_action="chat"))
    assert rc == 0
    content = (tmp_path / ".harness" / "mode.env").read_text(encoding="utf-8")
    assert "HARNESS_RUNNER=cursor_task" in content


def test_cmd_mode_run_clears_master_switch(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)
    cli_module.cmd_mode(settings, argparse.Namespace(mode_action="chat"))
    rc = cli_module.cmd_mode(settings, argparse.Namespace(mode_action="run"))
    assert rc == 0
    content = (tmp_path / ".harness" / "mode.env").read_text(encoding="utf-8")
    assert "HARNESS_RUNNER=cursor_task" not in content


def test_cmd_mode_show_does_not_write_file(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path)
    rc = cli_module.cmd_mode(settings, argparse.Namespace(mode_action="show"))
    assert rc == 0
    assert not (tmp_path / ".harness" / "mode.env").exists()


def test_mode_subparser_accepts_choices() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(["mode", "chat"])
    assert args.command == "mode"
    assert args.mode_action == "chat"


def test_mode_subparser_defaults_to_show() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(["mode"])
    assert args.mode_action == "show"
