"""Gate slots: worktree runs may overlap scoped gates; in-place stays serialized."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_tasktool_controller import _controller, _report

from harness.domain.enums import TaskStatus
from harness.tasktool.resources import ResourcePolicy


@pytest.mark.asyncio
async def test_second_gate_slot_used_when_first_lock_busy(tmp_path: Path) -> None:
    policy = ResourcePolicy(max_gates=2)
    controller, _, lifecycle = _controller(tmp_path, policy=policy)
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, dispatch, "HARNESS_DONE")
    # Slot 0 held by a live process (this one) — slot 1 must serve the gate.
    lock = controller.settings.root / ".harness" / "tasktool-gate.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}:other:task\n", encoding="utf-8")

    advanced = await controller.advance(run_id)

    assert advanced["tasks"][0]["status"] == TaskStatus.REVIEW.value
    assert sum(call[0] == "gates" for call in lifecycle.calls) == 1
    assert lock.exists()  # foreign slot-0 lock untouched
    lock.unlink()


@pytest.mark.asyncio
async def test_in_place_mode_keeps_single_gate_slot(tmp_path: Path) -> None:
    policy = ResourcePolicy(max_gates=2)
    controller, _, lifecycle = _controller(tmp_path, policy=policy)
    controller.settings.use_worktrees = False
    started = await controller.start(project="demo", goal="goal", approve_plan=True)
    run_id = str(started["run_id"])
    dispatch = controller.next(run_id, "agent")["dispatches"][0]
    _report(controller, tmp_path, dispatch, "HARNESS_DONE")
    lock = controller.settings.root / ".harness" / "tasktool-gate.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}:other:task\n", encoding="utf-8")

    contended = await controller.advance(run_id)

    # Shared checkout: same cwd, so the second slot must NOT be used.
    assert contended["tasks"][0]["status"] == TaskStatus.GATING.value
    assert sum(call[0] == "gates" for call in lifecycle.calls) == 0
    lock.unlink()
    replayed = await controller.advance(run_id)
    assert replayed["tasks"][0]["status"] == TaskStatus.REVIEW.value
