"""Phase 4 meta: AHE-lite changelog + prediction vs outcome eval.

Optional / solo-safe. See ``docs/PHASE4-META.md``.
"""

from __future__ import annotations

from harness.meta.changelog import (
    ChangelogEntry,
    PredictedMetrics,
    append_entry,
    default_changelog_path,
    load_entries,
    update_entry,
)
from harness.meta.eval import EvalReport, evaluate_entries, render_eval_markdown
from harness.meta.outcomes import RunOutcome, collect_run_outcome

__all__ = [
    "ChangelogEntry",
    "EvalReport",
    "PredictedMetrics",
    "RunOutcome",
    "append_entry",
    "collect_run_outcome",
    "default_changelog_path",
    "evaluate_entries",
    "load_entries",
    "render_eval_markdown",
    "update_entry",
]
