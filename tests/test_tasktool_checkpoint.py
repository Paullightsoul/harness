"""Tests for atomic TaskTool checkpoints."""

from __future__ import annotations

from pathlib import Path

from harness.tasktool.checkpoint import Checkpoint, CheckpointWriter


def test_atomic_checkpoint_roundtrip(tmp_path: Path) -> None:
    writer = CheckpointWriter(tmp_path / "repo")
    written = writer.write(
        "run-1",
        "001",
        stage="worker",
        status="gating",
        evidence_summary="HARNESS_DONE with targeted pytest",
        changed_files=["harness/tasktool/context.py", "tests/test_tasktool_context.py"],
        next_action="run local gates",
    )
    path = writer.path("run-1", "001")
    assert path.is_file()
    assert not path.with_name(".progress.md.tmp").exists()
    loaded = writer.read("run-1", "001")
    assert loaded is not None
    assert loaded == written
    assert loaded.stage == "worker"
    assert loaded.changed_files == (
        "harness/tasktool/context.py",
        "tests/test_tasktool_context.py",
    )
    summary = writer.summary_for_context("run-1", "001")
    assert "stage=worker" in summary
    assert "next=run local gates" in summary


def test_checkpoint_avoids_dumping_full_outputs(tmp_path: Path) -> None:
    writer = CheckpointWriter(tmp_path / "repo")
    huge = "word " * 5_000
    written = writer.write(
        "run-1",
        "001",
        stage="reviewer",
        status="merge_queue",
        evidence_summary=huge,
        changed_files=["a.py"],
        next_action="await merge approval",
    )
    assert len(written.evidence_summary) <= 800
    raw = writer.path("run-1", "001").read_text(encoding="utf-8")
    assert len(raw) < 2_500


def test_checkpoint_from_markdown_is_stable() -> None:
    text = Checkpoint(
        stage="gate",
        status="review",
        evidence_summary="ok",
        changed_files=("src/a.py",),
        next_action="reviewer",
        updated_at="2026-07-20T00:00:00+00:00",
    ).to_markdown()
    assert Checkpoint.from_markdown(text).changed_files == ("src/a.py",)
