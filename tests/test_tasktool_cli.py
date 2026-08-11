from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.config import Settings
from harness.interface import cli as cli_module


class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def start(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("start", kwargs))
        return {"ok": True, "run_id": "run-1", "status": "running"}

    def next(self, run_id: str, agent_id: str, limit: int) -> dict[str, object]:
        self.calls.append(("next", (run_id, agent_id, limit)))
        return {"ok": True, "run_id": run_id, "dispatches": []}

    def report(
        self,
        dispatch_id: str,
        result_file: str,
        agent_id: str,
        *,
        ok: bool,
        error: str,
        worker_id: str = "",
    ) -> dict[str, object]:
        self.calls.append(
            ("report", (dispatch_id, result_file, agent_id, ok, error, worker_id))
        )
        return {"ok": True, "dispatch_id": dispatch_id}

    async def advance(
        self,
        run_id: str,
        approved_merge: bool = False,
        *,
        approve_risk: bool = False,
    ) -> dict[str, object]:
        self.calls.append(("advance", (run_id, approved_merge, approve_risk)))
        return {"ok": True, "run_id": run_id}

    def status(self, run_id: str) -> dict[str, object]:
        return {"ok": True, "run_id": run_id}

    def abort(self, run_id: str, reason: str) -> dict[str, object]:
        return {"ok": True, "run_id": run_id, "reason": reason}

    async def resume(
        self,
        run_id: str,
        *,
        approve_risk: bool = False,
        answers: dict[str, str] | None = None,
        answers_file: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("resume", (run_id, approve_risk, answers, answers_file)))
        return {"ok": True, "run_id": run_id}


def test_tasktool_parser_exposes_frozen_surface() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(
        [
            "tasktool",
            "start",
            "goal",
            "--project",
            "demo",
            "--spec-source",
            "a.md",
            "--spec-source",
            "b.md",
        ]
    )

    assert args.tasktool_action == "start"
    assert args.spec_source == ["a.md", "b.md"]
    next_args = parser.parse_args(
        ["tasktool", "next", "run-1", "--agent-id", "cursor-root"]
    )
    # Default wave matches MAX_TASKTOOL_JOBS default on the 24-CPU host.
    assert next_args.limit == 12
    commands = {
        "start": ["goal"],
        "next": ["run-1", "--agent-id", "cursor-root"],
        "report": [
            "dispatch-1",
            "--result-file",
            "result.json",
            "--agent-id",
            "cursor-root",
            "--worker-id",
            "task-tool-job-1",
        ],
        "advance": ["run-1"],
        "status": ["run-1"],
        "abort": ["run-1"],
        "resume": ["run-1"],
    }
    assert {
        parser.parse_args(["tasktool", name, *arguments]).tasktool_action
        for name, arguments in commands.items()
    } == set(commands)


def test_tasktool_commands_emit_json_and_never_build_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli_module, "_tasktool_controller", lambda settings, args: fake)
    monkeypatch.setattr(
        cli_module,
        "build_runner",
        lambda *args, **kwargs: pytest.fail("TaskTool CLI constructed an agent runner"),
    )
    settings = Settings(root=tmp_path)
    args = cli_module.build_parser().parse_args(
        ["tasktool", "next", "run-1", "--agent-id", "cursor-root"]
    )

    rc = args.func(settings, args)

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-1"
    assert fake.calls == [("next", ("run-1", "cursor-root", 12))]


def test_all_tasktool_handlers_emit_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli_module, "_tasktool_controller", lambda settings, args: fake)
    parser = cli_module.build_parser()
    commands = [
        ["tasktool", "start", "goal"],
        ["tasktool", "next", "run-1", "--agent-id", "root"],
        [
            "tasktool",
            "report",
            "dispatch-1",
            "--result-file",
            "result.json",
            "--agent-id",
            "root",
        ],
        ["tasktool", "advance", "run-1"],
        ["tasktool", "status", "run-1"],
        ["tasktool", "abort", "run-1"],
        ["tasktool", "resume", "run-1"],
    ]

    for argv in commands:
        args = parser.parse_args(argv)
        assert args.func(Settings(root=tmp_path), args) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True


def test_tasktool_errors_are_json_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(settings: Settings, args: object) -> object:
        raise ValueError("bad request")

    monkeypatch.setattr(cli_module, "_tasktool_controller", fail)
    args = cli_module.build_parser().parse_args(["tasktool", "status", "missing"])

    rc = args.func(Settings(root=tmp_path), args)

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "bad request"


def test_advance_and_resume_expose_approve_risk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeController()
    monkeypatch.setattr(cli_module, "_tasktool_controller", lambda settings, args: fake)
    settings = Settings(root=tmp_path)

    advance_args = cli_module.build_parser().parse_args(
        ["tasktool", "advance", "run-1", "--approve-risk"]
    )
    assert advance_args.func(settings, advance_args) == 0
    assert fake.calls[-1] == ("advance", ("run-1", False, True))
    capsys.readouterr()

    resume_args = cli_module.build_parser().parse_args(
        ["tasktool", "resume", "run-1", "--approve-risk", "--answer", "q1=picked"]
    )
    assert resume_args.func(settings, resume_args) == 0
    assert fake.calls[-1] == ("resume", ("run-1", True, {"q1": "picked"}, None))
    assert json.loads(capsys.readouterr().out)["ok"] is True
