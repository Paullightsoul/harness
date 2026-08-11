"""Harness V4 Phase 2 — judge seats, precall budgets, phase UX, sprint-contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.domain.enums import EventType, RunStatus, TaskStatus
from harness.domain.models import Run, Task
from harness.policy.loop_detect import (
    fingerprints_from_result_payload,
    loop_stuck_from_fingerprints,
    tool_hash,
)
from harness.policy.phase import UxPhase, derive_phase
from harness.policy.precall import PrecallGovernor, PrecallLimits
from harness.policy.sprint_contract import (
    freeze_contract,
    is_frozen,
    needs_sprint_contract,
    verify_acceptance_unchanged,
)
from harness.store.repository import Store
from harness.tasktool.context import mask_observation


def test_derive_phase_budget_exhausted() -> None:
    snap = derive_phase(
        run_status="paused",
        task_statuses=["ready"],
        recent_event_types=["budget_predicate_hit", "budget_exceeded"],
    )
    assert snap.ux_phase is UxPhase.BUDGET_EXHAUSTED
    assert "budget" in snap.stall_reason


def test_derive_phase_coding_and_testing() -> None:
    coding = derive_phase(
        run_status="running",
        task_statuses=["running"],
        active_kinds=["worker"],
    )
    assert coding.ux_phase is UxPhase.CODING
    testing = derive_phase(
        run_status="running",
        task_statuses=["gating"],
        active_kinds=["gate"],
    )
    assert testing.ux_phase is UxPhase.TESTING


def test_derive_phase_waiting_human() -> None:
    snap = derive_phase(
        run_status="paused",
        task_statuses=["merge_queue"],
        recent_event_types=["human_gate_wait"],
    )
    assert snap.ux_phase is UxPhase.WAITING_ON_HUMAN


def test_precall_max_steps_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_PRECALL_BUDGET", "1")
    gov = PrecallGovernor(PrecallLimits(max_steps=3))
    assert gov.check(steps=2, force=True) is None
    hit = gov.check(steps=3, force=True)
    assert hit is not None
    assert hit.predicate == "max_steps"
    assert hit.phase == "budget_exhausted"


def test_precall_tool_hash_stuck(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_LOOP_DETECT", "1")
    monkeypatch.setenv("HARNESS_PRECALL_BUDGET", "0")
    gov = PrecallGovernor(PrecallLimits(max_tool_hash_repeat=2))
    hit = gov.check(tool_hash_hits=[("Read", {"path": "a"}, 5)])
    assert hit is not None
    assert hit.phase == "stuck"
    assert hit.predicate == "tool_hash_repeat"


def test_loop_detect_fingerprints() -> None:
    fp = tool_hash("Read", {"path": "/x"})
    stuck = loop_stuck_from_fingerprints([fp, fp, fp, fp], threshold=3)
    assert stuck
    assert stuck[0][2] == 4
    payload = {
        "tool_calls": [
            {"name": "Read", "input": {"path": "a"}},
            {"name": "Read", "input": {"path": "a"}},
            {"name": "Read", "input": {"path": "a"}},
            {"name": "Read", "input": {"path": "a"}},
        ]
    }
    fps = fingerprints_from_result_payload(payload)
    assert len(fps) == 4
    assert len(set(fps)) == 1


def test_sprint_contract_freeze_and_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SPRINT_CONTRACT", "1")
    assert needs_sprint_contract("medium")
    assert needs_sprint_contract("large")
    assert not needs_sprint_contract("small")
    lines = ["gate ok", "product works"]
    contract = freeze_contract(
        tmp_path, run_id="r1", task_id="001", acceptance_lines=lines
    )
    assert is_frozen(tmp_path, "001")
    assert contract.acceptance_sha256
    ok, reason = verify_acceptance_unchanged(tmp_path, "001", lines)
    assert ok and reason == "ok"
    ok2, reason2 = verify_acceptance_unchanged(tmp_path, "001", ["mutated"])
    assert not ok2
    assert reason2 == "sprint_contract_mutated"


def test_mask_observation_caps_noise() -> None:
    huge = "A" * 500 + "\n" + ("File \"x.py\", line 1\n" * 80)
    masked = mask_observation(huge, max_chars=300)
    assert len(masked) <= 300
    assert "masked" in masked or masked.endswith("...")


def test_controller_next_refuses_on_max_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from harness.config import Settings
    from harness.tasktool.controller import TaskToolController
    from harness.tenant.run_layout import ensure_run_layout

    monkeypatch.setenv("HARNESS_PRECALL_BUDGET", "1")
    monkeypatch.setenv("HARNESS_MAX_STEPS", "0")  # treated as disabled via empty? 
    # Use explicit settings.max_steps instead via env with positive.
    monkeypatch.setenv("HARNESS_MAX_STEPS", "1")

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

    settings = Settings(root=tmp_path, use_run_roots=True, max_steps=1, precall_budget=True)
    store = Store(tmp_path / "state.db")
    run_id = "run-budget"
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
    (paths.root / "controller.json").write_text(
        '{"status":"running","steps":1,"dispatch_kinds":{}}\n', encoding="utf-8"
    )
    # Minimal frozen plan so next doesn't fail plan hash — inject via meta only
    # and short-circuit by making plan load fail? Controller next verifies plan.
    # Instead unit-test governor path via _precall_check after constructing controller
    # with a stubbed _load_plan — exercise _refuse_precall through public next when
    # plan verifies. Simpler: call _precall_check / _refuse_precall directly.
    controller = TaskToolController(settings, store, repo)
    run = store.get_run(run_id)
    assert run is not None
    hit = controller._precall_check(run_id, run)
    assert hit is not None
    assert hit.predicate == "max_steps"
    refused = controller._refuse_precall(run_id, hit)
    assert refused["ok"] is False
    assert refused["phase"] == "budget_exhausted"
    assert store.get_run(run_id).status == RunStatus.PAUSED.value
    events = [e.type for e in store.list_events(run_id)]
    assert EventType.BUDGET_PREDICATE_HIT.value in events
    assert EventType.PHASE_CHANGED.value in events


def test_judge_approve_blocked_merge_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APPROVE must not enter MERGE_QUEUE when evidence is red (judge seat)."""
    import subprocess

    from harness.config import Settings
    from harness.evidence.acceptance import create_acceptance
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

    store = Store(tmp_path / "state.db")
    settings = Settings(root=tmp_path, ship_mode="merge", use_run_roots=True)
    run_id = "run-judge"
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
        acceptance_lines=["product works"],
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
    result = service.apply_review(run_id, "001", "VERDICT: APPROVE")
    assert result.action == "judge_approve_blocked"
    assert store.get_task(run_id, "001").status == TaskStatus.REVIEW.value


def test_status_markdown_includes_phase(tmp_path: Path) -> None:
    from harness.interface import cli as cli_module

    store = Store(tmp_path / "state.db")
    store.create_run(
        Run(
            id="run-phase",
            project="api",
            goal="g",
            status=RunStatus.RUNNING.value,
            base_branch="main",
        )
    )
    store.upsert_task(
        Task(
            id="001",
            run_id="run-phase",
            title="t",
            spec_path="x",
            status=TaskStatus.RUNNING.value,
            complexity="small",
        )
    )
    md = cli_module._format_status_markdown("run-phase", store)
    assert "## Phase (governor UX)" in md
    assert "**phase:**" in md
    assert "**stall_reason:**" in md
