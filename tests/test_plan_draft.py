"""v2-023: orchestrator checkpoint — ingest создаёт Run в статусе PLAN_DRAFT,
`harness run` отказывает без `--approve-plan`. Механическое правило вместо
руками-проверь PLAN.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import RunStatus
from harness.ingest import ingest_run
from harness.interface import cli as cli_module
from harness.store.repository import Store


def _settings_with_tasks(tmp_path: Path) -> Settings:
    tasks = tmp_path / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "task-001.md").write_text(
        '---\nid: "001"\ntitle: "stub"\nstatus: "todo"\nattempts: 0\n---\n# spec\n',
        encoding="utf-8",
    )
    (tmp_path / "PLAN.md").write_text("# plan stub\n", encoding="utf-8")
    return Settings(root=tmp_path)


def test_ingest_creates_plan_draft_status(tmp_path: Path) -> None:
    """v2-023: ingest создаёт Run со status='plan_draft', не 'planning'."""
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="g")
    run = store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.PLAN_DRAFT.value


def test_plan_draft_status_in_enum() -> None:
    assert RunStatus.PLAN_DRAFT.value == "plan_draft"


def test_run_subparser_accepts_approve_plan_flag() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(["run", "r1", "--approve-plan"])
    assert args.command == "run"
    assert args.approve_plan is True


def test_run_refuses_plan_draft_without_approve(
    tmp_path: Path, capsys,
) -> None:
    """harness run для PLAN_DRAFT без --approve-plan → exit 1."""
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="g")
    args = argparse.Namespace(
        run_id=run_id, project=None, approve_plan=False,
    )
    orig = cli_module._store
    cli_module._store = lambda s: store  # type: ignore[assignment]
    try:
        rc = cli_module.cmd_run(object(), args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]
    assert rc == 1
    err = capsys.readouterr().err
    assert "PLAN_DRAFT" in err
    assert "--approve-plan" in err
    # Run остался в PLAN_DRAFT
    run = store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.PLAN_DRAFT.value


def test_run_with_approve_plan_transitions_to_planning(
    tmp_path: Path, capsys,
) -> None:
    """harness run --approve-plan для PLAN_DRAFT → переводит в planning и запускает."""
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="g")

    # Мокаем engine.run чтобы не запускать реальный цикл.
    orig_run = asyncio.run
    calls: list[str] = []

    def _patched_run(coro):
        coro.close()

        async def _capture():
            # Запишем что Run был переведён в planning до запуска
            r = store.get_run(run_id)
            if r is not None:
                calls.append(r.status)
        return orig_run(_capture())

    cli_module.asyncio.run = _patched_run  # type: ignore[attr-defined]
    args = argparse.Namespace(
        run_id=run_id, project=None, approve_plan=True,
    )
    orig = cli_module._store
    cli_module._store = lambda s: store  # type: ignore[assignment]
    try:
        cli_module.cmd_run(settings, args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]
        cli_module.asyncio.run = orig_run  # type: ignore[attr-defined]
    # Run переведён в planning (до engine.run)
    run = store.get_run(run_id)
    assert run is not None
    assert run.status == "planning"
