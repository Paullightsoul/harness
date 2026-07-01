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
import os
import shutil
import subprocess
import sys
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import Role, RunnerKind
from harness.dotenv import load_dotenv
from harness.ingest import ingest_run
from harness.projects.registry import Project, ProjectRegistry
from harness.runner.factory import build_runner
from harness.scaffold import default_project_toml, detect_language
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
    row = store._conn.execute(  # noqa: SLF001 (служебный доступ в CLI допустим)
        "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    role = settings.role(Role.ORCHESTRATOR)
    runner = build_runner(role.runner)
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
        brain_root = settings.root.parent / "brain"
        lessons_block = format_lessons_for_plan(list_recent_lessons(brain_root, project_name))
    except Exception:  # noqa: BLE101 (lessons не должны валить plan)
        pass
    prompt = (
        f"{role_prompt}\n\n=== GOAL ===\n{args.goal}\n"
        f"{repo_block}\n"
        f"{lessons_block}\n"
        f"=== ШАБЛОН ЗАДАЧИ (соблюдай строго) ===\n{template}\n"
    )
    res = asyncio.run(runner.run(
        prompt, model=role.model, cwd=repo_root,
        log_path=settings.root / "logs" / "orchestrator.log",
    ))
    print(res.text)
    if not res.ok:
        print(f"планирование завершилось с ошибкой: {res.error}", file=sys.stderr)
        return 1
    print("\n✅ Планирование готово. Проверь PLAN.md и tasks/, затем: harness ingest / run")
    return 0


def cmd_verify(settings: Settings, args: argparse.Namespace) -> int:
    """v2-018: детерминированная проверка PLAN.md + tasks/ перед ingest."""
    from harness.tasks_io.verifier import verify_plan  # noqa: PLC0415
    report = verify_plan(settings.root)
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
    report = verify_plan(settings.root)
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
        force=getattr(args, "force", False),
    )
    reused = store.get_run(run_id)
    is_reused = reused is not None and reused.status != "planning"
    marker = " [reused]" if is_reused else ""
    print(f"создан run: {run_id}{marker}" + (f" (проект {project.name})" if project else ""))
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
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
    engine = Engine(settings, store, repo_root=_repo_root(settings, project))
    asyncio.run(engine.run(run_id))
    return cmd_status(settings, argparse.Namespace(
        run_id=run_id, markdown=False, write=None, project=None,
    ))


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


