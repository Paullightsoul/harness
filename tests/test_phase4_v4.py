"""Harness V4 Phase 4 — AHE-lite meta + skill GC."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.domain.models import Run, Task
from harness.meta.changelog import (
    ChangelogEntry,
    PredictedMetrics,
    append_entry,
    load_entries,
    new_entry_id,
    update_entry,
)
from harness.meta.cgroup_stub import STUB, cgroup_governor_available
from harness.meta.eval import evaluate_entries, evaluate_prediction, render_eval_markdown
from harness.meta.outcomes import RunOutcome, collect_run_outcome
from harness.skills.catalog import load_catalog, select_skills
from harness.skills.gc import apply_quarantine, propose_quarantine, restore_active
from harness.store.repository import Store


def _seed_run(store: Store, run_id: str = "run-meta-1") -> None:
    store.create_run(
        Run(
            id=run_id,
            project="zy",
            goal="phase4 meta",
            status="done",
            created_at="2026-08-05T12:00:00+00:00",
        )
    )
    store.upsert_task(
        Task(
            id="001",
            run_id=run_id,
            title="t",
            spec_path="tasks/task-001.md",
            status="done",
            complexity="small",
        )
    )
    attempt_id = store.start_attempt(run_id, "001", 1, "test")
    store.finish_attempt(
        attempt_id,
        run_id=run_id,
        task_id="001",
        worker_output="ok",
        gates_passed=True,
        verdict="approve",
        cost_credits=0.0,
        tokens_in=100,
        tokens_out=50,
    )
    store.add_event(run_id, "loop_stuck", task_id="001", detail={"reason": "tool hash"})
    store.add_event(run_id, "gate_result", task_id="001", detail={"passed": True})


def test_cgroup_stub_future_only() -> None:
    assert STUB is True
    assert cgroup_governor_available() is False


def test_changelog_append_and_load(tmp_path: Path) -> None:
    path = tmp_path / "ahe.jsonl"
    entry = ChangelogEntry(
        id=new_entry_id("enable evidence gate"),
        at="2026-08-05T10:00:00+00:00",
        change="enable evidence gate",
        predicted=PredictedMetrics(evidence_pct_min=40.0, stall_count_max=2),
        hypothesis="evidence% becomes meaningful",
    )
    append_entry(path, entry)
    loaded = load_entries(path)
    assert len(loaded) == 1
    assert loaded[0].change == "enable evidence gate"
    assert loaded[0].predicted.evidence_pct_min == 40.0


def test_evaluate_prediction_supported() -> None:
    predicted = PredictedMetrics(evidence_pct_min=50.0, stall_count_max=3)
    outcomes = [
        RunOutcome(
            run_id="r1",
            status="done",
            project="zy",
            created_at="2026-08-05T12:00:00+00:00",
            pipeline_pct=100.0,
            evidence_pct=60.0,
            tokens_est=1000,
            stall_count=1,
            pass_at_1=1.0,
            task_total=1,
            task_done=1,
        )
    ]
    checks = evaluate_prediction(predicted, outcomes)
    assert all(c.ok is True for c in checks)


def test_evaluate_prediction_contradicted_stalls() -> None:
    predicted = PredictedMetrics(stall_count_max=0)
    outcomes = [
        RunOutcome(
            run_id="r1",
            status="done",
            project="zy",
            created_at="",
            pipeline_pct=100.0,
            evidence_pct=80.0,
            tokens_est=None,
            stall_count=2,
            pass_at_1=None,
            task_total=1,
            task_done=1,
        )
    ]
    checks = evaluate_prediction(predicted, outcomes)
    assert any(c.metric == "stall_count_max" and c.ok is False for c in checks)


def test_collect_run_outcome_and_meta_eval(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = Store(db)
    _seed_run(store)
    # acceptance under harness run root — must be bound+attested for evidence% 100
    run_root = tmp_path / ".harness" / "runs" / "run-meta-1"
    run_root.mkdir(parents=True)
    from harness.evidence.acceptance import create_acceptance, flip_item
    from harness.evidence.ledger import EvidenceRecord, append_evidence

    create_acceptance(run_root / "acceptance.json", run_id="run-meta-1", acceptance_lines=["ok"])
    append_evidence(
        run_root,
        EvidenceRecord(
            id="ev-meta-1",
            at="2026-08-05T12:00:00+00:00",
            run_id="run-meta-1",
            task_id="001",
            kind="manual_check",
            exit_code=0,
            acceptance_item_ids=("ac-001",),
            source="control_plane",
        ),
    )
    flip_item(
        run_root / "acceptance.json",
        item_id="ac-001",
        evidence_ids=["ev-meta-1"],
        run_root=run_root,
    )
    outcome = collect_run_outcome(store, "run-meta-1", harness_root=tmp_path)
    assert outcome is not None
    assert outcome.stall_count == 1
    assert outcome.tokens_est == 150
    assert outcome.evidence_pct == 100.0

    changelog = tmp_path / "chg.jsonl"
    append_entry(
        changelog,
        ChangelogEntry(
            id="ahe-test-1",
            at="2026-08-05T11:00:00+00:00",
            change="honesty on",
            predicted=PredictedMetrics(evidence_pct_min=50.0, stall_count_max=5),
            verify_run_ids=["run-meta-1"],
        ),
    )
    report = evaluate_entries(
        store,
        harness_root=tmp_path,
        changelog_path=changelog,
        limit_recent=3,
    )
    assert report.entries[0].verdict == "supported"
    md = render_eval_markdown(report)
    assert "AHE-lite" in md
    assert "supported" in md


def test_meta_record_updates_entry(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = Store(db)
    _seed_run(store, "run-rec")
    path = tmp_path / "chg.jsonl"
    append_entry(
        path,
        ChangelogEntry(
            id="ahe-rec",
            at="2026-08-05T10:00:00+00:00",
            change="x",
            predicted=PredictedMetrics(stall_count_max=9),
        ),
    )
    outcome = collect_run_outcome(store, "run-rec", harness_root=tmp_path)
    assert outcome is not None
    updated = update_entry(
        path,
        "ahe-rec",
        actual=outcome.to_dict(),
        verify_run_ids=["run-rec"],
    )
    assert updated is not None
    assert updated.actual["run_id"] == "run-rec"
    assert updated.verify_run_ids == ["run-rec"]


def test_skill_gc_propose_and_approve(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-08-05T00:00:00Z",
                "policy": {"max_inject_per_dispatch": 3},
                "packs": {
                    "worker_core": ["keep-me"],
                    "human_only": ["human-skill"],
                },
                "skills": [
                    {
                        "id": "keep-me",
                        "path": ".cursor/skills/keep-me/SKILL.md",
                        "invocation": "model",
                        "roles": ["worker"],
                        "triggers": ["implement"],
                        "status": "active",
                    },
                    {
                        "id": "dusty",
                        "path": ".cursor/skills/dusty/SKILL.md",
                        "invocation": "model",
                        "roles": ["worker"],
                        "triggers": ["legacy"],
                        "status": "active",
                    },
                    {
                        "id": "human-skill",
                        "path": ".cursor/skills/human-skill/SKILL.md",
                        "invocation": "human",
                        "roles": ["human"],
                        "triggers": ["grill"],
                        "status": "active",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    store = Store(tmp_path / "state.db")
    catalog = load_catalog(catalog_path)
    proposal = propose_quarantine(catalog, store, catalog_path=catalog_path)
    assert "dusty" in proposal.quarantine_ids
    assert "keep-me" not in proposal.quarantine_ids
    assert "human-skill" not in proposal.quarantine_ids

    refused = apply_quarantine(catalog_path, ["dusty"], approve=False)
    assert refused["ok"] is False

    applied = apply_quarantine(catalog_path, ["dusty"], approve=True, note="phase4 gc")
    assert applied["ok"] is True
    assert "dusty" in applied["changed"]

    catalog2 = load_catalog(catalog_path)
    dusty = next(s for s in catalog2.skills if s.id == "dusty")
    assert dusty.status == "quarantined"
    # select skips quarantined
    paths = select_skills(catalog2, role="worker", stage="implement")
    assert all("dusty" not in p for p in paths)

    restored = restore_active(catalog_path, ["dusty"], approve=True)
    assert restored["ok"] is True
    catalog3 = load_catalog(catalog_path)
    assert next(s for s in catalog3.skills if s.id == "dusty").status == "active"


def test_cli_meta_and_gc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.interface import cli as cli_module

    root = tmp_path / "harness"
    root.mkdir()
    (root / ".harness").mkdir()
    monkeypatch.setenv("HARNESS_ROOT", str(root))
    monkeypatch.setenv("HARNESS_HOME", str(root))
    monkeypatch.chdir(root)

    # changelog add + list + eval via CLI
    rc = cli_module.main(
        [
            "meta",
            "changelog",
            "add",
            "--change",
            "pointer-only context",
            "--predict-evidence-min",
            "10",
            "--predict-stall-max",
            "5",
        ]
    )
    assert rc == 0
    ch_path = root / ".harness" / "meta" / "ahe-changelog.jsonl"
    assert ch_path.is_file()

    rc = cli_module.main(["meta", "changelog", "list"])
    assert rc == 0

    rc = cli_module.main(["meta", "eval", "--json"])
    assert rc == 0

    # skill gc propose against real catalog copy
    cat = root / "skills" / "catalog.json"
    cat.parent.mkdir(parents=True)
    cat.write_text(
        json.dumps(
            {
                "version": 1,
                "packs": {"worker_core": ["a"]},
                "skills": [
                    {
                        "id": "a",
                        "path": "a/SKILL.md",
                        "invocation": "model",
                        "roles": ["worker"],
                        "triggers": [],
                        "status": "active",
                    },
                    {
                        "id": "orphan",
                        "path": "orphan/SKILL.md",
                        "invocation": "model",
                        "roles": ["worker"],
                        "triggers": [],
                        "status": "active",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = cli_module.main(["skills", "gc", "propose", "--json"])
    assert rc == 0
    rc = cli_module.main(["skills", "gc", "apply", "--ids", "orphan"])
    assert rc == 1  # no --approve
    rc = cli_module.main(["skills", "gc", "apply", "--approve", "--ids", "orphan"])
    assert rc == 0
    data = json.loads(cat.read_text(encoding="utf-8"))
    assert next(s for s in data["skills"] if s["id"] == "orphan")["status"] == "quarantined"
