"""Semantic AC ↔ evidence binding (honesty fix 2026-08-06).

Prevents the news-bot failure mode: one green gate evidence blob flipping
many unrelated functional acceptance items.

Rules (defaults ON when ``HARNESS_EVIDENCE_GATE=1``):

1. **Exclusive bind** — each evidence_id may attach to at most one AC item
   (1:1). Escape: ``HARNESS_ALLOW_EVIDENCE_REUSE=1``.
2. **Declared map** — if a ledger row lists ``acceptance_item_ids``, flip is
   allowed only for those ids (gate rows auto-declare ``ac-gate-<gate_id>``).
3. **Mass flip ban** — reusing the same evidence across multiple functional
   items is refused. Escape: ``HARNESS_ALLOW_MASS_FLIP=1`` (also implies reuse).
"""

from __future__ import annotations

import os
from typing import Any


def exclusive_bind_enabled() -> bool:
    """1:1 evidence↔AC bind (default ON)."""
    if os.environ.get("HARNESS_ALLOW_EVIDENCE_REUSE", "0") == "1":
        return False
    if os.environ.get("HARNESS_ALLOW_MASS_FLIP", "0") == "1":
        return False
    return os.environ.get("HARNESS_EVIDENCE_EXCLUSIVE_BIND", "1") == "1"


def mass_flip_allowed() -> bool:
    """Emergency escape for batching many ACs onto one evidence blob."""
    return os.environ.get("HARNESS_ALLOW_MASS_FLIP", "0") == "1"


def require_declared_binding() -> bool:
    """Gate/sensor rows must declare the AC id they prove (default ON)."""
    if mass_flip_allowed():
        return False
    return os.environ.get("HARNESS_REQUIRE_DECLARED_BINDING", "1") == "1"


def nested_workers_required() -> bool:
    """Detect/fail cosplay when one agent reports all medium+ workers (default ON)."""
    return os.environ.get("HARNESS_REQUIRE_NESTED_WORKERS", "1") == "1"


def nested_workers_hard_fail() -> bool:
    """Refuse DONE/advance on cosplay when nested workers required (default ON).

    Escape hatch for emergencies: ``HARNESS_NESTED_WORKERS_HARD_FAIL=0`` (warn-only).
    """
    return os.environ.get("HARNESS_NESTED_WORKERS_HARD_FAIL", "1") == "1"


def evidence_ids_used_by_others(
    items: list[Any] | tuple[Any, ...],
    *,
    item_id: str,
    evidence_ids: list[str] | tuple[str, ...],
) -> list[str]:
    """Return evidence ids already bound to a different acceptance item."""
    wanted = {str(e) for e in evidence_ids if str(e).strip()}
    collisions: list[str] = []
    for item in items:
        other_id = str(getattr(item, "id", "") or "")
        if other_id == item_id:
            continue
        other_eids = getattr(item, "evidence_ids", ()) or ()
        for eid in other_eids:
            if str(eid) in wanted and str(eid) not in collisions:
                collisions.append(str(eid))
    return collisions


def validate_evidence_for_item(
    *,
    item_id: str,
    item_category: str,
    evidence_ids: list[str] | tuple[str, ...],
    evidence_rows: dict[str, dict[str, Any]],
    other_items: list[Any] | tuple[Any, ...] = (),
) -> None:
    """Raise ``ValueError`` when flip would violate semantic binding.

    Callers must already have verified green + attestation + trusted source.
    """
    ids = [str(e) for e in evidence_ids if str(e).strip()]
    if not ids:
        raise ValueError("flip requires non-empty evidence_ids")

    if exclusive_bind_enabled() and other_items:
        collisions = evidence_ids_used_by_others(
            other_items, item_id=item_id, evidence_ids=ids
        )
        if collisions:
            raise ValueError(
                "evidence_reuse_forbidden:"
                f"{','.join(collisions)} already bound to another acceptance item "
                "(set HARNESS_ALLOW_EVIDENCE_REUSE=1 to override)"
            )

    if require_declared_binding():
        for eid in ids:
            row = evidence_rows.get(eid) or {}
            declared = [
                str(x)
                for x in (row.get("acceptance_item_ids") or [])
                if str(x).strip()
            ]
            # Waive rows always target their item; empty declared on legacy
            # non-gate rows is refused for functional ACs.
            kind = str(row.get("kind") or "")
            if kind == "manual_waive":
                continue
            if declared:
                if item_id not in declared:
                    raise ValueError(
                        f"evidence_not_declared_for_item:{eid} "
                        f"declares={declared!r} item={item_id}"
                    )
                continue
            # Undeclared evidence: only allowed to flip matching gate AC when
            # gate_id convention matches, else refuse functional mass-bind.
            gate_id = str(row.get("gate_id") or "").strip()
            if item_category == "gate" and gate_id and item_id == f"ac-gate-{gate_id}":
                continue
            if item_category == "gate" and kind == "gate" and not declared:
                # Gate item without declaration — allow only ac-gate-<gate_id>.
                if gate_id and item_id == f"ac-gate-{gate_id}":
                    continue
            raise ValueError(
                f"evidence_undeclared_for_item:{eid} "
                f"(functional AC requires acceptance_item_ids including {item_id}; "
                "or use a dedicated sensor / waive)"
            )


