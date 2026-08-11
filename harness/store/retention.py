"""Retention for finished runs — store rows plus their on-disk spools.

Nothing here runs automatically. Run history is the audit trail: the evidence
ledger, the acceptance seal and every gate result live in the spool, so dropping
a run destroys the only proof that its work was verified. That is a decision for
an operator, not a default, so :func:`plan_prune` previews and
:func:`apply_prune` only acts on what the caller already saw.

Two rules the implementation enforces regardless of what is asked:

- only terminal runs (``done`` / ``failed`` / ``aborted``) are ever eligible —
  an active run is skipped even if it is the oldest;
- the newest ``keep`` terminal runs always survive.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from harness.store.repository import Store
from harness.tenant.run_layout import resolve_run_root

TERMINAL_STATUSES = frozenset({"done", "failed", "aborted"})


@dataclass(frozen=True)
class PrunableRun:
    run_id: str
    project: str
    status: str
    created_at: str
    rows: dict[str, int]
    spool_paths: tuple[str, ...]
    spool_bytes: int

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


@dataclass
class PrunePlan:
    keep: int
    candidates: list[PrunableRun] = field(default_factory=list)
    kept_run_ids: list[str] = field(default_factory=list)
    skipped_active: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(item.total_rows for item in self.candidates)

    @property
    def total_bytes(self) -> int:
        return sum(item.spool_bytes for item in self.candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "keep": self.keep,
            "kept_run_ids": self.kept_run_ids,
            "skipped_active": self.skipped_active,
            "total_rows": self.total_rows,
            "total_bytes": self.total_bytes,
            "candidates": [
                {
                    "run_id": item.run_id,
                    "project": item.project,
                    "status": item.status,
                    "created_at": item.created_at,
                    "rows": item.rows,
                    "spool_paths": list(item.spool_paths),
                    "spool_bytes": item.spool_bytes,
                }
                for item in self.candidates
            ],
        }


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _spools_for(store: Store, harness_root: Path, run_id: str) -> list[Path]:
    """Existing spool directories for a run, in the control plane and target repos."""
    found: list[Path] = []
    seen: set[Path] = set()
    roots = [harness_root]
    for dispatch in store.list_dispatches(run_id):
        raw = (dispatch.repo or "").strip()
        if raw:
            roots.append(Path(raw))
    for root in roots:
        candidate = resolve_run_root(root, run_id).root
        if candidate in seen or not candidate.is_dir():
            continue
        seen.add(candidate)
        found.append(candidate)
    return found


def plan_prune(
    store: Store,
    *,
    harness_root: Path,
    keep: int = 20,
    project: str | None = None,
) -> PrunePlan:
    """Decide which finished runs would be removed, without touching anything."""
    if keep < 0:
        raise ValueError("keep must be non-negative")
    plan = PrunePlan(keep=keep)
    terminal_seen = 0
    for run_id in store.list_run_ids(project=project):
        run = store.get_run(run_id)
        if run is None:
            continue
        if run.status not in TERMINAL_STATUSES:
            plan.skipped_active.append(run_id)
            continue
        terminal_seen += 1
        if terminal_seen <= keep:
            plan.kept_run_ids.append(run_id)
            continue
        spools = _spools_for(store, harness_root, run_id)
        plan.candidates.append(
            PrunableRun(
                run_id=run_id,
                project=run.project,
                status=run.status,
                created_at=run.created_at,
                rows=store.count_rows_for_run(run_id),
                spool_paths=tuple(str(p) for p in spools),
                spool_bytes=sum(_dir_size(p) for p in spools),
            )
        )
    return plan


def apply_prune(
    store: Store,
    plan: PrunePlan,
    *,
    drop_spools: bool = True,
) -> dict[str, object]:
    """Execute a plan produced by :func:`plan_prune`.

    Spool removal is separable: ``drop_spools=False`` shrinks the database while
    leaving the evidence ledgers on disk, which is the conservative choice when
    the runs are old but their audit trail still matters.
    """
    removed_rows = 0
    removed_spools: list[str] = []
    failures: list[dict[str, str]] = []
    for item in plan.candidates:
        if drop_spools:
            for raw in item.spool_paths:
                path = Path(raw)
                try:
                    shutil.rmtree(path)
                except OSError as exc:
                    failures.append({"path": raw, "error": str(exc)})
                    continue
                removed_spools.append(raw)
        removed_rows += sum(store.delete_run(item.run_id).values())
    if plan.candidates:
        store.vacuum()
    return {
        "runs_removed": len(plan.candidates),
        "rows_removed": removed_rows,
        "spools_removed": removed_spools,
        "failures": failures,
    }


def render_plan_markdown(plan: PrunePlan) -> str:
    lines = [
        "# Prune preview",
        "",
        f"- **keep (newest terminal runs):** {plan.keep}",
        f"- **would remove:** {len(plan.candidates)} run(s), "
        f"{plan.total_rows} row(s), {plan.total_bytes / 1024 / 1024:.1f} MiB of spool",
        f"- **kept:** {len(plan.kept_run_ids)}",
        f"- **skipped (not terminal):** {len(plan.skipped_active)}",
        "",
    ]
    if not plan.candidates:
        lines.append("Nothing to prune.")
        return "\n".join(lines) + "\n"
    lines += [
        "Removing a run destroys its evidence ledger and acceptance seal — the",
        "only proof its work was verified. Journals under `docs/history/` are",
        "written into the target repo and are **not** touched.",
        "",
        "| run_id | project | status | created | rows | spool MiB |",
        "|---|---|---|---|---|---|",
    ]
    for item in plan.candidates:
        lines.append(
            f"| `{item.run_id}` | {item.project} | `{item.status}` | "
            f"{item.created_at[:10]} | {item.total_rows} | "
            f"{item.spool_bytes / 1024 / 1024:.1f} |"
        )
    lines += ["", "Apply with `harness prune --apply`."]
    return "\n".join(lines) + "\n"
