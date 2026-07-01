"""v2-010: harness tail — live-tail event-log (long-poll каждые 500ms).

Показывает новые events (и опционально agent_events) по мере появления в store.
Ctrl-C → clean exit. --task / --kind фильтры. --agent-events включает транскрипт.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import threading
import time
from pathlib import Path

from harness.domain.enums import AgentEventKind, EventType, RunStatus
from harness.domain.models import AgentEvent, Run, Task
from harness.interface import cli as cli_module
from harness.store.repository import Store


def _seed(tmp_path: Path) -> tuple[Store, str]:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status=RunStatus.RUNNING.value))
    store.upsert_task(
        Task(id="001", run_id="r1", title="t", spec_path="x", status="running"),
    )
    store.add_event("r1", EventType.ATTEMPT_STARTED, task_id="001",
                    detail={"attempt": 1, "model": "auto"})
    return store, "r1"


def test_tail_subparser_accepts_flags() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args([
        "tail", "r1", "--task", "001", "--kind", "task_transition",
        "--agent-events", "--interval", "0.1",
    ])
    assert args.command == "tail"
    assert args.run_id == "r1"
    assert args.task == "001"
    assert args.kind == "task_transition"
    assert args.agent_events is True
    assert args.interval == 0.1


def test_tail_prints_existing_events_then_stops_on_keyboard_interrupt(
    tmp_path: Path, capsys,
) -> None:
    """Запускаем tail в потоке, прерываем — должны увидеть уже лежащие events."""
    store, run_id = _seed(tmp_path)
    args = argparse.Namespace(
        run_id=run_id, task=None, kind=None, agent_events=False, interval=0.05,
    )
    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]

    # Прерываем KeyboardInterrupt через ~0.2с после старта
    def _interrupt() -> None:
        time.sleep(0.2)
        # Послать SIGINT в текущий процесс — но проще: patch asyncio.run чтобы
        # бросил KeyboardInterrupt после первого цикла.
        pass

    timer = threading.Thread(target=_interrupt)
    # Подменим asyncio.run чтобы он запускал loop и прерывал после первой итерации
    orig_run = asyncio.run

    def _patched_run(coro):
        async def _short():
            await asyncio.wait_for(coro, timeout=0.3)
        with contextlib.suppress(TimeoutError, KeyboardInterrupt):
            orig_run(_short())

    cli_module.asyncio.run = _patched_run  # type: ignore[attr-defined]
    try:
        timer.start()
        rc = cli_module.cmd_tail(object(), args)
    finally:
        cli_module.asyncio.run = orig_run  # type: ignore[attr-defined]
        cli_module._store = orig  # type: ignore[assignment]
        timer.join(timeout=1.0)
    out = capsys.readouterr().out
    assert rc == 0
    # Событие ATTEMPT_STARTED должно появиться в выводе
    assert "attempt_started" in out
    assert "001" in out


def test_tail_filters_by_task(tmp_path: Path, capsys) -> None:
    """--task фильтр — только события этой задачи."""
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t1", spec_path="x", status="pending"))
    store.upsert_task(Task(id="002", run_id="r1", title="t2", spec_path="y", status="pending"))
    store.add_event("r1", EventType.TASK_CREATED, task_id="001", detail={"n": 1})
    store.add_event("r1", EventType.TASK_CREATED, task_id="002", detail={"n": 2})

    args = argparse.Namespace(
        run_id="r1", task="001", kind=None, agent_events=False, interval=0.05,
    )
    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]
    orig_run = asyncio.run

    def _patched_run(coro):
        async def _short():
            await asyncio.wait_for(coro, timeout=0.2)
        with contextlib.suppress(TimeoutError, KeyboardInterrupt):
            orig_run(_short())

    cli_module.asyncio.run = _patched_run  # type: ignore[attr-defined]
    try:
        cli_module.cmd_tail(object(), args)
    finally:
        cli_module.asyncio.run = orig_run  # type: ignore[attr-defined]
        cli_module._store = orig  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert "task-001" in out
    assert "task-002" not in out


def test_tail_includes_agent_events_when_flag_set(tmp_path: Path, capsys) -> None:
    """--agent-events — в поток добавляются agent_events (транскрипт)."""
    store, run_id = _seed(tmp_path)
    store.add_agent_events(run_id, "001", 1, [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value, payload={"name": "Read"}),
    ])
    args = argparse.Namespace(
        run_id=run_id, task=None, kind=None, agent_events=True, interval=0.05,
    )
    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]
    orig_run = asyncio.run

    def _patched_run(coro):
        async def _short():
            await asyncio.wait_for(coro, timeout=0.2)
        with contextlib.suppress(TimeoutError, KeyboardInterrupt):
            orig_run(_short())

    cli_module.asyncio.run = _patched_run  # type: ignore[attr-defined]
    try:
        cli_module.cmd_tail(object(), args)
    finally:
        cli_module.asyncio.run = orig_run  # type: ignore[attr-defined]
        cli_module._store = orig  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert "[agent]" in out
    assert "tool_call" in out
    assert "Read" in out
