"""RunMetaStore in isolation — no controller, no store, no lifecycle.

That it can be tested this way is the point of splitting it out of the 3.5k-line
controller: the metadata layer needs only a run-root resolver, so its invariants
(locked read-modify-write, atomic replace) can be pinned directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.tasktool.run_meta import RunMetaStore, atomic_write


@pytest.fixture()
def meta(tmp_path: Path) -> RunMetaStore:
    store = RunMetaStore(tmp_path, use_run_roots=True)
    store.run_dir("run-1").mkdir(parents=True)
    store.write("run-1", {"status": "running"})
    return store


def test_read_missing_is_an_error_unless_allowed(tmp_path: Path) -> None:
    store = RunMetaStore(tmp_path, use_run_roots=True)

    assert store.read("absent", missing_ok=True) == {}
    with pytest.raises(OSError):
        store.read("absent")


def test_read_rejects_non_object_metadata(meta: RunMetaStore) -> None:
    meta.path("run-1").write_text("[1, 2, 3]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid controller metadata"):
        meta.read("run-1")


def test_update_stamps_updated_at(meta: RunMetaStore) -> None:
    result = meta.update("run-1", lambda m: m.__setitem__("phase", "implement"))

    assert result["phase"] == "implement"
    assert "updated_at" in result
    assert meta.read("run-1")["phase"] == "implement"


def test_update_can_skip_when_there_is_no_metadata(tmp_path: Path) -> None:
    store = RunMetaStore(tmp_path, use_run_roots=True)

    def _boom(_: dict[str, object]) -> None:  # pragma: no cover - must not run
        raise AssertionError("mutate should not be called")

    assert store.update("absent", _boom, skip_if_missing=True) == {}
    assert not store.path("absent").exists()


def test_set_phase_fields_returns_the_previous_phase(meta: RunMetaStore) -> None:
    assert meta.set_phase_fields("run-1", "implement", stall_reason="none", detail="") == ""
    previous = meta.set_phase_fields("run-1", "review", stall_reason="none", detail="d")

    assert previous == "implement"
    stored = meta.read("run-1")
    assert stored["phase"] == "review"
    assert stored["phase_detail"] == "d"


def test_acceptance_repair_round_trip(meta: RunMetaStore) -> None:
    assert meta.acceptance_repair_pending("run-1") is False

    meta.mark_acceptance_repair("run-1", "gate red")
    assert meta.acceptance_repair_pending("run-1") is True

    meta.clear_acceptance_repair("run-1")
    assert meta.acceptance_repair_pending("run-1") is False


def test_acceptance_repair_infers_from_stall_reason(meta: RunMetaStore) -> None:
    meta.update("run-1", lambda m: m.__setitem__("stall_reason", "acceptance_repair:x"))
    assert meta.acceptance_repair_pending("run-1") is True

    meta.update("run-1", lambda m: m.__setitem__("stall_reason", "gate_red:acceptance_2"))
    assert meta.acceptance_repair_pending("run-1") is True


def test_repair_reason_is_truncated(meta: RunMetaStore) -> None:
    meta.mark_acceptance_repair("run-1", "x" * 900)

    assert len(str(meta.read("run-1")["acceptance_repair_reason"])) == 500


def test_dispatch_identity_separates_worker_from_lease_holder(meta: RunMetaStore) -> None:
    meta.record_dispatch_agent("run-1", "d1", "root-chat", "worker", worker_id="job-7")

    entry = meta.read("run-1")["dispatch_agents"]["d1"]
    assert entry == {"worker_id": "job-7", "agent_kind": "worker", "agent_id": "root-chat"}


def test_dispatch_identity_falls_back_to_the_lease_holder(meta: RunMetaStore) -> None:
    meta.record_dispatch_agent("run-1", "d1", "root-chat", "worker")

    assert meta.read("run-1")["dispatch_agents"]["d1"]["worker_id"] == "root-chat"


def test_dispatch_kind_round_trip(meta: RunMetaStore) -> None:
    meta.record_dispatch_kind("run-1", "d1", "reviewer")

    stored = meta.read("run-1")
    assert RunMetaStore.dispatch_kind(stored, "d1") == "reviewer"
    assert RunMetaStore.dispatch_kind(stored, "missing") == ""
    assert RunMetaStore.dispatch_kind({}, "d1") == ""


def test_step_counter_starts_at_zero_and_survives_junk(meta: RunMetaStore) -> None:
    meta.bump_step_counter("run-1")
    assert meta.read("run-1")["steps"] == 1

    meta.update("run-1", lambda m: m.__setitem__("steps", "not-a-number"))
    meta.bump_step_counter("run-1")
    assert meta.read("run-1")["steps"] == 1


def test_set_status_is_a_no_op_without_metadata(tmp_path: Path) -> None:
    store = RunMetaStore(tmp_path, use_run_roots=True)
    store.set_status("absent", "done")
    assert not store.path("absent").exists()


def test_atomic_write_replaces_content_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "f.json"

    atomic_write(target, json.dumps({"a": 1}))
    atomic_write(target, json.dumps({"a": 2}))

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert list(tmp_path.glob(".f.json*.tmp")) == []


def test_lock_is_reentrant_across_separate_acquisitions(meta: RunMetaStore) -> None:
    """Sequential acquisition must not deadlock the single-process happy path."""
    with meta.lock("run-1"):
        pass
    with meta.lock("run-1"):
        pass
    assert meta.read("run-1")["status"] == "running"
