"""Phase 0 doctor: false-coverage / soft-gates / multi-tenant."""

from __future__ import annotations

from pathlib import Path

from harness.doctor.diagnose import (
    build_false_coverage_report,
    inventory_soft_gates,
    render_false_coverage_markdown,
    render_soft_gates_markdown,
)
from harness.interface import cli as cli_module
from harness.profile import GateSpec
from harness.store.repository import Store


def test_soft_gate_detects_optional_and_exit0(tmp_path: Path) -> None:
    toml = tmp_path / ".harness" / "project.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(
        'language = "php"\n\n'
        '[[gates]]\n'
        'id = "phpunit-optional"\n'
        'cmd = "if true; then ./vendor/bin/phpunit || { echo fail; exit 0; }; fi"\n',
        encoding="utf-8",
    )
    findings = inventory_soft_gates([toml], also_scaffold_profiles=False)
    assert any(f.gate_id == "phpunit-optional" for f in findings)
    assert any(f.severity == "critical" for f in findings)
    md = render_soft_gates_markdown(findings)
    assert "phpunit-optional" in md


def test_soft_gate_blocking_clean(tmp_path: Path) -> None:
    toml = tmp_path / ".harness" / "project.toml"
    toml.parent.mkdir(parents=True)
    toml.write_text(
        'language = "python"\n\n[[gates]]\nid = "test"\ncmd = "pytest -q"\n',
        encoding="utf-8",
    )
    findings = inventory_soft_gates([toml], also_scaffold_profiles=False)
    assert findings == []


def test_false_coverage_stub_evidence(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    from harness.domain.models import Run, Task  # noqa: PLC0415

    store.create_run(
        Run(id="run-fc-1", project="demo", goal="test false coverage", status="done")
    )
    store.upsert_task(
        Task(
            id="001",
            run_id="run-fc-1",
            title="t1",
            spec_path="tasks/task-001.md",
            status="done",
            complexity="normal",
        )
    )
    report = build_false_coverage_report(store, harness_root=tmp_path, limit=5)
    assert report.evidence_implemented is True
    assert len(report.rows) == 1
    assert report.rows[0].pipeline_pct == 100.0
    assert report.rows[0].evidence_pct is None
    md = render_false_coverage_markdown(report)
    assert "pipeline%" in md
    assert "evidence%" in md


def test_false_coverage_reads_acceptance(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    from harness.domain.models import Run, Task  # noqa: PLC0415
    from harness.evidence.acceptance import create_acceptance  # noqa: PLC0415

    store.create_run(
        Run(id="run-fc-2", project="demo", goal="with evidence", status="done")
    )
    store.upsert_task(
        Task(
            id="001",
            run_id="run-fc-2",
            title="t1",
            spec_path="tasks/task-001.md",
            status="done",
            complexity="normal",
        )
    )
    run_root = tmp_path / ".harness" / "runs" / "run-fc-2"
    run_root.mkdir(parents=True)
    create_acceptance(run_root / "acceptance.json", run_id="run-fc-2", acceptance_lines=["x"])
    report = build_false_coverage_report(store, harness_root=tmp_path, limit=5)
    row = next(r for r in report.rows if r.run_id == "run-fc-2")
    assert row.evidence_pct == 0.0
    assert report.evidence_implemented is True


def test_doctor_false_coverage_subparser() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(["doctor", "--false-coverage-report", "--limit", "3"])
    assert args.false_coverage_report is True
    assert args.limit == 3


def test_classify_via_gate_spec_only() -> None:
    from harness.doctor.diagnose import _classify_soft_gate  # noqa: PLC0415

    hit = _classify_soft_gate(
        GateSpec("php-syntax", "php -l file.php"),
        source="x",
    )
    assert hit is None


def test_false_coverage_finds_acceptance_in_the_target_repo(tmp_path: Path) -> None:
    """The spool lives in the target repo, not in the control-plane checkout.

    Regression: resolving run roots only against ``harness_root`` left evidence%
    empty for every real run, hiding the exact gap (pipeline 100% vs low
    evidence%) that this report exists to surface.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from harness.domain.enums import (  # noqa: PLC0415
        DispatchStatus,
        ModelTier,
        ResourceClass,
        Role,
    )
    from harness.domain.models import Run, Task  # noqa: PLC0415
    from harness.evidence.acceptance import create_acceptance  # noqa: PLC0415
    from harness.tasktool.models import DispatchEnvelope, TaskStage  # noqa: PLC0415

    control_plane = tmp_path / "harness-checkout"
    target_repo = tmp_path / "target-repo"
    control_plane.mkdir()
    store = Store(control_plane / "state.db")
    store.create_run(Run(id="run-fc-3", project="demo", goal="g", status="done"))
    store.upsert_task(
        Task(
            id="001",
            run_id="run-fc-3",
            title="t1",
            spec_path="tasks/task-001.md",
            status="done",
            complexity="normal",
        )
    )
    stamp = datetime.now(tz=UTC).isoformat()
    store.create_dispatch(
        DispatchEnvelope(
            run_id="run-fc-3",
            dispatch_id="run-fc-3-001-worker-1",
            task_id="001",
            role=Role.WORKER,
            stage=TaskStage.IMPLEMENT,
            status=DispatchStatus.SUCCEEDED,
            prompt_path="p.md",
            result_path="r.json",
            model="cursor-grok-4.5-high",
            model_tier=ModelTier.STANDARD,
            repo=str(target_repo),
            worktree=str(target_repo),
            files_owned=(),
            read_only_context=(),
            frozen_contracts=(),
            acceptance=(),
            resource_class=ResourceClass.STANDARD,
            source_document_paths=(),
            dependencies=(),
            created_at=stamp,
            updated_at=stamp,
        )
    )
    run_root = target_repo / ".harness" / "runs" / "run-fc-3"
    run_root.mkdir(parents=True)
    create_acceptance(run_root / "acceptance.json", run_id="run-fc-3", acceptance_lines=["x"])

    report = build_false_coverage_report(store, harness_root=control_plane, limit=5)

    row = next(r for r in report.rows if r.run_id == "run-fc-3")
    # Pipeline says 100% done; evidence says nothing is actually proven.
    assert row.pipeline_pct == 100.0
    assert row.evidence_pct == 0.0
    assert str(run_root) in row.evidence_note
