"""YouTrack readiness mapping (Phase 1.5) — evidence_pct only.

If YouTrack credentials / client are unavailable in the environment, callers
get a clear stub payload instead of inventing FSM completion%.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.evidence.acceptance import evidence_pct_for_run, load_acceptance


@dataclass(frozen=True, slots=True)
class YouTrackReadiness:
    available: bool
    evidence_pct: float | None
    pipeline_pct: float | None
    readiness_pct: float | None  # always == evidence_pct when available
    marker_block: str
    reason: str
    evidence_map: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "evidence_pct": self.evidence_pct,
            "pipeline_pct": self.pipeline_pct,
            "readiness_pct": self.readiness_pct,
            "marker_block": self.marker_block,
            "reason": self.reason,
            "evidence_map": self.evidence_map,
            "policy": "readiness_equals_evidence_pct_never_pipeline",
        }


def youtrack_creds_available(repo: Path | None = None) -> bool:
    """True when a YouTrack env file or token is present (no network call)."""
    if os.environ.get("YOUTRACK_TOKEN") or os.environ.get("YT_TOKEN"):
        return True
    roots = [Path.cwd()]
    if repo is not None:
        roots.insert(0, repo)
    for root in roots:
        for name in (".env.local.youtrack", ".env.youtrack"):
            if (root / name).is_file():
                return True
    return False


def build_evidence_map(run_root: Path) -> list[dict[str, Any]]:
    path = run_root / "acceptance.json"
    if not path.is_file():
        return []
    doc = load_acceptance(path)
    rows: list[dict[str, Any]] = []
    for item in doc.items:
        rows.append(
            {
                "id": item.id,
                "description": item.description,
                "passes": item.passes,
                "waived": item.waived,
                "required": item.required,
                "evidence_ids": list(item.evidence_ids),
            }
        )
    return rows


def format_coverage_marker(*, evidence_pct: float, report_path: str = "") -> str:
    """Visible YouTrack block — readiness from evidence only."""
    date = __import__("datetime").datetime.now(
        tz=__import__("datetime").UTC
    ).strftime("%Y-%m-%d")
    lines = [
        "<!-- harness-coverage:start -->",
        f"**Готовность: {evidence_pct:.0f}%** — обновлено {date}",
        "",
        "_Источник: evidence% (product acceptance ledger). Pipeline/FSM% не используется._",
    ]
    if report_path:
        lines.append(f"Отчёт: `{report_path}`")
    lines.append("<!-- harness-coverage:end -->")
    return "\n".join(lines)


def readiness_for_run(
    run_root: Path,
    *,
    pipeline_pct: float | None = None,
    report_path: str = "",
    repo: Path | None = None,
) -> YouTrackReadiness:
    """Map YouTrack «Готовность» to evidence_pct; stub clearly if YT unavailable."""
    evidence_pct = evidence_pct_for_run(run_root)
    evidence_map = build_evidence_map(run_root)
    if evidence_pct is None:
        return YouTrackReadiness(
            available=False,
            evidence_pct=None,
            pipeline_pct=pipeline_pct,
            readiness_pct=None,
            marker_block="",
            reason="no_acceptance_json — refuse readiness %; comment in-progress only",
            evidence_map=[],
        )
    marker = format_coverage_marker(evidence_pct=evidence_pct, report_path=report_path)
    yt_ok = youtrack_creds_available(repo)
    if not yt_ok:
        return YouTrackReadiness(
            available=False,
            evidence_pct=evidence_pct,
            pipeline_pct=pipeline_pct,
            readiness_pct=evidence_pct,
            marker_block=marker,
            reason=(
                "YouTrack credentials not found "
                "(.env.local.youtrack / YOUTRACK_TOKEN) — "
                "payload ready for upsert when creds appear; "
                "do not publish FSM/pipeline% as readiness"
            ),
            evidence_map=evidence_map,
        )
    return YouTrackReadiness(
        available=True,
        evidence_pct=evidence_pct,
        pipeline_pct=pipeline_pct,
        readiness_pct=evidence_pct,
        marker_block=marker,
        reason="ok — upsert readiness_pct (=evidence_pct) only",
        evidence_map=evidence_map,
    )
