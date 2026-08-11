"""CLI harness (zero-dep, argparse).

    harness plan "<goal>"     планирование: оркестратор пишет PLAN.md + tasks/*.md
    harness ingest "<goal>"   загрузить tasks/ в store как новый Run
    harness run [run_id]      исполнить Run (по умолчанию — последний)
    harness status [run_id]   статусы задач
    harness events [run_id]   журнал событий
    harness projects ...      реестр проектов на VPS
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import Role, RunnerKind
from harness.domain.models import Run
from harness.dotenv import load_dotenv
from harness.durable.config import DurableConfig
from harness.ingest import ingest_run
from harness.projects.registry import Project, ProjectRegistry
from harness.runner.factory import build_runner
from harness.scaffold import default_project_toml, detect_language, scaffold_project
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def _store(settings: Settings) -> Store:
    return Store(settings.db_path)


def _resolve_project(settings: Settings, name: str | None) -> Project | None:
    if not name:
        return None
    return ProjectRegistry(settings.root / "projects.json").get(name)


def _repo_root(settings: Settings, project: Project | None) -> Path:
    return Path(project.repo_path) if project else settings.root


def _latest_run_id(store: Store) -> str | None:
    return store.latest_run_id()


def _latest_run_id_for_project(store: Store, project_name: str) -> str | None:
    """Последний run для конкретного проекта (для multi-project status/events/tail)."""
    return store.latest_run_id(project=project_name)


_TASK_PREFIXES = ("001", "002", "003", "004", "005", "006", "007", "008", "009")


def _save_plan_to_project(plan_text: str, repo_root: Path, project_name: str) -> Path:
    """Сохранить план от оркестратора в целевой репо.

    V4 default (``HARNESS_USE_RUN_ROOTS=1``): write PLAN.md + tasks/ only under
    ``.harness/runs/<run_id>/`` so a second concurrent plan cannot overwrite the
    first run's surface. Shared repo-root PLAN.md is left untouched.

    Legacy (``HARNESS_USE_RUN_ROOTS=0``): also write flat ``repo_root/PLAN.md`` +
    ``tasks/`` (old dogfood convention).
    """
    from harness.ingest import new_run_id  # noqa: PLC0415
    from harness.tenant.run_layout import (  # noqa: PLC0415
        ensure_run_layout,
        point_latest_symlink,
        shared_plan_writes_enabled,
        use_run_roots,
        write_plan_surface,
    )

    staging_tasks = repo_root / "tasks"
    staging_tasks.mkdir(parents=True, exist_ok=True)

    # 1. Собираем задачи, уже созданные планировщиком в repo_root/tasks/.
    existing_tasks: list[dict[str, str]] = []
    task_bodies: dict[str, str] = {}
    for task_path in sorted(staging_tasks.glob("task-*.md")):
        try:
            from harness.tasks_io.parser import parse_task_file  # noqa: PLC0415
            parsed = parse_task_file(task_path)
            existing_tasks.append({
                "id": parsed.id,
                "title": parsed.title,
                "complexity": parsed.complexity,
                "depends_on": "",
            })
            task_bodies[task_path.name] = task_path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001 (best-effort чтение существующих задач)
            continue

    # 2. Если планировщик не создал файлы (SDK-раннер), парсим текст ответа.
    parsed_from_text: list[dict[str, str]] = _parse_tasks_from_plan_text(plan_text)

    # 3. Мержим: файлы на диске приоритетнее; из текста дополняем depends_on/complexity.
    tasks_found: dict[str, dict[str, str]] = {t["id"]: t for t in existing_tasks}
    for t in parsed_from_text:
        if t["id"] not in tasks_found:
            tasks_found[t["id"]] = t
        else:
            tasks_found[t["id"]].setdefault("depends_on", t.get("depends_on", ""))
            if not tasks_found[t["id"]]["complexity"]:
                tasks_found[t["id"]]["complexity"] = t.get("complexity", "small")
    tasks_list = sorted(tasks_found.values(), key=lambda x: x["id"])

    # 4. Зависимости из heading-формата или таблицы в ответе планировщика.
    deps = _extract_plan_dependencies(plan_text)
    for t in tasks_list:
        t["depends_on"] = deps.get(t["id"], "")

    # 5. Build PLAN.md body.
    goal_text = plan_text.split("##", maxsplit=1)[0].strip() if "##" in plan_text else plan_text[:500]  # noqa: E501
    plan_content = f"""# PLAN

## Goal
{goal_text}

## Tasks

"""
    for t in tasks_list:
        plan_content += f"### {t['id']}: {t['title']}\n"
        plan_content += f"- complexity: {t['complexity']}\n"
        if t["depends_on"]:
            plan_content += f"- depends_on: {t['depends_on']}\n"
        plan_content += "\n"

    plan_content += f"""## Strategy
See individual task files in tasks/

