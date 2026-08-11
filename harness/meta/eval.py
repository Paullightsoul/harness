"""Compare AHE-lite predictions against recorded / recent run outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.meta.changelog import ChangelogEntry, PredictedMetrics, load_entries
from harness.meta.outcomes import (
    RunOutcome,
    collect_run_outcome,
    recent_run_ids,
    runs_after,
)
from harness.store.repository import Store


@dataclass
class PredictionCheck:
    metric: str
    predicted: float | int | None
    actual: float | int | None
    ok: bool | None  # None = inconclusive (missing actual)
    detail: str


@dataclass
class EntryEval:
    entry_id: str
    change: str
    status: str
    verdict: str  # supported | contradicted | inconclusive | open
    checks: list[PredictionCheck] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "change": self.change,
            "status": self.status,
            "verdict": self.verdict,
            "run_ids": list(self.run_ids),
            "notes": self.notes,
            "checks": [
                {
                    "metric": c.metric,
                    "predicted": c.predicted,
                    "actual": c.actual,
                    "ok": c.ok,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }


@dataclass
class EvalReport:
    generated_at: str
    harness_root: str
    entries: list[EntryEval] = field(default_factory=list)
    recent_outcomes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "harness_root": self.harness_root,
            "entries": [e.to_dict() for e in self.entries],
            "recent_outcomes": list(self.recent_outcomes),
            "summary": {
                "entries": len(self.entries),
                "supported": sum(1 for e in self.entries if e.verdict == "supported"),
                "contradicted": sum(
                    1 for e in self.entries if e.verdict == "contradicted"
                ),
                "inconclusive": sum(
                    1 for e in self.entries if e.verdict == "inconclusive"
                ),
                "open": sum(1 for e in self.entries if e.verdict == "open"),
            },
        }


def _mean_evidence(outcomes: list[RunOutcome]) -> float | None:
    vals = [o.evidence_pct for o in outcomes if o.evidence_pct is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _mean_tokens(outcomes: list[RunOutcome]) -> float | None:
    vals = [float(o.tokens_est) for o in outcomes if o.tokens_est is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _max_stalls(outcomes: list[RunOutcome]) -> int | None:
    if not outcomes:
        return None
    return max(o.stall_count for o in outcomes)


def _baseline_evidence(
    store: Store,
    entry: ChangelogEntry,
    *,
    harness_root: Path,
    repo_root: Path | None,
) -> float | None:
    if not entry.baseline_run_ids:
        return None
    outs: list[RunOutcome] = []
    for rid in entry.baseline_run_ids:
        o = collect_run_outcome(
            store, rid, harness_root=harness_root, repo_root=repo_root
        )
        if o is not None:
            outs.append(o)
    return _mean_evidence(outs)


def _baseline_tokens(
    store: Store,
    entry: ChangelogEntry,
    *,
    harness_root: Path,
    repo_root: Path | None,
) -> float | None:
    if not entry.baseline_run_ids:
        return None
    outs: list[RunOutcome] = []
    for rid in entry.baseline_run_ids:
        o = collect_run_outcome(
            store, rid, harness_root=harness_root, repo_root=repo_root
        )
        if o is not None:
            outs.append(o)
    return _mean_tokens(outs)


def evaluate_prediction(
    predicted: PredictedMetrics,
    outcomes: list[RunOutcome],
    *,
    baseline_evidence: float | None = None,
    baseline_tokens: float | None = None,
) -> list[PredictionCheck]:
    checks: list[PredictionCheck] = []
    evidence = _mean_evidence(outcomes)
    tokens = _mean_tokens(outcomes)
    stalls = _max_stalls(outcomes)

    if predicted.evidence_pct_min is not None:
        ok: bool | None
        if evidence is None:
            ok = None
            detail = "no evidence% on verify runs"
        else:
            ok = evidence >= predicted.evidence_pct_min
            detail = f"mean evidence%={evidence:.1f} vs min {predicted.evidence_pct_min}"
        checks.append(
            PredictionCheck(
                metric="evidence_pct_min",
                predicted=predicted.evidence_pct_min,
                actual=evidence,
                ok=ok,
                detail=detail,
            )
        )

    if predicted.evidence_pct_delta_pp is not None:
        if evidence is None or baseline_evidence is None:
            checks.append(
                PredictionCheck(
                    metric="evidence_pct_delta_pp",
                    predicted=predicted.evidence_pct_delta_pp,
                    actual=None,
                    ok=None,
                    detail="need baseline + verify evidence%",
                )
            )
        else:
            delta = evidence - baseline_evidence
            ok = delta >= predicted.evidence_pct_delta_pp
            checks.append(
                PredictionCheck(
                    metric="evidence_pct_delta_pp",
                    predicted=predicted.evidence_pct_delta_pp,
                    actual=round(delta, 2),
                    ok=ok,
                    detail=(
                        f"Δ={delta:.1f}pp "
                        f"(baseline {baseline_evidence:.1f} → {evidence:.1f})"
                    ),
                )
            )

    if predicted.tokens_est_max is not None:
        if tokens is None:
            ok = None
            detail = "tokens_est not recorded on verify runs"
        else:
            ok = tokens <= predicted.tokens_est_max
            detail = f"mean tokens_est={tokens:.0f} vs max {predicted.tokens_est_max}"
        checks.append(
            PredictionCheck(
                metric="tokens_est_max",
                predicted=predicted.tokens_est_max,
                actual=int(tokens) if tokens is not None else None,
                ok=ok,
                detail=detail,
            )
        )

    if predicted.tokens_est_delta_pct is not None:
        # Negative delta_pct means "expect fewer tokens" (e.g. -12 → ≤12% drop).
        if tokens is None or baseline_tokens is None or baseline_tokens <= 0:
            checks.append(
                PredictionCheck(
                    metric="tokens_est_delta_pct",
                    predicted=predicted.tokens_est_delta_pct,
                    actual=None,
                    ok=None,
                    detail="need baseline + verify tokens_est",
                )
            )
        else:
            delta_pct = ((tokens - baseline_tokens) / baseline_tokens) * 100.0
            # Prediction like -12 means we expect delta_pct <= -12.
            ok = delta_pct <= predicted.tokens_est_delta_pct
            checks.append(
                PredictionCheck(
                    metric="tokens_est_delta_pct",
                    predicted=predicted.tokens_est_delta_pct,
                    actual=round(delta_pct, 2),
                    ok=ok,
                    detail=(
                        f"Δ={delta_pct:.1f}% "
                        f"(baseline {baseline_tokens:.0f} → {tokens:.0f})"
                    ),
                )
            )

    if predicted.stall_count_max is not None:
        if stalls is None:
            ok = None
            detail = "no verify runs"
        else:
            ok = stalls <= predicted.stall_count_max
            detail = f"max stall_count={stalls} vs max {predicted.stall_count_max}"
        checks.append(
            PredictionCheck(
                metric="stall_count_max",
                predicted=predicted.stall_count_max,
                actual=stalls,
                ok=ok,
                detail=detail,
            )
        )

    return checks


def _verdict_from_checks(checks: list[PredictionCheck], *, has_runs: bool) -> str:
    if not checks:
        return "open" if not has_runs else "inconclusive"
    if not has_runs:
        return "open"
    decided = [c for c in checks if c.ok is not None]
    if not decided:
        return "inconclusive"
    if any(c.ok is False for c in decided):
        return "contradicted"
    if all(c.ok is True for c in decided):
        return "supported"
    return "inconclusive"


def evaluate_entries(
    store: Store,
    *,
    harness_root: Path,
    changelog_path: Path,
    repo_root: Path | None = None,
    limit_recent: int = 5,
    entry_ids: list[str] | None = None,
) -> EvalReport:
    entries = load_entries(changelog_path)
    if entry_ids:
        want = set(entry_ids)
        entries = [e for e in entries if e.id in want]

    report = EvalReport(
        generated_at=datetime.now(tz=UTC).isoformat(),
        harness_root=str(harness_root),
    )

    for rid in recent_run_ids(store, limit=limit_recent):
        outcome = collect_run_outcome(
            store, rid, harness_root=harness_root, repo_root=repo_root
        )
        if outcome is not None:
            report.recent_outcomes.append(outcome.to_dict())

    for entry in entries:
        verify_ids = list(entry.verify_run_ids)
        if not verify_ids and entry.actual.get("run_id"):
            verify_ids = [str(entry.actual["run_id"])]
        if not verify_ids and entry.at:
            verify_ids = runs_after(store, entry.at, limit=8)

        outcomes: list[RunOutcome] = []
        for rid in verify_ids:
            o = collect_run_outcome(
                store, rid, harness_root=harness_root, repo_root=repo_root
            )
            if o is not None:
                outcomes.append(o)

        # Prefer pre-recorded actual snapshot when present and no live runs.
        if not outcomes and entry.actual:
            outcomes.append(
                RunOutcome(
                    run_id=str(entry.actual.get("run_id") or ""),
                    status=str(entry.actual.get("status") or ""),
                    project=str(entry.actual.get("project") or ""),
                    created_at=str(entry.actual.get("created_at") or ""),
                    pipeline_pct=_as_float(entry.actual.get("pipeline_pct")),
                    evidence_pct=_as_float(entry.actual.get("evidence_pct")),
                    tokens_est=_as_int(entry.actual.get("tokens_est")),
                    stall_count=int(entry.actual.get("stall_count") or 0),
                    pass_at_1=_as_float(entry.actual.get("pass_at_1")),
                    task_total=int(entry.actual.get("task_total") or 0),
                    task_done=int(entry.actual.get("task_done") or 0),
                )
            )

        checks = evaluate_prediction(
            entry.predicted,
            outcomes,
            baseline_evidence=_baseline_evidence(
                store, entry, harness_root=harness_root, repo_root=repo_root
            ),
            baseline_tokens=_baseline_tokens(
                store, entry, harness_root=harness_root, repo_root=repo_root
            ),
        )
        verdict = entry.status if entry.status in {"waived", "verified", "falsified"} else (
            _verdict_from_checks(checks, has_runs=bool(outcomes))
        )
        if entry.status == "verified":
            verdict = "supported"
        elif entry.status == "falsified":
            verdict = "contradicted"

        report.entries.append(
            EntryEval(
                entry_id=entry.id,
                change=entry.change,
                status=entry.status,
                verdict=verdict,
                checks=checks,
                run_ids=[o.run_id for o in outcomes if o.run_id],
                notes=entry.notes,
            )
        )

    return report


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def render_eval_markdown(report: EvalReport) -> str:
    summary = report.to_dict()["summary"]
    lines = [
        "# AHE-lite meta eval",
        "",
        f"- **generated:** {report.generated_at}",
        f"- **harness_root:** `{report.harness_root}`",
        f"- **entries:** {summary['entries']} "
        f"(supported={summary['supported']}, contradicted={summary['contradicted']}, "
        f"inconclusive={summary['inconclusive']}, open={summary['open']})",
        "",
        "## Changelog predictions vs outcomes",
        "",
    ]
    if not report.entries:
        lines.append("_No changelog entries. Add with `harness meta changelog add`._")
        lines.append("")
    for entry in report.entries:
        lines.append(f"### `{entry.entry_id}` — {entry.verdict}")
        lines.append("")
        lines.append(f"- **change:** {entry.change}")
        lines.append(f"- **status:** `{entry.status}`")
        if entry.run_ids:
            lines.append(f"- **runs:** {', '.join(f'`{r}`' for r in entry.run_ids)}")
        if entry.notes:
            lines.append(f"- **notes:** {entry.notes}")
        if entry.checks:
            lines.append("")
            lines.append("| metric | predicted | actual | ok | detail |")
            lines.append("|---|---|---|---|---|")
            for c in entry.checks:
                mark = "—" if c.ok is None else ("✅" if c.ok else "❌")
                lines.append(
                    f"| `{c.metric}` | {c.predicted} | {c.actual} | {mark} | {c.detail} |"
                )
        lines.append("")

    lines.append("## Recent run outcomes")
    lines.append("")
    if not report.recent_outcomes:
        lines.append("_No runs in store._")
    else:
        lines.append(
            "| run_id | status | evidence% | tokens_est | stalls | pipeline% |"
        )
        lines.append("|---|---|---|---|---|---|")
        for o in report.recent_outcomes:
            ev = o.get("evidence_pct")
            ev_s = f"{ev:.0f}" if isinstance(ev, (int, float)) else "n/a"
            tok = o.get("tokens_est")
            tok_s = str(tok) if tok is not None else "n/a"
            pipe = o.get("pipeline_pct")
            pipe_s = f"{pipe:.0f}" if isinstance(pipe, (int, float)) else "n/a"
            lines.append(
                f"| `{o.get('run_id')}` | {o.get('status')} | {ev_s} | "
                f"{tok_s} | {o.get('stall_count')} | {pipe_s} |"
            )
    lines.append("")
    return "\n".join(lines)
