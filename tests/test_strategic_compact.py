"""v2-032: strategic compaction — подсказка `/compact` в feedback следующей попытки.

При `context_window_remaining` ниже порога (default 60k ≈ 30% от 200k, выше и
раньше, чем `CONTEXT_LOW` v2-013 ~20%) — движок добавляет в feedback подсказку
сохранить логичный breakpoint. Воркер сам решает, звать `/compact` (авто-compact
запрещён спекой). Вызов `/compact` воркером — обычный tool-call, попадает в уже
существующий `agent_events` транскрипт (v2-009) без дополнительного кода.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import AgentEventKind
from harness.domain.models import AgentEvent, Run
from harness.runner.base import AgentResult
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


def test_hint_empty_when_remaining_unknown(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    wres = AgentResult(ok=True, text="", context_window_remaining=None)
    assert engine._strategic_compact_hint(wres) == ""


def test_hint_empty_when_remaining_above_threshold(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    wres = AgentResult(ok=True, text="", context_window_remaining=100_000)
    assert engine._strategic_compact_hint(wres) == ""


def test_hint_present_when_remaining_below_threshold(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    wres = AgentResult(ok=True, text="", context_window_remaining=50_000)
    hint = engine._strategic_compact_hint(wres)
    assert "/compact" in hint
    assert "КОНТЕКСТ ЗАКАНЧИВАЕТСЯ" in hint
    assert "50000" in hint


def test_hint_mentions_what_to_keep_and_drop(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    wres = AgentResult(ok=True, text="", context_window_remaining=1_000)
    hint = engine._strategic_compact_hint(wres)
    assert "SHARED_TASK_NOTES.md" in hint
    assert "exploration" in hint.lower() or "неудачные" in hint.lower()


def test_hint_respects_env_threshold_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_COMPACT_THRESHOLD", "10000")
    engine = _engine(tmp_path)
    # 50k выше кастомного порога 10k — подсказки быть не должно.
    wres = AgentResult(ok=True, text="", context_window_remaining=50_000)
    assert engine._strategic_compact_hint(wres) == ""

    wres_low = AgentResult(ok=True, text="", context_window_remaining=5_000)
    assert "/compact" in engine._strategic_compact_hint(wres_low)


def test_default_threshold_is_higher_than_context_low() -> None:
    """v2-032 порог (default) должен быть выше v2-013 CONTEXT_LOW — компакт
    предлагается ПРЕВЕНТИВНО, раньше жёсткого предупреждения."""
    compact_default = int(os.environ.get("HARNESS_COMPACT_THRESHOLD", "60000"))
    context_low_default = int(os.environ.get("HARNESS_CONTEXT_LOW_THRESHOLD", "40000"))
    assert compact_default > context_low_default


def test_process_task_appends_compact_hint_to_feedback_source() -> None:
    """Регресс: _process_task зовёт _strategic_compact_hint и добавляет к feedback
    ДО решения о READY (значит следующая попытка его увидит через _worker_prompt)."""
    src = inspect.getsource(Engine._process_task)
    assert "_strategic_compact_hint" in src
    hint_idx = src.index("_strategic_compact_hint")
    ready_idx = src.index('TaskStatus.READY, note="на доработку"')
    assert hint_idx < ready_idx


def test_worker_prompt_md_documents_compact_protocol() -> None:
    text = (Path(__file__).resolve().parent.parent / "prompts" / "worker.md").read_text(
        encoding="utf-8",
    )
    assert "/compact" in text
    assert "КОНТЕКСТ ЗАКАНЧИВАЕТСЯ" in text


def test_compact_tool_call_round_trips_through_agent_events(tmp_path: Path) -> None:
    """Acceptance: agent_events показывает tool_call с именем compact — это уже
    штатный путь v2-009 (AGENT_EVENT/agent_events), без нового кода движка."""
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    events = [
        AgentEvent(
            kind=AgentEventKind.TOOL_CALL.value,
            payload={"name": "compact", "input": {}},
        ),
    ]
    store.add_agent_events("r1", "001", 1, events)

    stored = store.list_agent_events("r1", task_id="001")
    assert len(stored) == 1
    assert stored[0].kind == "tool_call"
    assert stored[0].payload["name"] == "compact"