## Target Repository
{repo_root}
"""

    run_id = new_run_id()
    paths = ensure_run_layout(
        repo_root,
        run_id,
        project=project_name,
        goal=goal_text[:200],
        prefer_modern=use_run_roots(),
    )
    # Ensure task stubs exist for ids discovered only from plan text.
    for t in tasks_list:
        name = f"task-{t['id']}.md"
        if name not in task_bodies:
            task_bodies[name] = (
                f"---\nid: \"{t['id']}\"\ntitle: \"{t['title']}\"\n"
                f"status: \"todo\"\ncomplexity: \"{t['complexity']}\"\nattempts: 0\n---\n\n"
                f"# {t['title']}\n"
            )
    write_plan_surface(paths, plan_content, task_bodies)
    point_latest_symlink(repo_root, run_id)

    if shared_plan_writes_enabled():
        # Legacy flat SoT — only when run-roots are explicitly off.
        (repo_root / "PLAN.md").write_text(plan_content, encoding="utf-8")
        for name, body in task_bodies.items():
            (staging_tasks / name).write_text(body, encoding="utf-8")
        plan_path = repo_root / "PLAN.md"
        tasks_dir = staging_tasks
    else:
        plan_path = paths.plan_md
        tasks_dir = paths.tasks_dir

    print(f"\n📝 План сохранён: {plan_path}")
    print(f"📁 Задачи: {tasks_dir} ({len(tasks_list)} tasks)")
    if not shared_plan_writes_enabled():
        print(f"🔒 Run root (SoT): {paths.root} (shared PLAN.md not overwritten)")

    return paths.root if use_run_roots() else repo_root

def _parse_tasks_from_plan_text(plan_text: str) -> list[dict[str, str]]:  # noqa: PLR0912
    """Best-effort извлечение задач из markdown-ответа планировщика.

    Поддерживает heading-формат (### 001: title + - complexity: small) и таблицы
    с id в первой колонке (включая **001** и "Порядок"). Возвращает список
    словарей {id, title, complexity, depends_on}.
    """
    tasks: list[dict[str, str]] = []
    seen: set[str] = set()

    # Heading-формат.
    heading_re = re.compile(r"^#{2,4}\s*(\d{3})\s*[:.\)]\s*(.+?)$", re.MULTILINE)
    for match in heading_re.finditer(plan_text):
        task_id = match.group(1)
        if task_id in seen:
            continue
        seen.add(task_id)
        title = match.group(2).strip().rstrip("|").strip()
        # Ищем complexity/depends_on в следующих 10 строках.
        start = match.end()
        complexity = "small"
        depends_on = ""
        for line in plan_text[start:start + 1000].splitlines()[:10]:
            low = line.lower()
            if "complexity" in low:
                complexity = _extract_complexity(line)
            if "depends_on" in low or "зависит" in low:
                depends_on = _extract_depends(line)
        tasks.append({
            "id": task_id, "title": title, "complexity": complexity, "depends_on": depends_on,
        })

    # Таблица: первая колонка с id (001, **001**, #001).
    for line in plan_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.split("|") if c.strip()]
        if not cells:
            continue
        first = cells[0]
        # Заголовок?
        if first.lower() in {"id", "—", "-", "порядок", "order", "task", "задача"}:
            continue
        m = re.search(r"\b(\d{3})\b", first)
        if not m:
            continue
        task_id = m.group(1)
        if task_id in seen:
            continue
        seen.add(task_id)
        title = cells[1] if len(cells) > 1 else f"Task {task_id}"
        # complexity-колонка: ищем small/medium/large/trivial.
        complexity = "small"
        for cell in cells:
            low = cell.lower()
            if low in {"trivial", "small", "medium", "large"}:
                complexity = low
                break
        tasks.append({"id": task_id, "title": title, "complexity": complexity, "depends_on": ""})

    return tasks


def _extract_complexity(line: str) -> str:
    low = line.lower()
    for tier in ("trivial", "small", "medium", "large"):
        if tier in low:
            return tier
    return "small"


def _extract_depends(line: str) -> str:
    # "- depends_on: 001, 002" или "- Зависит от: 001"
    match = re.search(r"[:：]\s*(.+?)$", line)
    if not match:
        return ""
    raw = match.group(1).replace("—", "").replace("-", "").strip()
    ids = [re.search(r"\b(\d{3})\b", part) for part in raw.split(",")]
    return ", ".join([m.group(1) for m in ids if m])


def _extract_plan_dependencies(plan_text: str) -> dict[str, str]:
    """Извлекает зависимости из heading-формата ответа планировщика."""
    deps: dict[str, str] = {}
    current: str | None = None
    for line in plan_text.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{2,4}\s*(\d{3})\s*[:.\)]", stripped)
        if heading:
            current = heading.group(1)
            deps.setdefault(current, "")
            continue
        if current is None:
            continue
        low = stripped.lower()
        if "depends_on" in low or "зависит" in low:
            deps[current] = _extract_depends(stripped)
    return deps


_LEGACY_NOTICE_SHOWN: set[str] = set()


def _legacy_warn(command: str, reason: str) -> None:
    """Make use of a compatibility path visible on the way past it.

    README/ARCHITECTURE call the push Engine and the file-spool bridge
    "deprecated compatibility", but nothing said so at the point of use, so there
    was no signal about whether anyone still relies on them. Emitted once per
    process, to stderr, so JSON on stdout stays parseable.
    """
    if command in _LEGACY_NOTICE_SHOWN:
        return
    _LEGACY_NOTICE_SHOWN.add(command)
    print(
        f"⚠️  `harness {command}` is a compatibility path, not the TaskTool-first "
        f"flow: {reason}. See README § Legacy / compatibility.",
        file=sys.stderr,
    )


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    _legacy_warn("plan", "planning happens in the root chat, then `harness verify`")
    role = settings.role(Role.ORCHESTRATOR)
    runner = build_runner(role.runner, role=Role.ORCHESTRATOR)
    role_prompt = (settings.prompts_dir / "orchestrator.md").read_text(encoding="utf-8")
    template = (settings.tasks_dir / "_TEMPLATE.md").read_text(encoding="utf-8")
    # v2-002: оркестратор запускается в CWD целевого репо (--project), не в доме
    # harness — иначе он не имеет кода в CWD и план галлюцинирует по absolute path.
    project = _resolve_project(settings, getattr(args, "project", None))
    repo_root = _repo_root(settings, project)
    repo_block = f"\n=== REPO ROOT (твой CWD) ===\n{repo_root}\nИзучай репо отсюда.\n"
    # v2-025: multi-run memory — lessons из прошлых прогонов этого проекта.
    lessons_block = ""
    project_name = project.name if project else "default"
    try:
        from harness.lessons.extractor import (  # noqa: PLC0415
            format_lessons_for_plan,
            list_recent_lessons,
        )
        from harness.tasktool.context import resolve_brain_root  # noqa: PLC0415

        brain_root = resolve_brain_root() or Path(settings.brain_root)
        lessons_block = (
            format_lessons_for_plan(list_recent_lessons(brain_root, project_name))
            if brain_root.exists()
            else ""
        )
    except Exception:  # noqa: BLE101 (lessons не должны валить plan)
        pass
    prompt = (
        f"{role_prompt}\n\n=== GOAL ===\n{args.goal}\n"
        f"{repo_block}\n"
        f"{lessons_block}\n"
        f"=== ШАБЛОН ЗАДАЧИ (соблюдай строго) ===\n{template}\n"
    )
    # Heartbeat/progress для SDK-раннера
    verbose = getattr(args, "verbose", False)
    progress_dots: list[str] = []

    def _progress(status: str) -> None:
        if not verbose:
            return
        if status == "started":
            print(f"🚀 Планирование начато (модель: {role.model})...", file=sys.stderr)
        elif status.startswith("thinking"):
            progress_dots.append(".")
            if len(progress_dots) % 10 == 0:
                print(f"  всё ещё думает{''.join(progress_dots)}", file=sys.stderr)
            else:
                print(".", end="", file=sys.stderr, flush=True)
        elif status == "waiting":
            print("⌛ Финализирует ответ...", file=sys.stderr)
        elif status == "done":
            print("✓ Готово!", file=sys.stderr)

    print(f"🚀 Запуск оркестратора (модель: {role.model}, runner: {role.runner.value})...")
    res = asyncio.run(runner.run(
        prompt, model=role.model, cwd=repo_root,
        log_path=settings.root / "logs" / "orchestrator.log",
        progress_callback=_progress if verbose else None,
    ))
    print(res.text)
    if not res.ok:
        print(f"планирование завершилось с ошибкой: {res.error}", file=sys.stderr)
        return 1
    # Сохраняем план в целевом проекте (v2-plan-save)
    _save_plan_to_project(res.text, repo_root, project_name)
    print("\n✅ Планирование готово. Проверь PLAN.md и tasks/, затем: harness ingest / run")
    return 0


def cmd_verify(settings: Settings, args: argparse.Namespace) -> int:
    """v2-018: детерминированная проверка PLAN.md + tasks/ перед ingest."""
    from harness.tasks_io.verifier import verify_plan  # noqa: PLC0415
    project = _resolve_project(settings, getattr(args, "project", None))
    repo_root = _repo_root(settings, project)
    report = verify_plan(repo_root)
    print(report.markdown())
    if not report.ok:
        print(f"\n❌ {len(report.errors)} error(s) — ingest будет отказан без --force",
              file=sys.stderr)
        return 1
    if report.warnings:
        print(f"\n⚠️ {len(report.warnings)} warning(s) — не блокируют", file=sys.stderr)
    return 0


def cmd_ingest(settings: Settings, args: argparse.Namespace) -> int:
    # v2-018: auto-verify перед ingest. Красный → отказ без --force.
    from harness.tasks_io.verifier import verify_plan  # noqa: PLC0415
    project = _resolve_project(settings, getattr(args, "project", None))
    repo_root = _repo_root(settings, project)
    report = verify_plan(repo_root)
    if not report.ok and not getattr(args, "force", False):
        print("❌ Plan verification FAILED:", file=sys.stderr)
        for f in report.errors:
            print(f"  task-{f.task_id}: {f.issue}", file=sys.stderr)
        print("поправь PLAN.md/tasks/ или запусти с --force", file=sys.stderr)
        return 1

    store = _store(settings)
    project = _resolve_project(settings, args.project)
    base = project.base_branch if project else None
    run_id = ingest_run(
        settings, store, project=args.project, goal=args.goal, base_branch=base,
        force=getattr(args, "force", False), plan_root=repo_root,
    )
    reused = store.get_run(run_id)
    is_reused = reused is not None and reused.status != "planning"
    marker = " [reused]" if is_reused else ""
    print(f"создан run: {run_id}{marker}" + (f" (проект {project.name})" if project else ""))
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    _legacy_warn(
        "run",
        "the push Engine drives agents from Python; use `tasktool next/report/advance`",
    )
    store = _store(settings)
    run_id = args.run_id or _latest_run_id(store)
    if not run_id:
        print("нет ни одного Run. Сначала: harness ingest \"<goal>\"", file=sys.stderr)
        return 1
    run = store.get_run(run_id)
    assert run is not None
    # v2-023: PLAN_DRAFT — отказ без --approve-plan (план требует подтверждения).
    if run.status == "plan_draft":
        if not getattr(args, "approve_plan", False):
            print(f"run {run_id} в статусе PLAN_DRAFT — подтверди план перед запуском:",
                  file=sys.stderr)
            print(f"  harness run {run_id} --approve-plan", file=sys.stderr)
            print("или сначала: harness verify", file=sys.stderr)
            return 1
        store.set_run_status(run_id, "planning")
    project = _resolve_project(settings, args.project)
    repo_root = _repo_root(settings, project)
    if DurableConfig.is_enabled():
        # v2-036: HARNESS_DURABLE=1 — раньше проверялось только в `doctor`, сам
        # `run` всегда шёл через asyncio Engine. EngineTaskExecutor (v2-034) уже
        # реальный, но был недостижим из CLI — подключаем сюда.
        rc = _run_durable(settings, store, run_id, run, repo_root)
        if rc != 0:
            return rc
    else:
        engine = Engine(settings, store, repo_root=repo_root)
        asyncio.run(engine.run(run_id))
    return cmd_status(settings, argparse.Namespace(
        run_id=run_id, markdown=False, write=None, project=None,
    ))


def _run_durable(
    settings: Settings, store: Store, run_id: str, run: Run, repo_root: Path,
) -> int:
    """v2-036: исполнить Run через `DurableOrchestrator` + `EngineTaskExecutor`
    (crash-safe DBOS-workflow) вместо asyncio `Engine`.

    Урезанный контракт относительно `Engine` — см. докстринг `EngineTaskExecutor`:
    без de-sloppify/eviction-context/completion-signal/tiered-pipeline/re-plan/
    Telegram-нотификаций/budget-governor, только базовый worker→gate→review→merge
    цикл, зато переживает падение процесса (DBOS чекпоинтит каждый шаг).
    """
    try:
        from harness.durable.executor import EngineTaskExecutor  # noqa: PLC0415
        from harness.durable.orchestrator import durable_session  # noqa: PLC0415
    except ModuleNotFoundError:
        print(
            "HARNESS_DURABLE=1, но пакет dbos не установлен.\n"
            '  Поставь: pip install -e ".[durable]"  (и подними Postgres: make pg-up)',
            file=sys.stderr,
        )
        return 1

    store.set_run_status(run_id, "running")
    executor = EngineTaskExecutor(settings, store, repo_root=repo_root, base_branch=run.base_branch)
    with durable_session(store, executor, DurableConfig(), instance_name=run_id) as orch:
        statuses = orch.run(run_id)
    all_done = bool(statuses) and all(s == "done" for s in statuses.values())
    # DurableOrchestrator обновляет статусы задач, но не run.status (см. его
    # докстринг) — синхронизируем здесь, иначе `harness status` после durable-
    # прогона показывал бы стухший "running"/"planning" при реально done/blocked.
    store.set_run_status(run_id, "done" if all_done else "paused")
    return 0


def cmd_approve(settings: Settings, args: argparse.Namespace) -> int:
    store = _store(settings)
    run_id = args.run_id or _latest_run_id(store)
    if not run_id:
        print("нет Run", file=sys.stderr)
        return 1
    project = _resolve_project(settings, args.project)
    engine = Engine(settings, store, repo_root=_repo_root(settings, project))
    asyncio.run(engine.approve_merge(run_id, args.task_id))
    return cmd_status(settings, argparse.Namespace(run_id=run_id))


def _metrics_summary(store: Store, run_id: str) -> dict[str, float | None]:
    """v2-030: pass@1/pass@3 (≥1 из k попыток approve) + pass^3 (все k попыток green-gate)."""
    return {
        "pass@1": store.pass_at_k(run_id, 1),
        "pass@3": store.pass_at_k(run_id, 3),
        "pass^3": store.pass_all_k(run_id, 3),
    }


def _format_metrics_line(store: Store, run_id: str) -> str:
    """'Metrics: pass@1=0.70 pass@3=0.91 pass^3=0.34' — пусто, если считать не по чему."""
    parts = [
        f"{name}={value:.2f}"
        for name, value in _metrics_summary(store, run_id).items()
        if value is not None
    ]
    return "Metrics: " + " ".join(parts) if parts else ""


def _resolve_run_root_for_status(
    settings: Settings | None, store: Store, run_id: str
) -> Path | None:
    """Best-effort locate ``.harness/runs/<id>/`` for evidence%.

    Falls back to harness root / project registry; returns None if unknown.
    """
    from contextlib import suppress  # noqa: PLC0415

    from harness.tenant.run_layout import resolve_run_root  # noqa: PLC0415

    run = store.get_run(run_id)
    candidates: list[Path] = []
    if settings is not None:
        project = None
        if run is not None and run.project:
            with suppress(Exception):
                project = _resolve_project(settings, run.project)
        root = getattr(settings, "root", None)
        if root is not None:
            candidates.append(_repo_root(settings, project))
            candidates.append(Path(root))
        elif project is not None:
            candidates.append(Path(project.repo_path))
    candidates.append(Path.cwd())
    seen: set[Path] = set()
    for repo in candidates:
        repo = repo.resolve()
        if repo in seen:
            continue
        seen.add(repo)
        paths = resolve_run_root(repo, run_id)
        if (paths.root / "acceptance.json").is_file() or (
            paths.root / "evidence" / "ledger.jsonl"
        ).is_file():
            return paths.root
        if paths.root.is_dir():
            return paths.root
    return None


def _phase_info_for_status(
    run_id: str,
    store: Store,
    *,
    settings: Settings | None = None,
    run: object | None = None,
    tasks: list | None = None,
) -> dict[str, str | None]:
    """Derive phase/stall for status UX (Phase 2)."""
    from harness.policy.phase import derive_phase  # noqa: PLC0415

    run_obj = run or store.get_run(run_id)
    task_list = tasks if tasks is not None else store.list_tasks(run_id)
    run_status = getattr(run_obj, "status", "unknown") if run_obj else "unknown"
    stall_override = None
    phase_override = None
    run_root = _resolve_run_root_for_status(settings, store, run_id)
    if run_root is not None:
        meta_path = run_root / "controller.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    stall_override = str(meta.get("stall_reason") or "") or None
                    phase_override = str(meta.get("phase") or "") or None
            except (OSError, json.JSONDecodeError):
                pass
    recent = [e.type for e in store.list_events(run_id)[-40:]]
    snap = derive_phase(
        run_status=str(run_status),
        task_statuses=[t.status for t in task_list],
        recent_event_types=recent,
        stall_override=stall_override,
        phase_override=phase_override,
    )
    return {
        "phase": snap.ux_phase.value,
        "loop_phase": snap.loop_phase.value if snap.loop_phase else None,
        "stall_reason": snap.stall_reason,
    }


def _format_status_markdown(
    run_id: str,
    store: Store,
    *,
    settings: Settings | None = None,
) -> str:
    """Portable markdown snapshot — evidence% primary, pipeline% secondary."""
    from harness.evidence.acceptance import evidence_pct_for_run, honesty_pcts_for_run  # noqa: PLC0415
    from harness.evidence.ledger import evidence_gate_enabled  # noqa: PLC0415

    run = store.get_run(run_id)
    assert run is not None
    tasks = store.list_tasks(run_id)
    done = sum(1 for t in tasks if t.status == "done")
    blocked = [t.id for t in tasks if t.status == "blocked"]
    ready = sum(1 for t in tasks if t.status == "ready")
    running = sum(1 for t in tasks if t.status == "running")
    pipeline_pct = (done / len(tasks) * 100.0) if tasks else None
    run_root = _resolve_run_root_for_status(settings, store, run_id)
    honesty = honesty_pcts_for_run(run_root) if run_root else None
    evidence_pct = honesty["evidence_pct"] if honesty else (
        evidence_pct_for_run(run_root) if run_root else None
    )
    ac_bound_pct = honesty["ac_bound_pct"] if honesty else None
    if evidence_pct is None:
        evidence_display = "n/a (no acceptance.json)"
    else:
        evidence_display = f"**{evidence_pct:.0f}%**"
    bound_display = (
        f"{ac_bound_pct:.0f}%" if ac_bound_pct is not None else "—"
    )
    pipeline_display = (
        f"{pipeline_pct:.0f}%" if pipeline_pct is not None else "—"
    )

    phase_info = _phase_info_for_status(run_id, store, settings=settings, run=run, tasks=tasks)

    lines: list[str] = [
        f"# Harness status — run `{run_id}`",
        "",
        f"- **project:** `{run.project}`",
        f"- **goal:** {run.goal}",
        f"- **status:** `{run.status}`",
        f"- **base branch:** `{run.base_branch}`",
        f"- **spent:** {run.spent_credits:.2f} credits"
        + (f" / {run.budget_credits:.2f} budget" if run.budget_credits else " (no budget)"),
        f"- **created:** {run.created_at}",
        f"- **HARNESS_EVIDENCE_GATE:** `{1 if evidence_gate_enabled() else 0}`",
        "",
        "## Phase (governor UX)",
        f"- **phase:** `{phase_info['phase']}`",
        f"- **stall_reason:** `{phase_info['stall_reason']}`",
        (
            f"- loop_phase: `{phase_info['loop_phase']}`"
            if phase_info.get("loop_phase")
            else "- loop_phase: —"
        ),
        "",
        "## Readiness (honesty)",
        f"- **evidence% (primary):** {evidence_display}",
        f"- ac_bound% (semantic bind): {bound_display}",
        f"- pipeline% (FSM, secondary): {pipeline_display}",
        f"- total tasks: **{len(tasks)}**",
        f"- done: **{done}** / ready: {ready} / running: {running} / blocked: {len(blocked)}",
        "",
    ]
    metrics_line = _format_metrics_line(store, run_id)
    if metrics_line:
        lines.append("## Metrics")
        lines.append("")
        lines.append(f"- {metrics_line}")
        lines.append("")
    lines += [
        "## Tasks",
        "",
        "| id | status | attempts | deps | complexity | title |",
        "|---|---|---|---|---|---|",
    ]
    for t in tasks:
        deps = ",".join(t.depends_on) or "—"
        lines.append(
            f"| `{t.id}` | `{t.status}` | {t.attempts} | {deps} | {t.complexity} | {t.title} |"
        )
    lines.append("")
    if blocked:
        lines.append("## Blocked (need human)")
        lines.append("")
        for tid in blocked:
            bt = store.get_task(run_id, tid)
            note: str = "—"
            if bt is not None and bt.note:
                note = bt.note
            lines.append(f"- `task-{tid}`: {note}")
        lines.append("")
    # v2-011: token-usage по задачам (последняя попытка)
    tok_rows = [
        ("task", "model", "tokens_in", "tokens_out", "cost", "kind"),
        ("---", "---", "---", "---", "---", "---"),
    ]
    any_tokens = False
    for t in tasks:
        last = store.last_attempt(run_id, t.id)
        if last is None:
            continue
        any_tokens = True
        tok_rows.append((
            f"`{t.id}`", last.model, str(last.tokens_in), str(last.tokens_out),
            f"{last.cost_credits:.2f}", last.cost_kind,
        ))
    if any_tokens:
        lines.append("## Token usage (last attempt per task)")
        lines.append("")
        for row in tok_rows:
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    # Последние 10 событий
    events = store.list_events(run_id)[-10:]
    if events:
        lines.append("## Last 10 events")
        lines.append("")
        lines.append("| # | at | type | task | detail |")
        lines.append("|---|---|---|---|---|")
        for e in events:
            tid = f"`{e.task_id}`" if e.task_id else "—"
            detail = str(e.detail)[:80].replace("|", "\\|")
            lines.append(f"| {e.id} | {e.at} | `{e.type}` | {tid} | {detail} |")
        lines.append("")
    return "\n".join(lines)


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    store = _store(settings)
    project = _resolve_project(settings, getattr(args, "project", None))
    run_id = getattr(args, "run_id", None)
    if not run_id:
        if project:
            run_id = _latest_run_id_for_project(store, project.name)
        else:
            run_id = _latest_run_id(store)
    if not run_id:
        print("нет Run", file=sys.stderr)
        return 1
    run = store.get_run(run_id)
    assert run is not None

    # v2-012: markdown-режим для portable handoff (--markdown / --write <path>).
    if getattr(args, "markdown", False):
        md = _format_status_markdown(run_id, store, settings=settings)
        write_path = getattr(args, "write", None)
        if write_path:
            target = Path(write_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md, encoding="utf-8")
            print(f"статус записан в {target}")
        else:
            print(md)
        return 0

    from harness.evidence.acceptance import evidence_pct_for_run  # noqa: PLC0415
    from harness.evidence.ledger import evidence_gate_enabled  # noqa: PLC0415

    tasks = store.list_tasks(run_id)
    done = sum(1 for t in tasks if t.status == "done")
    pipeline_pct = (100.0 * done / len(tasks)) if tasks else None
    run_root = _resolve_run_root_for_status(settings, store, run_id)
    evidence_pct = evidence_pct_for_run(run_root) if run_root else None
    print(f"run {run_id} [{run.status}]  потрачено: {run.spent_credits:.2f}"
          + (f"/{run.budget_credits:.2f}" if run.budget_credits else ""))
    ev = f"{evidence_pct:.0f}%" if evidence_pct is not None else "n/a"
    pipe = f"{pipeline_pct:.0f}%" if pipeline_pct is not None else "n/a"
    print(
        f"  evidence%={ev} (primary)  pipeline%={pipe} (secondary)  "
        f"gate={1 if evidence_gate_enabled() else 0}"
    )
    phase_info = _phase_info_for_status(
        run_id, store, settings=settings, run=run, tasks=tasks
    )
    print(
        f"  phase={phase_info['phase']}  stall_reason={phase_info['stall_reason']}"
    )
    metrics_line = _format_metrics_line(store, run_id)
    if metrics_line:
        print(f"  {metrics_line}")
    for t in tasks:
        deps = ",".join(t.depends_on) or "-"
        suffix = f"  task-{t.id:<5} {t.status:<12} deps[{deps}] attempts={t.attempts}  {t.title}"
        # v2-011: token-usage последней попытки (если есть).
        last = store.last_attempt(run_id, t.id)
        if last and (last.tokens_in or last.tokens_out):
            suffix += f"  [{last.tokens_in}in/{last.tokens_out}out {last.cost_kind}]"
        print(suffix)
    return 0


def cmd_events(settings: Settings, args: argparse.Namespace) -> int:
    store = _store(settings)
    project = _resolve_project(settings, getattr(args, "project", None))
    run_id = args.run_id or (
        _latest_run_id_for_project(store, project.name) if project else _latest_run_id(store)
    )
    if not run_id:
        print("нет Run", file=sys.stderr)
        return 1
    for e in store.list_events(run_id):
        tid = f"task-{e.task_id}" if e.task_id else "-"
        print(f"  #{e.id:<4} {e.at}  {e.type:<18} {tid:<10} {e.detail}")
    return 0


def cmd_tail(settings: Settings, args: argparse.Namespace) -> int:
    """v2-010: live-tail event-log (long-poll каждые 500ms).

    Показывает новые events (и опционально agent_events — транскрипт) по мере
    их появления в store. Ctrl-C → clean exit. Источник: `events` таблица
    (TASK_TRANSITION, ATTEMPT_STARTED, GATE_RESULT, REVIEW_RESULT, …) — это
    «живой» поток движка; agent_events пишутся bulk после прогона воркера.
    """
    store = _store(settings)
    project = _resolve_project(settings, getattr(args, "project", None))
    run_id = args.run_id or (
        _latest_run_id_for_project(store, project.name) if project else _latest_run_id(store)
    )
    if not run_id:
        print("нет Run", file=sys.stderr)
        return 1

    task_filter = getattr(args, "task", None)
    kind_filter = getattr(args, "kind", None)
    include_agent = getattr(args, "agent_events", False)
    interval = float(getattr(args, "interval", 0.5))

    print(f"[tail] run={run_id} task={task_filter or 'all'} kind={kind_filter or 'all'} "
          f"agent_events={include_agent} — Ctrl-C to stop", file=sys.stderr)

    async def _loop() -> None:
        last_event_id = 0
        last_agent_id = 0
        while True:
            new_events = store.list_events(run_id, after_id=last_event_id)
            for e in new_events:
                last_event_id = max(last_event_id, e.id)
                if task_filter and e.task_id != task_filter:
                    continue
                if kind_filter and e.type != kind_filter:
                    continue
                tid = f"task-{e.task_id}" if e.task_id else "-"
                print(f"  #{e.id:<4} {e.at}  {e.type:<18} {tid:<10} {e.detail}",
                      flush=True)
            if include_agent:
                new_agent = store.list_agent_events(
                    run_id, task_id=task_filter, after_id=last_agent_id,
                )
                for ae in new_agent:
                    last_agent_id = max(last_agent_id, ae.id)
                    if kind_filter and ae.kind != kind_filter:
                        continue
                    tid = f"task-{ae.task_id}" if ae.task_id else "-"
                    print(f"  #{ae.id:<4} {ae.at}  [agent] {ae.kind:<14} "
                          f"{tid:<10} {ae.payload}", flush=True)
            await asyncio.sleep(interval)

    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        print("\n[tail] stopped", file=sys.stderr)
    return 0


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                           check=False)
    except FileNotFoundError:
        return 1, "git не найден в PATH"
    return r.returncode, (r.stdout + r.stderr).strip()


def cmd_init(settings: Settings, args: argparse.Namespace) -> int:
    """Подготовить целевой репозиторий: git + .harness/project.toml под его стек."""
    repo = Path(args.repo).resolve() if args.repo else settings.root
    repo.mkdir(parents=True, exist_ok=True)
    print(f"init: {repo}")

    if not (repo / ".git").exists():
        _git(repo, "init")
        _git(repo, "checkout", "-B", args.base)
        print(f"  git init + ветка {args.base}")
    # первый коммит, если история пустая
    code, _ = _git(repo, "rev-parse", "HEAD")
    if code != 0:
        (repo / ".gitignore").exists() or (repo / ".gitignore").write_text(
            ".harness/state.db*\n.worktrees/\nlogs/\n"
            # Воркеры создают venv/кэши внутри worktree для гонки гейтов (ruff/mypy/pytest);
            # без этих строк `git add -A` в commit_all() затягивает их в diff задачи
            # (инцидент: 4203 файла venv в коммите → 28MB промпт → E2BIG в CliRunner).
            ".venv/\nvenv/\n__pycache__/\n*.pyc\n*.egg-info/\n"
            ".mypy_cache/\n.pytest_cache/\n.ruff_cache/\nnode_modules/\n.DS_Store\n",
            encoding="utf-8",
        )
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=harness@local", "-c", "user.name=harness",
             "commit", "-m", "init: harness")
        print("  первый коммит создан")

    profile = repo / ".harness" / "project.toml"
    if profile.exists():
        print(f"  профиль уже есть: {profile}")
    else:
        lang = args.language or detect_language(repo)
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(default_project_toml(lang), encoding="utf-8")
        print(f"  создан профиль ({lang}): {profile} — проверь команды гейтов!")

    # v2-037: создаём базовые файлы проекта (pyproject.toml, package.json, ...),
    # если их нет — чтобы гейты не падали из-за отсутствия тулчейна.
    created = scaffold_project(repo, args.language or detect_language(repo))
    for path in created:
        print(f"  создан файл: {path}")

    print("\nДальше:  harness doctor   →  harness plan \"<цель>\"  →  ingest  →  run")
    return 0


def cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    """v2-031: AgentShield-скан — статические правила + опционально adversarial (--opus)."""
    from harness.security.scanner import scan_directory  # noqa: PLC0415

    report = scan_directory(settings.root)
    output = report.markdown()

    if getattr(args, "opus", False):
        from harness.security.red_team import run_red_team  # noqa: PLC0415

        role = settings.role(Role.ORCHESTRATOR)
        runner = build_runner(role.runner, role=Role.ORCHESTRATOR)
        rt_report = asyncio.run(run_red_team(settings.root, runner, model=role.model))
        output += "\n\n" + rt_report.markdown()

    write_path = getattr(args, "write", None)
    if write_path:
        target = Path(write_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
        print(f"отчёт записан в {target}")
    else:
        print(output)
    return 2 if report.critical else 0


def _doctor_write(text: str, write_path: str | None) -> None:
    if write_path:
        target = Path(write_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"отчёт записан в {target}")
    else:
        print(text)


def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    """Проверка готовности окружения + V4 diagnose flags."""
    from harness import __version__ as harness_version  # noqa: PLC0415

    # V4 Phase 0: specialized reports (can run without full env green).
    if getattr(args, "false_coverage_report", False):
        from harness.doctor.diagnose import (  # noqa: PLC0415
            build_false_coverage_report,
            render_false_coverage_markdown,
        )

        store = _store(settings)
        run_ids = None
        if getattr(args, "run", None):
            run_ids = [args.run]
        report = build_false_coverage_report(
            store,
            harness_root=settings.root,
            limit=int(getattr(args, "limit", 8) or 8),
            run_ids=run_ids,
        )
        md = render_false_coverage_markdown(report)
        if getattr(args, "json", False):
            _doctor_write(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
                getattr(args, "write", None),
            )
        else:
            _doctor_write(md, getattr(args, "write", None))
        return 0

    if getattr(args, "multi_tenant_report", False):
        from harness.doctor.diagnose import (  # noqa: PLC0415
            build_multi_tenant_report,
            render_multi_tenant_markdown,
        )

        extras: list[Path] = []
        for raw in getattr(args, "repo", None) or []:
            extras.append(Path(raw))
        # Convenience: ZY dogfood if present
        zy = Path("/home/ZY-2nd-flight")
        if zy.is_dir() and zy not in extras:
            extras.append(zy)
        report = build_multi_tenant_report(settings, extra_repos=extras)
        md = render_multi_tenant_markdown(report)
        if getattr(args, "json", False):
            _doctor_write(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
                getattr(args, "write", None),
            )
        else:
            _doctor_write(md, getattr(args, "write", None))
        return 0

    if getattr(args, "soft_gates_report", False):
        from harness.doctor.diagnose import (  # noqa: PLC0415
            inventory_soft_gates,
            render_soft_gates_markdown,
        )

        paths: list[Path] = []
        project = _resolve_project(settings, getattr(args, "project", None))
        if project:
            paths.append(Path(project.repo_path) / ".harness" / "project.toml")
        for raw in getattr(args, "repo", None) or []:
            p = Path(raw)
            toml = p / ".harness" / "project.toml" if p.is_dir() else p
            paths.append(toml)
        zy_toml = Path("/home/ZY-2nd-flight/.harness/project.toml")
        if zy_toml.is_file() and zy_toml not in paths:
            paths.append(zy_toml)
        findings = inventory_soft_gates(paths, also_scaffold_profiles=True)
        _doctor_write(render_soft_gates_markdown(findings), getattr(args, "write", None))
        return 0

    if getattr(args, "meta_eval", False):
        from harness.meta.changelog import default_changelog_path  # noqa: PLC0415
        from harness.meta.eval import evaluate_entries, render_eval_markdown  # noqa: PLC0415

        store = _store(settings)
        path = default_changelog_path(settings.root)
        project = _resolve_project(settings, getattr(args, "project", None))
        repo = _repo_root(settings, project) if project else None
        for raw in getattr(args, "repo", None) or []:
            repo = Path(raw).resolve()
            break
        report = evaluate_entries(
            store,
            harness_root=settings.root,
            changelog_path=path,
            repo_root=repo,
            limit_recent=int(getattr(args, "limit", 8) or 8),
        )
        if getattr(args, "json", False):
            _doctor_write(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
                getattr(args, "write", None),
            )
        else:
            _doctor_write(render_eval_markdown(report), getattr(args, "write", None))
        return 0

    project = _resolve_project(settings, getattr(args, "project", None))
    repo = _repo_root(settings, project)
    # ok: True → ✅, False → ❌ (fails doctor), None → ⚠️ (deliberate but risky).
    rows: list[tuple[str, bool | None, str]] = []

    rows.append((
        "harness version",
        True,
        f"{harness_version} (root={settings.root})",
    ))

    # v2-031: AgentShield static scan промптов/правил harness (не целевого проекта).
    from harness.security.scanner import scan_directory  # noqa: PLC0415
    scan_report = scan_directory(settings.root)
    rows.append((
        "security scan (prompts/.cursor)", not scan_report.critical,
        f"grade {scan_report.grade}" if not scan_report.critical
        else f"{len(scan_report.critical)} critical — harness scan для деталей",
    ))

    rows.append(("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    rows.append(("git", shutil.which("git") is not None, shutil.which("git") or "—"))
    rows.append(("git-репозиторий цели", (repo / ".git").exists(), str(repo)))
    agent = shutil.which("cursor-agent")
    rows.append(("cursor-agent (воркеры CLI)", agent is not None, agent or "не в PATH"))

    sdk_roles = [r for r in Role if settings.role(r).runner == RunnerKind.SDK]
    if sdk_roles:
        has_key = bool(os.environ.get("CURSOR_API_KEY"))
        rows.append(("CURSOR_API_KEY (SDK-роли)", has_key,
                     "задан" if has_key else "нужен для sdk-ролей (оркестратор/ревьюер)"))

    rows.append((f"профиль {project.name if project else 'default'}",
                 (repo / ".harness" / "project.toml").exists(),
                 str(repo / ".harness" / "project.toml")))
    rows.append(("prompts/", settings.prompts_dir.exists(), str(settings.prompts_dir)))

    catalog_path = settings.root / "skills" / "catalog.json"
    rows.append((
        "skills catalog.json (v4)",
        catalog_path.is_file(),
        str(catalog_path),
    ))
    rows.append((
        "worktrees default",
        True,
        f"HARNESS_USE_WORKTREES={'1' if settings.use_worktrees else '0'}",
    ))
    rows.append((
        "allow soft gates",
        True,
        f"HARNESS_ALLOW_SOFT_GATES={os.environ.get('HARNESS_ALLOW_SOFT_GATES', '0')}",
    ))
    chat_mode = os.environ.get("HARNESS_RUNNER") == "cursor_task"
    rows.append((
        "runner mode",
        None if chat_mode else True,
        "chat (deprecated file-spool bridge) — `harness mode run` возвращает "
        "TaskTool-first; на pull-путь не влияет, но конфиг устарел"
        if chat_mode
        else "run (TaskTool-first)",
    ))
    evidence_gate_on = os.environ.get("HARNESS_EVIDENCE_GATE", "1") == "1"
    rows.append((
        "evidence gate",
        True if evidence_gate_on else None,
        "HARNESS_EVIDENCE_GATE=1"
        if evidence_gate_on
        else "HARNESS_EVIDENCE_GATE=0 — DONE не требует доказательств (docs заявляют ON)",
    ))
    from harness.evidence.integrity import attestation_posture  # noqa: PLC0415

    posture = attestation_posture()
    posture_warnings = list(posture["warnings"])
    rows.append((
        "attestation",
        True if not posture_warnings else None,
        "; ".join(posture_warnings)
        if posture_warnings
        else f"key={posture['key_source']} — {posture['guarantee']}",
    ))

    if DurableConfig.is_enabled():
        try:
            import dbos  # noqa: F401, PLC0415
            durable_ok, detail = True, "dbos установлен"
        except ModuleNotFoundError:
            durable_ok, detail = False, "pip install -e .[durable]"
        rows.append(("durable: dbos", durable_ok, detail))

    if settings.telegram_bot_token and settings.telegram_chat_id:
        rows.append(("telegram", True, "токен+чат заданы"))

    print(f"harness doctor — цель: {repo}\n")
    all_ok = True
    for name, ok, detail in rows:
        # ok=None → deliberate but risky setting: show it, do not fail the run.
        if ok is None:
            mark = "⚠️"
        else:
            mark = "✅" if ok else "❌"
            if not ok:
                all_ok = False
        print(f"  {mark} {name:<32} {detail}")
    print("\n" + ("Готово к работе." if all_ok else "Есть проблемы — поправь ❌ выше."))
    print(
        "\nV4 diagnose: doctor --false-coverage-report | "
        "--multi-tenant-report | --soft-gates-report | --meta-eval"
    )
    if scan_report.critical:
        print(
            f"\n❌ КРИТИЧНО: {len(scan_report.critical)} security finding(s) в "
            "prompts/.cursor — смотри `harness scan`", file=sys.stderr,
        )
        return 2
    return 0 if all_ok else 1


def cmd_skills(settings: Settings, args: argparse.Namespace) -> int:
    """V4 skills catalog / select / stocktake wrapper."""
    action = getattr(args, "skills_action", "catalog")
    if action == "stocktake":
        return cmd_stocktake(settings, args)

    from harness.skills.catalog import (  # noqa: PLC0415
        default_catalog_path,
        load_catalog,
        select_skills,
    )

    path = Path(getattr(args, "catalog", None) or default_catalog_path(settings.root))
    if not path.is_file():
        print(f"catalog not found: {path}", file=sys.stderr)
        return 1
    catalog = load_catalog(path)

    if action == "catalog":
        payload = {
            "version": catalog.version,
            "updated_at": catalog.updated_at,
            "policy": catalog.policy,
            "packs": catalog.packs,
            "skills": [
                {
                    "id": s.id,
                    "path": s.path,
                    "invocation": s.invocation,
                    "roles": list(s.roles),
                    "status": s.status,
                }
                for s in catalog.skills
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if action == "select":
        paths = select_skills(
            catalog,
            role=str(getattr(args, "role", "worker") or "worker"),
            stage=str(getattr(args, "stage", "") or ""),
            gate_fail=bool(getattr(args, "gate_fail", False)),
            limit=getattr(args, "limit", None),
        )
        print(json.dumps({"skill_paths": paths}, ensure_ascii=False, indent=2))
        return 0

    if action == "gc":
        return _cmd_skills_gc(settings, args, catalog_path=path)

    print(f"unknown skills action: {action}", file=sys.stderr)
    return 1


def _cmd_skills_gc(
    settings: Settings,
    args: argparse.Namespace,
    *,
    catalog_path: Path,
) -> int:
    """Phase 4 skill GC: propose / apply / restore / list quarantine."""
    from harness.skills.catalog import load_catalog  # noqa: PLC0415
    from harness.skills.gc import (  # noqa: PLC0415
        QUARANTINED,
        apply_quarantine,
        propose_quarantine,
        render_proposal_markdown,
        restore_active,
    )

    gc_action = getattr(args, "gc_action", "propose") or "propose"
    store = _store(settings)

    if gc_action == "propose":
        catalog = load_catalog(catalog_path)
        proposal = propose_quarantine(
            catalog,
            store,
            catalog_path=catalog_path,
            max_selects=int(getattr(args, "max_selects", 0) or 0),
            include_human_only=bool(getattr(args, "include_human_only", False)),
            protect_pack_core=not bool(getattr(args, "include_core", False)),
        )
        if getattr(args, "json", False):
            text = json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2) + "\n"
        else:
            text = render_proposal_markdown(proposal)
        write = getattr(args, "write", None)
        if write:
            Path(write).write_text(text, encoding="utf-8")
            print(f"отчёт записан в {write}")
        else:
            print(text, end="" if text.endswith("\n") else "\n")
        return 0

    if gc_action == "apply":
        raw_ids = getattr(args, "ids", None) or ""
        ids = [x.strip() for x in str(raw_ids).split(",") if x.strip()]
        if not ids:
            # Default: use current proposal list
            catalog = load_catalog(catalog_path)
            proposal = propose_quarantine(
                catalog,
                store,
                catalog_path=catalog_path,
                max_selects=int(getattr(args, "max_selects", 0) or 0),
                include_human_only=bool(getattr(args, "include_human_only", False)),
                protect_pack_core=not bool(getattr(args, "include_core", False)),
            )
            ids = list(proposal.quarantine_ids)
        result = apply_quarantine(
            catalog_path,
            ids,
            approve=bool(getattr(args, "approve", False)),
            note=str(getattr(args, "note", "") or ""),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if gc_action == "restore":
        raw_ids = getattr(args, "ids", None) or ""
        ids = [x.strip() for x in str(raw_ids).split(",") if x.strip()]
        if not ids:
            print(json.dumps({"ok": False, "error": "--ids required"}), file=sys.stderr)
            return 2
        result = restore_active(
            catalog_path,
            ids,
            approve=bool(getattr(args, "approve", False)),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if gc_action == "list":
        catalog = load_catalog(catalog_path)
        if getattr(args, "all", False):
            rows = [
                {"id": s.id, "path": s.path, "status": s.status}
                for s in catalog.skills
            ]
            print(json.dumps({"skills": rows}, ensure_ascii=False, indent=2))
        else:
            rows = [
                {"id": s.id, "path": s.path, "status": s.status}
                for s in catalog.skills
                if s.status == QUARANTINED
            ]
            print(json.dumps({"quarantined": rows}, ensure_ascii=False, indent=2))
        return 0

    print(f"unknown skills gc action: {gc_action}", file=sys.stderr)
    return 1


def cmd_meta(settings: Settings, args: argparse.Namespace) -> int:
    """Phase 4 AHE-lite: changelog + prediction vs outcome eval."""
    from harness.meta.changelog import (  # noqa: PLC0415
        ChangelogEntry,
        PredictedMetrics,
        append_entry,
        default_changelog_path,
        load_entries,
        new_entry_id,
        update_entry,
    )
    from harness.meta.eval import evaluate_entries, render_eval_markdown  # noqa: PLC0415
    from harness.meta.outcomes import collect_run_outcome  # noqa: PLC0415

    action = getattr(args, "meta_action", "eval") or "eval"
    path = Path(
        getattr(args, "changelog", None) or default_changelog_path(settings.root)
    )
    store = _store(settings)
    project = _resolve_project(settings, getattr(args, "project", None))
    repo = _repo_root(settings, project) if project or getattr(args, "repo", None) else None
    if getattr(args, "repo", None):
        repo = Path(args.repo).resolve()

    if action == "changelog":
        sub = getattr(args, "changelog_action", "list") or "list"
        if sub == "list":
            entries = load_entries(path)
            payload = {
                "path": str(path),
                "entries": [e.to_dict() for e in entries],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if sub == "add":
            change = str(getattr(args, "change", "") or "").strip()
            if not change:
                print(json.dumps({"ok": False, "error": "--change required"}), file=sys.stderr)
                return 2
            components = [
                c.strip()
                for c in str(getattr(args, "components", "") or "").split(",")
                if c.strip()
            ]
            predicted = PredictedMetrics(
                evidence_pct_min=getattr(args, "predict_evidence_min", None),
                evidence_pct_delta_pp=getattr(args, "predict_evidence_delta", None),
                tokens_est_max=getattr(args, "predict_tokens_max", None),
                tokens_est_delta_pct=getattr(args, "predict_tokens_delta_pct", None),
                stall_count_max=getattr(args, "predict_stall_max", None),
            )
            baselines = [
                x.strip()
                for x in str(getattr(args, "baseline_runs", "") or "").split(",")
                if x.strip()
            ]
            entry = ChangelogEntry(
                id=new_entry_id(change),
                at=datetime.now(tz=UTC).isoformat(),
                change=change,
                components=components,
                hypothesis=str(getattr(args, "hypothesis", "") or ""),
                predicted=predicted,
                baseline_run_ids=baselines,
            )
            append_entry(path, entry)
            print(json.dumps({"ok": True, "entry": entry.to_dict(), "path": str(path)},
                             ensure_ascii=False, indent=2))
            return 0
        print(f"unknown changelog action: {sub}", file=sys.stderr)
        return 1

    if action == "record":
        run_id = str(getattr(args, "run_id", "") or "").strip()
        if not run_id:
            print(json.dumps({"ok": False, "error": "--run-id required"}), file=sys.stderr)
            return 2
        outcome = collect_run_outcome(
            store, run_id, harness_root=settings.root, repo_root=repo
        )
        if outcome is None:
            print(json.dumps({"ok": False, "error": f"run not found: {run_id}"}),
                  file=sys.stderr)
            return 1
        entry_id = getattr(args, "changelog_id", None)
        if entry_id:
            updated = update_entry(
                path,
                str(entry_id),
                actual=outcome.to_dict(),
                verify_run_ids=[run_id],
                status=str(getattr(args, "status", None) or "open"),
            )
            if updated is None:
                print(json.dumps({"ok": False, "error": f"changelog id not found: {entry_id}"}),
                      file=sys.stderr)
                return 1
            print(json.dumps({"ok": True, "entry": updated.to_dict(), "outcome": outcome.to_dict()},
                             ensure_ascii=False, indent=2))
            return 0
        # No changelog id — just print / optionally append outcome snapshot file
        out_dir = settings.root / ".harness" / "meta" / "outcomes"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{run_id}.json"
        out_path.write_text(
            json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"ok": True, "outcome": outcome.to_dict(), "path": str(out_path)},
                         ensure_ascii=False, indent=2))
        return 0

    if action == "eval":
        report = evaluate_entries(
            store,
            harness_root=settings.root,
            changelog_path=path,
            repo_root=repo,
            limit_recent=int(getattr(args, "limit", 5) or 5),
            entry_ids=(
                [x.strip() for x in str(getattr(args, "ids", "") or "").split(",") if x.strip()]
                or None
            ),
        )
        if getattr(args, "json", False):
            text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
        else:
            text = render_eval_markdown(report)
        write = getattr(args, "write", None)
        if write:
            Path(write).write_text(text, encoding="utf-8")
            print(f"отчёт записан в {write}")
        else:
            print(text, end="" if text.endswith("\n") else "\n")
        return 0

    print(f"unknown meta action: {action}", file=sys.stderr)
    return 1


def cmd_chat(settings: Settings, args: argparse.Namespace) -> int:
    """v2-022c: chat-режим — весь пайплайн (оркестратор+воркеры+ревьюер) через
    Cursor IDE Task tool, вызываемый ИЗ ОДНОГО root-чата.

    Печатает инструкцию для запуска chat-bridge: env, spool-директория, ссылка
    на entry-point skill (`harness-chat-orchestrator`), который ведёт весь флоу
    от сбора требований до готового прогона.
    """
    bridge = settings.bridge_dir
    msg = f"""harness chat — весь пайплайн через Cursor IDE Task tool (v2-022c)

Архитектура: harness process = «слепой» control plane (гейты/мерж/бюджет/DAG),
единственная точка вызова Task tool — ТЕКУЩИЙ ЧАТ Cursor IDE. Через него идут
ВСЕ роли: оркестратор (планирование, с живыми уточняющими вопросами через
resume), воркеры и ревьюер (параллельно, до MAX_PARALLEL). Связь — file-spool
bridge: harness пишет requests/<id>.json, IDE-чат пишет responses/<id>.json.

Два независимых режима, переключаются одной командой:
     harness mode chat   # HARNESS_RUNNER=cursor_task — все 3 роли на bridge
     harness mode run    # обычный subprocess/SDK — fire-and-forget без чата
     harness mode show   # что сейчас активно

Для запуска (или просто попроси помощника прямо в чате «используй harness
chat-режим для этой задачи» — он сам выполнит шаги ниже через свой терминал):
1. Открой этот каталог в Cursor IDE: cd {settings.root}
2. В чате попроси: «прочитай .cursor/skills/harness-chat-orchestrator/SKILL.md
   и проведи меня через весь флоу для цели: <твоя задача + документы>».
3. Чат сам: harness mode chat → harness plan "<goal>" (в фоне) → обрабатывает
   вопросы оркестратора живьём → показывает тебе план → harness ingest →
   harness run --approve-plan (в фоне) → обрабатывает воркеров/ревьюера.

Bridge-директория: {bridge} (создаётся автоматически, requests/responses
видны на диске для дебага, переживают рестарт любой стороны).

Ручной путь (без entry-point skill, только воркеры через bridge, оркестратор
на SDK/CLI как раньше) — выставь `WORKER_RUNNER=cursor_task` отдельно вместо
`harness mode chat` и следуй `.cursor/skills/harness-bridge-worker/SKILL.md`.
"""
    print(msg)
    return 0


_MODE_ENV_FILE = ".harness/mode.env"


def cmd_mode(settings: Settings, args: argparse.Namespace) -> int:
    """v2-022c: переключатель run-режим <-> chat-режим одной командой.

    Пишет `.harness/mode.env` (грузится в `main()` ПЕРЕД `.env` — см. приоритет
    в `harness.config._env_runner`). `chat` — ставит `HARNESS_RUNNER=cursor_task`
    (master switch на все 3 роли сразу). `run` — очищает файл, роли берут
    обычные дефолты/то что задано явными ORCH_RUNNER/WORKER_RUNNER/REVIEWER_RUNNER
    в `.env`. Точечная настройка не теряется: явный `ROLE_RUNNER` в `.env`
    всегда выигрывает у `HARNESS_RUNNER` (см. `_env_runner`).
    """
    mode_path = settings.root / _MODE_ENV_FILE
    if args.mode_action == "chat":
        _legacy_warn(
            "mode chat",
            "the file-spool bridge is deprecated; the TaskTool pull loop replaces it",
        )
    if args.mode_action == "show":
        current = "chat" if os.environ.get("HARNESS_RUNNER") == "cursor_task" else "run"
        if mode_path.exists():
            content = mode_path.read_text(encoding="utf-8").strip()
            print(f"mode.env ({mode_path}): {content or '<пусто — run>'}")
        else:
            print(f"mode.env ({mode_path}): <нет файла — run>")
        print(f"эффективный режим сейчас (учитывая os.environ): {current}")
        return 0

    mode_path.parent.mkdir(parents=True, exist_ok=True)
    if args.mode_action == "chat":
        mode_path.write_text(
            "# сгенерировано `harness mode chat` — master switch на все роли.\n"
            "HARNESS_RUNNER=cursor_task\n",
            encoding="utf-8",
        )
        print(
            f"режим переключён на chat: {mode_path} → HARNESS_RUNNER=cursor_task\n"
            "Дальше: harness chat  (инструкция запуска bridge + skill в Cursor IDE)"
        )
    else:  # "run"
        mode_path.write_text(
            "# сгенерировано `harness mode run` — обычный subprocess/SDK режим.\n",
            encoding="utf-8",
        )
        print(f"режим переключён на run: {mode_path} очищен (обычные SDK/CLI раннеры)")
    return 0


def cmd_answer(settings: Settings, args: argparse.Namespace) -> int:
    """v2-020: применить ответы пользователя к задаче в NEEDS_CLARIFICATION."""
    store = _store(settings)
    run_id = args.run_id or _latest_run_id(store)
    if not run_id:
        print("нет Run", file=sys.stderr)
        return 1
    project = _resolve_project(settings, getattr(args, "project", None))
    engine = Engine(settings, store, repo_root=_repo_root(settings, project))
    from harness.tasks_io.question import parse_answer_args  # noqa: PLC0415
    answers = parse_answer_args(args.answers)
    if not answers:
        print("нет ответов. Формат: harness answer <run> <task> q1=\"value\" q2=\"...\"",
              file=sys.stderr)
        return 1
    asyncio.run(engine.answer(run_id, args.task_id, answers))
    print(f"ответы применены: run {run_id}, task-{args.task_id}, questions={list(answers)}")
    return 0


def cmd_dashboard(settings: Settings, args: argparse.Namespace) -> int:
    """v2-014: запустить web-дашборд (FastAPI + uvicorn)."""
    try:
        import uvicorn  # noqa: PLC0415 (опц. зависимость)
    except ModuleNotFoundError:
        print("uvicorn не установлен. Поставь: pip install fastapi uvicorn jinja2",
              file=sys.stderr)
        return 1
    from harness.dashboard.app import create_app  # noqa: PLC0415
    store = _store(settings)
    app = create_app(store)
    host = getattr(args, "host", "127.0.0.1")
    port = int(getattr(args, "port", 8420))
    print(f"dashboard: http://{host}:{port}  (Ctrl-C to stop)", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def cmd_bot(settings: Settings, args: argparse.Namespace) -> int:
    from harness.interface.bot import TelegramBot  # noqa: PLC0415 (ленивый: нужен httpx)

    try:
        asyncio.run(TelegramBot(settings).run())
    except KeyboardInterrupt:
        print("bot остановлен")
    return 0


def cmd_stocktake(settings: Settings, args: argparse.Namespace) -> int:
    """v2-035: аудит prompts/.cursor/skills — размер, свежесть, usage в agent_events."""
    from harness.skills.stocktake import run_stocktake  # noqa: PLC0415

    store = _store(settings)
    report = run_stocktake(settings.root, store)
    output = report.markdown()

    write_path = getattr(args, "write", None)
    if write_path:
        target = Path(write_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
        print(f"отчёт записан в {target}")
    else:
        print(output)
    return 0


def cmd_tenant(settings: Settings, args: argparse.Namespace) -> int:
    """Phase 1 multi-tenant lease helpers."""
    from harness.tenant.leases import list_leases  # noqa: PLC0415

    action = getattr(args, "tenant_action", "list")
    repo = Path(getattr(args, "repo", None) or settings.root).resolve()
    if action == "list":
        leases = list_leases(repo)
        print(
            json.dumps(
                {
                    "repo": str(repo),
                    "multi_tenant": settings.multi_tenant,
                    "leases": [lease.to_dict() for lease in leases],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(json.dumps({"ok": False, "error": f"unknown tenant action {action}"}))
    return 1


def _run_root_from_args(settings: Settings, args: argparse.Namespace) -> Path:
    from harness.tenant.run_layout import resolve_run_root  # noqa: PLC0415

    run_id = getattr(args, "run_id", None)
    if not run_id:
        raise ValueError("--run-id is required")
    repo = Path(getattr(args, "repo", None) or settings.root).resolve()
    return resolve_run_root(repo, run_id).root


def cmd_evidence(settings: Settings, args: argparse.Namespace) -> int:
    """Phase 1.5: evidence ledger add/list."""
    from harness.evidence.ledger import (  # noqa: PLC0415
        EvidenceRecord,
        append_evidence,
        list_evidence,
        new_evidence_id,
    )

    action = getattr(args, "evidence_action", "list")
    try:
        run_root = _run_root_from_args(settings, args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    if action == "list":
        print(json.dumps({"ok": True, "run_root": str(run_root), "rows": list_evidence(run_root)},
                         ensure_ascii=False, indent=2))
        return 0
    if action == "add":
        item_ids = tuple(
            x.strip()
            for x in (getattr(args, "acceptance_item", None) or [])
            if str(x).strip()
        )
        kind = str(getattr(args, "kind", "gate") or "gate")
        exit_code = int(getattr(args, "exit_code", 0))
        # Agents must not mint green gate evidence via CLI without an explicit
        # operator escape hatch — ProfileGate / lifecycle record_gate_run is SoT.
        if (
            kind == "gate"
            and exit_code == 0
            and os.environ.get("HARNESS_EVIDENCE_CLI_MINT", "0") != "1"
        ):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "refusing CLI mint of green gate evidence; "
                            "run gates via advance/ProfileGate, or set "
                            "HARNESS_EVIDENCE_CLI_MINT=1 for operator override"
                        ),
                    }
                ),
                file=sys.stderr,
            )
            return 1
        record = EvidenceRecord(
            id=new_evidence_id("ev"),
            at=datetime.now(tz=UTC).isoformat(),
            run_id=str(args.run_id),
            task_id=str(getattr(args, "task_id", "") or ""),
            kind=kind,
            gate_id=str(getattr(args, "gate", "") or ""),
            exit_code=exit_code,
            log_path=str(getattr(args, "log", "") or ""),
            acceptance_item_ids=item_ids,
            source="cli",
        )
        append_evidence(run_root, record)
        print(json.dumps({"ok": True, "record": record.to_dict()}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"ok": False, "error": f"unknown evidence action {action}"}))
    return 1


def cmd_acceptance(settings: Settings, args: argparse.Namespace) -> int:
    """Phase 1.5: acceptance show/flip/waive (control-plane only)."""
    from harness.evidence.acceptance import (  # noqa: PLC0415
        flip_item,
        load_acceptance,
        waive_item,
    )
    from harness.evidence.youtrack import readiness_for_run  # noqa: PLC0415

    action = getattr(args, "acceptance_action", "show")
    try:
        run_root = _run_root_from_args(settings, args)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    path = run_root / "acceptance.json"
    if action == "show":
        if not path.is_file():
            print(json.dumps({"ok": False, "error": "acceptance.json missing"}), file=sys.stderr)
            return 1
        doc = load_acceptance(path)
        yt = readiness_for_run(run_root, repo=Path(getattr(args, "repo", None) or settings.root))
        honesty = doc.honesty_pcts(run_root)
        print(
            json.dumps(
                {
                    "ok": True,
                    "acceptance": doc.to_dict(),
                    "evidence_pct": honesty["evidence_pct"],
                    "ac_bound_pct": honesty["ac_bound_pct"],
                    "passes_pct": honesty["passes_pct"],
                    "youtrack": yt.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if action == "flip":
        item_id = getattr(args, "item", None)
        evidence_ids = list(getattr(args, "evidence", None) or [])
        if not item_id or not evidence_ids:
            print(
                json.dumps({"ok": False, "error": "--item and --evidence required"}),
                file=sys.stderr,
            )
            return 1
        try:
            doc = flip_item(
                path, item_id=item_id, evidence_ids=evidence_ids, run_root=run_root
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            return 1
        honesty = doc.honesty_pcts(run_root)
        print(
            json.dumps(
                {
                    "ok": True,
                    "acceptance": doc.to_dict(),
                    "evidence_pct": honesty["evidence_pct"],
                    "ac_bound_pct": honesty["ac_bound_pct"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if action == "waive":
        item_id = getattr(args, "item", None)
        reason = getattr(args, "reason", None) or "human waive"
        if not item_id:
            print(json.dumps({"ok": False, "error": "--item required"}), file=sys.stderr)
            return 1
        try:
            doc = waive_item(path, item_id=item_id, reason=reason, run_root=run_root)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, "acceptance": doc.to_dict(), "evidence_pct": doc.evidence_pct},
                         ensure_ascii=False, indent=2))
        return 0
    print(json.dumps({"ok": False, "error": f"unknown acceptance action {action}"}))
    return 1


def cmd_brain(settings: Settings, args: argparse.Namespace) -> int:
    """Phase 3: sync / query / write-card against brain-agents (never human canon)."""
    from harness.brain_agents.cards import BrainCard, write_card  # noqa: PLC0415
    from harness.brain_agents.query import format_query_hits, query_cards  # noqa: PLC0415
    from harness.brain_agents.sync import sync_brain_agents  # noqa: PLC0415

    action = getattr(args, "brain_action", "query")
    agent_root = Path(
        getattr(args, "agent_root", None)
        or settings.brain_root
        or os.environ.get("HARNESS_BRAIN_ROOT", "/home/brain-agents")
    ).expanduser()
    canon = Path(
        getattr(args, "canon", None)
        or settings.brain_canon
        or os.environ.get("HARNESS_BRAIN_CANON", "/home/brain")
    ).expanduser()

    if action == "sync":
        result = sync_brain_agents(canon=canon, agent_root=agent_root)
        payload = {
            "ok": not result.errors,
            "agent_root": str(result.agent_root),
            "canon": str(result.canon),
            "synced_dirs": result.synced_dirs,
            "cards_written": result.cards_written,
            "index": str(result.index_path) if result.index_path else None,
            "skipped": result.skipped,
            "errors": result.errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not result.errors else 1

    if action == "query":
        q = getattr(args, "query", "") or ""
        if not q.strip():
            print(json.dumps({"ok": False, "error": "query text required"}), file=sys.stderr)
            return 2
        hits = query_cards(
            q,
            agent_root=agent_root,
            limit=int(getattr(args, "limit", 5) or 5),
            kind=getattr(args, "kind", None) or None,
        )
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "ok": True,
                        "hits": [
                            {
                                "id": h.card.id,
                                "title": h.card.title,
                                "kind": h.card.kind,
                                "path": h.card.path,
                                "score": h.score,
                                "pointer": h.pointer,
                                "summary": h.card.summary,
                            }
                            for h in hits
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(format_query_hits(hits))
        return 0

    if action == "write-card":
        card = BrainCard(
            id=str(getattr(args, "id") or "card"),
            title=str(getattr(args, "title") or getattr(args, "id") or "card"),
            kind=str(getattr(args, "kind", None) or "other"),
            path=str(getattr(args, "path") or ""),
            summary=str(getattr(args, "summary") or ""),
            tags=[t for t in str(getattr(args, "tags") or "").split(",") if t.strip()],
            source="agent",
        )
        path = write_card(agent_root / "cards", card)
        print(json.dumps({"ok": True, "path": str(path)}, ensure_ascii=False))
        return 0

    print(json.dumps({"ok": False, "error": f"unknown brain action {action}"}))
    return 1


def cmd_projects(settings: Settings, args: argparse.Namespace) -> int:
    reg = ProjectRegistry(settings.root / "projects.json")
    if args.action == "add":
        reg.add(Project(name=args.name, repo_path=args.repo, base_branch=args.base))
        print(f"добавлен проект {args.name}")
    else:
        for p in reg.list():
            print(f"  {p.name:<20} {p.repo_path}  ({p.base_branch})")
    return 0


def cmd_prune(settings: Settings, args: argparse.Namespace) -> int:
    """Retention for finished runs. Previews unless --apply is given."""
    from harness.store.retention import (  # noqa: PLC0415
        apply_prune,
        plan_prune,
        render_plan_markdown,
    )

    store = _store(settings)
    try:
        plan = plan_prune(
            store,
            harness_root=settings.root,
            keep=int(getattr(args, "keep", 20)),
            project=getattr(args, "project", None),
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1

    if not getattr(args, "apply", False):
        if getattr(args, "json", False):
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(render_plan_markdown(plan))
        return 0

    result = apply_prune(
        store, plan, drop_spools=not getattr(args, "keep_spools", False)
    )
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 1 if result["failures"] else 0


def cmd_context(settings: Settings, args: argparse.Namespace) -> int:
    """Собрать (или обновить) project context: структура, стек, гейты, brain.

    Планнер/оркестратор читает markdown по `markdown_path` перед декомпозицией;
    контроллер сам инжектит компактный brief в каждый dispatch.
    """
    from harness.tasktool.context import resolve_brain_root  # noqa: PLC0415
    from harness.tasktool.project_context import load_or_build  # noqa: PLC0415

    project = _resolve_project(settings, getattr(args, "project", None))
    explicit_repo = getattr(args, "repo", None)
    repo_root = (
        Path(explicit_repo).resolve() if explicit_repo else _repo_root(settings, project)
    )
    project_name = project.name if project else repo_root.name
    try:
        context, md_path = load_or_build(
            repo_root,
            project_name,
            brain_root=resolve_brain_root(),
            refresh=bool(getattr(args, "refresh", False)),
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "project": context.project,
                "markdown_path": str(md_path),
                "head_sha": context.head_sha,
                "language": context.language,
                "canon": context.canon,
                "gates": list(context.gates),
                "graphify": context.graphify_hint,
                "brain_adrs": list(context.brain_adrs),
                "brain_incidents": list(context.brain_incidents),
                "lessons_count": context.lessons_count,
                "generated_at": context.generated_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _tasktool_controller(settings: Settings, args: argparse.Namespace) -> object:
    from harness.tasktool.controller import TaskToolController  # noqa: PLC0415

    project = _resolve_project(settings, getattr(args, "project", None))
    explicit_repo = getattr(args, "repo", None)
    repo_root = Path(explicit_repo).resolve() if explicit_repo else _repo_root(settings, project)
    return TaskToolController(settings, _store(settings), repo_root)


def _tasktool_emit(payload: dict[str, object]) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ok", True) else 1


def _tasktool_command(
    settings: Settings,
    args: argparse.Namespace,
    operation: str,
) -> int:
    try:
        controller = _tasktool_controller(settings, args)
        if operation == "start":
            payload = asyncio.run(controller.start(  # type: ignore[attr-defined]
                project=args.project,
                goal=args.goal,
                base_branch=args.base_branch,
                approve_plan=args.approve_plan,
                force=args.force,
                spec_sources=tuple(args.spec_source),
                user=getattr(args, "user", None),
            ))
        elif operation == "next":
            payload = controller.next(args.run_id, args.agent_id, args.limit)  # type: ignore[attr-defined]
        elif operation == "report":
            payload = controller.report(  # type: ignore[attr-defined]
                args.dispatch_id,
                args.result_file,
                args.agent_id,
                ok=not args.error,
                error=args.error or "",
                worker_id=getattr(args, "worker_id", "") or "",
            )
        elif operation == "advance":
            payload = asyncio.run(
                controller.advance(  # type: ignore[attr-defined]
                    args.run_id,
                    approved_merge=args.approved_merge,
                    approve_risk=getattr(args, "approve_risk", False),
                )
            )
        elif operation == "status":
            payload = controller.status(args.run_id)  # type: ignore[attr-defined]
        elif operation == "abort":
            payload = controller.abort(args.run_id, args.reason)  # type: ignore[attr-defined]
        elif operation == "resume":
            answers = None
            raw_answers = getattr(args, "answers", None) or []
            if raw_answers:
                from harness.tasks_io.question import parse_answer_args  # noqa: PLC0415

                answers = parse_answer_args(list(raw_answers))
            payload = asyncio.run(
                controller.resume(  # type: ignore[attr-defined]
                    args.run_id,
                    approve_risk=getattr(args, "approve_risk", False),
                    answers=answers or None,
                    answers_file=getattr(args, "answers_file", None),
                )
            )
        else:
            raise ValueError(f"unknown tasktool operation: {operation}")
    except (KeyError, RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    return _tasktool_emit(payload)


def cmd_tasktool_start(settings: Settings, args: argparse.Namespace) -> int:
    return _tasktool_command(settings, args, "start")


def cmd_tasktool_next(settings: Settings, args: argparse.Namespace) -> int:
    return _tasktool_command(settings, args, "next")


def cmd_tasktool_report(settings: Settings, args: argparse.Namespace) -> int:
    return _tasktool_command(settings, args, "report")


def cmd_tasktool_advance(settings: Settings, args: argparse.Namespace) -> int:
    return _tasktool_command(settings, args, "advance")


def cmd_tasktool_status(settings: Settings, args: argparse.Namespace) -> int:
    return _tasktool_command(settings, args, "status")


def cmd_tasktool_abort(settings: Settings, args: argparse.Namespace) -> int:
    return _tasktool_command(settings, args, "abort")


def cmd_tasktool_resume(settings: Settings, args: argparse.Namespace) -> int:
    return _tasktool_command(settings, args, "resume")


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    p = argparse.ArgumentParser(prog="harness", description="Оркестрация агентов Cursor")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("plan", help="оркестратор пишет PLAN.md + tasks/*.md")
    sp.add_argument("goal")
    sp.add_argument("--project", default=None,
                    help="имя проекта из реестра — CWD оркестратора = репо-цель")
    sp.add_argument("--verbose", "-v", action="store_true",
                    help="показывать прогресс (heartbeat) во время планирования")
    sp.set_defaults(func=cmd_plan)

    si = sub.add_parser("ingest", help="загрузить tasks/ в store как новый Run")
    si.add_argument("goal")
    si.add_argument("--project", default="default")
    si.add_argument("--force", action="store_true",
                    help="создать новый Run даже при существующем активном (v2-005) "
                         "и пропустить plan-verification (v2-018)")
    si.set_defaults(func=cmd_ingest)

    sver = sub.add_parser("verify", help="проверить PLAN.md + tasks/ перед ingest (v2-018)")
    sver.add_argument("--project", default=None, help="имя проекта из реестра (репо-цель)")
    sver.set_defaults(func=cmd_verify)

    sr = sub.add_parser("run", help="исполнить Run")
    sr.add_argument("run_id", nargs="?", default=None)
    sr.add_argument("--project", default=None, help="имя проекта из реестра (репо-цель)")
    sr.add_argument("--approve-plan", action="store_true",
                    help="подтвердить PLAN_DRAFT и запустить (v2-023)")
    sr.set_defaults(func=cmd_run)

    sa = sub.add_parser("approve", help="подтвердить отложенный мерж (human-gate) и продолжить")
    sa.add_argument("run_id", nargs="?", default=None)
    sa.add_argument("task_id")
    sa.add_argument("--project", default=None)
    sa.set_defaults(func=cmd_approve)

    ss = sub.add_parser("status", help="статусы задач")
    ss.add_argument("run_id", nargs="?", default=None)
    ss.add_argument("--project", default=None, help="имя проекта из реестра (репо-цель)")
    ss.add_argument("--markdown", action="store_true",
                    help="вывести статус как markdown (v2-012)")
    ss.add_argument("--write", default=None,
                    help="путь файла для --markdown (atomic write; иначе stdout)")
    ss.set_defaults(func=cmd_status)

    se = sub.add_parser("events", help="журнал событий")
    se.add_argument("run_id", nargs="?", default=None)
    se.add_argument("--project", default=None, help="имя проекта из реестра (репо-цель)")
    se.set_defaults(func=cmd_events)

    stail = sub.add_parser("tail", help="live-tail event-log (long-poll)")
    stail.add_argument("run_id", nargs="?", default=None)
    stail.add_argument("--project", default=None, help="имя проекта из реестра (репо-цель)")
    stail.add_argument("--task", default=None, help="фильтр по task_id")
    stail.add_argument("--kind", default=None,
                       help="фильтр по типу события (event type или agent kind)")
    stail.add_argument("--agent-events", action="store_true",
                       help="включить agent_events транскрипт в поток")
    stail.add_argument("--interval", type=float, default=0.5,
                       help="интервал опроса БД, сек (default 0.5)")
    stail.set_defaults(func=cmd_tail)

    sin = sub.add_parser("init", help="подготовить репозиторий: git + .harness/project.toml")
    sin.add_argument("--repo", default=None, help="путь к целевому репо (иначе HARNESS_ROOT)")
    sin.add_argument("--language", default=None, help="python|typescript|go|rust (иначе авто)")
    sin.add_argument("--base", default="main")
    sin.set_defaults(func=cmd_init)

    sd = sub.add_parser(
        "doctor",
        help="проверка готовности + V4 diagnose (false-coverage / multi-tenant / soft-gates)",
    )
    sd.add_argument("--project", default=None)
    sd.add_argument(
        "--false-coverage-report",
        action="store_true",
        help="FSM/pipeline%% vs pass@k vs evidence%% stub (Phase 0)",
    )
    sd.add_argument(
        "--multi-tenant-report",
        action="store_true",
        help=".active locks, PLAN collision, worktrees, soft gates",
    )
    sd.add_argument(
        "--soft-gates-report",
        action="store_true",
        help="inventory soft/optional gates (scaffold + project.toml)",
    )
    sd.add_argument(
        "--meta-eval",
        action="store_true",
        help="AHE-lite prediction vs outcome (Phase 4)",
    )
    sd.add_argument("--run", default=None, help="ограничить false-coverage одним run_id")
    sd.add_argument("--limit", type=int, default=8, help="сколько последних runs для false-coverage")
    sd.add_argument(
        "--repo",
        action="append",
        default=[],
        help="доп. репо для multi-tenant / soft-gates (repeatable)",
    )
    sd.add_argument("--json", action="store_true", help="JSON вместо markdown (где применимо)")
    sd.add_argument("--write", default=None, help="путь файла для отчёта")
    sd.set_defaults(func=cmd_doctor)

    ssk = sub.add_parser(
        "skills",
        help="V4 curated catalog: catalog | select | stocktake | gc",
    )
    ssk_sub = ssk.add_subparsers(dest="skills_action")
    ssk_cat = ssk_sub.add_parser("catalog", help="показать skills/catalog.json")
    ssk_cat.add_argument("--catalog", default=None, help="путь к catalog.json")
    ssk_cat.set_defaults(func=cmd_skills)
    ssk_sel = ssk_sub.add_parser("select", help="выбрать 1–3 skill_paths для роли/stage")
    ssk_sel.add_argument("--role", default="worker")
    ssk_sel.add_argument("--stage", default="")
    ssk_sel.add_argument("--gate-fail", action="store_true")
    ssk_sel.add_argument("--limit", type=int, default=None)
    ssk_sel.add_argument("--catalog", default=None)
    ssk_sel.set_defaults(func=cmd_skills)
    ssk_st = ssk_sub.add_parser("stocktake", help="алиас harness stocktake")
    ssk_st.add_argument("--write", default=None)
    ssk_st.set_defaults(func=cmd_skills, skills_action="stocktake")
    ssk_gc = ssk_sub.add_parser(
        "gc",
        help="Phase 4 skill GC: propose/apply quarantine (never auto-delete)",
    )
    ssk_gc_sub = ssk_gc.add_subparsers(dest="gc_action")
    ssk_gc_prop = ssk_gc_sub.add_parser("propose", help="stocktake unused catalog skills")
    ssk_gc_prop.add_argument("--catalog", default=None)
    ssk_gc_prop.add_argument("--max-selects", type=int, default=0)
    ssk_gc_prop.add_argument("--include-human-only", action="store_true")
    ssk_gc_prop.add_argument(
        "--include-core",
        action="store_true",
        help="allow proposing core pack skills (dangerous)",
    )
    ssk_gc_prop.add_argument("--json", action="store_true")
    ssk_gc_prop.add_argument("--write", default=None)
    ssk_gc_prop.set_defaults(func=cmd_skills, skills_action="gc", gc_action="propose")
    ssk_gc_apply = ssk_gc_sub.add_parser(
        "apply",
        help="quarantine skills (requires --approve; never mid-run auto)",
    )
    ssk_gc_apply.add_argument("--catalog", default=None)
    ssk_gc_apply.add_argument("--ids", default="", help="comma-separated skill ids")
    ssk_gc_apply.add_argument("--approve", action="store_true")
    ssk_gc_apply.add_argument("--note", default="")
    ssk_gc_apply.add_argument("--max-selects", type=int, default=0)
    ssk_gc_apply.add_argument("--include-human-only", action="store_true")
    ssk_gc_apply.add_argument("--include-core", action="store_true")
    ssk_gc_apply.set_defaults(func=cmd_skills, skills_action="gc", gc_action="apply")
    ssk_gc_restore = ssk_gc_sub.add_parser("restore", help="restore quarantined → active")
    ssk_gc_restore.add_argument("--catalog", default=None)
    ssk_gc_restore.add_argument("--ids", required=True)
    ssk_gc_restore.add_argument("--approve", action="store_true")
    ssk_gc_restore.set_defaults(func=cmd_skills, skills_action="gc", gc_action="restore")
    ssk_gc_list = ssk_gc_sub.add_parser("list", help="list quarantined catalog skills")
    ssk_gc_list.add_argument("--catalog", default=None)
    ssk_gc_list.add_argument("--all", action="store_true", help="list all statuses")
    ssk_gc_list.set_defaults(func=cmd_skills, skills_action="gc", gc_action="list")
    ssk_gc.set_defaults(func=cmd_skills, skills_action="gc", gc_action="propose")
    ssk.set_defaults(func=cmd_skills, skills_action="catalog")

    smeta = sub.add_parser(
        "meta",
        help="Phase 4 AHE-lite: changelog predictions + eval vs run outcomes",
    )
    meta_sub = smeta.add_subparsers(dest="meta_action")
    meta_ch = meta_sub.add_parser("changelog", help="list or add falsifiable harness edits")
    meta_ch_sub = meta_ch.add_subparsers(dest="changelog_action")
    meta_ch_list = meta_ch_sub.add_parser("list", help="list changelog entries")
    meta_ch_list.add_argument("--changelog", default=None)
    meta_ch_list.set_defaults(func=cmd_meta, meta_action="changelog", changelog_action="list")
    meta_ch_add = meta_ch_sub.add_parser("add", help="record harness change + predicted metrics")
    meta_ch_add.add_argument("--change", required=True)
    meta_ch_add.add_argument("--hypothesis", default="")
    meta_ch_add.add_argument("--components", default="")
    meta_ch_add.add_argument("--predict-evidence-min", type=float, default=None)
    meta_ch_add.add_argument("--predict-evidence-delta", type=float, default=None)
    meta_ch_add.add_argument("--predict-tokens-max", type=int, default=None)
    meta_ch_add.add_argument("--predict-tokens-delta-pct", type=float, default=None)
    meta_ch_add.add_argument("--predict-stall-max", type=int, default=None)
    meta_ch_add.add_argument("--baseline-runs", default="")
    meta_ch_add.add_argument("--changelog", default=None)
    meta_ch_add.set_defaults(func=cmd_meta, meta_action="changelog", changelog_action="add")
    meta_ch.set_defaults(func=cmd_meta, meta_action="changelog", changelog_action="list")
    meta_rec = meta_sub.add_parser("record", help="attach actual run metrics to changelog / snapshot")
    meta_rec.add_argument("--run-id", required=True)
    meta_rec.add_argument("--changelog-id", default=None)
    meta_rec.add_argument("--status", default=None, help="optional status patch (open|verified|…)")
    meta_rec.add_argument("--changelog", default=None)
    meta_rec.add_argument("--project", default=None)
    meta_rec.add_argument("--repo", default=None)
    meta_rec.set_defaults(func=cmd_meta, meta_action="record")
    meta_eval = meta_sub.add_parser("eval", help="prediction vs outcome report")
    meta_eval.add_argument("--changelog", default=None)
    meta_eval.add_argument("--limit", type=int, default=5)
    meta_eval.add_argument("--ids", default="", help="comma-separated changelog ids")
    meta_eval.add_argument("--project", default=None)
    meta_eval.add_argument("--repo", default=None)
    meta_eval.add_argument("--json", action="store_true")
    meta_eval.add_argument("--write", default=None)
    meta_eval.set_defaults(func=cmd_meta, meta_action="eval")
    smeta.set_defaults(func=cmd_meta, meta_action="eval")

    sten = sub.add_parser("tenant", help="multi-tenant lease index (Phase 1)")
    ten_sub = sten.add_subparsers(dest="tenant_action")
    ten_list = ten_sub.add_parser("list", help="list active leases for a repo")
    ten_list.add_argument("--repo", default=None)
    ten_list.set_defaults(func=cmd_tenant, tenant_action="list")
    sten.set_defaults(func=cmd_tenant, tenant_action="list")

    sev = sub.add_parser("evidence", help="evidence ledger (Phase 1.5 Honesty)")
    ev_sub = sev.add_subparsers(dest="evidence_action")
    ev_list = ev_sub.add_parser("list", help="list ledger.jsonl rows")
    ev_list.add_argument("--run-id", required=True)
    ev_list.add_argument("--repo", default=None)
    ev_list.set_defaults(func=cmd_evidence, evidence_action="list")
    ev_add = ev_sub.add_parser("add", help="append evidence row (control plane / CLI)")
    ev_add.add_argument("--run-id", required=True)
    ev_add.add_argument("--repo", default=None)
    ev_add.add_argument("--task-id", default="")
    ev_add.add_argument("--gate", default="")
    ev_add.add_argument("--kind", default="gate")
    ev_add.add_argument("--exit-code", type=int, default=0)
    ev_add.add_argument("--log", default="")
    ev_add.add_argument(
        "--acceptance-item",
        action="append",
        default=[],
        help="acceptance item id covered by this evidence (repeatable)",
    )
    ev_add.set_defaults(func=cmd_evidence, evidence_action="add")
    sev.set_defaults(func=cmd_evidence, evidence_action="list")

    sacc = sub.add_parser("acceptance", help="acceptance ledger (Phase 1.5 Honesty)")
    acc_sub = sacc.add_subparsers(dest="acceptance_action")
    acc_show = acc_sub.add_parser("show", help="show acceptance.json + evidence%")
    acc_show.add_argument("--run-id", required=True)
    acc_show.add_argument("--repo", default=None)
    acc_show.set_defaults(func=cmd_acceptance, acceptance_action="show")
    acc_flip = acc_sub.add_parser(
        "flip", help="flip item passes=true when green evidence exists"
    )
    acc_flip.add_argument("--run-id", required=True)
    acc_flip.add_argument("--repo", default=None)
    acc_flip.add_argument("--item", required=True)
    acc_flip.add_argument(
        "--evidence",
        action="append",
        default=[],
        required=True,
        help="evidence id (repeatable)",
    )
    acc_flip.set_defaults(func=cmd_acceptance, acceptance_action="flip")
    acc_waive = acc_sub.add_parser("waive", help="human waive an acceptance item")
    acc_waive.add_argument("--run-id", required=True)
    acc_waive.add_argument("--repo", default=None)
    acc_waive.add_argument("--item", required=True)
    acc_waive.add_argument("--reason", default="human waive")
    acc_waive.set_defaults(func=cmd_acceptance, acceptance_action="waive")
    sacc.set_defaults(func=cmd_acceptance, acceptance_action="show")

    sbr = sub.add_parser(
        "brain",
        help="Phase 3 brain-agents: sync | query | write-card (never writes human /home/brain)",
    )
    br_sub = sbr.add_subparsers(dest="brain_action")
    br_sync = br_sub.add_parser("sync", help="sync selected dirs from canon → agent layer + cards")
    br_sync.add_argument("--canon", default=None, help="human vault (default HARNESS_BRAIN_CANON)")
    br_sync.add_argument(
        "--agent-root",
        default=None,
        help="agent layer (default HARNESS_BRAIN_ROOT=/home/brain-agents)",
    )
    br_sync.set_defaults(func=cmd_brain, brain_action="sync")
    br_q = br_sub.add_parser("query", help="search agent cards by keywords")
    br_q.add_argument("query", nargs="?", default="", help="search text")
    br_q.add_argument("--limit", type=int, default=5)
    br_q.add_argument("--kind", default=None, help="filter: adr|architecture|incident|…")
    br_q.add_argument("--agent-root", default=None)
    br_q.add_argument("--json", action="store_true")
    br_q.set_defaults(func=cmd_brain, brain_action="query")
    br_wc = br_sub.add_parser("write-card", help="write an agent-layer card (not into human canon)")
    br_wc.add_argument("--id", required=True)
    br_wc.add_argument("--title", default="")
    br_wc.add_argument("--kind", default="other")
    br_wc.add_argument("--path", default="")
    br_wc.add_argument("--summary", default="")
    br_wc.add_argument("--tags", default="")
    br_wc.add_argument("--agent-root", default=None)
    br_wc.set_defaults(func=cmd_brain, brain_action="write-card")
    sbr.set_defaults(func=cmd_brain, brain_action="query")

    sscan = sub.add_parser(
        "scan", help="AgentShield-скан prompts/.cursor на injection/secret-leak (v2-031)",
    )
    sscan.add_argument("--opus", action="store_true",
                       help="+ adversarial red-team/blue-team на Opus (дорого, opt-in)")
    sscan.add_argument("--write", default=None, help="путь файла для отчёта (иначе stdout)")
    sscan.set_defaults(func=cmd_scan)

    sb = sub.add_parser("bot", help="запустить Telegram-бота (команды /status /approve /runs)")
    sb.set_defaults(func=cmd_bot)

    sdash = sub.add_parser("dashboard", help="запустить web-дашборд (v2-014)")
    sdash.add_argument("--host", default="127.0.0.1")
    sdash.add_argument("--port", type=int, default=8420)
    sdash.set_defaults(func=cmd_dashboard)

    schat = sub.add_parser(
        "chat",
        help="инструкция для chat-режима (v2-022b, Cursor IDE Task tool bridge)",
    )
    schat.set_defaults(func=cmd_chat)

    smode = sub.add_parser(
        "mode",
        help="переключить run <-> chat режим одной командой (v2-022c)",
    )
    smode.add_argument("mode_action", choices=["run", "chat", "show"], default="show", nargs="?")
    smode.set_defaults(func=cmd_mode)

    sans = sub.add_parser("answer", help="ответить на question-protocol (v2-020)")
    sans.add_argument("run_id", nargs="?", default=None)
    sans.add_argument("task_id")
    sans.add_argument("--project", default=None)
    sans.add_argument("answers", nargs="*", help="q1=\"value\" q2=\"...\"")
    sans.set_defaults(func=cmd_answer)

    sst = sub.add_parser(
        "stocktake", help="аудит prompts/.cursor/skills — usage/размер (v2-035)",
    )
    sst.add_argument("--write", default=None, help="путь файла для отчёта (иначе stdout)")
    sst.set_defaults(func=cmd_stocktake)

    sctx = sub.add_parser(
        "context",
        help="собрать project context (структура, стек, brain) для планнера/агентов",
    )
    sctx.add_argument("--project", default=None, help="имя проекта из реестра")
    sctx.add_argument("--repo", default=None, help="явный путь к репо (вместо реестра)")
    sctx.add_argument("--refresh", action="store_true", help="игнорировать кэш")
    sctx.set_defaults(func=cmd_context)

    sprune = sub.add_parser(
        "prune",
        help="удалить старые завершённые run'ы (превью по умолчанию, --apply для удаления)",
    )
    sprune.add_argument(
        "--keep",
        type=int,
        default=20,
        help="сколько последних завершённых run'ов сохранить (default 20)",
    )
    sprune.add_argument("--project", default=None, help="ограничить одним проектом")
    sprune.add_argument(
        "--apply", action="store_true", help="выполнить удаление (без флага — только превью)"
    )
    sprune.add_argument(
        "--keep-spools",
        action="store_true",
        help="удалить только строки БД, оставив evidence/acceptance на диске",
    )
    sprune.add_argument("--json", action="store_true", help="вывести план как JSON")
    sprune.set_defaults(func=cmd_prune)

    spj = sub.add_parser("projects", help="реестр проектов")
    spj.add_argument("action", choices=["list", "add"], default="list", nargs="?")
    spj.add_argument("--name")
    spj.add_argument("--repo")
    spj.add_argument("--base", default="main")
    spj.set_defaults(func=cmd_projects)

    stt = sub.add_parser("tasktool", help="pull-based TaskTool control plane")
    tasktool = stt.add_subparsers(dest="tasktool_action", required=True)

    tt_start = tasktool.add_parser("start", help="verify and freeze existing PLAN/tasks")
    tt_start.add_argument("goal")
    tt_start.add_argument("--project", default="default")
    tt_start.add_argument("--repo", default=None)
    tt_start.add_argument("--base-branch", default=None)
    tt_start.add_argument("--spec-source", action="append", default=[])
    tt_start.add_argument("--approve-plan", action="store_true")
    tt_start.add_argument("--force", action="store_true")
    tt_start.add_argument(
        "--user",
        default=None,
        help="tenant / lease owner (default: HARNESS_TENANT_ID or $USER)",
    )
    tt_start.add_argument(
        "--run-id",
        default=None,
        help="stub: reserved for resume-by-id / multi-tenant CLI (Phase 1)",
    )
    tt_start.set_defaults(func=cmd_tasktool_start)

    tt_next = tasktool.add_parser("next", help="claim ready agent dispatches")
    tt_next.add_argument("run_id")
    tt_next.add_argument("--project", default=None)
    tt_next.add_argument("--repo", default=None)
    tt_next.add_argument("--limit", type=int, default=12)
    tt_next.add_argument("--agent-id", required=True)
    tt_next.set_defaults(func=cmd_tasktool_next)

    tt_report = tasktool.add_parser("report", help="report a file-backed task result")
    tt_report.add_argument("dispatch_id")
    tt_report.add_argument("--project", default=None)
    tt_report.add_argument("--repo", default=None)
    tt_report.add_argument("--result-file", required=True)
    tt_report.add_argument("--agent-id", required=True)
    tt_report.add_argument(
        "--worker-id",
        default="",
        help=(
            "identity of the Task Tool job that did the work (defaults to --agent-id). "
            "Pass a distinct id per worker so the anti-cosplay boundary check sees "
            "real executors while the root chat keeps holding the lease"
        ),
    )
    outcome = tt_report.add_mutually_exclusive_group()
    outcome.add_argument("--ok", action="store_true", default=True)
    outcome.add_argument("--error", default="")
    tt_report.set_defaults(func=cmd_tasktool_report)

    tt_advance = tasktool.add_parser("advance", help="apply deterministic lifecycle stages")
    tt_advance.add_argument("run_id")
    tt_advance.add_argument("--project", default=None)
    tt_advance.add_argument("--repo", default=None)
    tt_advance.add_argument(
        "--approved-merge",
        action="store_true",
        help="ship approval only: merge tasks already in merge_queue",
    )
    tt_advance.add_argument(
        "--approve-risk",
        action="store_true",
        help="risk approval only: clear the persisted risk gate (never ship)",
    )
    tt_advance.set_defaults(func=cmd_tasktool_advance)

    tt_status = tasktool.add_parser("status")
    tt_status.add_argument("run_id")
    tt_status.add_argument("--project", default=None)
    tt_status.add_argument("--repo", default=None)
    tt_status.set_defaults(func=cmd_tasktool_status)

    tt_resume = tasktool.add_parser("resume")
    tt_resume.add_argument("run_id")
    tt_resume.add_argument("--project", default=None)
    tt_resume.add_argument("--repo", default=None)
    tt_resume.add_argument(
        "--approve-risk",
        action="store_true",
        help="risk approval only: clear the persisted risk gate (never ship)",
    )
    tt_resume.add_argument(
        "--answers-file",
        default=None,
        help="JSON answers file under the run spool (.harness/tasktool/<run>/...)",
    )
    tt_resume.add_argument(
        "--answer",
        action="append",
        default=[],
        dest="answers",
        help='inline answer as q1=value (repeatable; never conflated with ship approval)',
    )
    tt_resume.set_defaults(func=cmd_tasktool_resume)

    tt_abort = tasktool.add_parser("abort")
    tt_abort.add_argument("run_id")
    tt_abort.add_argument("--project", default=None)
    tt_abort.add_argument("--repo", default=None)
    tt_abort.add_argument("--reason", default="aborted by user")
    tt_abort.set_defaults(func=cmd_tasktool_abort)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # HARNESS_HOME — alias for HARNESS_ROOT (v4 discoverability); ROOT wins if both set.
    if "HARNESS_ROOT" not in os.environ and os.environ.get("HARNESS_HOME"):
        os.environ["HARNESS_ROOT"] = os.environ["HARNESS_HOME"]
    root = Path(os.environ.get("HARNESS_ROOT", ".")).resolve()
    # v2-022c: mode.env грузится ПЕРВЫМ — задаёт HARNESS_RUNNER (master switch),
    # если явные ORCH_RUNNER/WORKER_RUNNER/REVIEWER_RUNNER не заданы отдельно в
    # .env, они возьмут его (см. `harness mode`, `Settings._runner_kind`).
    load_dotenv(root / ".harness" / "mode.env")
    load_dotenv(root / ".env")  # turnkey: .env подхватывается автоматически
    settings = Settings()
    settings.root.joinpath("logs").mkdir(parents=True, exist_ok=True)
    func = args.func
    return int(func(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
