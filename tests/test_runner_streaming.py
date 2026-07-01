"""v2-009: пост-прогонный транскрипт агент-событий.

SdkRunner собирает `run.messages()` в AgentResult.events. Engine bulk-insert'ит
их в `agent_events` таблицу + пишет `logs/<role>-<task>-a<N>.ndjson` (JSONL).
Real-time стриминг через async-bridge — follow-up (v2-010).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.config import Settings
from harness.domain.enums import AgentEventKind, EventType, RunStatus, TaskStatus
from harness.domain.models import AgentEvent, Run, Task
from harness.runner.sdk_runner import SdkRunner, _agent_events_from_message
from harness.scheduler.engine import Engine
from harness.store.repository import Store

# ── SdkRunner: сбор транскрипта из run.messages() ─────────────────────────────

class _FakeSdkModule:
    def __init__(self, result: object, messages: list[object] | None = None) -> None:
        self._result = result
        self._messages = messages or []
        self.Agent = MagicMock()
        self.AgentOptions = MagicMock()
        self.LocalAgentOptions = MagicMock()
        agent = SimpleNamespace(agent_id="agent-fake")

        class _Ctx:
            def __enter__(self_) -> object:
                return agent

            def __exit__(self_, *args: object) -> None:
                return None

        run = SimpleNamespace(wait=lambda: self._result, messages=lambda: iter(self._messages))
        agent.send = MagicMock(return_value=run)
        self.Agent.create = MagicMock(return_value=_Ctx())

    class CursorAgentError(Exception):
        pass


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, result: object, messages: list[object] | None = None,
) -> None:
    fake = _FakeSdkModule(result, messages)
    monkeypatch.setitem(__import__("sys").modules, "cursor_sdk", fake)


def _text_block(text: str) -> object:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name: str, inp: object) -> object:
    return SimpleNamespace(type="tool_use", name=name, input=inp)


def _message(msg_type: str, content: list[object]) -> object:
    return SimpleNamespace(type=msg_type, message=SimpleNamespace(content=content))


@pytest.mark.asyncio
async def test_sdk_runner_collects_transcript_from_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run.messages() с text + tool_use блоками → AgentResult.events заполнен."""
    result = SimpleNamespace(status="finished", result="done", cost_credits=2.0, usage=None)
    messages = [
        _message("assistant", [_text_block("hello world")]),
        _message("tool_use", [_tool_use_block("Read", {"file_path": "/tmp/x.py"})]),
        _message("assistant", [_text_block("all done")]),
    ]
    _install_fake_sdk(monkeypatch, result, messages)
    runner = SdkRunner(api_key="fake")
    res = await runner.run("p", model="auto", cwd=tmp_path)
    assert res.ok
    assert len(res.events) == 3
    kinds = [e.kind for e in res.events]
    assert AgentEventKind.ASSISTANT_MSG.value in kinds
    assert AgentEventKind.TOOL_CALL.value in kinds
    # tool_call payload содержит name и input
    tool_event = next(e for e in res.events if e.kind == AgentEventKind.TOOL_CALL.value)
    assert tool_event.payload["name"] == "Read"
    assert tool_event.payload["input"]["file_path"] == "/tmp/x.py"


@pytest.mark.asyncio
async def test_sdk_runner_fallback_when_no_messages_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если run.messages() не существует (старый SDK) — events пусто, прогон не падает."""
    result = SimpleNamespace(status="finished", result="done", cost_credits=1.0, usage=None)
    fake = _FakeSdkModule(result, messages=[])
    # Уберём messages()
    agent = SimpleNamespace(agent_id="a")
    run = SimpleNamespace(wait=lambda: result)

    class _Ctx:
        def __enter__(self_) -> object:
            return agent

        def __exit__(self_, *args: object) -> None:
            return None

    agent.send = MagicMock(return_value=run)
    fake.Agent.create = MagicMock(return_value=_Ctx())
    monkeypatch.setitem(__import__("sys").modules, "cursor_sdk", fake)
    runner = SdkRunner(api_key="fake")
    res = await runner.run("p", model="auto", cwd=tmp_path)
    assert res.ok
    assert res.events == []


def test_agent_events_from_message_handles_text_and_tool() -> None:
    msg = _message("assistant", [_text_block("hi"), _tool_use_block("Bash", {"cmd": "ls"})])
    events = _agent_events_from_message(msg)
    assert len(events) == 2
    assert events[0].kind == AgentEventKind.ASSISTANT_MSG.value
    assert events[0].payload["text"] == "hi"
    assert events[1].kind == AgentEventKind.TOOL_CALL.value
    assert events[1].payload["name"] == "Bash"


def test_agent_events_from_message_unknown_block_type() -> None:
    """Незнакомый block_type → OTHER event, не падаем."""
    msg = _message("assistant", [SimpleNamespace(type="weird_block", data="x")])
    events = _agent_events_from_message(msg)
    assert len(events) == 1
    assert events[0].kind == AgentEventKind.OTHER.value


def test_agent_events_from_message_empty_content() -> None:
    msg = SimpleNamespace(type="x", message=SimpleNamespace(content=None))
    assert _agent_events_from_message(msg) == []


# ── Store: agent_events таблица ───────────────────────────────────────────────

def test_store_add_and_list_agent_events(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status=RunStatus.RUNNING.value))
    store.upsert_task(
        Task(id="001", run_id="r1", title="t", spec_path="x", status=TaskStatus.READY.value),
    )
    events = [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value, payload={"name": "Read"}),
        AgentEvent(kind=AgentEventKind.ASSISTANT_MSG.value, payload={"text": "hi"}),
    ]
    store.add_agent_events("r1", "001", 1, events)
    listed = store.list_agent_events("r1", "001")
    assert len(listed) == 2
    assert listed[0].kind == AgentEventKind.TOOL_CALL.value
    assert listed[0].payload["name"] == "Read"
    assert listed[1].kind == AgentEventKind.ASSISTANT_MSG.value


def test_list_agent_events_filter_by_task(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    store.upsert_task(Task(id="002", run_id="r1", title="t2", spec_path="y", status="pending"))
    store.add_agent_events("r1", "001", 1, [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value, payload={"n": 1}),
    ])
    store.add_agent_events("r1", "002", 1, [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value, payload={"n": 2}),
    ])
    only_001 = store.list_agent_events("r1", "001")
    assert len(only_001) == 1
    assert only_001[0].payload["n"] == 1
    all_events = store.list_agent_events("r1")
    assert len(all_events) == 2


def test_agent_event_type_in_enum() -> None:
    """Регресс: EventType.AGENT_EVENT существует для event-log."""
    assert EventType.AGENT_EVENT.value == "agent_event"


# ── Engine: ndjson-лог транскрипта ─────────────────────────────────────────────

def test_engine_writes_ndjson_transcript(tmp_path: Path) -> None:
    """Engine._write_transcript_ndjson пишет JSONL-файл для дебага/tail."""
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r} stub\n", encoding="utf-8")
    engine = Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))
    events = [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value, payload={"name": "Read"}),
        AgentEvent(kind=AgentEventKind.ASSISTANT_MSG.value, payload={"text": "hi"}),
    ]
    engine._write_transcript_ndjson("001", 1, events, "worker")
    ndjson = tmp_path / "logs" / "worker-001-a1.ndjson"
    assert ndjson.exists()
    lines = ndjson.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["kind"] == AgentEventKind.TOOL_CALL.value
    assert parsed[0]["payload"]["name"] == "Read"
    assert parsed[1]["kind"] == AgentEventKind.ASSISTANT_MSG.value
