"""Phase 0.5: skill_paths inject into prompts + catalog select_for_dispatch."""

from __future__ import annotations

from pathlib import Path

from harness.domain.enums import DispatchStatus, ModelTier, ResourceClass, Role
from harness.domain.models import Run, Task
from harness.skills.catalog import load_catalog, select_for_dispatch, select_skills
from harness.tasktool.models import DispatchEnvelope, TaskStage
from harness.tasktool.prompt_builder import build_prompt


def test_select_for_dispatch_worker() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = select_for_dispatch(root, kind="worker", stage="implement")
    assert 1 <= len(paths) <= 3
    assert all(p.endswith("SKILL.md") for p in paths)
    assert not any("grill-me" in p or "firecrawl" in p for p in paths)


def test_prompt_injects_skill_paths() -> None:
    run = Run(id="run-1", project="demo", goal="g", status="running")
    task = Task(id="001", run_id="run-1", title="T", spec_path="t.md", status="ready")
    skills = (
        ".cursor/skills/anti-hallucination/SKILL.md",
        ".cursor/skills/tdd-workflow/SKILL.md",
    )
    prompt = build_prompt(
        run,
        task,
        TaskStage.IMPLEMENT,
        spec_text="# Spec\n\nDo the thing.\n",
        kind="worker",
        worktree="/tmp/wt",
        files_owned=("src/**",),
        skill_paths=skills,
    )
    assert "Skills to apply" in prompt
    assert "anti-hallucination/SKILL.md" in prompt
    assert "## Injected skills" in prompt
    assert skills[0] in prompt


def test_envelope_skill_paths_roundtrip() -> None:
    now = "2026-08-05T12:00:00+00:00"
    envelope = DispatchEnvelope(
        run_id="run-1",
        dispatch_id="d-1",
        task_id="001",
        role=Role.WORKER,
        stage=TaskStage.IMPLEMENT,
        status=DispatchStatus.PENDING,
        prompt_path="prompts/d-1.md",
        result_path="results/d-1.json",
        model="cursor-grok-4.5-high",
        model_tier=ModelTier.STANDARD,
        repo="/repo",
        worktree="/wt",
        files_owned=("src/a.py",),
        read_only_context=(),
        frozen_contracts=("proto",),
        acceptance=("ok",),
        resource_class=ResourceClass.STANDARD,
        source_document_paths=("spec.md",),
        dependencies=(),
        created_at=now,
        updated_at=now,
        skill_paths=(".cursor/skills/anti-hallucination/SKILL.md",),
    )
    restored = DispatchEnvelope.from_json(envelope.to_json())
    assert restored.skill_paths == envelope.skill_paths
    payload = envelope.to_dict()
    del payload["skill_paths"]
    legacy = DispatchEnvelope.from_dict(payload)
    assert legacy.skill_paths == ()


def test_worker_core_pack_le_6_model() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = load_catalog(root / "skills" / "catalog.json")
    core = catalog.packs.get("worker_core") or []
    assert len(core) <= 6
    human = set(catalog.packs.get("human_only") or [])
    assert not (set(core) & human)
    paths = select_skills(catalog, role="worker", stage="implement")
    assert len(paths) <= catalog.max_inject
    assert len(paths) <= 3
