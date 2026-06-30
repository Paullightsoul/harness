"""Загрузка PLAN.md + tasks/*.md в store как новый Run с графом зависимостей."""

from __future__ import annotations

from datetime import UTC, datetime

from harness.config import Settings
from harness.domain.enums import EventType, TaskStatus
from harness.domain.models import Run, Task
from harness.store.repository import Store
from harness.tasks_io.parser import parse_plan_dependencies, parse_task_file


def new_run_id() -> str:
    return datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")


def ingest_run(
    settings: Settings, store: Store, *, project: str, goal: str, base_branch: str | None = None
) -> str:
    """Создаёт Run и задачи из файлов tasks/ с зависимостями из PLAN.md."""
    run_id = new_run_id()
    store.create_run(Run(
        id=run_id, project=project, goal=goal, status="planning",
        base_branch=base_branch or settings.base_branch, budget_credits=settings.run_budget,
    ))

    deps = parse_plan_dependencies(settings.root / "PLAN.md")
    task_files = sorted(settings.tasks_dir.glob("task-*.md"))
    for path in task_files:
        parsed = parse_task_file(path)
        status = TaskStatus.DONE if parsed.status == "done" else TaskStatus.PENDING
        store.upsert_task(Task(
            id=parsed.id, run_id=run_id, title=parsed.title, spec_path=str(path),
            status=status.value, depends_on=deps.get(parsed.id, []),
            provides=parsed.provides, complexity=parsed.complexity, attempts=parsed.attempts,
        ))
        store.add_event(run_id, EventType.TASK_CREATED, task_id=parsed.id,
                        detail={"title": parsed.title, "deps": deps.get(parsed.id, [])})
    return run_id
