"""Semantic AC↔evidence binding — mass-flip ban, exclusive bind, DONE gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.evidence.acceptance import (
    create_acceptance,
    flip_item,
    load_acceptance,
)
from harness.evidence.ledger import (
    EvidenceRecord,
    append_evidence,
    done_allowed,
    new_evidence_id,
    record_gate_run,
)


def _mint(
    tmp_path: Path,
    *,
    run_id: str,
    item_id: str,
    eid: str | None = None,
    kind: str = "manual_check",
    gate_id: str = "",
) -> str:
    evidence_id = eid or new_evidence_id()
    append_evidence(
        tmp_path,
        EvidenceRecord(
            id=evidence_id,
            at="2026-08-06T12:00:00+00:00",
            run_id=run_id,
            task_id="001",
            kind=kind,
            gate_id=gate_id,
            exit_code=0,
            acceptance_item_ids=(item_id,),
            source="control_plane",
        ),
    )
    return evidence_id


def test_cannot_green_ten_acs_with_one_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    monkeypatch.delenv("HARNESS_ALLOW_MASS_FLIP", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_EVIDENCE_REUSE", raising=False)
    path = tmp_path / "acceptance.json"
    lines = [f"AC line {i}" for i in range(1, 11)]
    create_acceptance(path, run_id="r1", acceptance_lines=lines)
    # One gate blob — historically used to mass-flip all functional ACs.
    gate = record_gate_run(
        tmp_path,
        run_id="r1",
        task_id="001",
        gate_id="test",
        exit_code=0,
    )
    # Gate evidence declares only ac-gate-test — cannot flip functional ac-001.
    with pytest.raises(ValueError, match="evidence_not_declared_for_item|evidence_undeclared"):
        flip_item(path, item_id="ac-001", evidence_ids=[gate.id], run_root=tmp_path)

    # Even with forged declaration on a single row targeting one AC, reuse on #2 fails.
    shared = _mint(tmp_path, run_id="r1", item_id="ac-001", eid="ev-shared-only")
    flip_item(path, item_id="ac-001", evidence_ids=[shared], run_root=tmp_path)
    with pytest.raises(ValueError, match="evidence_reuse_forbidden"):
        flip_item(path, item_id="ac-002", evidence_ids=[shared], run_root=tmp_path)

    doc = load_acceptance(path)
    assert sum(1 for i in doc.items if i.passes) == 1
    ok, reason = done_allowed(tmp_path)
    assert ok is False
    assert "acceptance_red" in reason or "acceptance_unbound" in reason


def test_mass_flip_refused_same_gate_blob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(
        path,
        run_id="r1",
        acceptance_lines=[f"f{i}" for i in range(5)],
        gate_ids=["suite"],
    )
    rec = record_gate_run(
        tmp_path, run_id="r1", task_id="001", gate_id="suite", exit_code=0
    )
    # Matching gate AC is OK.
    flip_item(path, item_id="ac-gate-suite", evidence_ids=[rec.id], run_root=tmp_path)
    # Functional items must not ride the same gate evidence.
    for item_id in ("ac-001", "ac-002", "ac-003"):
        with pytest.raises(ValueError, match="evidence_not_declared|evidence_undeclared|evidence_reuse"):
            flip_item(path, item_id=item_id, evidence_ids=[rec.id], run_root=tmp_path)


def test_binding_required_for_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", acceptance_lines=["A", "B"], gate_ids=[])
    e1 = _mint(tmp_path, run_id="r1", item_id="ac-001")
    e2 = _mint(tmp_path, run_id="r1", item_id="ac-002")
    flip_item(path, item_id="ac-001", evidence_ids=[e1], run_root=tmp_path)
    flip_item(path, item_id="ac-002", evidence_ids=[e2], run_root=tmp_path)
    ok, reason = done_allowed(tmp_path)
    assert ok is True, reason
    assert reason == "ok"

    # Forge shared evidence on disk + refresh seal via flip path would be blocked;
    # simulate shared ownership by rewriting acceptance through control-plane save
    # after hand-edit would break seal — instead corrupt via exclusive check:
    from harness.evidence.acceptance import save_acceptance  # noqa: PLC0415
    from harness.evidence.acceptance import AcceptanceDoc, AcceptanceItem  # noqa: PLC0415

    doc = load_acceptance(path)
    shared_items = []
    for item in doc.items:
        if item.id in {"ac-001", "ac-002"}:
            shared_items.append(
                AcceptanceItem(
                    id=item.id,
                    category=item.category,
                    description=item.description,
                    steps=item.steps,
                    required=True,
                    passes=True,
                    evidence_ids=(e1,),
                )
            )
        else:
            shared_items.append(item)
    save_acceptance(
        path,
        AcceptanceDoc(
            version=doc.version,
            run_id=doc.run_id,
            items=tuple(shared_items),
            policy=doc.policy,
        ),
    )
    ok2, reason2 = done_allowed(tmp_path)
    assert ok2 is False
    assert "evidence_shared" in reason2 or "acceptance_unbound" in reason2


def test_evidence_pct_ignores_unbound_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", acceptance_lines=["A", "B"])
    e1 = _mint(tmp_path, run_id="r1", item_id="ac-001")
    flip_item(path, item_id="ac-001", evidence_ids=[e1], run_root=tmp_path)
    # Hand-edit second item to passes:true with reused evidence (seal rewritten
    # via save_acceptance to isolate % formula from seal check).
    from harness.evidence.acceptance import (  # noqa: PLC0415
        AcceptanceDoc,
        AcceptanceItem,
        save_acceptance,
    )

    doc = load_acceptance(path)
    forged = []
    for item in doc.items:
        if item.id == "ac-002":
            forged.append(
                AcceptanceItem(
                    id=item.id,
                    category=item.category,
                    description=item.description,
                    steps=item.steps,
                    required=True,
                    passes=True,
                    evidence_ids=(e1,),
                )
            )
        else:
            forged.append(item)
    save_acceptance(
        path,
        AcceptanceDoc(
            version=doc.version,
            run_id=doc.run_id,
            items=tuple(forged),
            policy=doc.policy,
        ),
    )
    honesty = load_acceptance(path).honesty_pcts(tmp_path)
    # passes_pct inflated to 100; evidence_pct only counts exclusive binds → 0 or 50.
    assert honesty["passes_pct"] == 100.0
    assert honesty["evidence_pct"] < 100.0
    # Shared evidence means neither item is exclusively bound.
    assert honesty["evidence_pct"] == 0.0
    assert honesty["ac_bound_pct"] == 0.0


def test_mass_flip_escape_hatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    monkeypatch.setenv("HARNESS_ALLOW_MASS_FLIP", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", acceptance_lines=["A", "B"])
    shared = EvidenceRecord(
        id="ev-mass",
        at="2026-08-06T12:00:00+00:00",
        run_id="r1",
        task_id="001",
        kind="manual_check",
        exit_code=0,
        acceptance_item_ids=("ac-001", "ac-002"),
        source="control_plane",
    )
    append_evidence(tmp_path, shared)
    flip_item(path, item_id="ac-001", evidence_ids=["ev-mass"], run_root=tmp_path)
    flip_item(path, item_id="ac-002", evidence_ids=["ev-mass"], run_root=tmp_path)
    doc = load_acceptance(path)
    assert all(i.passes for i in doc.items)


def test_gate_auto_flip_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    path = tmp_path / "acceptance.json"
    create_acceptance(path, run_id="r1", gate_ids=["php-syntax"], acceptance_lines=[])
    rec = record_gate_run(
        tmp_path, run_id="r1", task_id="001", gate_id="php-syntax", exit_code=0
    )
    from harness.evidence.acceptance import auto_flip_gate_items  # noqa: PLC0415

    flipped = auto_flip_gate_items(
        path, run_root=tmp_path, gate_evidence=[("php-syntax", rec.id)]
    )
    assert flipped == ["ac-gate-php-syntax"]
    ok, reason = done_allowed(tmp_path, task_id="001")
    assert ok is True, reason
