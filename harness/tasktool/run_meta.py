"""Ownership of ``<run-root>/controller.json`` — the run's control-plane metadata.

Split out of ``TaskToolController``, which had grown to ~3.5k lines and ~90
methods on one class. This is the most self-contained cluster in there: it needs
only the run-root resolver, never the store or the lifecycle, so it can be
reasoned about and tested on its own.

The invariant this module exists to hold: **every read-modify-write of
``controller.json`` happens under a cross-process lock, and every write lands
atomically.** The orchestration skill tells the dispatcher to ``report`` each
Task Tool completion the moment it lands, so several ``harness tasktool``
processes touch one run's metadata concurrently.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from harness.tenant.run_layout import resolve_run_root

META_FILENAME = "controller.json"
LOCK_FILENAME = ".controller.lock"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` in one step, with a temp name no other writer shares.

    A temp name derived from the target (``.controller.json.tmp``) let two
    processes interleave their bytes into one file and then both rename it; the
    loser's ``os.replace`` raised ``FileNotFoundError`` and took the whole
    command down with it. Same pattern as ``harness/tenant/leases.py``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


class RunMetaStore:
    """Reads and writes one repo's run metadata files."""

    def __init__(self, repo_root: Path, *, use_run_roots: bool) -> None:
        self._repo_root = repo_root
        self._use_run_roots = use_run_roots

    # ── Paths ────────────────────────────────────────────────────────────────
    def run_dir(self, run_id: str) -> Path:
        paths = resolve_run_root(
            self._repo_root, run_id, prefer_modern=self._use_run_roots
        )
        return paths.root

    def path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / META_FILENAME

    # ── Primitives ───────────────────────────────────────────────────────────
    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        """Serialize read-modify-write on this run's metadata across processes."""
        lock_path = self.path(run_id).with_name(LOCK_FILENAME)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self, run_id: str, *, missing_ok: bool = False) -> dict[str, object]:
        meta_path = self.path(run_id)
        if missing_ok and not meta_path.exists():
            return {}
        value = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"invalid controller metadata: {meta_path}")
        return value

    def write(self, run_id: str, meta: dict[str, object]) -> None:
        atomic_write(
            self.path(run_id),
            json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )

    def update(
        self,
        run_id: str,
        mutate: Callable[[dict[str, object]], None],
        *,
        missing_ok: bool = True,
        skip_if_missing: bool = False,
    ) -> dict[str, object]:
        """Apply ``mutate`` to the run meta while holding the cross-process lock."""
        with self.lock(run_id):
            meta = self.read(run_id, missing_ok=missing_ok)
            if skip_if_missing and not meta:
                return meta
            mutate(meta)
            meta["updated_at"] = _now()
            self.write(run_id, meta)
            return meta

    # ── Fields ───────────────────────────────────────────────────────────────
    def set_status(self, run_id: str, status: str) -> None:
        def _apply(meta: dict[str, object]) -> None:
            meta["status"] = status

        self.update(run_id, _apply, skip_if_missing=True)

    def set_phase_fields(
        self, run_id: str, phase: str, *, stall_reason: str, detail: str
    ) -> str:
        """Write the phase fields and return the phase that was there before.

        The caller emits ``PHASE_CHANGED`` when the value actually moved — that
        needs the store, which this module deliberately does not know about.
        """
        previous: list[str] = []

        def _apply(meta: dict[str, object]) -> None:
            previous.append(str(meta.get("phase") or ""))
            meta["phase"] = phase
            meta["stall_reason"] = stall_reason
            if detail:
                meta["phase_detail"] = detail

        self.update(run_id, _apply)
        return previous[0] if previous else ""

    def mark_acceptance_repair(self, run_id: str, reason: str) -> None:
        def _apply(meta: dict[str, object]) -> None:
            meta["acceptance_repair_pending"] = True
            meta["acceptance_repair_reason"] = reason[:500]

        self.update(run_id, _apply)

    def clear_acceptance_repair(self, run_id: str) -> None:
        with self.lock(run_id):
            meta = self.read(run_id, missing_ok=True)
            if not meta.get("acceptance_repair_pending"):
                return
            meta.pop("acceptance_repair_pending", None)
            meta.pop("acceptance_repair_reason", None)
            meta["updated_at"] = _now()
            self.write(run_id, meta)

    def acceptance_repair_pending(
        self, run_id: str, meta: dict[str, object] | None = None
    ) -> bool:
        data = meta if meta is not None else self.read(run_id, missing_ok=True)
        if bool(data.get("acceptance_repair_pending")):
            return True
        stall = str(data.get("stall_reason") or "")
        return stall.startswith("acceptance_repair:") or stall.startswith(
            "gate_red:acceptance_"
        )

    def record_dispatch_agent(
        self,
        run_id: str,
        dispatch_id: str,
        agent_id: str,
        kind: str,
        *,
        worker_id: str = "",
    ) -> None:
        """Track lease holder and executor separately for the boundary check."""

        def _apply(meta: dict[str, object]) -> None:
            agents = dict(_dict(meta.get("dispatch_agents")))
            agents[dispatch_id] = {
                "worker_id": worker_id.strip() or agent_id,
                "agent_kind": kind,
                "agent_id": agent_id,
            }
            meta["dispatch_agents"] = agents

        self.update(run_id, _apply)

    def record_dispatch_kind(self, run_id: str, dispatch_id: str, kind: str) -> None:
        def _apply(meta: dict[str, object]) -> None:
            kinds = dict(_dict(meta.get("dispatch_kinds")))
            kinds[dispatch_id] = kind
            meta["dispatch_kinds"] = kinds

        self.update(run_id, _apply, missing_ok=False)

    def bump_step_counter(self, run_id: str) -> None:
        def _apply(meta: dict[str, object]) -> None:
            current = meta.get("steps")
            meta["steps"] = (current if isinstance(current, int) else 0) + 1

        self.update(run_id, _apply)

    @staticmethod
    def dispatch_kind(meta: dict[str, object], dispatch_id: str) -> str:
        value = _dict(meta.get("dispatch_kinds")).get(dispatch_id)
        return value if isinstance(value, str) else ""
