"""Multi-active lease index for Phase 1 isolation.

When ``HARNESS_MULTI_TENANT=1``, concurrent runs share a repo via
``.harness/leases.json`` instead of a single O_EXCL ``.active`` file.

Conflict policy (A→C design):
- worktrees on → allow multiple leases (Hands isolation)
- worktrees off → refuse second lease unless files_owned are disjoint

Acquire/release use ``flock`` on ``.harness/.leases.lock`` so concurrent
processes cannot lose updates via TOCTOU on the JSON index.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from harness.tasktool.models import paths_overlap


LEASES_REL = ".harness/leases.json"
LEASES_LOCK_REL = ".harness/.leases.lock"


@dataclass(frozen=True, slots=True)
class Lease:
    run_id: str
    user: str
    acquired_at: str
    goal: str = ""
    project: str = ""
    files_owned: tuple[str, ...] = ()
    use_worktrees: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "user": self.user,
            "acquired_at": self.acquired_at,
            "goal": self.goal,
            "project": self.project,
            "files_owned": list(self.files_owned),
            "use_worktrees": self.use_worktrees,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lease:
        owned = data.get("files_owned") or []
        return cls(
            run_id=str(data["run_id"]),
            user=str(data.get("user") or ""),
            acquired_at=str(data.get("acquired_at") or ""),
            goal=str(data.get("goal") or ""),
            project=str(data.get("project") or ""),
            files_owned=tuple(str(x) for x in owned),
            use_worktrees=bool(data.get("use_worktrees", True)),
        )


def leases_path(repo_root: Path) -> Path:
    return repo_root / LEASES_REL


def _lock_path(repo_root: Path) -> Path:
    return repo_root / LEASES_LOCK_REL


@contextmanager
def _leases_lock(repo_root: Path) -> Iterator[None]:
    path = _lock_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def default_tenant_id(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return (
        os.environ.get("HARNESS_TENANT_ID")
        or os.environ.get("USER")
        or "anonymous"
    )


def load_leases(repo_root: Path) -> dict[str, Lease]:
    path = leases_path(repo_root)
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    leases_raw = raw.get("leases") if isinstance(raw, dict) else None
    if not isinstance(leases_raw, dict):
        return {}
    out: dict[str, Lease] = {}
    for run_id, entry in leases_raw.items():
        if isinstance(entry, dict):
            payload = dict(entry)
            payload.setdefault("run_id", run_id)
            out[str(run_id)] = Lease.from_dict(payload)
    return out


def _write_leases(repo_root: Path, leases: dict[str, Lease]) -> None:
    path = leases_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "leases": {rid: lease.to_dict() for rid, lease in sorted(leases.items())},
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".leases-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def acquire_lease(
    repo_root: Path,
    run_id: str,
    *,
    user: str | None = None,
    goal: str = "",
    project: str = "",
    files_owned: tuple[str, ...] = (),
    use_worktrees: bool = True,
    allow_same: bool = True,
) -> Lease:
    """Register a multi-active lease or raise if conflict policy fails."""
    tenant = default_tenant_id(user)
    with _leases_lock(repo_root):
        leases = load_leases(repo_root)
        existing = leases.get(run_id)
        if existing is not None:
            if allow_same and existing.run_id == run_id:
                return existing
            raise RuntimeError(f"lease already held for run {run_id}")

        if not use_worktrees:
            for other in leases.values():
                if other.use_worktrees:
                    continue
                for left in files_owned:
                    for right in other.files_owned:
                        if paths_overlap(left, right):
                            raise RuntimeError(
                                f"files_owned overlap with active run {other.run_id} "
                                f"({left!r} vs {right!r}); enable worktrees or wait"
                            )
                if not files_owned and not other.files_owned:
                    # Both claim whole-repo in-place — refuse.
                    raise RuntimeError(
                        f"repository already has in-place TaskTool run {other.run_id}; "
                        "set HARNESS_USE_WORKTREES=1 for multi-active"
                    )

        lease = Lease(
            run_id=run_id,
            user=tenant,
            acquired_at=datetime.now(tz=UTC).isoformat(),
            goal=goal,
            project=project,
            files_owned=files_owned,
            use_worktrees=use_worktrees,
        )
        leases[run_id] = lease
        _write_leases(repo_root, leases)
        return lease


def release_lease(repo_root: Path, run_id: str) -> bool:
    with _leases_lock(repo_root):
        leases = load_leases(repo_root)
        if run_id not in leases:
            return False
        del leases[run_id]
        _write_leases(repo_root, leases)
        return True


def list_leases(repo_root: Path) -> list[Lease]:
    return list(load_leases(repo_root).values())
