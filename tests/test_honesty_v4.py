"""Phase 1.5 Honesty-MVP: ledger, acceptance flip, DONE gate, evidence%."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness.evidence.acceptance import (
    create_acceptance,
    flip_item,
    load_acceptance,
)
from harness.evidence.ledger import (
    EvidenceRecord,
    append_evidence,
    done_allowed,
    list_evidence,
    new_evidence_id,
    record_gate_run,
)
from harness.evidence.youtrack import readiness_for_run
from harness.gates.profile_gate import ProfileGate, harden_soft_cmd, is_soft_gate
from harness.interface import cli as cli_module
from harness.profile import GateSpec, ProjectProfile


def test_cannot_done_without_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    create_acceptance(
        tmp_path / "acceptance.json",
        run_id="r1",
        gate_ids=["php-syntax"],
        acceptance_lines=[],
    )
    ok, reason = done_allowed(tmp_path)
    assert ok is False
    assert reason == "no_evidence_recorded"


def test_cannot_done_when_acceptance_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    create_acceptance(
        tmp_path / "acceptance.json",
        run_id="r1",
        gate_ids=["php-syntax"],
        acceptance_lines=[],
    )
    record_gate_run(
        tmp_path,
        run_id="r1",
        task_id="001",
        gate_id="php-syntax",
        exit_code=0,
        log_path="evidence/gates/php-syntax/stdout.txt",
    )
    # Ledger has green gate, but acceptance item not flipped yet.
    ok, reason = done_allowed(tmp_path, task_id="001")
    assert ok is False
    assert reason.startswith("acceptance_red:")


def test_done_allowed_after_flip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", gate_ids=["php-syntax"], acceptance_lines=[])
    rec = record_gate_run(
        tmp_path,
        run_id="r1",
        task_id="001",
        gate_id="php-syntax",
        exit_code=0,
    )
    flip_item(path, item_id="ac-gate-php-syntax", evidence_ids=[rec.id], run_root=tmp_path)
    ok, reason = done_allowed(tmp_path, task_id="001")
    assert ok is True
    assert reason == "ok"


def test_flip_rejects_missing_or_red_evidence(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", acceptance_lines=["Login works"])
    with pytest.raises(ValueError, match="evidence not in ledger"):
        flip_item(path, item_id="ac-001", evidence_ids=["ev-missing"], run_root=tmp_path)
    red = EvidenceRecord(
        id=new_evidence_id(),
        at="2026-08-05T12:00:00+00:00",
        run_id="r1",
        task_id="001",
        kind="gate",
        gate_id="x",
        exit_code=1,
        acceptance_item_ids=("ac-001",),
    )
    append_evidence(tmp_path, red)
    with pytest.raises(ValueError, match="evidence not green"):
        flip_item(path, item_id="ac-001", evidence_ids=[red.id], run_root=tmp_path)


def test_ledger_append_on_gate_helper(tmp_path: Path) -> None:
    rec = record_gate_run(
        tmp_path,
        run_id="r1",
        task_id="001",
        gate_id="php-syntax",
        exit_code=0,
        log_path="/tmp/out.txt",
    )
    rows = list_evidence(tmp_path)
    assert len(rows) == 1
    assert rows[0]["id"] == rec.id
    assert rows[0]["exit_code"] == 0
    assert rows[0]["log_path"] == "/tmp/out.txt"
    assert "ac-gate-php-syntax" in rows[0]["acceptance_item_ids"]


def test_evidence_pct_calculation(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.json"
    doc = create_acceptance(
        path,
        run_id="r1",
        acceptance_lines=["A", "B"],
        gate_ids=["lint"],
    )
    assert doc.evidence_pct == 0.0
    green = EvidenceRecord(
        id="ev-1",
        at="2026-08-05T12:00:00+00:00",
        run_id="r1",
        task_id="001",
        kind="manual_check",
        exit_code=0,
        acceptance_item_ids=("ac-001",),
        source="control_plane",
    )
    append_evidence(tmp_path, green)
    flip_item(path, item_id="ac-001", evidence_ids=["ev-1"], run_root=tmp_path)
    doc2 = load_acceptance(path)
    # 1 of 3 required green+bound
    assert abs(doc2.honesty_pcts(tmp_path)["evidence_pct"] - (100.0 / 3.0)) < 0.01
    assert abs(doc2.honesty_pcts(tmp_path)["ac_bound_pct"] - (100.0 / 3.0)) < 0.01


def test_profile_gate_writes_logs_and_runs(tmp_path: Path) -> None:
    class OkSandbox:
        async def exec(self, command: str, cwd: Path) -> tuple[int, str]:
            return 0, f"ok:{command}"

    gate = ProfileGate(
        ProjectProfile(gates=(GateSpec("lint", "echo lint"),)),
        OkSandbox(),
        allow_soft_gates=False,
    )
    log_dir = tmp_path / "evidence" / "gates"
    result = asyncio.run(gate.check(tmp_path, log_dir=log_dir))
    assert result.passed is True
    assert len(result.gate_runs) == 1
    assert result.gate_runs[0].exit_code == 0
    assert Path(result.gate_runs[0].log_path).is_file()


def test_soft_gate_hardened_when_honesty(tmp_path: Path) -> None:
    assert is_soft_gate("phpunit-optional", "phpunit skipped (no vendor)")
    hard = harden_soft_cmd(
        "phpunit-optional",
        "if [ -x vendor/bin/phpunit ]; then ./vendor/bin/phpunit; else echo skipped; fi",
    )
    assert "exit 1" in hard
    assert "HARNESS_ALLOW_SOFT_GATES" in hard

    class CaptureSandbox:
        def __init__(self) -> None:
            self.cmds: list[str] = []

        async def exec(self, command: str, cwd: Path) -> tuple[int, str]:
            self.cmds.append(command)
            return 1, "missing"

    sandbox = CaptureSandbox()
    gate = ProfileGate(
        ProjectProfile(
            gates=(
                GateSpec(
                    "phpunit-optional",
                    "if [ -x backend/app/vendor/bin/phpunit ]; then ./vendor/bin/phpunit; "
                    "else echo 'phpunit skipped (no vendor)'; fi",
                ),
            )
        ),
        sandbox,
        allow_soft_gates=False,
    )
    result = asyncio.run(gate.check(tmp_path))
    assert result.passed is False
    assert "exit 1" in sandbox.cmds[0]


def test_soft_gate_allowed_when_flag_on(tmp_path: Path) -> None:
    class CaptureSandbox:
        def __init__(self) -> None:
            self.cmds: list[str] = []

        async def exec(self, command: str, cwd: Path) -> tuple[int, str]:
            self.cmds.append(command)
            return 0, "skipped"

    sandbox = CaptureSandbox()
    soft_cmd = "else echo 'phpunit skipped (no vendor)'; fi"
    gate = ProfileGate(
        ProjectProfile(gates=(GateSpec("phpunit-optional", soft_cmd),)),
        sandbox,
        allow_soft_gates=True,
    )
    result = asyncio.run(gate.check(tmp_path))
    assert result.passed is True
    assert sandbox.cmds[0] == soft_cmd


def test_youtrack_readiness_maps_evidence_not_pipeline(tmp_path: Path) -> None:
    create_acceptance(tmp_path / "acceptance.json", run_id="r1", acceptance_lines=["x"])
    yt = readiness_for_run(tmp_path, pipeline_pct=97.0)
    assert yt.evidence_pct == 0.0
    assert yt.readiness_pct == 0.0
    assert yt.pipeline_pct == 97.0
    assert "evidence" in yt.reason.lower() or "YouTrack" in yt.reason
    assert "Готовность: 0%" in yt.marker_block


def test_status_markdown_evidence_primary(tmp_path: Path) -> None:
    from harness.domain.enums import RunStatus, TaskStatus  # noqa: PLC0415
    from harness.domain.models import Run, Task  # noqa: PLC0415
    from harness.store.repository import Store  # noqa: PLC0415

    store = Store(tmp_path / "state.db")
    store.create_run(
        Run(
            id="run-h",
            project="demo",
            goal="honesty",
            status=RunStatus.RUNNING.value,
            base_branch="main",
        )
    )
    store.upsert_task(
        Task(
            id="001",
            run_id="run-h",
            title="t",
            spec_path="x",
            status=TaskStatus.DONE.value,
        )
    )
    # Materialise acceptance under modern run root relative to cwd/tmp.
    run_root = tmp_path / ".harness" / "runs" / "run-h"
    run_root.mkdir(parents=True)
    create_acceptance(run_root / "acceptance.json", run_id="run-h", acceptance_lines=["smoke"])

    from harness.config import Settings  # noqa: PLC0415

    settings = Settings(root=tmp_path)
    md = cli_module._format_status_markdown("run-h", store, settings=settings)
    assert "evidence% (primary)" in md
    assert "pipeline% (FSM, secondary)" in md
    assert "completion:" not in md or "pipeline%" in md
    assert "**0%**" in md


def test_evidence_gate_off_still_allows_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "0")
    ok, reason = done_allowed(tmp_path)
    assert ok is True
    assert reason == "evidence_gate_disabled"


def test_lifecycle_run_gates_appends_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from harness.config import Settings
    from harness.domain.enums import RunStatus, TaskStatus
    from harness.domain.models import Run, Task
    from harness.store.repository import Store
    from harness.tasktool.lifecycle import TaskLifecycleService
    from harness.tenant.run_layout import ensure_run_layout

    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "0")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "b"], cwd=repo, check=True)
    profile = repo / ".harness" / "project.toml"
    profile.parent.mkdir()
    profile.write_text('[[gates]]\nid = "ok"\ncmd = "/bin/true"\n', encoding="utf-8")

    store = Store(tmp_path / "state.db")
    settings = Settings(root=tmp_path, use_run_roots=True, allow_soft_gates=False)
    run_id = "run-ev"
    store.create_run(
        Run(
            id=run_id,
            project="demo",
            goal="g",
            status=RunStatus.RUNNING.value,
            base_branch="main",
        )
    )
    paths = ensure_run_layout(repo, run_id)
    create_acceptance(
        paths.acceptance_json,
        run_id=run_id,
        gate_ids=["ok"],
        acceptance_lines=[],
    )
    store.upsert_task(
        Task(
            id="001",
            run_id=run_id,
            title="t",
            spec_path="x",
            status=TaskStatus.GATING.value,
            worktree_path=str(repo),
            branch="task-001",
        )
    )
    service = TaskLifecycleService(settings, store, repo)
    result = asyncio.run(service.run_gates(run_id, "001"))
    assert result.passed is True, result.output
    rows = list_evidence(paths.root)
    assert len(rows) >= 1
    assert rows[0]["gate_id"] == "ok"
    assert rows[0]["exit_code"] == 0
    assert rows[0]["log_path"]
    doc = load_acceptance(paths.acceptance_json)
    assert any(i.id == "ac-gate-ok" and i.passes for i in doc.items)


def test_lifecycle_done_refused_without_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from harness.config import Settings
    from harness.domain.enums import RunStatus, TaskStatus
    from harness.domain.models import Run, Task
    from harness.store.repository import Store
    from harness.tasktool.lifecycle import TaskLifecycleService
    from harness.tenant.run_layout import ensure_run_layout

    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "b"], cwd=repo, check=True)
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.toml").write_text(
        '[[gates]]\nid = "ok"\ncmd = "true"\n', encoding="utf-8"
    )

    store = Store(tmp_path / "state.db")
    settings = Settings(root=tmp_path, ship_mode="manual", use_run_roots=True)
    run_id = "run-refuse"
    store.create_run(
        Run(
            id=run_id,
            project="demo",
            goal="g",
            status=RunStatus.RUNNING.value,
            base_branch="main",
        )
    )
    paths = ensure_run_layout(repo, run_id)
    create_acceptance(
        paths.acceptance_json,
        run_id=run_id,
        gate_ids=["ok"],
        acceptance_lines=[],
    )
    store.upsert_task(
        Task(
            id="001",
            run_id=run_id,
            title="t",
            spec_path="x",
            status=TaskStatus.REVIEW.value,
            worktree_path=str(repo),
        )
    )
    service = TaskLifecycleService(settings, store, repo)
    result = service.apply_review(run_id, "001", "VERDICT: APPROVE\nlooks good")
    assert result.action == "judge_approve_blocked"
    assert store.get_task(run_id, "001").status == TaskStatus.REVIEW.value
    events = [e.type for e in store.list_events(run_id)]
    assert "judge_approve_blocked" in events


def test_evidence_gate_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_EVIDENCE_GATE", raising=False)
    from harness.evidence.ledger import evidence_gate_enabled

    assert evidence_gate_enabled() is True


def test_forged_acceptance_without_seal_blocks_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", gate_ids=["lint"], acceptance_lines=[])
    rec = record_gate_run(
        tmp_path, run_id="r1", task_id="001", gate_id="lint", exit_code=0
    )
    # Agent forges passes:true on disk without control-plane flip/seal refresh.
    forged = json.loads(path.read_text(encoding="utf-8"))
    for item in forged["items"]:
        item["passes"] = True
        item["evidence_ids"] = [rec.id]
    path.write_text(json.dumps(forged, indent=2) + "\n", encoding="utf-8")
    ok, reason = done_allowed(tmp_path, task_id="001")
    assert ok is False
    assert "seal" in reason


def test_forged_ledger_without_attestation_blocks_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", gate_ids=["lint"], acceptance_lines=[])
    # Hand-written green ledger row — no HMAC attestation.
    ledger = tmp_path / "evidence" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    fake_id = "ev-forged-no-hmac"
    ledger.write_text(
        json.dumps(
            {
                "id": fake_id,
                "at": "2026-08-06T00:00:00+00:00",
                "run_id": "r1",
                "task_id": "001",
                "kind": "gate",
                "gate_id": "lint",
                "exit_code": 0,
                "source": "advance",
                "acceptance_item_ids": ["ac-gate-lint"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Also forge acceptance flips + try to copy an old seal (content mismatch).
    forged = {
        "version": 1,
        "run_id": "r1",
        "policy": {},
        "items": [
            {
                "id": "ac-gate-lint",
                "category": "gate",
                "description": "Gate lint",
                "steps": ["gate:lint"],
                "required": True,
                "passes": True,
                "evidence_ids": [fake_id],
                "waived": False,
            }
        ],
    }
    path.write_text(json.dumps(forged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ok, reason = done_allowed(tmp_path, task_id="001")
    assert ok is False
    assert "seal" in reason or "attestation" in reason


def test_cli_source_evidence_not_trusted_for_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    monkeypatch.setenv("HARNESS_EVIDENCE_CLI_MINT", "0")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", gate_ids=["lint"], acceptance_lines=[])
    rec = EvidenceRecord(
        id=new_evidence_id(),
        at="2026-08-06T00:00:00+00:00",
        run_id="r1",
        task_id="001",
        kind="gate",
        gate_id="lint",
        exit_code=0,
        acceptance_item_ids=("ac-gate-lint",),
        source="cli",
    )
    append_evidence(tmp_path, rec)
    # Control-plane flip still requires trusted source.
    with pytest.raises(ValueError, match="untrusted source"):
        flip_item(path, item_id="ac-gate-lint", evidence_ids=[rec.id], run_root=tmp_path)
