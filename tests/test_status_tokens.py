"""v2-011: expose tokens_in/out + cost_kind в harness status.

Данные уже лежат в `attempts` (v2-003). v2-011 добавляет:
  - Store.last_attempt(run, task) — последняя попытка с tokens/cost_kind.
  - plain `harness status` — суффикс `[50k in / 12k out estimated]` если есть.
  - markdown `## Token usage (last attempt per task)` секция.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from harness.domain.enums import RunStatus, TaskStatus
from harness.domain.models import Run, Task
from harness.interface import cli as cli_module
from harness.store.repository import Store


def _seed_with_attempt(tmp_path: Path) -> tuple[Store, str]:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(
        id="r1", project="api", goal="goal", status=RunStatus.RUNNING.value,
    ))
    store.upsert_task(Task(
        id="001", run_id="r1", title="JWT", spec_path="x",
        status=TaskStatus.DONE.value, complexity="normal", attempts=2,
    ))
    # Первая попытка без tokens (как старая БД)
    aid1 = store.start_attempt("r1", "001", 1, "auto")
    store.finish_attempt(
        aid1, run_id="r1", task_id="001", worker_output="out1",
        gates_passed=False, verdict="changes", cost_credits=0.5,
    )
    # Вторая — с tokens (как v2-003)
    aid2 = store.start_attempt("r1", "001", 2, "kimi-k2.5")
    store.finish_attempt(
        aid2, run_id="r1", task_id="001", worker_output="out2",
        gates_passed=True, verdict="approve", cost_credits=1.5,
        cost_kind="estimated", tokens_in=50_000, tokens_out=12_000,
    )
    return store, "r1"


def test_last_attempt_returns_highest_number(tmp_path: Path) -> None:
    store, run_id = _seed_with_attempt(tmp_path)
    last = store.last_attempt(run_id, "001")
    assert last is not None
    assert last.number == 2
    assert last.model == "kimi-k2.5"
    assert last.tokens_in == 50_000
    assert last.tokens_out == 12_000
    assert last.cost_kind == "estimated"


def test_last_attempt_none_when_no_attempts(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="planning"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    assert store.last_attempt("r1", "001") is None


def test_list_attempts_orders_by_number(tmp_path: Path) -> None:
    store, run_id = _seed_with_attempt(tmp_path)
    attempts = store.list_attempts(run_id, "001")
    assert [a.number for a in attempts] == [1, 2]


def test_status_plain_shows_token_suffix(tmp_path: Path, capsys) -> None:
    """plain `harness status` — суффикс [50000in/12000out estimated] для задачи с токенами."""
    store, run_id = _seed_with_attempt(tmp_path)
    args = argparse.Namespace(run_id=run_id, markdown=False, write=None, project=None)
    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]
    try:
        cli_module.cmd_status(object(), args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]
    out = capsys.readouterr().out
    assert "50000in/12000out" in out
    assert "estimated" in out


def test_status_markdown_has_token_usage_section(tmp_path: Path) -> None:
    store, run_id = _seed_with_attempt(tmp_path)
    md = cli_module._format_status_markdown(run_id, store)
    assert "## Token usage" in md
    assert "kimi-k2.5" in md
    assert "50000" in md
    assert "12000" in md
    assert "estimated" in md


def test_status_markdown_skips_token_section_when_empty(tmp_path: Path) -> None:
    """Если ни у одной задачи нет attempts — секция Token usage не добавляется."""
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="planning"))
    store.upsert_task(Task(id="001", run_id="r1", title="t", spec_path="x", status="pending"))
    md = cli_module._format_status_markdown("r1", store)
    assert "## Token usage" not in md
