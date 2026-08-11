"""Acceptance contract (Phase 1.5 Honesty-MVP) — default-FAIL product checklist.

Items start with ``passes:false``. Only the control plane may flip to true, and
only when matching green evidence_ids exist in the ledger. Agents must not
self-report product readiness by editing this file.

Semantic binding (2026-08-06): each evidence_id binds 1:1 to an AC item
(or via declared ``acceptance_item_ids`` on the ledger row). Mass-flip of many
functional ACs from one gate blob is refused by default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from harness.evidence.binding import (
    compute_honesty_pcts,
    validate_evidence_for_item,
)
from harness.evidence.integrity import (
    evidence_source_trusted,
    verify_evidence_attestation,
    write_acceptance_seal,
)
from harness.evidence.ledger import (
    evidence_row_is_green,
    get_evidence_by_ids,
    list_evidence,
)

ACCEPTANCE_VERSION = 1


@dataclass(frozen=True, slots=True)
class AcceptanceItem:
    id: str
    category: str
    description: str
    steps: tuple[str, ...]
    required: bool = True
    passes: bool = False
    evidence_ids: tuple[str, ...] = ()
    waived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
            "steps": list(self.steps),
            "required": self.required,
            "passes": self.passes,
            "evidence_ids": list(self.evidence_ids),
            "waived": self.waived,
        }


@dataclass(frozen=True, slots=True)
class AcceptanceDoc:
    version: int
    run_id: str
    items: tuple[AcceptanceItem, ...]
    policy: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "policy": self.policy,
            "items": [item.to_dict() for item in self.items],
        }

    @property
    def evidence_pct(self) -> float:
        """Share of required items that pass **and** have valid exclusive binding.

        Unbound ``passes:true`` (legacy / forged reuse) does **not** inflate this.
        When no ledger is available (doc-only), falls back to passes-only share.
        """
        return self.honesty_pcts().get("evidence_pct", 0.0)

    @property
    def ac_bound_pct(self) -> float:
        """Share of required items with valid exclusive attested evidence binding."""
        return self.honesty_pcts().get("ac_bound_pct", 0.0)

    def honesty_pcts(self, run_root: Path | None = None) -> dict[str, float]:
        """Compute evidence_pct / ac_bound_pct / passes_pct.

        ``run_root`` defaults to None → passes-only fallback (no ledger to attest).
        Prefer ``honesty_pcts_for_run(run_root)`` / ``evidence_pct_for_run``.
        """
        if run_root is None:
            required = [i for i in self.items if i.required and not i.waived]
            if not required:
                return {"evidence_pct": 100.0, "ac_bound_pct": 100.0, "passes_pct": 100.0}
            passes = sum(1 for i in required if i.passes)
            boundish = sum(1 for i in required if i.passes and i.evidence_ids)
            n = len(required)
            return {
                "evidence_pct": 100.0 * boundish / n,
                "ac_bound_pct": 100.0 * boundish / n,
                "passes_pct": 100.0 * passes / n,
            }
        from harness.evidence.ledger import _validate_evidence_row  # noqa: PLC0415

        rows = list_evidence(run_root)
        by_id = {str(r.get("id")): r for r in rows if r.get("id")}
        return compute_honesty_pcts(
            self.items, by_id=by_id, validate_row=_validate_evidence_row
        )


# Agents must not edit passes; only control-plane flip/waive APIs may.
_DEFAULT_POLICY: dict[str, Any] = {
    "agent_may_edit": [],
    "forbid_delete_items": True,
    "forbid_edit_steps": True,
    "flip_requires_evidence_ids": True,
    "flip_via": "control_plane",
    "exclusive_evidence_bind": True,
    "forbid_mass_flip": True,
    "require_declared_binding": True,
}


def default_fail_items(
    acceptance_lines: list[str] | None = None,
    *,
    gate_ids: list[str] | None = None,
) -> list[AcceptanceItem]:
    """Build default-FAIL items from plan AC lines + optional gate sensors."""
    items: list[AcceptanceItem] = []
    lines = [ln.strip() for ln in (acceptance_lines or []) if ln.strip()]
    if not lines and not gate_ids:
        lines = ["Product smoke not yet specified (scaffold)"]
    for index, line in enumerate(lines, start=1):
        items.append(
            AcceptanceItem(
                id=f"ac-{index:03d}",
                category="functional",
                description=line,
                steps=(line,),
                required=True,
                passes=False,
            )
        )
    for gate_id in gate_ids or []:
        gid = gate_id.strip()
        if not gid:
            continue
        items.append(
            AcceptanceItem(
                id=f"ac-gate-{gid}",
                category="gate",
                description=f"Gate `{gid}` green (exit 0)",
                steps=(f"gate:{gid}",),
                required=True,
                passes=False,
            )
        )
    return items


def create_acceptance(
    path: Path,
    *,
    run_id: str,
    acceptance_lines: list[str] | None = None,
    gate_ids: list[str] | None = None,
) -> AcceptanceDoc:
    """Write acceptance.json with all passes:false (idempotent if exists)."""
    if path.is_file():
        return load_acceptance(path)
    doc = AcceptanceDoc(
        version=ACCEPTANCE_VERSION,
        run_id=run_id,
        items=tuple(default_fail_items(acceptance_lines, gate_ids=gate_ids)),
        policy=dict(_DEFAULT_POLICY),
    )
    save_acceptance(path, doc)
    return doc


def save_acceptance(path: Path, doc: AcceptanceDoc) -> None:
    """Persist acceptance.json and seal it (control-plane only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_acceptance_seal(path)


def load_acceptance(path: Path) -> AcceptanceDoc:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = tuple(
        AcceptanceItem(
            id=str(item["id"]),
            category=str(item.get("category") or "functional"),
            description=str(item.get("description") or ""),
            steps=tuple(str(s) for s in (item.get("steps") or [])),
            required=bool(item.get("required", True)),
            passes=bool(item.get("passes", False)),
            evidence_ids=tuple(str(e) for e in (item.get("evidence_ids") or [])),
            waived=bool(item.get("waived", False)),
        )
        for item in (raw.get("items") or [])
        if isinstance(item, dict)
    )
    return AcceptanceDoc(
        version=int(raw.get("version") or ACCEPTANCE_VERSION),
        run_id=str(raw.get("run_id") or ""),
        items=items,
        policy=dict(raw.get("policy") or _DEFAULT_POLICY),
    )


def evidence_pct_for_run(run_root: Path) -> float | None:
    path = run_root / "acceptance.json"
    if not path.is_file():
        return None
    return load_acceptance(path).honesty_pcts(run_root)["evidence_pct"]


def ac_bound_pct_for_run(run_root: Path) -> float | None:
    path = run_root / "acceptance.json"
    if not path.is_file():
        return None
    return load_acceptance(path).honesty_pcts(run_root)["ac_bound_pct"]


def honesty_pcts_for_run(run_root: Path) -> dict[str, float] | None:
    path = run_root / "acceptance.json"
    if not path.is_file():
        return None
    return load_acceptance(path).honesty_pcts(run_root)


def flip_item(
    path: Path,
    *,
    item_id: str,
    evidence_ids: list[str] | tuple[str, ...],
    run_root: Path | None = None,
) -> AcceptanceDoc:
    """Flip ``passes`` to true only when matching green **bound** evidence exists.

    Raises ``ValueError`` if evidence is missing/red, semantically unbound,
    reused across items, or item unknown. Never trusts agent self-report.
    """
    doc = load_acceptance(path)
    root = run_root or path.parent
    ids = tuple(str(e) for e in evidence_ids if str(e).strip())
    if not ids:
        raise ValueError("flip requires non-empty evidence_ids")

    found = get_evidence_by_ids(root, ids)
    missing = [eid for eid in ids if eid not in found]
    if missing:
        raise ValueError(f"evidence not in ledger: {','.join(missing)}")
    red = [eid for eid, row in found.items() if not evidence_row_is_green(row)]
    if red:
        raise ValueError(f"evidence not green: {','.join(red)}")
    for eid, row in found.items():
        ok, reason = verify_evidence_attestation(row)
        if not ok:
            raise ValueError(f"evidence attestation invalid ({eid}): {reason}")
        if not evidence_source_trusted(row):
            raise ValueError(f"evidence untrusted source ({eid}): {row.get('source')!r}")

    target = next((i for i in doc.items if i.id == item_id), None)
    if target is None:
        raise ValueError(f"unknown acceptance item: {item_id}")
    if target.waived:
        raise ValueError(f"item {item_id} is waived; cannot flip")

    validate_evidence_for_item(
        item_id=item_id,
        item_category=target.category,
        evidence_ids=ids,
        evidence_rows=found,
        other_items=doc.items,
    )

    updated: list[AcceptanceItem] = []
    for item in doc.items:
        if item.id != item_id:
            updated.append(item)
            continue
        merged = tuple(dict.fromkeys([*item.evidence_ids, *ids]))
        updated.append(replace(item, passes=True, evidence_ids=merged))

    new_doc = AcceptanceDoc(
        version=doc.version,
        run_id=doc.run_id,
        items=tuple(updated),
        policy=doc.policy,
    )
    save_acceptance(path, new_doc)
    _record_flip_audit(
        root,
        run_id=doc.run_id,
        item_id=item_id,
        evidence_ids=ids,
    )
    return new_doc


def _record_flip_audit(
    run_root: Path,
    *,
    run_id: str,
    item_id: str,
    evidence_ids: tuple[str, ...],
) -> None:
    """Append ledger audit row for acceptance flips (honesty trail)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from harness.evidence.ledger import (  # noqa: PLC0415
        EvidenceRecord,
        append_evidence,
        new_evidence_id,
    )

    append_evidence(
        run_root,
        EvidenceRecord(
            id=new_evidence_id("flip"),
            at=datetime.now(tz=UTC).isoformat(),
            run_id=run_id,
            task_id="",
            kind="acceptance_flip",
            exit_code=0,
            acceptance_item_ids=(item_id,),
            artifact_paths=tuple(evidence_ids),
            source="control_plane",
        ),
    )


def waive_item(
    path: Path,
    *,
    item_id: str,
    reason: str,
    run_root: Path | None = None,
) -> AcceptanceDoc:
    """Human waive: marks item waived (does not set passes via agent claim)."""
    from harness.evidence.ledger import (  # noqa: PLC0415
        EvidenceRecord,
        append_evidence,
        new_evidence_id,
    )
    from datetime import UTC, datetime  # noqa: PLC0415

    doc = load_acceptance(path)
    root = run_root or path.parent
    updated: list[AcceptanceItem] = []
    hit = False
    waive_id = new_evidence_id("waive")
    for item in doc.items:
        if item.id != item_id:
            updated.append(item)
            continue
        hit = True
        updated.append(
            replace(
                item,
                waived=True,
                passes=False,
                evidence_ids=tuple(dict.fromkeys([*item.evidence_ids, waive_id])),
            )
        )
    if not hit:
        raise ValueError(f"unknown acceptance item: {item_id}")

    append_evidence(
        root,
        EvidenceRecord(
            id=waive_id,
            at=datetime.now(tz=UTC).isoformat(),
            run_id=doc.run_id,
            task_id="",
            kind="manual_waive",
            exit_code=0,
            acceptance_item_ids=(item_id,),
            source=f"waive:{reason[:200]}",
        ),
    )
    new_doc = AcceptanceDoc(
        version=doc.version,
        run_id=doc.run_id,
        items=tuple(updated),
        policy=doc.policy,
    )
    save_acceptance(path, new_doc)
    return new_doc


def auto_flip_gate_items(
    path: Path,
    *,
    run_root: Path,
    gate_evidence: list[tuple[str, str]],
) -> list[str]:
    """Flip ``ac-gate-<id>`` items for green gate evidence.

    ``gate_evidence`` is a list of ``(gate_id, evidence_id)`` for exit_code==0 runs.
    Returns flipped item ids. Silent skip if item absent or already green.
    """
    if not path.is_file() or not gate_evidence:
        return []
    flipped: list[str] = []
    for gate_id, evidence_id in gate_evidence:
        item_id = f"ac-gate-{gate_id}"
        doc = load_acceptance(path)
        item = next((i for i in doc.items if i.id == item_id), None)
        if item is None or item.waived or item.passes:
            continue
        try:
            flip_item(path, item_id=item_id, evidence_ids=[evidence_id], run_root=run_root)
            flipped.append(item_id)
        except ValueError:
            continue
    return flipped


def pipeline_vs_evidence(
    *,
    pipeline_pct: float | None,
    evidence_pct: float | None,
    ac_bound_pct: float | None = None,
) -> dict[str, Any]:
    """Helper for status UX / YouTrack mapping."""
    return {
        "evidence_pct": evidence_pct,
        "ac_bound_pct": ac_bound_pct,
        "pipeline_pct": pipeline_pct,
        "primary": "evidence_pct",
        "secondary": "pipeline_pct",
        "formula": (
            "evidence_pct = required items with passes:true AND valid exclusive "
            "attested evidence bind; ac_bound_pct = share with valid bind"
        ),
    }
