"""Per-run directory layout under ``.harness/runs/<run_id>/`` (Phase 1).

Legacy spool lived at ``.harness/tasktool/<run_id>/``. V4 prefers the runs/
tree; if only the legacy path exists, we keep reading it (compat warning via
caller). Shared repo-root ``PLAN.md`` / ``tasks/`` are no longer SoT for an
active run once a run root is materialised.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_RUN_ROOT = ".harness/runs"
LEGACY_TASKTOOL = ".harness/tasktool"


@dataclass(frozen=True, slots=True)
class RunPaths:
    """Resolved paths for one TaskTool run."""

    run_id: str
    root: Path
    legacy: bool
    plan_json: Path
    plan_md: Path
    tasks_dir: Path
    spool_prompts: Path
    spool_results: Path
    spool_specs: Path
    spool_pending: Path
    controller_json: Path
    meta_json: Path
    acceptance_json: Path
    evidence_dir: Path
    active_lock: Path


def run_root_rel() -> str:
    return os.environ.get("HARNESS_RUN_ROOT", DEFAULT_RUN_ROOT).strip() or DEFAULT_RUN_ROOT


def use_run_roots() -> bool:
    """Default on in v4; set ``HARNESS_USE_RUN_ROOTS=0`` for legacy tasktool spool only."""
    return os.environ.get("HARNESS_USE_RUN_ROOTS", "1") == "1"


def legacy_spool(repo_root: Path, run_id: str) -> Path:
    return repo_root / LEGACY_TASKTOOL / run_id


def modern_spool(repo_root: Path, run_id: str) -> Path:
    return repo_root / run_root_rel() / run_id


def resolve_run_root(
    repo_root: Path,
    run_id: str,
    *,
    prefer_modern: bool | None = None,
) -> RunPaths:
    """Pick modern runs/ path, or legacy tasktool/ if that is the only existing tree."""
    modern = modern_spool(repo_root, run_id)
    legacy = legacy_spool(repo_root, run_id)
    modern_pref = use_run_roots() if prefer_modern is None else prefer_modern
    if modern_pref:
        if legacy.exists() and not modern.exists():
            root, is_legacy = legacy, True
        else:
            root, is_legacy = modern, False
    else:
        if modern.exists() and not legacy.exists():
            root, is_legacy = modern, False
        else:
            root, is_legacy = legacy, True
    return _paths_for(run_id, root, legacy=is_legacy)


def _paths_for(run_id: str, root: Path, *, legacy: bool) -> RunPaths:
    return RunPaths(
        run_id=run_id,
        root=root,
        legacy=legacy,
        plan_json=root / "plan.json",
        plan_md=root / "PLAN.md",
        tasks_dir=root / "tasks",
        spool_prompts=root / "prompts",
        spool_results=root / "results",
        spool_specs=root / "specs",
        spool_pending=root / "pending",
        controller_json=root / "controller.json",
        meta_json=root / "meta.json",
        acceptance_json=root / "acceptance.json",
        evidence_dir=root / "evidence",
        active_lock=root / ".active",
    )


def ensure_run_layout(
    repo_root: Path,
    run_id: str,
    *,
    tenant_id: str = "",
    goal: str = "",
    project: str = "",
    harness_version: str = "4.0.0-dev",
    copy_plan_from: Path | None = None,
    source_plan_root: Path | None = None,
    force_snapshot: bool = False,
    prefer_modern: bool | None = None,
) -> RunPaths:
    """Create per-run directories and optional PLAN/tasks snapshot (isolation surface)."""
    paths = resolve_run_root(repo_root, run_id, prefer_modern=prefer_modern)
    modern_pref = use_run_roots() if prefer_modern is None else prefer_modern
    # Always materialise modern layout when prefer_modern and we're not
    # stuck on a pre-existing legacy-only tree.
    if modern_pref and paths.legacy:
        root = paths.root
    elif modern_pref:
        root = modern_spool(repo_root, run_id)
        paths = _paths_for(run_id, root, legacy=False)
    else:
        root = paths.root

    for directory in (
        paths.spool_prompts,
        paths.spool_results,
        paths.spool_specs,
        paths.spool_pending,
        paths.tasks_dir,
        paths.evidence_dir / "gates",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    meta = {
        "run_id": run_id,
        "tenant_id": tenant_id or os.environ.get("HARNESS_TENANT_ID") or os.environ.get("USER", ""),
        "goal": goal,
        "project": project,
        "harness_version": harness_version,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "layout": "legacy" if paths.legacy else "runs",
    }
    if not paths.meta_json.exists():
        paths.meta_json.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if source_plan_root is not None:
        snapshot_plan_surface(source_plan_root, paths, force=force_snapshot)
    else:
        plan_source = copy_plan_from or (repo_root / "PLAN.md")
        if plan_source.is_file() and (force_snapshot or not paths.plan_md.exists()):
            shutil.copy2(plan_source, paths.plan_md)
        tasks_source = repo_root / "tasks"
        if tasks_source.is_dir() and paths.tasks_dir.exists():
            if force_snapshot or not any(paths.tasks_dir.iterdir()):
                for src in tasks_source.glob("*.md"):
                    shutil.copy2(src, paths.tasks_dir / src.name)

    return paths


def snapshot_plan_surface(
    source_root: Path,
    paths: RunPaths,
    *,
    force: bool = True,
) -> None:
    """Copy PLAN.md + tasks/*.md from ``source_root`` into an exclusive run surface.

    When ``force`` is True (default), overwrites the run's plan files so a start
    can freeze the plan observed at claim time. Never writes back to ``source_root``.
    """
    source_root = Path(source_root)
    paths.tasks_dir.mkdir(parents=True, exist_ok=True)
    plan_src = source_root / "PLAN.md"
    if plan_src.is_file() and (force or not paths.plan_md.exists()):
        shutil.copy2(plan_src, paths.plan_md)
    tasks_src = source_root / "tasks"
    if tasks_src.is_dir():
        if force:
            for existing in paths.tasks_dir.glob("*.md"):
                existing.unlink(missing_ok=True)
        if force or not any(paths.tasks_dir.glob("*.md")):
            for src in tasks_src.glob("*.md"):
                shutil.copy2(src, paths.tasks_dir / src.name)


def write_plan_surface(
    paths: RunPaths,
    plan_content: str,
    tasks: dict[str, str],
) -> tuple[Path, Path]:
    """Write PLAN.md + task files only under the run root (no shared repo SoT)."""
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.tasks_dir.mkdir(parents=True, exist_ok=True)
    paths.plan_md.write_text(plan_content, encoding="utf-8")
    for name, body in tasks.items():
        (paths.tasks_dir / name).write_text(body, encoding="utf-8")
    return plan_surface_paths(paths)


def point_latest_symlink(repo_root: Path, run_id: str) -> Path:
    """Best-effort discoverability symlink; racy under concurrency — not SoT."""
    runs_dir = repo_root / run_root_rel()
    runs_dir.mkdir(parents=True, exist_ok=True)
    latest = runs_dir / "latest"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(run_id, target_is_directory=True)
    return latest


def shared_plan_writes_enabled() -> bool:
    """Shared repo-root PLAN.md/tasks/ are SoT only when run-roots are off."""
    return not use_run_roots()


def plan_surface_paths(paths: RunPaths) -> tuple[Path, Path]:
    """Return (PLAN.md, tasks/) that this run owns — isolation DoD helper."""
    return paths.plan_md, paths.tasks_dir
