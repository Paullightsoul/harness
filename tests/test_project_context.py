from __future__ import annotations

import json
from pathlib import Path

from harness.tasktool.project_context import (
    collect_project_context,
    load_or_build,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads" / "main").write_text(
        "a" * 40 + "\n", encoding="utf-8"
    )
    (repo / "src" / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "README.md").write_text(
        "# Demo service\n\nAPI_KEY=super-secret-value\nHandles demo flows.\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["fastapi>=0.110", "structlog"]\n',
        encoding="utf-8",
    )
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.toml").write_text(
        'language = "python"\ncanon = "python-backend"\n\n'
        '[[gates]]\nid = "lint"\ncmd = "ruff check ."\n',
        encoding="utf-8",
    )
    return repo


def _brain(tmp_path: Path) -> Path:
    brain = tmp_path / "brain"
    (brain / "architecture").mkdir(parents=True)
    (brain / "architecture" / "demo.md").write_text(
        "# demo — architecture\n\nService layout and flows.\n", encoding="utf-8"
    )
    (brain / "decisions").mkdir()
    (brain / "decisions" / "ADR-0042-demo-cache.md").write_text(
        "# ADR-0042: cache strategy for demo\n\ndemo uses Redis.\n", encoding="utf-8"
    )
    (brain / "decisions" / "ADR-0001-unrelated.md").write_text(
        "# ADR-0001: something else entirely\n\nNothing here.\n", encoding="utf-8"
    )
    (brain / "incidents").mkdir()
    (brain / "incidents" / "2026-01-01-demo-outage.md").write_text(
        "# demo outage\n\nPostmortem.\n", encoding="utf-8"
    )
    (brain / "lessons" / "demo").mkdir(parents=True)
    (brain / "lessons" / "demo" / "task-001-20260101-000000.md").write_text(
        "# Lesson\n", encoding="utf-8"
    )
    return brain


def test_collect_gathers_layout_stack_and_brain(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brain = _brain(tmp_path)

    context = collect_project_context(repo, "demo", brain_root=brain)

    assert context.head_sha == "a" * 12
    assert context.language == "python"
    assert context.gates == ("lint: ruff check .",)
    assert "src/" in context.layout
    assert "src/app/" in context.layout
    assert any("fastapi" in line for line in context.manifests)
    assert "Demo service" in context.readme_head
    assert "super-secret-value" not in context.readme_head  # redacted
    assert "demo — architecture" in context.brain_architecture
    assert any("ADR-0042" in item for item in context.brain_adrs)
    assert not any("ADR-0001" in item for item in context.brain_adrs)
    assert any("demo-outage" in item for item in context.brain_incidents)
    assert context.lessons_count == 1

    markdown = context.to_markdown()
    assert "## Layout (top levels)" in markdown
    assert "## Brain (knowledge layer)" in markdown

    brief = context.brief(char_budget=700)
    assert len(brief) <= 700
    assert "demo (python/python-backend)" in brief
    assert "ADR-0042" in brief


def test_load_or_build_caches_by_head_and_refreshes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    brain = _brain(tmp_path)

    first, md_path = load_or_build(repo, "demo", brain_root=brain)
    assert md_path.is_file()
    cached, _ = load_or_build(repo, "demo", brain_root=brain)
    assert cached.generated_at == first.generated_at  # served from cache

    # New HEAD invalidates the cache.
    (repo / ".git" / "refs" / "heads" / "main").write_text(
        "b" * 40 + "\n", encoding="utf-8"
    )
    rebuilt, _ = load_or_build(repo, "demo", brain_root=brain)
    assert rebuilt.head_sha == "b" * 12
    assert rebuilt.generated_at != first.generated_at

    forced, _ = load_or_build(repo, "demo", brain_root=brain, refresh=True)
    assert forced.generated_at != rebuilt.generated_at

    payload = json.loads(
        (repo / ".harness" / "context" / "project-context.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["project"] == "demo"


def test_manifests_found_in_subprojects_not_just_root(tmp_path: Path) -> None:
    """A root scaffold stub used to hide the whole real stack of a monorepo."""
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "scaffold-stub"\ndependencies = []\n', encoding="utf-8"
    )
    (repo / "backend").mkdir()
    (repo / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "real-api"\ndependencies = ["fastapi>=0.110", "redis>=5"]\n',
        encoding="utf-8",
    )
    (repo / "frontend").mkdir()
    (repo / "frontend" / "package.json").write_text(
        '{"name": "web", "dependencies": {"react": "^19", "zustand": "^5"}}',
        encoding="utf-8",
    )

    manifests = collect_project_context(repo, "demo").manifests

    assert any("backend/pyproject.toml: real-api" in line for line in manifests)
    assert any("fastapi" in line for line in manifests)
    assert any("frontend/package.json: web" in line for line in manifests)
    assert any("react" in line for line in manifests)


def test_manifest_scan_skips_vendored_and_generated_dirs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for noisy in ("node_modules", ".venv"):
        (repo / noisy).mkdir()
        (repo / noisy / "package.json").write_text(
            '{"name": "vendored", "dependencies": {}}', encoding="utf-8"
        )

    manifests = collect_project_context(repo, "demo").manifests

    assert not any("vendored" in line for line in manifests)


def test_missing_brain_and_bare_repo_are_fine(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()

    context = collect_project_context(repo, "bare", brain_root=None)

    assert context.head_sha == ""
    assert context.brain_adrs == ()
    assert context.lessons_count == 0
    assert context.to_markdown().startswith("# Project context — bare")
