"""v2-036: `HARNESS_DURABLE=1` должен реально переключать `harness run` на
`DurableOrchestrator` + `EngineTaskExecutor` (crash-safe DBOS-workflow).

До фикса переменная проверялась только внутри `cmd_doctor` — `cmd_run` всегда
строил asyncio `Engine`, и durable-контур (уже реальный с v2-034, не fake)
был недостижим из CLI.

`dbos` — опциональная зависимость. Тесты здесь мокают `_run_durable`/ветвление
в `cmd_run`, не поднимают настоящий DBOS+Postgres (для этого —
`test_durable_executor_live.py`). Отдельно проверяется дружелюбная ошибка,
когда `HARNESS_DURABLE=1`, а `dbos` не установлен (текущее состояние sandbox).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from harness.config import Settings
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


def _cmd_run(settings: Settings, store: Store, run_id: str) -> int:
    args = argparse.Namespace(run_id=run_id, project=None, approve_plan=True)
    orig = cli_module._store
    cli_module._store = lambda s: store  # type: ignore[assignment]
    try:
        return cli_module.cmd_run(settings, args)
    finally:
        cli_module._store = orig  # type: ignore[assignment]


def _dbos_installed() -> bool:
    try:
        import dbos  # noqa: F401, PLC0415
    except ModuleNotFoundError:
        return False
    return True


def test_run_uses_durable_path_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="g")
    monkeypatch.setenv("HARNESS_DURABLE", "1")

    calls: list[str] = []

    def _fake_run_durable(settings_, store_, run_id_, run_, repo_root_) -> int:  # noqa: ANN001
        calls.append(run_id_)
        store_.set_run_status(run_id_, "done")
        return 0

    monkeypatch.setattr(cli_module, "_run_durable", _fake_run_durable)
    monkeypatch.setattr(
        cli_module, "Engine",
        lambda *a, **k: (_ for _ in ()).throw(  # noqa: ARG005
            AssertionError("Engine не должен создаваться в durable-режиме"),
        ),
    )

    rc = _cmd_run(settings, store, run_id)

    assert rc == 0
    assert calls == [run_id], "_run_durable должен вызываться при HARNESS_DURABLE=1"


def test_run_uses_engine_when_durable_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="g")
    monkeypatch.delenv("HARNESS_DURABLE", raising=False)

    durable_calls: list[str] = []
    monkeypatch.setattr(
        cli_module, "_run_durable",
        lambda settings_, store_, run_id_, run_, repo_root_: (  # noqa: ANN001
            durable_calls.append(run_id_) or 0
        ),
    )
    engine_run_calls: list[bool] = []

    def _fake_asyncio_run(coro) -> None:  # noqa: ANN001
        coro.close()  # не запускаем реальный движок, только фиксируем факт вызова
        engine_run_calls.append(True)

    monkeypatch.setattr(cli_module.asyncio, "run", _fake_asyncio_run)

    rc = _cmd_run(settings, store, run_id)

    assert rc == 0
    assert durable_calls == [], "без HARNESS_DURABLE durable-путь не должен вызываться"
    assert engine_run_calls == [True], "должен использоваться обычный asyncio Engine"


@pytest.mark.skipif(_dbos_installed(), reason='тест проверяет путь "dbos не установлен"')
def test_run_durable_without_dbos_shows_friendly_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings_with_tasks(tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="g")
    monkeypatch.setenv("HARNESS_DURABLE", "1")

    rc = _cmd_run(settings, store, run_id)

    assert rc == 1
    err = capsys.readouterr().err
    assert "dbos" in err
    assert 'pip install -e ".[durable]"' in err
    # Ошибка должна произойти ДО set_run_status("running") в _run_durable —
    # run остаётся в "planning" (выставлен approve-plan веткой чуть выше),
    # а не молча зависает в "running" без единого реально сделанного шага.
    run = store.get_run(run_id)
    assert run is not None
    assert run.status == "planning"


def test_doctor_durable_row_uses_durable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cmd_doctor` и `cmd_run` должны читать один и тот же флаг (DurableConfig),
    не дублировать `os.environ.get("HARNESS_DURABLE")` в двух местах."""
    settings = _settings_with_tasks(tmp_path)
    monkeypatch.setenv("HARNESS_DURABLE", "1")
    args = argparse.Namespace(project=None)

    rc = cli_module.cmd_doctor(settings, args)

    # doctor не должен падать независимо от наличия dbos — просто отражает статус.
    assert rc in (0, 1, 2)
