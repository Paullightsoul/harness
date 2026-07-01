"""v2-020: question-protocol — оркестратор/воркер возвращают maшиночитимый
question-блок, движок ставит NEEDS_CLARIFICATION + INPUT_REQUESTED, пользователь
отвечает `harness answer <run> <task> q1="..."` → CLARIFICATION_ANSWERED + READY.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import EventType, RunStatus, TaskStatus
from harness.domain.models import Run, Task
from harness.scheduler.engine import Engine
from harness.store.repository import Store
from harness.tasks_io.question import (
    QuestionBlock,
    format_answers_for_feedback,
    parse_answer_args,
    parse_question_block,
)

# ── parse_question_block ──────────────────────────────────────────────────────

def test_parse_question_block_fenced() -> None:
    out = (
        "размышления...\n```question\n"
        '{"need_input": true, "questions": ['
        '{"id": "q1", "question": "JWT shared?", "options": ["shared", "own"], '
        '"why": "task-002 says unify but not who issues"}'
        "]}\n```\n"
    )
    qb = parse_question_block(out)
    assert qb.parsed
    assert qb.need_input
    assert qb.has_questions
    assert len(qb.questions) == 1
    assert qb.questions[0].id == "q1"
    assert qb.questions[0].options == ["shared", "own"]
    assert "unify" in qb.questions[0].why


def test_parse_question_block_no_block_returns_unparsed() -> None:
    qb = parse_question_block("воркер просто текст без question-блока")
    assert not qb.parsed
    assert not qb.has_questions


def test_parse_question_block_need_input_false() -> None:
    out = '```question\n{"need_input": false, "questions": []}\n```'
    qb = parse_question_block(out)
    assert qb.parsed
    assert not qb.has_questions


def test_parse_question_block_fallback_raw_json() -> None:
    out = (
        'вот вопрос: {"need_input": true, "questions": ['
        '{"id": "q1", "question": "X?", "options": [], "why": ""}'
        "]}"
    )
    qb = parse_question_block(out)
    assert qb.parsed
    assert qb.has_questions
    assert qb.questions[0].id == "q1"


def test_parse_question_block_skips_json_without_need_input() -> None:
    """Сырой JSON без need_input — не question-блок."""
    qb = parse_question_block('{"other": "data"}')
    assert not qb.parsed


def test_parse_question_block_malformed_json() -> None:
    qb = parse_question_block("```question\n{not valid}\n```")
    assert not qb.parsed


def test_parse_question_block_ignores_questions_without_id() -> None:
    out = (
        '```question\n{"need_input": true, "questions": ['
        '{"question": "no id"}, {"id": "q1", "question": "ok"}'
        "]}\n```"
    )
    qb = parse_question_block(out)
    assert qb.has_questions
    assert len(qb.questions) == 1  # только q1
    assert qb.questions[0].id == "q1"


# ── helpers ───────────────────────────────────────────────────────────────────

def test_format_answers_for_feedback() -> None:
    s = format_answers_for_feedback({"q1": "shared", "q2": "own issuance"})
    assert "=== ANSWERS FROM USER ===" in s
    assert "q1: shared" in s
    assert "q2: own issuance" in s


def test_format_answers_empty() -> None:
    assert format_answers_for_feedback({}) == ""


def test_parse_answer_args_basic() -> None:
    assert parse_answer_args(['q1=value1', 'q2=value2']) == {
        "q1": "value1", "q2": "value2",
    }


def test_parse_answer_args_quoted() -> None:
    assert parse_answer_args(['q1="value with spaces"', "q2='single'"]) == {
        "q1": "value with spaces", "q2": "single",
    }


def test_parse_answer_args_no_equals_ignored() -> None:
    assert parse_answer_args(["noequals", "q1=val"]) == {"q1": "val"}


# ── engine integration ────────────────────────────────────────────────────────

def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator", "replan"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


def test_handle_input_request_records_event_and_status(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status=RunStatus.RUNNING.value))
    store.upsert_task(Task(
        id="001", run_id="r1", title="t", spec_path="x", status="running",
    ))
    qb = QuestionBlock(
        need_input=True,
        questions=[__import__("harness.tasks_io.question", fromlist=["Question"]).Question(
            id="q1", question="X?", options=["a", "b"], why="because"
        )],
        parsed=True,
    )
    engine._handle_input_request("r1", "001", qb)
    events = [e for e in store.list_events("r1") if e.type == EventType.INPUT_REQUESTED]
    assert events
    assert events[0].detail["questions"][0]["id"] == "q1"
    t = store.get_task("r1", "001")
    assert t is not None
    assert t.status == TaskStatus.NEEDS_CLARIFICATION.value


@pytest.mark.asyncio
async def test_answer_records_event_and_resumes_to_ready(tmp_path: Path) -> None:
    """Engine.answer() — записывает CLARIFICATION_ANSWERED, ставит READY."""
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status=RunStatus.RUNNING.value))
    store.upsert_task(Task(
        id="001", run_id="r1", title="t", spec_path="x",
        status=TaskStatus.NEEDS_CLARIFICATION.value,
    ))
    # Мокаем engine.run чтобы не запускать реальный цикл.
    async def _noop_run(rid: str) -> None:
        return None
    engine.run = _noop_run  # type: ignore[assignment]

    await engine.answer("r1", "001", {"q1": "shared"})

    events = [e for e in store.list_events("r1") if e.type == EventType.CLARIFICATION_ANSWERED]
    assert events
    assert events[0].detail["answers"] == {"q1": "shared"}
    t = store.get_task("r1", "001")
    assert t is not None
    assert t.status == TaskStatus.READY.value


@pytest.mark.asyncio
async def test_answer_rejects_wrong_status(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="ready"))
    with pytest.raises(ValueError, match="не в NEEDS_CLARIFICATION"):
        await engine.answer("r1", "001", {"q1": "x"})


def test_input_requested_event_type_in_enum() -> None:
    assert EventType.INPUT_REQUESTED.value == "input_requested"
    assert EventType.CLARIFICATION_ANSWERED.value == "clarification_answered"