def collect_evidence_ownership(
    items: list[Any] | tuple[Any, ...],
) -> dict[str, list[str]]:
    """Map evidence_id → list of acceptance item ids that reference it."""
    ownership: dict[str, list[str]] = {}
    for item in items:
        item_id = str(getattr(item, "id", "") or "")
        for eid in getattr(item, "evidence_ids", ()) or ():
            key = str(eid)
            ownership.setdefault(key, []).append(item_id)
    return ownership


def item_has_valid_exclusive_binding(
    item: Any,
    *,
    by_id: dict[str, dict[str, Any]],
    ownership: dict[str, list[str]],
    validate_row,  # Callable[[dict, str], tuple[bool, str]]
) -> tuple[bool, str]:
    """Whether an item's evidence_ids are attested, green, exclusive, and declared."""
    eids = tuple(getattr(item, "evidence_ids", ()) or ())
    if not eids:
        return False, "missing_evidence_ids"
    item_id = str(getattr(item, "id", "") or "")
    category = str(getattr(item, "category", "") or "functional")
    for eid in eids:
        row = by_id.get(str(eid))
        if row is None:
            return False, f"evidence_missing:{eid}"
        ok, reason = validate_row(row, str(eid))
        if not ok:
            return False, reason
        owners = ownership.get(str(eid), [])
        if exclusive_bind_enabled() and len(set(owners)) > 1:
            return False, f"evidence_shared:{eid}:owners={sorted(set(owners))}"
        try:
            validate_evidence_for_item(
                item_id=item_id,
                item_category=category,
                evidence_ids=[str(eid)],
                evidence_rows={str(eid): row},
                other_items=(),  # exclusivity already checked via ownership
            )
        except ValueError as exc:
            return False, str(exc)
    return True, "ok"


def compute_honesty_pcts(
    items: list[Any] | tuple[Any, ...],
    *,
    by_id: dict[str, dict[str, Any]],
    validate_row,
) -> dict[str, float]:
    """Return evidence_pct + ac_bound_pct for required non-waived items.

    ``evidence_pct`` — share that ``passes`` **and** have valid exclusive
    attested binding (unbound passes do not inflate %).

    ``ac_bound_pct`` — share with valid exclusive attested binding (passes
    optional); useful to see binding progress vs flip progress.
    """
    required = [
        i
        for i in items
        if bool(getattr(i, "required", True)) and not bool(getattr(i, "waived", False))
    ]
    if not required:
        return {"evidence_pct": 100.0, "ac_bound_pct": 100.0, "passes_pct": 100.0}

    ownership = collect_evidence_ownership(items)
    bound = 0
    bound_and_pass = 0
    passes_only = 0
    for item in required:
        ok, _ = item_has_valid_exclusive_binding(
            item, by_id=by_id, ownership=ownership, validate_row=validate_row
        )
        if ok:
            bound += 1
            if bool(getattr(item, "passes", False)):
                bound_and_pass += 1
        if bool(getattr(item, "passes", False)):
            passes_only += 1
    n = len(required)
    return {
        "evidence_pct": 100.0 * bound_and_pass / n,
        "ac_bound_pct": 100.0 * bound / n,
        "passes_pct": 100.0 * passes_only / n,
    }
