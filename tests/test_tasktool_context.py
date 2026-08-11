"""Tests for budgeted TaskTool ContextPack packing."""

from __future__ import annotations

from pathlib import Path

from harness.domain.enums import TaskStatus
from harness.domain.models import Run, Task
from harness.profile import ProjectProfile
from harness.store.repository import Store
from harness.tasktool.context import (
    DEFAULT_CHAR_BUDGET,
    build_context_pack,
    redact_secrets,
)


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def _run_task(
    store: Store,
    tmp_path: Path,
    *,
    depends_on: list[str] | None = None,
    provides: str = "",
    dep_provides: str = "public API v1",
) -> tuple[Run, Task]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    spec = repo / "task-002.md"
    spec.write_text(
        "# Acceptance criteria\n- `pytest -q`\n- contracts hold\n",
        encoding="utf-8",
    )
    run = Run("r1", "demo", "ship", "running")
    store.create_run(run)
    if depends_on:
        dep = Task(
            "001",
            "r1",
            "dep",
            str(repo / "task-001.md"),
            TaskStatus.DONE.value,
            provides=dep_provides,
        )
        (repo / "task-001.md").write_text("# Provides\n- x\n", encoding="utf-8")
        store.upsert_task(dep)
    task = Task(
        "002",
        "r1",
        "worker",
        str(spec),
        TaskStatus.READY.value,
        depends_on=depends_on or [],
        provides=provides,
    )
    store.upsert_task(task)
    return run, task


def test_budget_priority_truncates_lessons_before_acceptance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run, task = _run_task(store, tmp_path)
    brain = tmp_path / "brain"
    lessons = brain / "lessons" / "demo"
    lessons.mkdir(parents=True)
    for index in range(3):
        (lessons / f"task-00{index}-20260101-00000{index}.md").write_text(
            "LESSON " + ("noise " * 400),
            encoding="utf-8",
        )
    pack = build_context_pack(
        run=run,
        task=task,
        store=store,
        repo_root=tmp_path / "repo",
        brain_root=brain,
        profile=ProjectProfile(canon="python-backend"),
        char_budget=700,
        lesson_limit=3,
        pointer_only=False,
    )
    assert "pytest -q" in pack.rendered or "contracts hold" in pack.rendered
    assert "### Acceptance & dependency contracts" in pack.rendered
    assert len(pack.rendered) <= 700
    # Lessons are the first thing to go under pressure.
    assert pack.rendered.count("LESSON") <= 1


def test_dependency_provides_from_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run, task = _run_task(store, tmp_path, depends_on=["001"], dep_provides="ExactDepAPI")
    pack = build_context_pack(
        run=run,
        task=task,
        store=store,
        repo_root=tmp_path / "repo",
        profile=ProjectProfile(canon="python-backend"),
        pointer_only=False,
    )
    assert pack.dependency_provides == (("001", "ExactDepAPI"),)
    assert "depends:001 provides: ExactDepAPI" in pack.rendered


def test_secret_like_values_are_redacted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run, task = _run_task(
        store,
        tmp_path,
        depends_on=["001"],
        dep_provides="token=sk-abcdefghijklmnopqrstuvwxyz123456 feedback",
    )
    pack = build_context_pack(
        run=run,
        task=task,
        store=store,
        repo_root=tmp_path / "repo",
        feedback="Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        checkpoint_text="password=super-secret-value keep going",
        profile=ProjectProfile(canon="python-backend"),
        char_budget=DEFAULT_CHAR_BUDGET,
        pointer_only=False,
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in pack.rendered
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in pack.rendered
    assert "super-secret-value" not in pack.rendered
    assert "[REDACTED" in pack.rendered or "[REDACTED]" in redact_secrets(
        "api_key=sk-abcdefghijklmnopqrstuvwxyz123456"
    )


def test_section_caps_scale_with_the_pack_budget(tmp_path: Path) -> None:
    """One knob resizes every section, so a big budget is not eaten by old caps."""
    store = _store(tmp_path)
    long_provides = "DepAPI " + ("detail " * 600)
    run, task = _run_task(
        store, tmp_path, depends_on=["001"], dep_provides=long_provides
    )
    common = {
        "run": run,
        "task": task,
        "store": store,
        "repo_root": tmp_path / "repo",
        "profile": ProjectProfile(canon="python-backend"),
        "checkpoint_text": "checkpoint " * 800,
        "pointer_only": False,
    }
    small = build_context_pack(**common, char_budget=9_000)
    large = build_context_pack(**common, char_budget=60_000)

    assert len(large.dependency_provides[0][1]) > len(small.dependency_provides[0][1])
    assert len(large.checkpoint) > len(small.checkpoint)
    assert len(large.rendered) > len(small.rendered)
    assert len(large.rendered) <= 60_000


def test_default_budget_carries_a_full_task_worth_of_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run, task = _run_task(store, tmp_path, depends_on=["001"], dep_provides="DepAPI")
    pack = build_context_pack(
        run=run,
        task=task,
        store=store,
        repo_root=tmp_path / "repo",
        profile=ProjectProfile(canon="python-backend"),
        checkpoint_text="stage=gate; files=a.py,b.py",
        feedback="previous attempt failed on mypy",
        project_brief="demo (python/python-backend)",
        pointer_only=False,
    )
    assert DEFAULT_CHAR_BUDGET >= 30_000
    assert "previous attempt failed on mypy" in pack.rendered
    assert "stage=gate" in pack.rendered
    assert "DepAPI" in pack.rendered


def test_redact_secrets_helper() -> None:
    text = redact_secrets("api_key=sk-abcdefghijklmnopqrstuvwxyz123456\nsafe line")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "safe line" in text
