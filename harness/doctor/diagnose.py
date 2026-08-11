"""Phase 0 diagnostics for harness-v4 (Honesty baseline).

False-coverage: FSM/pipeline % vs pass@k vs evidence% (stub until Phase 1.5).
Multi-tenant: .active locks, shared PLAN.md collision risk, worktrees default.
Soft gates: heuristic inventory of optional / always-green behaviour sensors.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness.config import Settings
from harness.profile import GateSpec, load_profile
from harness.store.repository import Store

# Heuristics for "soft" gates that can hide missing behaviour evidence.
_SOFT_ID_RE = re.compile(r"optional|soft|skip|non.?blocking", re.I)
_SOFT_CMD_MARKERS = (
    "exit 0",
    "skipped (no vendor)",
    "phpunit skipped",
    "non-blocking",
    "|| true",
    "||{ echo",
    "|| { echo",
)


@dataclass(frozen=True)
class SoftGateFinding:
    source: str
    gate_id: str
    cmd: str
    reasons: tuple[str, ...]
    severity: str  # "info" | "warn" | "critical"


@dataclass
class RunCoverageRow:
    run_id: str
    project: str
    status: str
    goal: str
    task_total: int
    task_done: int
    pipeline_pct: float | None
    pass_at_1: float | None
    pass_at_3: float | None
    pass_all_3: float | None
    evidence_pct: float | None  # from acceptance.json when present
    evidence_note: str
    gap_pipeline_vs_pass1_pp: float | None
    gate_results_total: int
    gate_results_passed: int


@dataclass
class FalseCoverageReport:
    generated_at: str
    harness_root: str
    evidence_implemented: bool
    rows: list[RunCoverageRow] = field(default_factory=list)
    summary: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "harness_root": self.harness_root,
            "evidence_implemented": self.evidence_implemented,
            "rows": [asdict(r) for r in self.rows],
            "summary": self.summary,
        }


@dataclass
class MultiTenantReport:
    generated_at: str
    harness_root: str
    use_worktrees_default: bool
    multi_tenant_flag: bool
    allow_soft_gates: bool
    active_locks: list[dict[str, str]] = field(default_factory=list)
    plan_collision_risks: list[dict[str, str]] = field(default_factory=list)
    soft_gates: list[SoftGateFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "harness_root": self.harness_root,
            "use_worktrees_default": self.use_worktrees_default,
            "multi_tenant_flag": self.multi_tenant_flag,
            "allow_soft_gates": self.allow_soft_gates,
            "active_locks": self.active_locks,
            "plan_collision_risks": self.plan_collision_risks,
            "soft_gates": [asdict(g) for g in self.soft_gates],
            "notes": self.notes,
        }


def _classify_soft_gate(gate: GateSpec, *, source: str) -> SoftGateFinding | None:
    reasons: list[str] = []
    if _SOFT_ID_RE.search(gate.id):
        reasons.append(f"gate id suggests optional/soft: {gate.id!r}")
    cmd = gate.cmd
    cmd_l = cmd.lower()
    for marker in _SOFT_CMD_MARKERS:
        if marker.lower() in cmd_l:
            reasons.append(f"cmd contains soft marker {marker!r}")
    # Explicit non-blocking fail pattern seen on ZY stand:
    # `./vendor/bin/phpunit ... || { ...; exit 0; }`
    if re.search(r"\|\|\s*\{[^}]*exit\s+0", cmd, re.I | re.S):
        reasons.append("cmd swallows failure with `|| { …; exit 0; }`")
    if not reasons:
        return None
    severity = "critical" if any("exit 0" in r or "swallows" in r for r in reasons) else "warn"
    if severity != "critical" and "optional" in gate.id.lower():
        severity = "warn"
    return SoftGateFinding(
        source=source,
        gate_id=gate.id,
        cmd=cmd if len(cmd) <= 240 else cmd[:237] + "...",
        reasons=tuple(reasons),
        severity=severity,
    )


def inventory_soft_gates(
    paths: list[Path],
    *,
    also_scaffold_profiles: bool = True,
) -> list[SoftGateFinding]:
    """Scan project.toml paths (+ optional scaffold templates) for soft gates."""
    findings: list[SoftGateFinding] = []
    seen: set[tuple[str, str]] = set()

    for path in paths:
        root = path
        if path.name == "project.toml" or path.as_posix().endswith(".harness/project.toml"):
            root = path.parent.parent if path.parent.name == ".harness" else path.parent
        try:
            profile = load_profile(root)
        except Exception as exc:  # noqa: BLE001 — doctor must not crash on bad toml
            findings.append(
                SoftGateFinding(
                    source=str(path),
                    gate_id="(unreadable)",
                    cmd="",
                    reasons=(f"profile load failed: {exc}",),
                    severity="warn",
                )
            )
            continue
        for gate in profile.gates:
            key = (str(path), gate.id)
            if key in seen:
                continue
            seen.add(key)
            hit = _classify_soft_gate(gate, source=str(path.resolve()))
            if hit:
                findings.append(hit)

    if also_scaffold_profiles:
        from harness.scaffold import _PROFILES  # noqa: PLC0415, SLF001

        for lang, body in _PROFILES.items():
            # Parse gates from scaffold template text without writing disk.
            try:
                import tomllib  # noqa: PLC0415

                data = tomllib.loads(body)
            except Exception:  # noqa: BLE001
                continue
            raw_gates = data.get("gates") or []
            if not isinstance(raw_gates, list):
                continue
            for g in raw_gates:
                if not isinstance(g, dict):
                    continue
                gid = str(g.get("id") or "")
                cmd = str(g.get("cmd") or "")
                if not gid:
                    continue
                hit = _classify_soft_gate(
                    GateSpec(gid, cmd),
                    source=f"scaffold:{lang}",
                )
                if hit and (hit.source, hit.gate_id) not in seen:
                    seen.add((hit.source, hit.gate_id))
                    findings.append(hit)

    return findings


def render_soft_gates_markdown(findings: list[SoftGateFinding]) -> str:
    lines = [
        "# Soft gates inventory (harness-v4 Phase 0)",
        "",
        f"- **generated:** {datetime.now(tz=UTC).isoformat()}",
        f"- **findings:** {len(findings)}",
        "",
        "Heuristics: gate id `optional|soft|…`, cmd markers (`exit 0` after fail,",
        "`skipped (no vendor)`, `|| true`, non-blocking fail swallow).",
        "",
        "| severity | source | gate_id | reasons |",
        "|---|---|---|---|",
    ]
    if not findings:
        lines.append("| — | — | — | no soft-gate heuristics matched |")
    for f in findings:
        reasons = "; ".join(f.reasons).replace("|", "\\|")
        lines.append(
            f"| `{f.severity}` | `{f.source}` | `{f.gate_id}` | {reasons} |"
        )
    lines += [
        "",
        "## Notes",
        "",
        "- Soft skip when vendor missing ≠ always soft-fail: a real phpunit failure",
        "  should still fail unless the cmd explicitly `exit 0` on fail.",
        "- V4 default: `HARNESS_ALLOW_SOFT_GATES=0` — ProfileGate hardens soft cmds.",
        "- YouTrack «Готовность» must equal **evidence%** (never FSM pipeline%).",
        "  See `harness evidence` / `harness acceptance` and orch skill §9.",
        "",
    ]
    return "\n".join(lines)


def _candidate_run_roots(store: Store, harness_root: Path, run_id: str) -> list[Path]:
    """Where run ``run_id`` may have written its acceptance ledger.

    The control plane and the target repo are usually different trees: the spool
    lives under ``<target-repo>/.harness/runs/<id>/``, not under the harness
    checkout. Resolving only against ``harness_root`` made evidence% silently
    empty for every real run, which is exactly the false coverage this report
    exists to surface. Dispatch envelopes carry the repo they ran in, so use
    those, newest first, and fall back to the control-plane tree.
    """
    from harness.tenant.run_layout import resolve_run_root  # noqa: PLC0415

    roots: list[Path] = [resolve_run_root(harness_root, run_id).root]
    seen: set[Path] = set(roots)
    for dispatch in store.list_dispatches(run_id):
        raw = (dispatch.repo or "").strip()
        if not raw:
            continue
        candidate = resolve_run_root(Path(raw), run_id).root
        if candidate not in seen:
            seen.add(candidate)
            roots.append(candidate)
    return roots


def build_false_coverage_report(
    store: Store,
    *,
    harness_root: Path,
    limit: int = 8,
    run_ids: list[str] | None = None,
) -> FalseCoverageReport:
    """Compare pipeline (FSM) % vs pass@k vs evidence% (acceptance ledger)."""
    from harness.evidence.acceptance import evidence_pct_for_run  # noqa: PLC0415

    rows: list[RunCoverageRow] = []
    ids = run_ids or store.list_run_ids(limit=limit)

    gaps: list[float] = []
    evidence_hits = 0
    for rid in ids:
        run = store.get_run(rid)
        if run is None:
            continue
        tasks = store.list_tasks(rid)
        done = sum(1 for t in tasks if t.status == "done")
        pipeline = (done / len(tasks) * 100.0) if tasks else None
        p1 = store.pass_at_k(rid, 1)
        p3 = store.pass_at_k(rid, 3)
        pa3 = store.pass_all_k(rid, 3)
        gap = None
        if pipeline is not None and p1 is not None:
            gap = pipeline - (p1 * 100.0)
            gaps.append(gap)

        gate_details = store.list_event_details(rid, "gate_result")
        g_total = len(gate_details)
        g_pass = sum(1 for detail in gate_details if detail.get("passed") is True)

        evidence_pct: float | None = None
        evidence_note = "no acceptance.json for run"
        searched: list[str] = []
        for run_root in _candidate_run_roots(store, harness_root, rid):
            searched.append(str(run_root))
            try:
                evidence_pct = evidence_pct_for_run(run_root)
            except (OSError, ValueError, json.JSONDecodeError):
                evidence_note = f"failed to load acceptance ledger under {run_root}"
                continue
            if evidence_pct is not None:
                evidence_hits += 1
                evidence_note = f"from acceptance.json required items ({run_root})"
                break
        if evidence_pct is None and searched:
            evidence_note = "acceptance.json missing under: " + ", ".join(searched)

        rows.append(
            RunCoverageRow(
                run_id=rid,
                project=run.project,
                status=run.status,
                goal=(run.goal[:120] + "…") if len(run.goal) > 120 else run.goal,
                task_total=len(tasks),
                task_done=done,
                pipeline_pct=pipeline,
                pass_at_1=p1,
                pass_at_3=p3,
                pass_all_3=pa3,
                evidence_pct=evidence_pct,
                evidence_note=evidence_note,
                gap_pipeline_vs_pass1_pp=gap,
                gate_results_total=g_total,
                gate_results_passed=g_pass,
            )
        )

    summary: dict[str, object] = {
        "runs_scored": len(rows),
        "runs_with_evidence_pct": evidence_hits,
        "mean_gap_pipeline_vs_pass1_pp": (
            round(sum(gaps) / len(gaps), 1) if gaps else None
        ),
        "pass_at_k_missing_runs": sum(1 for r in rows if r.pass_at_1 is None),
        "diagnosis": (
            "Primary readiness = evidence%. High pipeline% with low evidence% "
            "or missing/weak pass@k → false coverage risk. "
            "HARNESS_EVIDENCE_GATE defaults ON in v4; set =0 only as emergency escape. "
            "DONE requires sealed acceptance + attested ledger."
        ),
    }
    return FalseCoverageReport(
        generated_at=datetime.now(tz=UTC).isoformat(),
        harness_root=str(harness_root.resolve()),
        evidence_implemented=True,
        rows=rows,
        summary=summary,
    )


def render_false_coverage_markdown(report: FalseCoverageReport) -> str:
    lines = [
        "# False-coverage report (harness-v4 Phase 0)",
        "",
        f"- **generated:** {report.generated_at}",
        f"- **harness_root:** `{report.harness_root}`",
        f"- **evidence_implemented:** {report.evidence_implemented}",
        f"- **runs_scored:** {report.summary.get('runs_scored')}",
        f"- **pass@k missing runs:** {report.summary.get('pass_at_k_missing_runs')}",
        f"- **mean gap pipeline% − pass@1×100:** "
        f"{report.summary.get('mean_gap_pipeline_vs_pass1_pp')}",
        "",
        str(report.summary.get("diagnosis", "")),
        "",
        "| run_id | status | pipeline% | pass@1 | pass@3 | pass^3 | evidence% | gap_pp | gates ok/total |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report.rows:
        def _pct(v: float | None) -> str:
            return "—" if v is None else f"{v:.0f}%"

        def _f(v: float | None) -> str:
            return "—" if v is None else f"{v:.2f}"

        lines.append(
            f"| `{r.run_id}` | `{r.status}` | {_pct(r.pipeline_pct)} | "
            f"{_f(r.pass_at_1)} | {_f(r.pass_at_3)} | {_f(r.pass_all_3)} | "
            f"{_pct(r.evidence_pct)} | {_f(r.gap_pipeline_vs_pass1_pp)} | "
            f"{r.gate_results_passed}/{r.gate_results_total} |"
        )
    lines += [
        "",
        "## Legend",
        "",
        "- **pipeline%** = FSM done_tasks / tasks (legacy «completion» / YouTrack risk).",
        "- **pass@k** = store metrics (approve within k attempts; trivial excluded).",
        "- **evidence%** = green required acceptance items (Phase 1.5; stub here).",
        "- Large positive **gap_pp** with soft gates → honesty debt.",
        "",
    ]
    return "\n".join(lines)


def _scan_active_locks(candidate_roots: list[Path]) -> list[dict[str, str]]:
    locks: list[dict[str, str]] = []
    for root in candidate_roots:
        for rel in (".harness/.active", ".active", ".harness/tasktool/.active"):
            path = root / rel
            if not path.is_file():
                continue
            try:
                run_id = path.read_text(encoding="utf-8").strip().splitlines()[0]
            except OSError as exc:
                locks.append(
                    {
                        "path": str(path),
                        "run_id": "",
                        "error": str(exc),
                    }
                )
                continue
            locks.append({"path": str(path.resolve()), "run_id": run_id, "error": ""})
    return locks


def _plan_collision_risks(candidate_roots: list[Path]) -> list[dict[str, str]]:
    risks: list[dict[str, str]] = []
    from harness.tenant.run_layout import use_run_roots  # noqa: PLC0415

    run_roots_on = use_run_roots()
    for root in candidate_roots:
        shared = root / "PLAN.md"
        tasks = root / "tasks"
        runs = root / ".harness" / "runs"
        if shared.is_file() and tasks.is_dir():
            if run_roots_on:
                risks.append(
                    {
                        "repo": str(root.resolve()),
                        "kind": "shared_root_plan_compat",
                        "detail": (
                            "legacy flat PLAN.md + tasks/ still present — "
                            "v4 SoT is .harness/runs/<id>/ (defaults ON); "
                            "harness plan no longer overwrites shared PLAN"
                        ),
                    }
                )
            else:
                risks.append(
                    {
                        "repo": str(root.resolve()),
                        "kind": "shared_root_plan",
                        "detail": (
                            "legacy SoT PLAN.md + tasks/ at repo root — "
                            "second concurrent goal can overwrite "
                            "(set HARNESS_USE_RUN_ROOTS=1)"
                        ),
                    }
                )
        if runs.is_dir() and any(runs.iterdir()):
            risks.append(
                {
                    "repo": str(root.resolve()),
                    "kind": "has_runs_dir",
                    "detail": (
                        f"found {runs} — preferred multi-tenant SoT "
                        f"(HARNESS_USE_RUN_ROOTS={'1' if run_roots_on else '0'})"
                    ),
                }
            )
    return risks


def build_multi_tenant_report(
    settings: Settings,
    *,
    extra_repos: list[Path] | None = None,
) -> MultiTenantReport:
    from harness.projects.registry import ProjectRegistry  # noqa: PLC0415

    roots: list[Path] = [settings.root]
    try:
        for proj in ProjectRegistry(settings.root / "projects.json").list():
            roots.append(Path(proj.repo_path))
    except Exception:  # noqa: BLE001
        pass
    for p in extra_repos or []:
        roots.append(p)
    # de-dupe preserving order
    seen: set[str] = set()
    uniq: list[Path] = []
    for r in roots:
        key = str(r.resolve()) if r.exists() else str(r)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    soft_paths = [
        r / ".harness" / "project.toml"
        for r in uniq
        if (r / ".harness" / "project.toml").is_file()
    ]
    soft = inventory_soft_gates(soft_paths, also_scaffold_profiles=True)

    notes = [
        f"HARNESS_USE_WORKTREES default in Settings={settings.use_worktrees} "
        f"(env raw={os.environ.get('HARNESS_USE_WORKTREES', '<unset>')})",
        f"HARNESS_USE_RUN_ROOTS default in Settings={settings.use_run_roots} "
        f"(env raw={os.environ.get('HARNESS_USE_RUN_ROOTS', '<unset>')})",
        "V4 defaults ON: worktrees + run roots + multi_tenant — safe for ~4 "
        "parallel tenants on distinct goals (shared PLAN no longer SoT).",
        "Prod /home/1.harness remains legacy until explicit cutover.",
    ]
    return MultiTenantReport(
        generated_at=datetime.now(tz=UTC).isoformat(),
        harness_root=str(settings.root.resolve()),
        use_worktrees_default=settings.use_worktrees,
        multi_tenant_flag=os.environ.get("HARNESS_MULTI_TENANT", "1") == "1",
        allow_soft_gates=os.environ.get("HARNESS_ALLOW_SOFT_GATES", "0") == "1",
        active_locks=_scan_active_locks(uniq),
        plan_collision_risks=_plan_collision_risks(uniq),
        soft_gates=soft,
        notes=notes,
    )


def render_multi_tenant_markdown(report: MultiTenantReport) -> str:
    lines = [
        "# Multi-tenant diagnose (harness-v4 Phase 0)",
        "",
        f"- **generated:** {report.generated_at}",
        f"- **harness_root:** `{report.harness_root}`",
        f"- **use_worktrees (effective):** `{report.use_worktrees_default}`",
        f"- **HARNESS_MULTI_TENANT:** `{report.multi_tenant_flag}`",
        f"- **HARNESS_ALLOW_SOFT_GATES:** `{report.allow_soft_gates}`",
        "",
        "## Active locks (`.active`)",
        "",
    ]
    if not report.active_locks:
        lines.append("_none found in scanned roots_")
    else:
        for lock in report.active_locks:
            err = f" error={lock['error']}" if lock.get("error") else ""
            lines.append(f"- `{lock['path']}` → run `{lock.get('run_id', '')}`{err}")
    lines += ["", "## PLAN / SoT collision risks", ""]
    if not report.plan_collision_risks:
        lines.append("_none_")
    else:
        for risk in report.plan_collision_risks:
            lines.append(
                f"- **{risk['kind']}** @ `{risk['repo']}`: {risk['detail']}"
            )
    lines += [
        "",
        f"## Soft gates ({len(report.soft_gates)})",
        "",
    ]
    for g in report.soft_gates:
        lines.append(
            f"- `{g.severity}` `{g.gate_id}` @ `{g.source}` — "
            + "; ".join(g.reasons)
        )
    lines += ["", "## Notes", ""]
    for n in report.notes:
        lines.append(f"- {n}")
    lines.append("")
    return "\n".join(lines)
