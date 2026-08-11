"""v2-036: ingest/verify должны находить план там, где его реально пишет
`harness plan` (`interface/cli.py::_save_plan_to_project`) — в
`.harness/runs/<run_id>/{PLAN.md,tasks/}` с симлинком `.../runs/latest` на
последний прогон, — а не только в `settings.root` напрямую.

До фикса: `ingest_run` смотрел строго в `settings.root/{PLAN.md,tasks/}`,
находил там пусто и создавал Run без единой задачи (`SELECT * FROM tasks`
возвращал []). Дополнительно `parse_plan_dependencies` понимал только
markdown-таблицу, а `_save_plan_to_project` пишет heading+bullet формат —
поэтому даже при правильном каталоге depends_on был бы всегда пуст.
"""
from __future__ import annotations

from pathlib import Path

from harness.config import Settings
from harness.domain.enums import TaskStatus
from harness.ingest import ingest_run
from harness.store.repository import Store
from harness.tasks_io.verifier import verify_plan


def _write_task(tasks_dir: Path, task_id: str, *, provides: str = "интерфейс X") -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / f"task-{task_id}.md").write_text(
        f'---\nid: "{task_id}"\ntitle: "задача {task_id}"\ncomplexity: small\n'
        f"attempts: 0\n---\n\n"
        f"# Контекст\nstub\n\n# Файлы\n- TBD\n\n"
        f"# Acceptance criteria\n- [ ] `pytest tests/test_x.py` зелёный\n\n"
        f"# Provides\n{provides}\n",
        encoding="utf-8",
    )


def _seed_plan_run(root: Path) -> Path:
    """Раскладка ровно как после `harness plan`: root/.harness/runs/<id>/
    {PLAN.md,tasks/} + симлинк .../latest -> <id>. PLAN.md — в heading-формате
    (реальный вывод `_save_plan_to_project`, НЕ markdown-таблица)."""
    run_dir = root / ".harness" / "runs" / "run-20260702-104335"
    tasks_dir = run_dir / "tasks"
    _write_task(tasks_dir, "001")
    _write_task(tasks_dir, "002")
    (run_dir / "PLAN.md").write_text(
        "# PLAN\n\n## Goal\nтестовая цель\n\n## Tasks\n\n"
        "### 001: база\n- complexity: small\n\n"
        "### 002: зависит от 001\n- complexity: small\n- depends_on: 001\n\n"
        "## Strategy\nSee individual task files in tasks/\n",
        encoding="utf-8",
    )
    latest = root / ".harness" / "runs" / "latest"
    latest.symlink_to(run_dir.name, target_is_directory=True)
    return run_dir


def test_ingest_run_populates_tasks_from_runs_latest(tmp_path: Path) -> None:
    _seed_plan_run(tmp_path)
    settings = Settings(root=tmp_path)
    store = Store(tmp_path / "state.db")

    run_id = ingest_run(settings, store, project="p", goal="тестовая цель")

    tasks = store.list_tasks(run_id)
    assert {t.id for t in tasks} == {"001", "002"}, "ingest не должен возвращать 0 задач"


def test_ingest_run_task_without_deps_is_ready(tmp_path: Path) -> None:
    _seed_plan_run(tmp_path)
    settings = Settings(root=tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="тестовая цель")

    t = store.get_task(run_id, "001")
    assert t is not None
    assert t.status == TaskStatus.READY.value
    assert t.depends_on == []
    assert t.provides == "интерфейс X"


def test_ingest_run_task_with_unmet_deps_is_pending(tmp_path: Path) -> None:
    _seed_plan_run(tmp_path)
    settings = Settings(root=tmp_path)
    store = Store(tmp_path / "state.db")
    run_id = ingest_run(settings, store, project="p", goal="тестовая цель")

    t = store.get_task(run_id, "002")
    assert t is not None
    assert t.status == TaskStatus.PENDING.value
    assert t.depends_on == ["001"]


def test_ingest_run_still_works_with_flat_root_layout(tmp_path: Path) -> None:
    """Backward-compat: без .harness/runs/ (dogfood-конвенция, root/PLAN.md
    + root/tasks/) поведение не меняется."""
    tasks = tmp_path / "tasks"
    _write_task(tasks, "001")
    (tmp_path / "PLAN.md").write_text("# plan stub\n", encoding="utf-8")
    settings = Settings(root=tmp_path)
    store = Store(tmp_path / "state.db")

    run_id = ingest_run(settings, store, project="p", goal="цель")

    tasks_out = store.list_tasks(run_id)
    assert {t.id for t in tasks_out} == {"001"}
    assert tasks_out[0].status == TaskStatus.READY.value


def test_verify_plan_reads_from_runs_latest(tmp_path: Path) -> None:
    _seed_plan_run(tmp_path)
    report = verify_plan(tmp_path)
    assert report.ok, [f.issue for f in report.errors]


def test_verify_plan_missing_everywhere_still_fails(tmp_path: Path) -> None:
    """Ни .harness/runs/latest/, ни плоского PLAN.md — ошибка как раньше."""
    report = verify_plan(tmp_path)
    assert not report.ok
    assert any("PLAN.md не найден" in f.issue for f in report.errors)
