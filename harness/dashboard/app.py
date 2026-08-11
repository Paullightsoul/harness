"""v2-014: Web-дашборд поверх event-log + agent_events.

FastAPI + WebSocket. Источник данных — `Store` (SQLite). WebSocket `/ws/run/<id>`
стримит новые events + agent_events (long-poll каждые 1s). Минимальный vanilla JS
фронт (без SPA-фреймворка).

Запуск: `harness dashboard [--host 127.0.0.1] [--port 8420]`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from harness.store.repository import Store

_TEMPLATES_DIR = Path(__file__).with_name("templates")
_STATIC_DIR = Path(__file__).with_name("static")


def create_app(store: Store) -> FastAPI:
    """Собрать FastAPI-приложение с заданным store.

    Store передаётся извне (вне request scope) — один инстанс на процесс.
    """
    app = FastAPI(title="harness dashboard", docs_url="/api/docs")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ── HTML pages ────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "index.html", {"runs": _list_runs(store)},
        )

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str) -> HTMLResponse:
        run = store.get_run(run_id)
        if run is None:
            return HTMLResponse("run not found", status_code=404)
        tasks = store.list_tasks(run_id)
        return templates.TemplateResponse(
            request, "run.html", {"run": run, "tasks": tasks},
        )

    # ── JSON API ──────────────────────────────────────────────────────────────
    @app.get("/api/runs")
    async def api_runs() -> list[dict[str, Any]]:
        return _list_runs(store)

    @app.get("/api/runs/{run_id}")
    async def api_run(run_id: str) -> JSONResponse:
        run = store.get_run(run_id)
        if run is None:
            return JSONResponse({"error": "run not found"}, status_code=404)
        tasks = store.list_tasks(run_id)
        from harness.policy.phase import derive_phase  # noqa: PLC0415

        recent = [e.type for e in store.list_events(run_id)[-40:]]
        phase = derive_phase(
            run_status=run.status,
            task_statuses=[t.status for t in tasks],
            recent_event_types=recent,
        )
        return JSONResponse({
            "run": {
                "id": run.id, "project": run.project, "goal": run.goal,
                "status": run.status, "spent_credits": run.spent_credits,
                "budget_credits": run.budget_credits,
                "phase": phase.ux_phase.value,
                "stall_reason": phase.stall_reason,
                "loop_phase": phase.loop_phase.value if phase.loop_phase else None,
            },
            "tasks": [
                {
                    "id": t.id, "title": t.title, "status": t.status,
                    "attempts": t.attempts, "complexity": t.complexity,
                    "depends_on": t.depends_on, "note": t.note,
                }
                for t in tasks
            ],
        })

    @app.get("/api/runs/{run_id}/events")
    async def api_events(run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        return [
            {
                "id": e.id, "type": e.type, "task_id": e.task_id,
                "detail": e.detail, "at": e.at,
            }
            for e in store.list_events(run_id, after_id=after_id)
        ]

    @app.get("/api/runs/{run_id}/agent_events")
    async def api_agent_events(
        run_id: str, task_id: str | None = None, after_id: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": ae.id, "kind": ae.kind, "task_id": ae.task_id,
                "payload": ae.payload, "at": ae.at,
            }
            for ae in store.list_agent_events(run_id, task_id=task_id, after_id=after_id)
        ]

    @app.get("/api/runs/{run_id}/tokens")
    async def api_tokens(run_id: str) -> list[dict[str, Any]]:
        """Token-usage последней попытки каждой задачи — для budget-метра."""
        out: list[dict[str, Any]] = []
        for t in store.list_tasks(run_id):
            last = store.last_attempt(run_id, t.id)
            if last is None:
                continue
            out.append({
                "task_id": t.id, "model": last.model,
                "tokens_in": last.tokens_in, "tokens_out": last.tokens_out,
                "cost_credits": last.cost_credits, "cost_kind": last.cost_kind,
            })
        return out

    # ── WebSocket: live-стрим новых events + agent_events ─────────────────────
    @app.websocket("/ws/run/{run_id}")
    async def ws_run(ws: WebSocket, run_id: str) -> None:
        await ws.accept()
        last_event_id = 0
        last_agent_id = 0
        # Отправить накопленные events сразу (history), затем live-poll.
        try:
            while True:
                new_events = store.list_events(run_id, after_id=last_event_id)
                for e in new_events:
                    last_event_id = max(last_event_id, e.id)
                    await ws.send_text(json.dumps({
                        "stream": "events", "id": e.id, "type": e.type,
                        "task_id": e.task_id, "detail": e.detail, "at": e.at,
                    }, ensure_ascii=False))
                new_agent = store.list_agent_events(run_id, after_id=last_agent_id)
                for ae in new_agent:
                    last_agent_id = max(last_agent_id, ae.id)
                    await ws.send_text(json.dumps({
                        "stream": "agent_events", "id": ae.id, "kind": ae.kind,
                        "task_id": ae.task_id, "payload": ae.payload, "at": ae.at,
                    }, ensure_ascii=False))
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            return

    return app


def _list_runs(store: Store) -> list[dict[str, Any]]:
    """Все run'ы из store, последние сверху."""
    return [
        {
            "id": run.id, "project": run.project, "goal": run.goal,
            "status": run.status, "spent_credits": run.spent_credits,
            "budget_credits": run.budget_credits, "created_at": run.created_at,
        }
        for run in store.list_runs(limit=100)
    ]
