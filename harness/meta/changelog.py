"""AHE-lite: falsifiable harness changelog with predicted metrics.

Each harness edit that claims an effect records a prediction; later dogfood
runs attach actuals via ``meta record`` / ``meta eval``. Never mutates prod
``/home/1.harness`` — lives under v4 ``.harness/meta/``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class PredictedMetrics:
    """Falsifiable predictions for the next dogfood window."""

    evidence_pct_min: float | None = None
    evidence_pct_delta_pp: float | None = None
    tokens_est_max: int | None = None
    tokens_est_delta_pct: float | None = None
    stall_count_max: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> PredictedMetrics:
        raw = raw or {}
        return cls(
            evidence_pct_min=_opt_float(raw.get("evidence_pct_min")),
            evidence_pct_delta_pp=_opt_float(raw.get("evidence_pct_delta_pp")),
            tokens_est_max=_opt_int(raw.get("tokens_est_max")),
            tokens_est_delta_pct=_opt_float(raw.get("tokens_est_delta_pct")),
            stall_count_max=_opt_int(raw.get("stall_count_max")),
        )


@dataclass
class ChangelogEntry:
    id: str
    at: str
    change: str
    components: list[str] = field(default_factory=list)
    hypothesis: str = ""
    predicted: PredictedMetrics = field(default_factory=PredictedMetrics)
    baseline_run_ids: list[str] = field(default_factory=list)
    verify_run_ids: list[str] = field(default_factory=list)
    actual: dict[str, Any] = field(default_factory=dict)
    status: str = "open"  # open | verified | falsified | waived | inconclusive
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "change": self.change,
            "components": list(self.components),
            "hypothesis": self.hypothesis,
            "predicted": self.predicted.to_dict(),
            "baseline_run_ids": list(self.baseline_run_ids),
            "verify_run_ids": list(self.verify_run_ids),
            "actual": dict(self.actual),
            "status": self.status,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ChangelogEntry:
        return cls(
            id=str(raw.get("id") or ""),
            at=str(raw.get("at") or ""),
            change=str(raw.get("change") or ""),
            components=[str(c) for c in (raw.get("components") or [])],
            hypothesis=str(raw.get("hypothesis") or ""),
            predicted=PredictedMetrics.from_dict(
                raw.get("predicted") if isinstance(raw.get("predicted"), dict) else {}
            ),
            baseline_run_ids=[str(x) for x in (raw.get("baseline_run_ids") or [])],
            verify_run_ids=[str(x) for x in (raw.get("verify_run_ids") or [])],
            actual=dict(raw.get("actual") or {})
            if isinstance(raw.get("actual"), dict)
            else {},
            status=str(raw.get("status") or "open"),
            notes=str(raw.get("notes") or ""),
        )


def default_changelog_path(harness_root: Path) -> Path:
    return harness_root / ".harness" / "meta" / "ahe-changelog.jsonl"


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _slug(text: str, *, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return (s or "change")[:max_len]


def new_entry_id(change: str, *, at: datetime | None = None) -> str:
    stamp = (at or datetime.now(tz=UTC)).strftime("%Y%m%d-%H%M%S")
    return f"ahe-{stamp}-{_slug(change)}"


def load_entries(path: Path) -> list[ChangelogEntry]:
    if not path.is_file():
        return []
    out: list[ChangelogEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict) and raw.get("id"):
            out.append(ChangelogEntry.from_dict(raw))
    return out


def append_entry(path: Path, entry: ChangelogEntry) -> ChangelogEntry:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
    return entry


def rewrite_entries(path: Path, entries: list[ChangelogEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in entries
    )
    path.write_text(body, encoding="utf-8")


def update_entry(path: Path, entry_id: str, **patches: Any) -> ChangelogEntry | None:
    entries = load_entries(path)
    found: ChangelogEntry | None = None
    updated: list[ChangelogEntry] = []
    for entry in entries:
        if entry.id != entry_id:
            updated.append(entry)
            continue
        data = entry.to_dict()
        for key, value in patches.items():
            if key == "predicted" and isinstance(value, PredictedMetrics):
                data["predicted"] = value.to_dict()
            elif key == "predicted" and isinstance(value, dict):
                data["predicted"] = value
            else:
                data[key] = value
        found = ChangelogEntry.from_dict(data)
        updated.append(found)
    if found is None:
        return None
    rewrite_entries(path, updated)
    return found
