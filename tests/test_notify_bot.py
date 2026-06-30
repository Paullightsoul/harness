from __future__ import annotations

import asyncio
from pathlib import Path

from harness.config import Settings
from harness.domain.models import Run, Task
from harness.interface.bot import format_runs, format_status, parse_command
from harness.interface.notify import (
    ConsoleNotifier,
    NullNotifier,
    TelegramNotifier,
    build_notifier,
)
from harness.scheduler.engine import Engine
from harness.store.repository import Store

# ── notifier ──────────────────────────────────────────────────────────────────


def test_build_notifier_console_without_telegram() -> None:
    assert isinstance(build_notifier("", ""), ConsoleNotifier)


def test_build_notifier_telegram_when_configured() -> None:
    assert isinstance(build_notifier("token", "123"), TelegramNotifier)


def test_null_and_console_notifier_do_not_raise(capsys) -> None:  # type: ignore[no-untyped-def]
    asyncio.run(NullNotifier().notify("t", "b"))
    asyncio.run(ConsoleNotifier().notify("Заголовок", "тело"))
    assert "Заголовок" in capsys.readouterr().err


# ── bot ───────────────────────────────────────────────────────────────────────


def test_parse_command_strips_slash_and_botname() -> None:
    assert parse_command("/approve@my_bot r1 003") == ("approve", ["r1", "003"])
    assert parse_command("/status") == ("status", [])
    assert parse_command("просто текст") == ("", [])


def test_format_status_and_runs(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    assert "r1" in format_runs(store)
    status = format_status(store, "r1")
    assert "task-001" in status and "r1" in status


# ── мульти-проект ─────────────────────────────────────────────────────────────


def test_engine_uses_repo_root_for_profile(tmp_path: Path) -> None:
    control = tmp_path / "control"
    repo = tmp_path / "repo"
    (control / ".harness").mkdir(parents=True)
    repo.mkdir()
    (repo / ".harness").mkdir()
    # профиль целевого репозитория, отличный от control root
    (repo / ".harness" / "project.toml").write_text(
        'language = "typescript"\n[[gates]]\nid="lint"\ncmd="pnpm lint"\n', encoding="utf-8"
    )

    store = Store(control / ".harness" / "state.db")
    engine = Engine(Settings(root=control), store, repo_root=repo)
    assert engine._repo_root == repo
    assert engine._profile.language == "typescript"
    assert engine._profile.gate_commands == ["pnpm lint"]
