"""Regression fixtures for ZY dogfood control-plane bugs (no live PLAN drift)."""

from __future__ import annotations

from pathlib import Path

from harness.policy.scope import parse_spec_files
from harness.scaffold import default_project_toml, detect_language
from harness.tasks_io.parser import parse_plan_dependencies

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "zy_dogfood"


def test_zy_plan_dependencies_survive_partition_table() -> None:
    """Historical ZY PLAN with partition / Depends tables must keep DAG edges."""
    plan = FIXTURES / "PLAN-partition-deps.md"
    deps = parse_plan_dependencies(plan)
    assert deps["002"] == ["001"]
    assert deps["004"] == ["002", "003"]
    assert deps["007"] == ["004", "005", "006"]


def test_zy_task_004_files_owned_from_english_table() -> None:
    """English Owned files table must yield product paths (not only report md)."""
    spec = FIXTURES / "task-004-english-owned.md"
    owned = parse_spec_files(spec.read_text(encoding="utf-8"))
    assert "backend/app/routes/api.php" in owned
    assert any("RegionController.php" in path for path in owned)


def test_php_scaffold_detects_composer_and_blocking_phpunit(tmp_path: Path) -> None:
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    assert detect_language(tmp_path) == "php"
    profile = default_project_toml("php")
    assert "php -l" in profile
    assert 'id = "phpunit"' in profile
    assert "vendor/bin/phpunit" in profile
    # Soft swallow must not be the scaffold default.
    assert "|| exit 0" not in profile
    assert "phpunit skipped" not in profile
    assert "ruff" not in profile
