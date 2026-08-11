"""v2-022 + v2-022b: CursorTaskRunner tests.

v2-022 (stub) — без bridge env возвращается понятная ошибка (back-compat).
v2-022b (live) — с bridge_path: реальный file-spool клиент. Тестируем через
fake asyncio responder, который читает request и пишет response в spool.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from harness.domain.enums import Role, RunnerKind
from harness.runner.base import AgentResult, AgentRunner
from harness.runner.bridge import PROTO_VERSION
from harness.runner.cursor_task_runner import CursorTaskRunner
from harness.runner.factory import build_runner


@pytest.mark.asyncio
async def test_cursor_task_runner_stub_without_bridge(tmp_path: Path) -> None:
    """Без bridge — понятная ошибка, не падает (v2-022 back-compat)."""
    runner = CursorTaskRunner(bridge_path=None)
    res = await runner.run("prompt", model="auto", cwd=tmp_path)
    assert isinstance(res, AgentResult)
    assert not res.ok
    assert "IDE-bridge" in res.error or "bridge" in res.error


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


def test_build_runner_uses_default_bridge_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARNESS_CHAT_BRIDGE", raising=False)
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    runner = build_runner(RunnerKind.CURSOR_TASK)
    assert isinstance(runner, CursorTaskRunner)
    assert runner._bridge == str((tmp_path / ".harness" / "bridge").resolve())  # noqa: SLF001


def test_build_runner_prefers_explicit_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_CHAT_BRIDGE", "/explicit/bridge")
    runner = build_runner(RunnerKind.CURSOR_TASK)
    assert isinstance(runner, CursorTaskRunner)
    assert runner._bridge == "/explicit/bridge"  # noqa: SLF001


# ---- v2-022c: role field tests ------------------------------------------------

def test_build_runner_passes_role_to_cursor_task_runner() -> None:
    """factory.build_runner(CURSOR_TASK, role=...) прокидывает role в runner."""
    runner = build_runner(RunnerKind.CURSOR_TASK, role=Role.ORCHESTRATOR)
    assert isinstance(runner, CursorTaskRunner)
    assert runner._role == Role.ORCHESTRATOR  # noqa: SLF001


def test_build_runner_without_role_defaults_to_none() -> None:
    """Без явного role (например SDK/CLI-раннеры не передают его) — None."""
    runner = build_runner(RunnerKind.CURSOR_TASK)
    assert isinstance(runner, CursorTaskRunner)
    assert runner._role is None  # noqa: SLF001


async def _capture_role_responder(spool_dir: Path, seen_roles: list[str]) -> None:
    """Читает role из request ДО того как ответить — request будет cleanup'нут
    после response, поэтому role нужно перехватить именно на этом шаге."""
    req_dir = spool_dir / "requests"
    for _ in range(200):
        files = [p for p in req_dir.iterdir() if p.suffix == ".json"] if req_dir.exists() else []
        if files:
            break
        await asyncio.sleep(0.01)
    else:
        return
    data = json.loads(files[0].read_text(encoding="utf-8"))
    seen_roles.append(data.get("role", ""))
    await _fake_responder(spool_dir)


@pytest.mark.asyncio
async def test_cursor_task_runner_writes_role_orchestrator_in_request(tmp_path: Path) -> None:
    """role=ORCHESTRATOR → BridgeRequest.role == 'orchestrator' на диске."""
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(
        bridge_path=str(spool), role=Role.ORCHESTRATOR, timeout=3.0, poll_interval=0.01,
    )
    seen_roles: list[str] = []
    task = asyncio.create_task(_capture_role_responder(spool, seen_roles))
    await runner.run("plan this", model="auto", cwd=tmp_path)
    await task
    assert seen_roles == ["orchestrator"]


@pytest.mark.asyncio
async def test_cursor_task_runner_defaults_role_worker_in_request(tmp_path: Path) -> None:
    """Без role в конструкторе → BridgeRequest.role == 'worker' (default)."""
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(bridge_path=str(spool), timeout=3.0, poll_interval=0.01)
    seen_roles: list[str] = []
    task = asyncio.create_task(_capture_role_responder(spool, seen_roles))
    await runner.run("do task", model="auto", cwd=tmp_path)
    await task
    assert seen_roles == ["worker"]


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


# ---- v2-022b: live bridge tests ----------------------------------------------

async def _fake_responder(spool: Path, *, text: str = "result", ok: bool = True,
                          error: str = "", delay: float = 0.05) -> None:
    """Читать первый request, писать response — имитация IDE-side skill."""
    req_dir = spool / "requests"
    # Ждём появления request файла.
    for _ in range(200):
        files = [p for p in req_dir.iterdir() if p.suffix == ".json"] if req_dir.exists() else []
        if files:
            break
        await asyncio.sleep(0.01)
    else:
        return
    req_path = files[0]
    req = json.loads(req_path.read_text(encoding="utf-8"))
    resp_dir = spool / "responses"
    resp_dir.mkdir(parents=True, exist_ok=True)
    resp_path = resp_dir / f"{req['id']}.json"
    tmp = resp_path.with_suffix(".json.tmp")
    payload = {
        "id": req["id"], "ok": ok, "text": text, "error": error,
        "cost_credits": 0.0, "tokens_in": 10, "tokens_out": 5, "agent_id": "fake",
        "proto": PROTO_VERSION,
    }
    await asyncio.sleep(delay)
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.rename(resp_path)


@pytest.mark.asyncio
async def test_cursor_task_runner_live_returns_response(tmp_path: Path) -> None:
    """С bridge_path: пишет request, получает response, возвращает AgentResult.ok."""
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(
        bridge_path=str(spool), timeout=3.0, poll_interval=0.01,
    )
    task = asyncio.create_task(_fake_responder(spool, text="hello from subagent"))
    res = await runner.run("do task", model="auto", cwd=tmp_path)
    await task
    assert res.ok
    assert res.text == "hello from subagent"
    assert res.agent_id == "fake"
    assert res.tokens_in == 10
    assert res.tokens_out == 5
    assert res.cost_kind == "estimated"  # cost=0 → estimated


@pytest.mark.asyncio
async def test_cursor_task_runner_live_error_response(tmp_path: Path) -> None:
    """Response с ok=False → AgentResult.ok=False, error передан."""
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(
        bridge_path=str(spool), timeout=3.0, poll_interval=0.01,
    )
    task = asyncio.create_task(
        _fake_responder(spool, ok=False, text="", error="subagent crashed")
    )
    res = await runner.run("do task", model="auto", cwd=tmp_path)
    await task
    assert not res.ok
    assert res.error == "subagent crashed"


async def _invalid_responder(spool: Path, payload: dict[str, object]) -> None:
    req_dir = spool / "requests"
    for _ in range(200):
        files = list(req_dir.glob("*.json"))
        if files:
            break
        await asyncio.sleep(0.01)
    else:
        return
    request = json.loads(files[0].read_text(encoding="utf-8"))
    response_dir = spool / "responses"
    response_dir.mkdir(parents=True, exist_ok=True)
    (response_dir / f"{request['id']}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,error_fragment",
    [
        ({"id": "wrong", "ok": True, "proto": PROTO_VERSION}, "id mismatch"),
        ({"id": "placeholder", "ok": True, "proto": "999"}, "protocol mismatch"),
        ({"id": "placeholder", "ok": "yes", "proto": PROTO_VERSION}, "ok must be"),
    ],
)
async def test_cursor_task_runner_invalid_response_becomes_error(
    tmp_path: Path,
    payload: dict[str, object],
    error_fragment: str,
) -> None:
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(bridge_path=str(spool), timeout=3.0, poll_interval=0.01)

    async def responder() -> None:
        req_dir = spool / "requests"
        for _ in range(200):
            files = list(req_dir.glob("*.json"))
            if files:
                request_id = files[0].stem
                adjusted = {
                    key: request_id if value == "placeholder" else value
                    for key, value in payload.items()
                }
                await _invalid_responder(spool, adjusted)
                return
            await asyncio.sleep(0.01)

    task = asyncio.create_task(responder())
    result = await runner.run("sensitive prompt", model="auto", cwd=tmp_path)
    await task
    assert not result.ok
    assert error_fragment in result.error
    assert "sensitive prompt" not in result.error


@pytest.mark.asyncio
async def test_cursor_task_runner_live_timeout(tmp_path: Path) -> None:
    """Нет responder → timeout, понятная ошибка с request id."""
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(
        bridge_path=str(spool), timeout=0.1, poll_interval=0.02,
    )
    res = await runner.run("do task", model="auto", cwd=tmp_path)
    assert not res.ok
    assert "timeout" in res.error
    assert "skill" in res.error or "harness-bridge-worker" in res.error
    # Request файл остаётся (orphan) — не cleanup при timeout
    req_files = list((spool / "requests").glob("*.json"))
    assert len(req_files) == 1


@pytest.mark.asyncio
async def test_cursor_task_runner_reports_polling_progress_without_prompt(
    tmp_path: Path,
) -> None:
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(bridge_path=str(spool), timeout=3.0, poll_interval=0.01)
    messages: list[str] = []
    task = asyncio.create_task(_fake_responder(spool, delay=0.05))
    await runner.run(
        "prompt-with-secret",
        model="auto",
        cwd=tmp_path,
        progress_callback=messages.append,
    )
    await task
    assert any("waiting for response" in message for message in messages)
    assert all("prompt-with-secret" not in message for message in messages)


@pytest.mark.asyncio
async def test_cursor_task_runner_live_cleanup_after_response(tmp_path: Path) -> None:
    """После успешного response — request и response файлы удалены."""
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(
        bridge_path=str(spool), timeout=3.0, poll_interval=0.01,
    )
    task = asyncio.create_task(_fake_responder(spool))
    await runner.run("do task", model="auto", cwd=tmp_path)
    await task
    assert not list((spool / "requests").glob("*.json"))
    assert not list((spool / "responses").glob("*.json"))


@pytest.mark.asyncio
async def test_cursor_task_runner_live_writes_log_path(tmp_path: Path) -> None:
    """log_path — туда пишется финальный text сабагента."""
    spool = tmp_path / "bridge"
    runner = CursorTaskRunner(
        bridge_path=str(spool), timeout=3.0, poll_interval=0.01,
    )
    log_path = tmp_path / "logs" / "task-001.txt"
    task = asyncio.create_task(_fake_responder(spool, text="FINAL OUTPUT"))
    await runner.run("do task", model="auto", cwd=tmp_path, log_path=log_path)
    await task
    assert log_path.exists()
    assert log_path.read_text(encoding="utf-8") == "FINAL OUTPUT"
