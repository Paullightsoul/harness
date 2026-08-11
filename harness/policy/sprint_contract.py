"""Sprint-contract freeze for medium/large tasks (Phase 2 P2.2).

When ``HARNESS_SPRINT_CONTRACT=1``, medium/large/high tasks must freeze an
acceptance snapshot *before* coding. Agents cannot mutate the frozen contract;
flip still goes through the evidence ledger (honesty). Default off for solo.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_CONTRACT_COMPLEXITY = frozenset({"medium", "normal", "large", "high"})


def sprint_contract_enabled() -> bool:
    return os.environ.get("HARNESS_SPRINT_CONTRACT", "0") == "1"


def needs_sprint_contract(complexity: str) -> bool:
    if not sprint_contract_enabled():
        return False
    return (complexity or "").lower().strip() in _CONTRACT_COMPLEXITY


@dataclass(frozen=True, slots=True)
class SprintContract:
    run_id: str
    task_id: str
    acceptance_sha256: str
    acceptance_items: tuple[str, ...]
    frozen_at: str
    status: str = "frozen"  # frozen | superseded

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "acceptance_sha256": self.acceptance_sha256,
            "acceptance_items": list(self.acceptance_items),
            "frozen_at": self.frozen_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SprintContract:
        items = data.get("acceptance_items") or []
        if not isinstance(items, list):
            items = []
        return cls(
            run_id=str(data.get("run_id") or ""),
            task_id=str(data.get("task_id") or ""),
            acceptance_sha256=str(data.get("acceptance_sha256") or ""),
            acceptance_items=tuple(str(i) for i in items),
            frozen_at=str(data.get("frozen_at") or ""),
            status=str(data.get("status") or "frozen"),
        )


def contract_path(run_root: Path, task_id: str) -> Path:
    return run_root / "sprint_contracts" / f"{task_id}.json"


def acceptance_fingerprint(acceptance_lines: list[str] | tuple[str, ...]) -> str:
    normalized = "\n".join(line.strip() for line in acceptance_lines if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_frozen(run_root: Path, task_id: str) -> bool:
    path = contract_path(run_root, task_id)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(data.get("status") or "") == "frozen" and bool(data.get("acceptance_sha256"))


def freeze_contract(
    run_root: Path,
    *,
    run_id: str,
    task_id: str,
    acceptance_lines: list[str] | tuple[str, ...],
) -> SprintContract:
    """Write frozen sprint contract; idempotent if fingerprint matches."""
    path = contract_path(run_root, task_id)
    digest = acceptance_fingerprint(acceptance_lines)
    if path.is_file():
        existing = SprintContract.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if existing.acceptance_sha256 == digest and existing.status == "frozen":
            return existing
    contract = SprintContract(
        run_id=run_id,
        task_id=task_id,
        acceptance_sha256=digest,
        acceptance_items=tuple(line.strip() for line in acceptance_lines if line.strip()),
        frozen_at=datetime.now(tz=UTC).isoformat(),
        status="frozen",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(contract.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return contract


def verify_acceptance_unchanged(
    run_root: Path,
    task_id: str,
    acceptance_lines: list[str] | tuple[str, ...],
) -> tuple[bool, str]:
    """True when frozen contract still matches current acceptance text."""
    path = contract_path(run_root, task_id)
    if not path.is_file():
        return False, "sprint_contract_missing"
    contract = SprintContract.from_dict(json.loads(path.read_text(encoding="utf-8")))
    digest = acceptance_fingerprint(acceptance_lines)
    if contract.acceptance_sha256 != digest:
        return False, "sprint_contract_mutated"
    return True, "ok"
