"""Card schema for brain-agents (pointer-first, not full vault dumps)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CARD_SCHEMA_VERSION = 1


@dataclass(slots=True)
class BrainCard:
    """Compact agent-readable index card pointing at a canon (or agent) note."""

    id: str
    title: str
    kind: str  # adr | architecture | incident | lesson | template | other
    path: str  # absolute or agent-relative path to source markdown
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    source: str = "canon"  # canon | agent
    updated_at: str = ""
    schema_version: int = CARD_SCHEMA_VERSION

    def ensure_timestamp(self) -> None:
        if not self.updated_at:
            self.updated_at = datetime.now(UTC).isoformat()


def card_to_dict(card: BrainCard) -> dict[str, Any]:
    card.ensure_timestamp()
    return asdict(card)


def write_card(cards_dir: Path, card: BrainCard) -> Path:
    """Persist one card as ``cards/<id>.json`` (id sanitized)."""
    cards_dir.mkdir(parents=True, exist_ok=True)
    safe_id = _safe_id(card.id)
    card.id = safe_id
    card.ensure_timestamp()
    path = cards_dir / f"{safe_id}.json"
    path.write_text(
        json.dumps(card_to_dict(card), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def load_card(path: Path) -> BrainCard | None:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return BrainCard(
            id=str(raw.get("id") or path.stem),
            title=str(raw.get("title") or ""),
            kind=str(raw.get("kind") or "other"),
            path=str(raw.get("path") or ""),
            summary=str(raw.get("summary") or ""),
            tags=[str(t) for t in (raw.get("tags") or []) if str(t).strip()],
            source=str(raw.get("source") or "canon"),
            updated_at=str(raw.get("updated_at") or ""),
            schema_version=int(raw.get("schema_version") or CARD_SCHEMA_VERSION),
        )
    except (TypeError, ValueError):
        return None


def _safe_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value.strip())
    return cleaned.strip("-")[:120] or "card"
