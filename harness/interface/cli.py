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
    prompt = (
        f"{role_prompt}\n\n=== GOAL ===\n{args.goal}\n\n"
        f"=== ШАБЛОН ЗАДАЧИ (соблюдай строго) ===\n{template}\n"
    )
    res = asyncio.run(runner.run(
        prompt, model=role.model, cwd=settings.root,
        log_path=settings.root / "logs" / "orchestrator.log",
    ))
    print(res.text)
    if not res.ok:
        print(f"планирование завершилось с ошибкой: {res.error}", file=sys.stderr)
        return 1
    print("\n✅ Планирование готово. Проверь PLAN.md и tasks/, затем: harness ingest / run")
    return 0


def cmd_ingest(settings: Settings, args: argparse.Namespace) -> int:
    store = _store(settings)
    project = _resolve_project(settings, args.project)
    base = project.base_branch if project else None
    run_id = ingest_run(settings, store, project=args.project, goal=args.goal, base_branch=base)
    print(f"создан run: {run_id}" + (f" (проект {project.name})" if project else ""))
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    store = _store(settings)
    run_id = args.run_id or _latest_run_id(store)
    if not run_id:
        print("нет ни одного Run. Сначала: harness ingest \"<goal>\"", file=sys.stderr)
        return 1
    project = _resolve_project(settings, args.project)
    engine = Engine(settings, store, repo_root=_repo_root(settings, project))
    asyncio.run(engine.run(run_id))
    return cmd_status(settings, argparse.Namespace(run_id=run_id))


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


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    store = _store(settings)
    run_id = getattr(args, "run_id", None) or _latest_run_id(store)
    if not run_id:
        print("нет Run", file=sys.stderr)
        return 1
    run = store.get_run(run_id)
    assert run is not None
    print(f"run {run_id} [{run.status}]  потрачено: {run.spent_credits:.2f}"
          + (f"/{run.budget_credits:.2f}" if run.budget_credits else ""))
    for t in store.list_tasks(run_id):
        deps = ",".join(t.depends_on) or "-"
        print(f"  task-{t.id:<5} {t.status:<12} deps[{deps}] attempts={t.attempts}  {t.title}")
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


def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    """Проверка готовности окружения к работе."""
    project = _resolve_project(settings, getattr(args, "project", None))
    repo = _repo_root(settings, project)
    rows: list[tuple[str, bool, str]] = []

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
    return 0 if all_ok else 1


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness", description="Оркестрация агентов Cursor")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("plan", help="оркестратор пишет PLAN.md + tasks/*.md")
    sp.add_argument("goal")
    sp.set_defaults(func=cmd_plan)

    si = sub.add_parser("ingest", help="загрузить tasks/ в store как новый Run")
    si.add_argument("goal")
    si.add_argument("--project", default="default")
    si.set_defaults(func=cmd_ingest)

    sr = sub.add_parser("run", help="исполнить Run")
    sr.add_argument("run_id", nargs="?", default=None)
    sr.add_argument("--project", default=None, help="имя проекта из реестра (репо-цель)")
    sr.set_defaults(func=cmd_run)

    sa = sub.add_parser("approve", help="подтвердить отложенный мерж (human-gate) и продолжить")
    sa.add_argument("run_id", nargs="?", default=None)
    sa.add_argument("task_id")
    sa.add_argument("--project", default=None)
    sa.set_defaults(func=cmd_approve)

    ss = sub.add_parser("status", help="статусы задач")
    ss.add_argument("run_id", nargs="?", default=None)
    ss.set_defaults(func=cmd_status)

    se = sub.add_parser("events", help="журнал событий")
    se.add_argument("run_id", nargs="?", default=None)
    se.set_defaults(func=cmd_events)

    sin = sub.add_parser("init", help="подготовить репозиторий: git + .harness/project.toml")
    sin.add_argument("--repo", default=None, help="путь к целевому репо (иначе HARNESS_ROOT)")
    sin.add_argument("--language", default=None, help="python|typescript|go|rust (иначе авто)")
    sin.add_argument("--base", default="main")
    sin.set_defaults(func=cmd_init)

    sd = sub.add_parser("doctor", help="проверка готовности окружения")
    sd.add_argument("--project", default=None)
    sd.set_defaults(func=cmd_doctor)

    sb = sub.add_parser("bot", help="запустить Telegram-бота (команды /status /approve /runs)")
    sb.set_defaults(func=cmd_bot)

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
