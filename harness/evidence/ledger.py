"""Evidence ledger (Phase 1.5 Honesty-MVP) — append-only JSONL.

Gates and control-plane sensors write rows with exit codes + log paths.
DONE is blocked when ``HARNESS_EVIDENCE_GATE`` is on (v4 default **ON**;
escape hatch ``HARNESS_EVIDENCE_GATE=0``) unless acceptance is green with
matching **attested** ledger evidence (agents cannot forge DONE on disk).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import __version__ as HARNESS_VERSION
from harness.evidence.integrity import (
    attest_evidence_row,
    evidence_source_trusted,
    verify_acceptance_seal,
    verify_evidence_attestation,
)


def evidence_gate_enabled() -> bool:
    """V4 default ON — DONE requires attested evidence; set ``=0`` to disable."""
    return os.environ.get("HARNESS_EVIDENCE_GATE", "1") == "1"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    id: str
    at: str
    run_id: str
    task_id: str
    kind: str
    gate_id: str = ""
    exit_code: int | None = None
    log_path: str = ""
    artifact_paths: tuple[str, ...] = ()
    acceptance_item_ids: tuple[str, ...] = ()
    source: str = "advance"
    harness_version: str = HARNESS_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "gate_id": self.gate_id,
            "exit_code": self.exit_code,
            "log_path": self.log_path,
            "artifact_paths": list(self.artifact_paths),
            "acceptance_item_ids": list(self.acceptance_item_ids),
            "source": self.source,
            "harness_version": self.harness_version,
        }

    @property
    def is_green(self) -> bool:
        if self.kind in {"manual_waive", "acceptance_flip"}:
            return True
        if self.exit_code is None:
            return False
        return self.exit_code == 0


def ledger_path(run_root: Path) -> Path:
    return run_root / "evidence" / "ledger.jsonl"


def append_evidence(run_root: Path, record: EvidenceRecord) -> Path:
    """Append one attested row (HMAC). Only control-plane callers should use this."""
    path = ledger_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    signed = attest_evidence_row(record.to_dict())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(signed, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def new_evidence_id(prefix: str = "ev") -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}-{stamp}"


def list_evidence(run_root: Path) -> list[dict[str, Any]]:
    path = ledger_path(run_root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def get_evidence_by_ids(run_root: Path, evidence_ids: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    wanted = set(evidence_ids)
    found: dict[str, dict[str, Any]] = {}
    for row in list_evidence(run_root):
        rid = str(row.get("id") or "")
        if rid in wanted:
            found[rid] = row
    return found


def evidence_row_is_green(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind") or "")
    if kind in {"manual_waive", "acceptance_flip"}:
        return True
    code = row.get("exit_code")
    return code == 0


def record_gate_run(
    run_root: Path,
    *,
    run_id: str,
    task_id: str,
    gate_id: str,
    exit_code: int,
    log_path: str = "",
    acceptance_item_ids: tuple[str, ...] = (),
    source: str = "advance",
) -> EvidenceRecord:
    """Append one gate sensor row; returns the written record."""
    item_ids = acceptance_item_ids
    if not item_ids and gate_id:
        # Convention: gate-linked acceptance items use id ``ac-gate-<gate_id>``.
        item_ids = (f"ac-gate-{gate_id}",)
    record = EvidenceRecord(
        id=new_evidence_id("ev"),
        at=datetime.now(tz=UTC).isoformat(),
        run_id=run_id,
        task_id=task_id,
        kind="gate",
        gate_id=gate_id,
        exit_code=exit_code,
        log_path=log_path,
        acceptance_item_ids=item_ids,
        source=source,
    )
    append_evidence(run_root, record)
    return record


def _validate_evidence_row(row: dict[str, Any], eid: str) -> tuple[bool, str]:
    ok, reason = verify_evidence_attestation(row)
    if not ok:
        return False, f"evidence_attestation_invalid:{eid}:{reason}"
    if not evidence_source_trusted(row):
        return False, f"evidence_untrusted_source:{eid}"
    if not evidence_row_is_green(row):
        return False, f"evidence_red:{eid}"
    return True, "ok"


def done_allowed(
    run_root: Path,
    *,
    required_item_ids: list[str] | None = None,
    task_id: str | None = None,
) -> tuple[bool, str]:
    """DONE gate: when flag off, always allow; when on, require sealed acceptance + attested evidence.

    Rules (honesty mode):
    - acceptance.json must match control-plane ``acceptance.seal``;
    - required items must ``passes:true`` (or waived);
    - each green item must reference attested evidence_ids from a trusted source;
    - referenced evidence must be green (exit_code==0 or manual_waive);
    - optional ``required_item_ids`` further constrains the check;
    - optional ``task_id`` requires at least one green gate row for that task.
    """
    if not evidence_gate_enabled():
        return True, "evidence_gate_disabled"

    from harness.evidence.acceptance import load_acceptance  # noqa: PLC0415

    acceptance_path = run_root / "acceptance.json"
    if not acceptance_path.is_file():
        return False, "no_acceptance_json"

    seal_ok, seal_reason = verify_acceptance_seal(acceptance_path)
    if not seal_ok:
        return False, seal_reason

    doc = load_acceptance(acceptance_path)
    rows = list_evidence(run_root)
    if not rows:
        return False, "no_evidence_recorded"

    by_id = {str(r.get("id")): r for r in rows if r.get("id")}

    required = [
        item
        for item in doc.items
        if item.required and not item.waived
    ]
    if required_item_ids is not None:
        wanted = set(required_item_ids)
        required = [item for item in required if item.id in wanted]
        # Explicit ids that are missing from the doc still block DONE.
        known = {item.id for item in doc.items}
        missing_ids = [i for i in required_item_ids if i not in known]
        if missing_ids:
            return False, f"unknown_acceptance_items:{','.join(missing_ids)}"

    if not required:
        # No required product items — still demand at least one green gate row.
        green_gates = []
        for r in rows:
            if r.get("kind") != "gate":
                continue
            if task_id is not None and r.get("task_id") != task_id:
                continue
            ok, _ = _validate_evidence_row(r, str(r.get("id") or "?"))
            if ok:
                green_gates.append(r)
        if not green_gates:
            return False, "no_green_gate_evidence"
        return True, "ok_no_required_items"

    for item in required:
        if not item.passes:
            return False, f"acceptance_red:{item.id}"
        if not item.evidence_ids:
            return False, f"acceptance_missing_evidence_ids:{item.id}"
        for eid in item.evidence_ids:
            row = by_id.get(eid)
            if row is None:
                return False, f"evidence_missing:{eid}"
            ok, reason = _validate_evidence_row(row, eid)
            if not ok:
                return False, reason

    # Semantic 1:1 bind — shared evidence across ACs blocks DONE.
    from harness.evidence.binding import (  # noqa: PLC0415
        collect_evidence_ownership,
        exclusive_bind_enabled,
        item_has_valid_exclusive_binding,
    )

    ownership = collect_evidence_ownership(doc.items)
    if exclusive_bind_enabled():
        for eid, owners in ownership.items():
            uniq = sorted(set(owners))
            if len(uniq) > 1:
                return False, f"evidence_shared:{eid}:owners={uniq}"

    for item in required:
        ok, reason = item_has_valid_exclusive_binding(
            item,
            by_id=by_id,
            ownership=ownership,
            validate_row=_validate_evidence_row,
        )
        if not ok:
            return False, f"acceptance_unbound:{item.id}:{reason}"

    if task_id is not None:
        task_green = False
        for r in rows:
            if r.get("task_id") != task_id or r.get("kind") != "gate":
                continue
            ok, _ = _validate_evidence_row(r, str(r.get("id") or "?"))
            if ok:
                task_green = True
                break
        if not task_green:
            return False, f"no_green_gate_for_task:{task_id}"

    return True, "ok"
