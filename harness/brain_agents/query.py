"""Query brain-agents cards / index (pointer retrieval, not RAG dumps)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from harness.brain_agents.cards import BrainCard, load_card
from harness.brain_agents.sync import DEFAULT_AGENT_ROOT


@dataclass(frozen=True, slots=True)
class QueryHit:
    card: BrainCard
    score: float
    pointer: str


def query_cards(
    query: str,
    *,
    agent_root: Path | None = None,
    limit: int = 5,
    kind: str | None = None,
) -> list[QueryHit]:
    """Rank cards by simple token overlap on id/title/summary/tags/path."""
    root = (agent_root or DEFAULT_AGENT_ROOT).expanduser()
    tokens = _tokens(query)
    if not tokens:
        return []
    cards = _load_all_cards(root)
    hits: list[QueryHit] = []
    for card in cards:
        if kind and card.kind != kind:
            continue
        score = _score(card, tokens)
        if score <= 0:
            continue
        hits.append(
            QueryHit(
                card=card,
                score=score,
                pointer=f"brain:{card.kind}:{card.path} — {card.title}",
            )
        )
    hits.sort(key=lambda h: (-h.score, h.card.id))
    return hits[: max(1, limit)]


def format_query_hits(hits: list[QueryHit]) -> str:
    if not hits:
        return "(no brain-agents cards matched)"
    lines = []
    for hit in hits:
        lines.append(
            f"- [{hit.score:.1f}] {hit.card.kind}/{hit.card.id}: {hit.card.title}\n"
            f"  pointer: {hit.card.path}\n"
            f"  {hit.card.summary[:160]}"
        )
    return "\n".join(lines)


def _load_all_cards(root: Path) -> list[BrainCard]:
    cards_dir = root / "cards"
    cards: list[BrainCard] = []
    if cards_dir.is_dir():
        for path in sorted(cards_dir.glob("*.json")):
            card = load_card(path)
            if card is not None:
                cards.append(card)
        return cards
    # Fallback: index.json only
    index_path = root / "index.json"
    if not index_path.is_file():
        return cards
    try:
        raw: object = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cards
    if not isinstance(raw, dict):
        return cards
    for item in raw.get("cards") or []:
        if not isinstance(item, dict):
            continue
        cards.append(
            BrainCard(
                id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                kind=str(item.get("kind") or "other"),
                path=str(item.get("path") or ""),
                summary="",
                tags=[str(t) for t in (item.get("tags") or [])],
                source="canon",
            )
        )
    return cards


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 2}


def _score(card: BrainCard, tokens: set[str]) -> float:
    hay = " ".join(
        [
            card.id,
            card.title,
            card.summary,
            card.kind,
            card.path,
            " ".join(card.tags),
        ]
    ).lower()
    score = 0.0
    for tok in tokens:
        if tok in hay:
            score += 1.0
            if tok in card.id.lower() or tok in card.title.lower():
                score += 1.5
    return score
