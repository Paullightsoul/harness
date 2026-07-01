"""v2-013: контекст-монитор — CONTEXT_LOW / SCOPE_EXIT / LOOP_STUCK warnings в event-log.

Не блокирует воркера (пока) — observability. CONTEXT_LOW: context_window_remaining
< threshold. SCOPE_EXIT: файлы вне списка «Файлы:» из спеки. LOOP_STUCK: один
tool-call > 3 раз за попытку.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import AgentEventKind, EventType
from harness.domain.models import AgentEvent, Run, Task
from harness.policy.loop_detect import loop_stuck
from harness.policy.scope import parse_spec_files, scope_violations
from harness.runner.base import AgentResult
from harness.scheduler.engine import Engine
from harness.store.repository import Store

# ── scope: парсер + детектор ──────────────────────────────────────────────────

def test_parse_spec_files_extracts_paths() -> None:
    spec = """# Файлы
- src/auth.py
- tests/test_auth.py
- src/schemas/**
"""
    paths = parse_spec_files(spec)
    assert "src/auth.py" in paths
    assert "tests/test_auth.py" in paths
    assert "src/schemas/**" in paths


def test_parse_spec_files_empty_section() -> None:
    assert parse_spec_files("# Контекст\nнет файлов\n") == []


def test_scope_violations_finds_files_outside_spec() -> None:
    spec = """# Файлы
- src/auth.py
- tests/test_auth.py
"""
    changed = ["src/auth.py", "src/auth.py", "README.md", "infra/deploy.yaml"]
    viol = scope_violations(changed, spec)
    assert "README.md" in viol
    assert "infra/deploy.yaml" in viol
    assert "src/auth.py" not in viol


def test_scope_violations_supports_glob() -> None:
    spec = """# Файлы
- src/schemas/**
"""
    changed = ["src/schemas/auth.py", "src/schemas/user.py", "src/auth.py"]
    viol = scope_violations(changed, spec)
    assert "src/auth.py" in viol
    assert "src/schemas/auth.py" not in viol
    assert "src/schemas/user.py" not in viol


# ── loop_detect ───────────────────────────────────────────────────────────────

def test_loop_stuck_detects_repeated_tool_calls() -> None:
    events = [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                   payload={"name": "Read", "input": {"file_path": "/x.py"}}),
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                   payload={"name": "Read", "input": {"file_path": "/x.py"}}),
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                   payload={"name": "Read", "input": {"file_path": "/x.py"}}),
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                   payload={"name": "Read", "input": {"file_path": "/x.py"}}),
    ]
    stuck = loop_stuck(events, threshold=3)
    assert len(stuck) == 1
    name, inp, count = stuck[0]
    assert name == "Read"
    assert count == 4
    assert inp["file_path"] == "/x.py"


def test_loop_stuck_ignores_different_inputs() -> None:
    events = [
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                   payload={"name": "Read", "input": {"file_path": "/a.py"}}),
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                   payload={"name": "Read", "input": {"file_path": "/b.py"}}),
        AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                   payload={"name": "Read", "input": {"file_path": "/c.py"}}),
    ]
    assert loop_stuck(events, threshold=2) == []


def test_loop_stuck_ignores_non_tool_events() -> None:
    events = [
        AgentEvent(kind=AgentEventKind.ASSISTANT_MSG.value, payload={"text": "hi"}),
    ] * 5
    assert loop_stuck(events) == []


def test_loop_stuck_empty() -> None:
    assert loop_stuck([]) == []


# ── engine integration ────────────────────────────────────────────────────────

def _engine(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for r in ("reviewer", "worker", "orchestrator"):
        (prompts / f"{r}.md").write_text(f"# {r}\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


def test_check_context_low_emits_event_when_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_CONTEXT_LOW_THRESHOLD", "50000")
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="running"))
    wres = AgentResult(ok=True, text="", context_window_remaining=10_000)
    engine._check_context_low("r1", "001", wres)
    events = [e for e in store.list_events("r1") if e.type == EventType.CONTEXT_LOW]
    assert events, "CONTEXT_LOW должен быть записан"
    assert events[0].detail["remaining"] == 10_000


def test_check_context_low_no_event_when_above_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_CONTEXT_LOW_THRESHOLD", "50000")
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="running"))
    wres = AgentResult(ok=True, text="", context_window_remaining=100_000)
    engine._check_context_low("r1", "001", wres)
    assert not [e for e in store.list_events("r1") if e.type == EventType.CONTEXT_LOW]


def test_check_context_low_no_event_when_remaining_unknown(tmp_path: Path) -> None:
    """Если context_window_remaining = None (рантайм не отдал) — нет события."""
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="running"))
    wres = AgentResult(ok=True, text="", context_window_remaining=None)
    engine._check_context_low("r1", "001", wres)
    assert not [e for e in store.list_events("r1") if e.type == EventType.CONTEXT_LOW]


def test_check_loop_stuck_emits_event(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="running"))
    wres = AgentResult(
        ok=True, text="",
        events=[
            AgentEvent(kind=AgentEventKind.TOOL_CALL.value,
                       payload={"name": "Read", "input": {"file_path": "/x.py"}}),
        ] * 5,
    )
    engine._check_loop_stuck("r1", "001", wres)
    stuck_events = [e for e in store.list_events("r1") if e.type == EventType.LOOP_STUCK]
    assert stuck_events
    assert stuck_events[0].detail["tool"] == "Read"
    assert stuck_events[0].detail["count"] == 5


def test_check_scope_exit_emits_event_for_files_outside_spec(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    spec = tmp_path / "task-001.md"
    spec.write_text(
        "# Файлы\n- src/auth.py\n- tests/test_auth.py\n", encoding="utf-8",
    )
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path=str(spec), status="running"))
    task = store.get_task("r1", "001")
    assert task is not None
    viol = engine._check_scope_exit("r1", "001", task, ["src/auth.py", "README.md"])
    assert "README.md" in viol
    assert "src/auth.py" not in viol
    events = [e for e in store.list_events("r1") if e.type == EventType.SCOPE_EXIT]
    assert events
    assert "README.md" in events[0].detail["files"]
