"""v2-012: harness status --markdown / --write — portable handoff-снапшот.

Markdown-формат: readiness (counts + completion %), tasks table, blocked section,
last 10 events. --write <path> — atomic write в файл; без --write — stdout.
Без --markdown — старый plain-text вывод (обратная совместимость).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from harness.domain.enums import RunStatus, TaskStatus
from harness.domain.models import Run, Task
from harness.interface import cli as cli_module
from harness.store.repository import Store


def _seed_store(tmp_path: Path) -> tuple[Store, str]:
    store = Store(tmp_path / "state.db")
    store.create_run(Run(
        id="run-test", project="api", goal="добавить REST API",
        status=RunStatus.RUNNING.value, base_branch="main",
        budget_credits=100.0, spent_credits=14.2,
    ))
    store.upsert_task(Task(
        id="001", run_id="run-test", title="JWT auth", spec_path="x",
        status=TaskStatus.DONE.value, complexity="normal", attempts=4,
    ))
    store.upsert_task(Task(
        id="002", run_id="run-test", title="Users CRUD", spec_path="y",
        status=TaskStatus.READY.value, depends_on=["001"], complexity="high",
    ))
    store.upsert_task(Task(
        id="003", run_id="run-test", title="Blocked thing", spec_path="z",
        status=TaskStatus.BLOCKED.value, note="need human: unclear spec",
    ))
    return store, "run-test"


def _settings(tmp_path: Path) -> object:
    """Settings с root=tmp_path — для CLI-функции нужен только store, settings не используется."""
    return object()  # cmd_status не обращается к settings при наличии store


def test_status_markdown_contains_sections(tmp_path: Path) -> None:
    store, run_id = _seed_store(tmp_path)
    md = cli_module._format_status_markdown(run_id, store)
    assert "# Harness status" in md
    assert "run-test" in md
    assert "## Readiness" in md
    assert "## Tasks" in md
    assert "## Blocked" in md
    assert "JWT auth" in md
    assert "need human" in md
    # таблица задач
    assert "| id | status | attempts | deps | complexity | title |" in md
    # completion %
    assert "completion:" in md


def test_status_markdown_to_stdout(tmp_path: Path, capsys) -> None:
    store, run_id = _seed_store(tmp_path)
    args = argparse.Namespace(
        run_id=run_id, markdown=True, write=None, project=None,
    )
    # monkey-patch _store чтобы вернуть наш store
    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]
    try:
        rc = cli_module.cmd_status(_settings(tmp_path), args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]
    captured = capsys.readouterr()
    assert rc == 0
    assert "# Harness status" in captured.out


def test_status_markdown_write_to_file(tmp_path: Path, capsys) -> None:
    store, run_id = _seed_store(tmp_path)
    target = tmp_path / "out" / "status.md"
    args = argparse.Namespace(
        run_id=run_id, markdown=True, write=str(target), project=None,
    )
    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]
    try:
        rc = cli_module.cmd_status(_settings(tmp_path), args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]
    captured = capsys.readouterr()
    assert rc == 0
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "# Harness status" in content
    assert "run-test" in content
    assert f"статус записан в {target}" in captured.out


def test_status_without_markdown_keeps_plain_text(tmp_path: Path, capsys) -> None:
    """Обратная совместимость: без --markdown — plain text как раньше."""
    store, run_id = _seed_store(tmp_path)
    args = argparse.Namespace(
        run_id=run_id, markdown=False, write=None, project=None,
    )
    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]
    try:
        rc = cli_module.cmd_status(_settings(tmp_path), args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]
    captured = capsys.readouterr()
    assert rc == 0
    # plain-text — НЕ markdown
    assert "# Harness status" not in captured.out
    assert "run-test" in captured.out
    assert "JWT auth" in captured.out


def test_status_subparser_accepts_markdown_and_write_flags() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(["status", "run-x", "--markdown", "--write", "/tmp/s.md"])
    assert args.command == "status"
    assert args.markdown is True
    assert args.write == "/tmp/s.md"
    assert args.run_id == "run-x"


def _add_approved_attempt(store: Store, run_id: str, task_id: str) -> None:
    attempt_id = store.start_attempt(run_id, task_id, 1, model="auto")
    store.finish_attempt(
        attempt_id, run_id=run_id, task_id=task_id, worker_output="",
        gates_passed=True, verdict="approve", cost_credits=0.1,
    )


def test_status_markdown_includes_metrics_when_attempts_exist(tmp_path: Path) -> None:
    """v2-030: блок ## Metrics с pass@k появляется, когда есть хотя бы одна попытка."""
    store, run_id = _seed_store(tmp_path)
    _add_approved_attempt(store, run_id, "001")

    md = cli_module._format_status_markdown(run_id, store)
    assert "## Metrics" in md
    assert "pass@1=" in md


def test_status_plain_text_includes_metrics_line_when_attempts_exist(
    tmp_path: Path, capsys,
) -> None:
    store, run_id = _seed_store(tmp_path)
    _add_approved_attempt(store, run_id, "001")
    args = argparse.Namespace(run_id=run_id, markdown=False, write=None, project=None)

    orig = cli_module._store
    cli_module._store = lambda settings: store  # type: ignore[assignment]
    try:
        rc = cli_module.cmd_status(_settings(tmp_path), args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]
    captured = capsys.readouterr()
    assert rc == 0
    assert "Metrics: pass@1=" in captured.out