def _format_status_markdown(run_id: str, store: Store) -> str:
    """v2-012: portable markdown-снапшот для handoff между сменами/чата.

    Секции: readiness, run summary, tasks table, бюджет, последние события.
    """
    run = store.get_run(run_id)
    assert run is not None
    tasks = store.list_tasks(run_id)
    done = sum(1 for t in tasks if t.status == "done")
    blocked = [t.id for t in tasks if t.status == "blocked"]
    ready = sum(1 for t in tasks if t.status == "ready")
    running = sum(1 for t in tasks if t.status == "running")

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
        "",
        "## Readiness",
        f"- total tasks: **{len(tasks)}**",
        f"- done: **{done}** / ready: {ready} / running: {running} / blocked: {len(blocked)}",
        f"- completion: **{done / len(tasks) * 100:.0f}%**" if tasks else "- completion: —",
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
    run_id = getattr(args, "run_id", None) or _latest_run_id(store)
    if not run_id:
        print("нет Run", file=sys.stderr)
        return 1
    run = store.get_run(run_id)
    assert run is not None

    # v2-012: markdown-режим для portable handoff (--markdown / --write <path>).
    if getattr(args, "markdown", False):
        md = _format_status_markdown(run_id, store)
        write_path = getattr(args, "write", None)
        if write_path:
            target = Path(write_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(md, encoding="utf-8")
            print(f"статус записан в {target}")
        else:
            print(md)
        return 0

    print(f"run {run_id} [{run.status}]  потрачено: {run.spent_credits:.2f}"
          + (f"/{run.budget_credits:.2f}" if run.budget_credits else ""))
    metrics_line = _format_metrics_line(store, run_id)
    if metrics_line:
        print(f"  {metrics_line}")
    for t in store.list_tasks(run_id):
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
    run_id = args.run_id or _latest_run_id(store)
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
    run_id = args.run_id or _latest_run_id(store)
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
            ".harness/state.db*\n.worktrees/\nlogs/\n", encoding="utf-8"
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
        runner = build_runner(role.runner)
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


def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    """Проверка готовности окружения к работе."""
    project = _resolve_project(settings, getattr(args, "project", None))
    repo = _repo_root(settings, project)
    rows: list[tuple[str, bool, str]] = []

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

    if os.environ.get("HARNESS_DURABLE") == "1":
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
        mark = "✅" if ok else "❌"
        if not ok:
            all_ok = False
        print(f"  {mark} {name:<28} {detail}")
    print("\n" + ("Готово к работе." if all_ok else "Есть проблемы — поправь ❌ выше."))
    if scan_report.critical:
        print(
            f"\n❌ КРИТИЧНО: {len(scan_report.critical)} security finding(s) в "
            "prompts/.cursor — смотри `harness scan`", file=sys.stderr,
        )
        return 2
    return 0 if all_ok else 1


def cmd_chat(settings: Settings, args: argparse.Namespace) -> int:
    """v2-022: chat-режим — оркестратор в Cursor IDE, воркеры через Task tool.

    Текущая реализация: печатает инструкцию для запуска из Cursor IDE с
    подключённым bridge. Реальная интеграция (live Task tool calls) — v2-022b.
    """
    msg = f"""harness chat — режим оркестрации через Cursor IDE Task tool

Для запуска:
1. Открой этот каталог в Cursor IDE: cd {settings.root}
2. В новом чате вставь промпт:
   "Я оркестратор harness. Прочитай GUIDE.md и PLAN.md. Для каждой
   ready-задачи вызови Task tool с subagent_type=generalPurpose,
   prompt=<Task Brief из tasks/task-<id>.md>. Гейты/мерж/бюджет —
   на harness (HARNESS_RUNNER=cursor_task)."
3. Установи env: HARNESS_RUNNER=cursor_task HARNESS_CHAT_BRIDGE=<path>
4. Запусти: harness run --approve-plan

Реальная bridge-интеграция (live Task tool) — v2-022b (follow-up).
Сейчас CursorTaskRunner вернёт понятную ошибку при вызове.
"""
    print(msg)
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


def cmd_projects(settings: Settings, args: argparse.Namespace) -> int:
    reg = ProjectRegistry(settings.root / "projects.json")
    if args.action == "add":
        reg.add(Project(name=args.name, repo_path=args.repo, base_branch=args.base))
        print(f"добавлен проект {args.name}")
    else:
        for p in reg.list():
            print(f"  {p.name:<20} {p.repo_path}  ({p.base_branch})")
    return 0


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    p = argparse.ArgumentParser(prog="harness", description="Оркестрация агентов Cursor")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("plan", help="оркестратор пишет PLAN.md + tasks/*.md")
    sp.add_argument("goal")
    sp.add_argument("--project", default=None,
                    help="имя проекта из реестра — CWD оркестратора = репо-цель")
    sp.set_defaults(func=cmd_plan)

    si = sub.add_parser("ingest", help="загрузить tasks/ в store как новый Run")
    si.add_argument("goal")
    si.add_argument("--project", default="default")
    si.add_argument("--force", action="store_true",
                    help="создать новый Run даже при существующем активном (v2-005) "
                         "и пропустить plan-verification (v2-018)")
    si.set_defaults(func=cmd_ingest)

    sver = sub.add_parser("verify", help="проверить PLAN.md + tasks/ перед ingest (v2-018)")
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
    ss.add_argument("--markdown", action="store_true",
                    help="вывести статус как markdown (v2-012)")
    ss.add_argument("--write", default=None,
                    help="путь файла для --markdown (atomic write; иначе stdout)")
    ss.set_defaults(func=cmd_status)

    se = sub.add_parser("events", help="журнал событий")
    se.add_argument("run_id", nargs="?", default=None)
    se.set_defaults(func=cmd_events)

    stail = sub.add_parser("tail", help="live-tail event-log (long-poll)")
    stail.add_argument("run_id", nargs="?", default=None)
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

    sd = sub.add_parser("doctor", help="проверка готовности окружения")
    sd.add_argument("--project", default=None)
    sd.set_defaults(func=cmd_doctor)

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

    schat = sub.add_parser("chat", help="инструкция для chat-режима (v2-022, Cursor IDE Task tool)")
    schat.set_defaults(func=cmd_chat)

    sans = sub.add_parser("answer", help="ответить на question-protocol (v2-020)")
    sans.add_argument("run_id", nargs="?", default=None)
    sans.add_argument("task_id")
    sans.add_argument("--project", default=None)
    sans.add_argument("answers", nargs="*", help="q1=\"value\" q2=\"...\"")
    sans.set_defaults(func=cmd_answer)

    spj = sub.add_parser("projects", help="реестр проектов")
    spj.add_argument("action", choices=["list", "add"], default="list", nargs="?")
    spj.add_argument("--name")
    spj.add_argument("--repo")
    spj.add_argument("--base", default="main")
    spj.set_defaults(func=cmd_projects)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(os.environ.get("HARNESS_ROOT", ".")).resolve()
    load_dotenv(root / ".env")  # turnkey: .env подхватывается автоматически
    settings = Settings()
    settings.root.joinpath("logs").mkdir(parents=True, exist_ok=True)
    func = args.func
    return int(func(settings, args))


if __name__ == "__main__":
    raise SystemExit(main())
