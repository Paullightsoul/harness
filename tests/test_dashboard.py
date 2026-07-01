"""v2-014: web-дашборд — FastAPI routes.

TestClient в starlette 0.36 + Python 3.12 ломается (`self.app = None` во
WrapASGI2). Используем httpx.ASGITransport напрямую — работает стабильнее.
WebSocket live-стриминг тестируем отдельно (через websockets lib), здесь —
только HTTP routes.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from harness.dashboard.app import create_app
from harness.domain.enums import AgentEventKind, EventType, RunStatus, TaskStatus
from harness.domain.models import AgentEvent, Run, Task
from harness.store.repository import Store


@pytest.fixture
def app_and_store(tmp_path: Path) -> tuple:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(
        id="r1", project="api", goal="REST API users", status=RunStatus.RUNNING.value,
        base_branch="main", budget_credits=100.0, spent_credits=14.2,
    ))
    store.upsert_task(Task(
        id="001", run_id="r1", title="JWT auth", spec_path="x",
        status=TaskStatus.DONE.value, complexity="normal", attempts=4,
    ))
    store.upsert_task(Task(
        id="002", run_id="r1", title="Users CRUD", spec_path="y",
        status=TaskStatus.READY.value, depends_on=["001"], complexity="high",
    ))
    store.add_event("r1", EventType.TASK_CREATED, task_id="001", detail={"n": 1})
    store.add_event("r1", EventType.ATTEMPT_STARTED, task_id="001",
                    detail={"attempt": 1, "model": "auto"})
    store.add_agent_events("r1", "001", 1, [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value, payload={"name": "Read"}),
        AgentEvent(kind=AgentEventKind.ASSISTANT_MSG.value, payload={"text": "hi"}),
    ])
    app = create_app(store)
    return app, store


@pytest.mark.asyncio
async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.get(path)


@pytest.mark.asyncio
async def test_index_page_lists_runs(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/")
    assert r.status_code == 200
    assert "r1" in r.text
    assert "REST API users" in r.text


@pytest.mark.asyncio
async def test_run_page_shows_tasks(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/run/r1")
    assert r.status_code == 200
    assert "JWT auth" in r.text
    assert "Users CRUD" in r.text


@pytest.mark.asyncio
async def test_run_page_404_for_unknown(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/run/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_api_runs_returns_list(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/api/runs")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert any(run["id"] == "r1" for run in data)


@pytest.mark.asyncio
async def test_api_run_detail(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/api/runs/r1")
    assert r.status_code == 200
    data = r.json()
    assert data["run"]["id"] == "r1"
    assert len(data["tasks"]) == 2
    assert data["tasks"][0]["id"] == "001"


@pytest.mark.asyncio
async def test_api_events(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/api/runs/r1/events")
    assert r.status_code == 200
    events = r.json()
    types = [e["type"] for e in events]
    assert "task_created" in types
    assert "attempt_started" in types


@pytest.mark.asyncio
async def test_api_events_after_id_filter(app_and_store) -> None:
    app, _ = app_and_store
    all_events = (await _get(app, "/api/runs/r1/events")).json()
    first_id = all_events[0]["id"]
    rest = (await _get(app, f"/api/runs/r1/events?after_id={first_id}")).json()
    assert all(e["id"] > first_id for e in rest)


@pytest.mark.asyncio
async def test_api_agent_events(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/api/runs/r1/agent_events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 2
    kinds = [e["kind"] for e in events]
    assert "tool_call" in kinds
    assert "assistant_msg" in kinds


@pytest.mark.asyncio
async def test_api_agent_events_filter_by_task(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/api/runs/r1/agent_events?task_id=001")
    assert r.status_code == 200
    assert all(e["task_id"] == "001" for e in r.json())


@pytest.mark.asyncio
async def test_api_tokens_returns_list(app_and_store) -> None:
    app, _ = app_and_store
    r = await _get(app, "/api/runs/r1/tokens")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_dashboard_app_creation_does_not_crash(tmp_path: Path) -> None:
    """Регресс: create_app со свежим store — не падает, routes регистрируются."""
    store = Store(tmp_path / "state.db")
    app = create_app(store)
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/" in routes
    assert "/api/runs" in routes
    assert "/ws/run/{run_id}" in routes

