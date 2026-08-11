"""Загрузка PLAN.md + tasks/*.md в store как новый Run с графом зависимостей.

v2-005: idempotent ingest — повторный вызов с тем же (project, goal, base_branch)
возвращает существующий не-терминальный Run вместо создания нового. `force=True`
принудительно создаёт новый (пользователь явно перезапускает).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType, TaskStatus
from harness.domain.models import Run, Task
from harness.store.repository import Store
from harness.tasks_io.parser import parse_plan_dependencies, parse_task_file, resolve_plan_root


def new_run_id() -> str:
    # v2-005: +uuid-суффикс — иначе коллизия в пределах секунды (параллельные ingest'ы
    # или быстрые тесты) даёт UNIQUE constraint failed.
    return datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _goal_hash(project: str, goal: str, base_branch: str) -> str:
    return hashlib.sha256(f"{project}|{goal}|{base_branch}".encode()).hexdigest()[:16]


def ingest_run(
    settings: Settings,
    store: Store,
    *,
    project: str,
    goal: str,
    base_branch: str | None = None,
    force: bool = False,
    plan_root: Path | None = None,
    run_id: str | None = None,
) -> str:
    """Создаёт Run и задачи из файлов tasks/ с зависимостями из PLAN.md.

    При `force=False` (по умолчанию): если уже есть активный Run с тем же
    `(project, goal, base_branch)` — возвращает его id и пишет `RUN_REUSED` event,
    не дублируя задачи. Если существующий Run терминальный (`done`/`failed`) или
    `force=True` — создаёт новый.

    Args:
        plan_root: каталог с PLAN.md + tasks/ (по умолчанию settings.root).
                   Для multi-project это repo_root целевого проекта.
                   Pass a concrete ``.harness/runs/<id>/`` snapshot to avoid the
                   racy ``latest`` symlink under concurrent multi-tenant starts.
        run_id: optional pre-allocated id (must match the snapshot run root).
    """
    base = base_branch or settings.base_branch
    ghash = _goal_hash(project, goal, base)

    if not force:
        existing = store.find_active_run_by_goal_hash(ghash)
        if existing is not None:
            store.add_event(
                existing.id, EventType.RUN_REUSED,
                detail={"goal": goal, "reused_from": existing.id},
            )
            return existing.id

    run_id = run_id or new_run_id()
    store.create_run(Run(
        id=run_id, project=project, goal=goal,
        status="plan_draft",  # v2-023: ждёт подтверждения плана перед запуском
        base_branch=base, budget_credits=settings.run_budget, goal_hash=ghash,
    ))

    # v2-036: plan/tasks обычно лежат не в settings.root напрямую, а в
    # .harness/runs/latest/ (см. `_save_plan_to_project`) — resolve_plan_root
    # находит актуальный каталог, иначе ingest молча брал бы 0 файлов из
    # несуществующего/пустого settings.tasks_dir и создавал Run без задач.
    # v2-037: для multi-project ищем план в целевом репо, а не в доме harness.
    # Concrete per-run surfaces short-circuit inside resolve_plan_root.
    plan_root = resolve_plan_root(plan_root or settings.root)
    deps = parse_plan_dependencies(plan_root / "PLAN.md")
    task_files = sorted((plan_root / "tasks").glob("task-*.md"))
    for path in task_files:
        parsed = parse_task_file(path)
        # PLAN table wins when present; task-body "Depends on:" fills gaps.
        task_deps = list(deps.get(parsed.id) or parsed.depends_on or [])
        if parsed.status == "done":
            status = TaskStatus.DONE
        elif parsed.status == "blocked":
            status = TaskStatus.BLOCKED
        elif task_deps:
            status = TaskStatus.PENDING   # ждёт незавершённых зависимостей
        else:
            status = TaskStatus.READY     # зависимостей нет — можно брать сразу
        store.upsert_task(Task(
            id=parsed.id, run_id=run_id, title=parsed.title, spec_path=str(path),
            status=status.value, depends_on=task_deps,
            provides=parsed.provides, complexity=parsed.complexity, attempts=parsed.attempts,
        ))
        store.add_event(run_id, EventType.TASK_CREATED, task_id=parsed.id,
                        detail={"title": parsed.title, "deps": task_deps})
    return run_id
