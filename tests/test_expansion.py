from __future__ import annotations

import json
from pathlib import Path

from harness.domain.enums import ModelTier, ResourceClass, Role
from harness.tasktool.expansion import (
    ExpansionArtifact,
    ExpansionStore,
    build_child_plan_tasks,
    child_task_id,
    new_artifact,
    parse_expansion,
    pattern_covered_by,
    validate_expansion,
)
from harness.tasktool.models import PlanTask, TaskStage


def _decision_text(payload: dict[str, object]) -> str:
    return "Analysis...\n\n```json\n" + json.dumps(payload) + "\n```\n"


def _valid_payload() -> dict[str, object]:
    return {
        "decompose": True,
        "reason": "independent api and model layers",
        "subtasks": [
            {
                "id": "models",
                "title": "Data models",
                "summary": "Create ORM models",
                "files_owned": ["src/feature/models/**"],
                "depends_on": [],
                "acceptance": ["pytest tests/test_models.py -q"],
                "complexity": "small",
            },
            {
                "id": "api",
                "title": "API layer",
                "summary": "Thin routers over the service",
                "files_owned": ["src/feature/api/**"],
                "depends_on": ["models"],
                "acceptance": ["pytest tests/test_api.py -q"],
                "complexity": "small",
            },
        ],
    }


def _parent(tmp_path: Path) -> PlanTask:
    return PlanTask(
        task_id="004",
        role=Role.WORKER,
        stage=TaskStage.IMPLEMENT,
        prompt_path=str(tmp_path / "p.md"),
        result_path=str(tmp_path / "r.json"),
        model="cursor-grok-4.5-high",
        model_tier=ModelTier.HEAVY,
        repo=str(tmp_path),
        worktree=str(tmp_path),
        files_owned=("src/feature/**",),
        read_only_context=(),
        frozen_contracts=("TaskTool protocol 3.0",),
        acceptance=("pytest -q",),
        resource_class=ResourceClass.HEAVY,
        source_document_paths=(str(tmp_path / "spec.md"),),
    )


def test_parse_expansion_valid_and_last_block_wins() -> None:
    text = (
        _decision_text({"decompose": False, "reason": "draft"})
        + "second thoughts\n"
        + _decision_text(_valid_payload())
    )
    decision = parse_expansion(text)
    assert decision is not None
    assert decision.decompose is True
    assert [item.child_id for item in decision.subtasks] == ["models", "api"]
    assert decision.subtasks[1].depends_on == ("models",)


def test_parse_expansion_decompose_false_and_malformed() -> None:
    direct = parse_expansion(_decision_text({"decompose": False, "reason": "small task"}))
    assert direct is not None
    assert direct.decompose is False
    assert direct.reason == "small task"

    assert parse_expansion("no json here") is None
    assert parse_expansion("```json\n{\"foo\": 1}\n```") is None
    assert parse_expansion("```json\n{broken\n```") is None
    # decompose=true without subtasks is not a valid decision either.
    assert parse_expansion(_decision_text({"decompose": True, "subtasks": []})) is None


def test_validate_expansion_accepts_valid_partition() -> None:
    decision = parse_expansion(_decision_text(_valid_payload()))
    assert decision is not None
    errors = validate_expansion(
        decision,
        parent_files_owned=("src/feature/**",),
        existing_task_ids=["001", "004"],
        parent_task_id="004",
    )
    assert errors == []


def test_validate_expansion_rejects_escape_overlap_cycle_and_large() -> None:
    payload = _valid_payload()
    subtasks = payload["subtasks"]
    assert isinstance(subtasks, list)
    first = dict(subtasks[0])
    second = dict(subtasks[1])
    first["files_owned"] = ["src/other/**"]  # escapes parent
    second["files_owned"] = ["src/feature/api/**"]
    second["depends_on"] = ["api"]  # self-dependency
    second["complexity"] = "large"  # recursion forbidden
    payload["subtasks"] = [first, second]
    decision = parse_expansion(_decision_text(payload))
    assert decision is not None

    errors = validate_expansion(
        decision,
        parent_files_owned=("src/feature/**",),
        existing_task_ids=[],
        parent_task_id="004",
    )
    joined = "\n".join(errors)
    assert "outside parent ownership" in joined
    assert "depends on itself" in joined
    assert "not allowed for children" in joined

    overlap = _valid_payload()
    overlap_subtasks = overlap["subtasks"]
    assert isinstance(overlap_subtasks, list)
    overlap_first = dict(overlap_subtasks[0])
    overlap_first["files_owned"] = ["src/feature/**"]  # covers sibling
    overlap["subtasks"] = [overlap_first, overlap_subtasks[1]]
    overlapping = parse_expansion(_decision_text(overlap))
    assert overlapping is not None
    overlap_errors = validate_expansion(
        overlapping,
        parent_files_owned=("src/feature/**",),
        existing_task_ids=[],
        parent_task_id="004",
    )
    assert any("overlap" in error for error in overlap_errors)


def test_validate_expansion_limits_children_count() -> None:
    payload = _valid_payload()
    template = payload["subtasks"]
    assert isinstance(template, list)
    payload["subtasks"] = [
        {
            "id": f"part{index}",
            "title": f"part {index}",
            "summary": "…",
            "files_owned": [f"src/feature/p{index}/**"],
            "depends_on": [],
            "acceptance": [],
            "complexity": "small",
        }
        for index in range(9)
    ]
    decision = parse_expansion(_decision_text(payload))
    assert decision is not None
    errors = validate_expansion(
        decision,
        parent_files_owned=("src/feature/**",),
        existing_task_ids=[],
        parent_task_id="004",
        max_children=8,
    )
    assert any("too many subtasks" in error for error in errors)


def test_pattern_covered_by() -> None:
    assert pattern_covered_by("src/api/users.py", "src/api/**")
    assert pattern_covered_by("src/api/v1/**", "src/api/**")
    assert pattern_covered_by("src/api", "src/api/**")
    assert pattern_covered_by("src/api/users.py", "src/api/users.py")
    assert not pattern_covered_by("src/other/x.py", "src/api/**")
    assert not pattern_covered_by("src/api2/x.py", "src/api/**")
    assert not pattern_covered_by("src/**", "src/api/**")


def test_build_children_freeze_specs_and_artifact_roundtrip(tmp_path: Path) -> None:
    decision = parse_expansion(_decision_text(_valid_payload()))
    assert decision is not None
    spool = tmp_path / "spool"

    children = build_child_plan_tasks(
        decision,
        parent=_parent(tmp_path),
        run_id="run-1",
        spool=spool,
        model_for_child=lambda _c: (
            "cursor-grok-4.5-high",
            ModelTier.STANDARD,
            ResourceClass.STANDARD,
        ),
    )

    assert [child.task_id for child in children] == ["004-models", "004-api"]
    assert children[1].dependencies == ("004-models",)
    for child in children:
        spec = Path(child.frozen_spec_path)
        assert spec.is_file()
        text = spec.read_text(encoding="utf-8")
        assert "Decompose: no" in text
        assert "# Файлы" in text
        assert child.spec_sha256

    artifact = new_artifact(
        run_id="run-1", parent_task_id="004", reason="split", children=children
    )
    store = ExpansionStore(spool)
    path, digest = store.write(artifact)
    assert path.is_file()
    assert digest
    loaded = store.read("004")
    assert isinstance(loaded, ExpansionArtifact)
    assert [child.task_id for child in loaded.children] == ["004-models", "004-api"]
    assert store.read_all()[0].parent_task_id == "004"
    assert child_task_id("004", "models") == "004-models"
