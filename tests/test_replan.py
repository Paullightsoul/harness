from __future__ import annotations

import asyncio
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType, TaskStatus
from harness.domain.models import Run, Task
from harness.replan import ReplanAction, parse_replan
from harness.runner.base import AgentResult
from harness.scheduler.engine import Engine
from harness.store.repository import Store

# ── парсер ────────────────────────────────────────────────────────────────────


def test_parse_refine() -> None:
    d = parse_replan('```replan\n{"action":"refine","reason":"r","spec":"NEW"}\n```')
    assert d.action == ReplanAction.REFINE
    assert d.spec == "NEW"


def test_parse_split() -> None:
    d = parse_replan(
        '```replan\n{"action":"split","subtasks":'
        '[{"id":"010","title":"a","spec":"s","depends_on":["001"]}]}\n```'
    )
    assert d.action == ReplanAction.SPLIT
    assert d.subtasks[0].id == "010"
    assert d.subtasks[0].depends_on == ["001"]


def test_parse_block() -> None:
    assert parse_replan('```replan\n{"action":"block","reason":"need human"}\n```').action == (
        ReplanAction.BLOCK
    )


def test_garbage_defaults_to_block() -> None:
    assert parse_replan("оркестратор просто поболтал").action == ReplanAction.BLOCK


def test_refine_without_spec_is_block() -> None:
    assert parse_replan('```replan\n{"action":"refine"}\n```').action == ReplanAction.BLOCK


def test_split_without_subtasks_is_block() -> None:
    assert parse_replan('```replan\n{"action":"split","subtasks":[]}\n```').action == (
        ReplanAction.BLOCK
    )


# ── интеграция в движок ───────────────────────────────────────────────────────


class _FakeRunner:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    async def run(
        self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None
    ) -> AgentResult:
        self.calls += 1
        return AgentResult(ok=True, text=self._text)


def _engine_with_task(tmp_path: Path) -> tuple[Engine, Store]:
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "replan.md").write_text("RE-PLAN ROLE", encoding="utf-8")
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    spec = tmp_path / "tasks" / "task-001.md"
    spec.write_text("OLD SPEC", encoding="utf-8")

    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(
        Task(id="001", run_id="r1", title="t", spec_path=str(spec), status="pending")
    )
    # доводим до REVIEW — отсюда валиден переход в ESCALATE
    for dst in (TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.GATING, TaskStatus.REVIEW):
        task = store.get_task("r1", "001")
        assert task is not None
        store.transition_task(task, dst)

    engine = Engine(Settings(root=tmp_path), store)
    return engine, store


def test_escalate_refine_resets_and_rewrites_spec(tmp_path: Path) -> None:
    engine, store = _engine_with_task(tmp_path)
    engine._orchestrator_runner = _FakeRunner(  # type: ignore[assignment]
        '```replan\n{"action":"refine","spec":"BETTER SPEC"}\n```'
    )
    asyncio.run(engine._escalate("r1", "001", feedback="gates failed"))

    task = store.get_task("r1", "001")
    assert task is not None
    assert task.status == TaskStatus.READY
    assert task.attempts == 0
    assert Path(task.spec_path).read_text(encoding="utf-8") == "BETTER SPEC"
    assert engine._replans["001"] == 1
    assert any(e.type == EventType.REPLANNED for e in store.list_events("r1"))


def test_escalate_split_creates_subtasks(tmp_path: Path) -> None:
    engine, store = _engine_with_task(tmp_path)
    engine._orchestrator_runner = _FakeRunner(  # type: ignore[assignment]
        '```replan\n{"action":"split","subtasks":'
        '[{"id":"010","title":"sub","spec":"SUBSPEC","depends_on":[]}]}\n```'
    )
    asyncio.run(engine._escalate("r1", "001", feedback="too big"))

    original = store.get_task("r1", "001")
    sub = store.get_task("r1", "010")
    assert original is not None and original.status == TaskStatus.BLOCKED
    assert sub is not None and sub.status == TaskStatus.PENDING
    assert Path(sub.spec_path).read_text(encoding="utf-8") == "SUBSPEC"


def test_escalate_block_decision(tmp_path: Path) -> None:
    engine, store = _engine_with_task(tmp_path)
    engine._orchestrator_runner = _FakeRunner(  # type: ignore[assignment]
        '```replan\n{"action":"block","reason":"need creds"}\n```'
    )
    asyncio.run(engine._escalate("r1", "001"))
    task = store.get_task("r1", "001")
    assert task is not None and task.status == TaskStatus.BLOCKED


def test_escalate_blocks_when_budget_exhausted(tmp_path: Path) -> None:
    engine, store = _engine_with_task(tmp_path)
    fake = _FakeRunner('```replan\n{"action":"refine","spec":"X"}\n```')
    engine._orchestrator_runner = fake  # type: ignore[assignment]
    engine._replans["001"] = engine._s.max_replans  # бюджет уже исчерпан
    asyncio.run(engine._escalate("r1", "001"))

    task = store.get_task("r1", "001")
    assert task is not None and task.status == TaskStatus.BLOCKED
    assert fake.calls == 0  # оркестратор не вызывался — сразу BLOCKED