"""Concurrent updates to ``controller.json`` must not lose or corrupt entries.

The orchestration skill tells the dispatcher to ``report`` each Task Tool
completion the moment it lands, so several ``harness tasktool`` processes can
touch one run's meta at the same time. Two things used to break there:

- read-modify-write had no cross-process lock, so the second writer silently
  dropped the first one's ``dispatch_agents`` entry;
- ``_atomic_write`` derived its temp file from the target name, so concurrent
  writers interleaved bytes into one temp file and then both renamed it.
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from harness.config import Settings
from harness.store.repository import Store
from harness.tasktool.controller import TaskToolController

_WORKERS = 12


class _NoopLifecycle:
    def __init__(self, store: Store) -> None:
        self.store = store


def _build(tmp_path: Path, index: int) -> TaskToolController:
    # controller.json is the shared resource under test; give each process its
    # own store so SQLite schema init does not become the contention point.
    store = Store(tmp_path / f"state-{index}.db")
    settings = Settings(root=tmp_path, use_run_roots=True, use_worktrees=False)
    return TaskToolController(
        settings, store, tmp_path, lifecycle=_NoopLifecycle(store)
    )


def _record_one(args: tuple[str, str, int]) -> None:
    """Runs in a separate process: record one dispatch↔agent mapping."""
    root, run_id, index = args
    controller = _build(Path(root), index)
    controller.run_meta.record_dispatch_agent(
        run_id, f"dispatch-{index}", "root-chat", "worker", worker_id=f"worker-{index}"
    )


@pytest.fixture()
def _seeded(tmp_path: Path) -> tuple[Path, str, Path]:
    run_id = "run-meta"
    run_root = tmp_path / ".harness" / "runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "controller.json").write_text(
        json.dumps({"status": "running", "dispatch_kinds": {}, "dispatch_agents": {}})
        + "\n",
        encoding="utf-8",
    )
    return tmp_path, run_id, run_root


def test_concurrent_processes_do_not_lose_meta_updates(
    _seeded: tuple[Path, str, Path],
) -> None:
    tmp_path, run_id, run_root = _seeded

    ctx = mp.get_context("fork")
    with ctx.Pool(processes=6) as pool:
        pool.map(_record_one, [(str(tmp_path), run_id, i) for i in range(_WORKERS)])

    meta = json.loads((run_root / "controller.json").read_text(encoding="utf-8"))
    agents = meta["dispatch_agents"]
    assert sorted(agents) == sorted(f"dispatch-{i}" for i in range(_WORKERS))
    assert {a["worker_id"] for a in agents.values()} == {
        f"worker-{i}" for i in range(_WORKERS)
    }
    # Pre-existing keys survive the concurrent rewrites.
    assert meta["status"] == "running"


def test_concurrent_writes_leave_valid_json(_seeded: tuple[Path, str, Path]) -> None:
    """No interleaved temp file: the surviving controller.json always parses."""
    tmp_path, run_id, run_root = _seeded

    ctx = mp.get_context("fork")
    with ctx.Pool(processes=6) as pool:
        pool.map(_record_one, [(str(tmp_path), run_id, i) for i in range(_WORKERS)])

    raw = (run_root / "controller.json").read_text(encoding="utf-8")
    assert isinstance(json.loads(raw), dict)
    # And no temp files were left behind.
    assert [p.name for p in run_root.glob(".controller.json*.tmp")] == []


def test_atomic_write_cleans_up_on_failure(tmp_path: Path) -> None:
    """A failed write leaves neither a half-written target nor a temp file."""
    target = tmp_path / "out.json"
    target.write_text("original\n", encoding="utf-8")

    with pytest.raises(TypeError):
        TaskToolController._atomic_write(target, object())  # type: ignore[arg-type]

    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob(".out.json*.tmp")) == []
