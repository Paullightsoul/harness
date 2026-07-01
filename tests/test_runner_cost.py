"""v2-003: точный cost из SDK + cost_kind=actual|estimated.

Ключевая дыра #2: SDK отдаёт cost_credits=0 для подписочных/китайских моделей
(подтверждено на glm-5.2-high в run-20260630-093910). 7 ревьюеров и 7 эскалаций
не попадали в бюджет. Фикс: при cost_credits=0 — fallback на эвристику,
`cost_kind="estimated"`. При cost_credits>0 — `cost_kind="actual"`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.domain.enums import EventType, RunStatus, TaskStatus
from harness.domain.models import Run, Task
from harness.runner.cli_runner import CliRunner
from harness.runner.sdk_runner import SdkRunner
from harness.store.db import connect
from harness.store.repository import Store

# ── SdkRunner через mock SDK ──────────────────────────────────────────────────

class _FakeSdkModule:
    """Мок cursor_sdk: Agent.create — context manager, возвращает агент+result."""

    class CursorAgentError(Exception):
        pass

    def __init__(self, result: object) -> None:
        self._result = result
        self.Agent = MagicMock()
        self.AgentOptions = MagicMock()
        self.LocalAgentOptions = MagicMock()
        # Agent.create возвращает context manager с агентом
        agent = SimpleNamespace(agent_id="agent-fake")

        class _Ctx:
            def __enter__(self) -> object:
                return agent

            def __exit__(self, *args: object) -> None:
                return None

        run = SimpleNamespace(wait=lambda: self._result)
        agent.send = MagicMock(return_value=run)
        self.Agent.create = MagicMock(return_value=_Ctx())


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    fake = _FakeSdkModule(result)
    monkeypatch.setitem(__import__("sys").modules, "cursor_sdk", fake)


@pytest.mark.asyncio
async def test_sdk_runner_actual_cost_when_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK cost_credits > 0 → cost_kind='actual', используется SDK cost."""
    result = SimpleNamespace(
        status="finished", result="done", cost_credits=3.5, usage=None,
    )
    _install_fake_sdk(monkeypatch, result)
    runner = SdkRunner(api_key="fake")
    res = await runner.run("p", model="claude-opus-4-8-thinking-high", cwd=tmp_path)
    assert res.ok
    assert res.cost_credits == 3.5
    assert res.cost_kind == "actual"


@pytest.mark.asyncio
async def test_sdk_runner_fallback_to_estimate_when_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK cost_credits=0 (подписочная glm) → fallback на эвристику, cost_kind='estimated'.

    Это и есть фикс дыры #2 — без fallback 7 ревьюеров не попадали в бюджет.
    """
    result = SimpleNamespace(status="finished", result="done", cost_credits=0.0, usage=None)
    _install_fake_sdk(monkeypatch, result)
    runner = SdkRunner(api_key="fake")
    res = await runner.run("p", model="glm-5.2-high", cwd=tmp_path)
    assert res.ok
    assert res.cost_credits == 1.2  # эвристика для glm-5.2-high
    assert res.cost_kind == "estimated"


@pytest.mark.asyncio
async def test_sdk_runner_reads_usage_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если result.usage есть — достаём tokens_in/out и context_window_remaining."""
    usage = SimpleNamespace(
        input_tokens=50000, output_tokens=12000, context_window_remaining=150000,
    )
    result = SimpleNamespace(
        status="finished", result="done", cost_credits=2.0, usage=usage,
    )
    _install_fake_sdk(monkeypatch, result)
    runner = SdkRunner(api_key="fake")
    res = await runner.run("p", model="auto", cwd=tmp_path)
    assert res.tokens_in == 50000
    assert res.tokens_out == 12000
    assert res.context_window_remaining == 150000


@pytest.mark.asyncio
async def test_cli_runner_marks_estimated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI всегда estimated — нет машиночитаемого cost."""
    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"done\n", b""

    async def fake_exec(*cmd: str, cwd: str, stdout: int, stderr: int) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.delenv("HARNESS_PROMPT_VIA_SHELL", raising=False)

    runner = CliRunner()
    res = await runner.run("p", model="auto", cwd=tmp_path, log_path=tmp_path / "l.log")
    assert res.cost_kind == "estimated"
    assert res.cost_credits == 0.5  # эвристика для auto


# ── Store: finish_attempt сохраняет новые колонки ─────────────────────────────

def test_finish_attempt_persists_cost_kind_and_tokens(tmp_path: Path) -> None:
    """finish_attempt пишет cost_kind, tokens_in, tokens_out в attempts."""
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status=RunStatus.PLANNING.value))
    store.upsert_task(
        Task(id="001", run_id="r1", title="t", spec_path="x", status=TaskStatus.PENDING.value),
    )
    attempt_id = store.start_attempt("r1", "001", 1, "glm-5.2-high")
    store.finish_attempt(
        attempt_id, run_id="r1", task_id="001", worker_output="out",
        gates_passed=True, verdict="approve", cost_credits=1.2,
        cost_kind="estimated", tokens_in=50000, tokens_out=12000,
    )
    row = store._conn.execute(  # noqa: SLF001
        "SELECT cost_kind, tokens_in, tokens_out FROM attempts WHERE id=?",
        (attempt_id,),
    ).fetchone()
    assert row["cost_kind"] == "estimated"
    assert row["tokens_in"] == 50000
    assert row["tokens_out"] == 12000


def test_migrate_adds_cost_columns_to_existing_db(tmp_path: Path) -> None:
    """Старая БД без cost_kind/tokens — миграция добавляет колонки идемпотентно."""
    db = tmp_path / "old.db"
    conn = connect(db)
    # Создаём старую схему без новых колонок
    conn.executescript("""
    CREATE TABLE runs (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL,
        status TEXT NOT NULL, base_branch TEXT NOT NULL DEFAULT 'main',
        budget_credits REAL, spent_credits REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
        task_id TEXT NOT NULL, number INTEGER NOT NULL, model TEXT NOT NULL,
        worker_output TEXT NOT NULL DEFAULT '', gates_passed INTEGER,
        verdict TEXT, cost_credits REAL NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL, finished_at TEXT NOT NULL DEFAULT ''
    );
    """)
    conn.close()
    # Теперь init_db должна мигрировать
    store = Store(db)
    cols = {row["name"] for row in store._conn.execute("PRAGMA table_info(attempts)")}  # noqa: SLF001
    assert "cost_kind" in cols
    assert "tokens_in" in cols
    assert "tokens_out" in cols
    # и runs.goal_hash
    run_cols = {row["name"] for row in store._conn.execute("PRAGMA table_info(runs)")}  # noqa: SLF001
    assert "goal_hash" in run_cols


def test_budget_spent_event_carries_cost_kind(tmp_path: Path) -> None:
    """add_spend пишет cost_kind в BUDGET_SPENT event для аудита."""
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status=RunStatus.PLANNING.value))
    store.add_spend("r1", 1.5, cost_kind="estimated")
    events = [e for e in store.list_events("r1") if e.type == EventType.BUDGET_SPENT]
    assert events, "BUDGET_SPENT event должен быть"
    assert events[0].detail.get("cost_kind") == "estimated"
