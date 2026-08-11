"""Agent second-brain layer: cards, sync from human canon, query CLI.

Human vault ``/home/brain`` is read-only source of truth. Agents write only
into the agent layer (default ``/home/brain-agents``), never into the canon.
"""

from __future__ import annotations

from harness.brain_agents.cards import BrainCard, card_to_dict, load_card, write_card
from harness.brain_agents.query import query_cards
from harness.brain_agents.sync import (
    DEFAULT_CANON,
    DEFAULT_AGENT_ROOT,
    SyncResult,
    sync_brain_agents,
)

__all__ = [
    "DEFAULT_AGENT_ROOT",
    "DEFAULT_CANON",
    "BrainCard",
    "SyncResult",
    "card_to_dict",
    "load_card",
    "query_cards",
    "sync_brain_agents",
    "write_card",
]
