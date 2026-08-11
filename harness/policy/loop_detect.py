"""v2-013 / V4 Phase 2: LOOP_STUCK — tool-hash / loop detect.

Если за попытку один и тот же tool_call (same name + same input) повторяется
больше `threshold` раз — воркер застрял. Источник: `AgentResult.events`
или сырые tool fingerprints из TaskTool result JSON.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Any

from harness.domain.enums import AgentEventKind
from harness.domain.models import AgentEvent


def loop_stuck(events: list[AgentEvent], threshold: int = 3) -> list[tuple[str, Any, int]]:
    """Возвращает [(tool_name, input, count), ...] для tool-call'ов, повторённых
    больше `threshold` раз с идентичным input.

    Идемпотентность: один и тот же Read одного и того же файла > 3 раз — маркер
    зацикливания. Не падает на незнакомых payload.
    """
    if not events:
        return []
    counter: Counter[tuple[str, str]] = Counter()
    payload_lookup: dict[tuple[str, str], Any] = {}
    for ev in events:
        if ev.kind != AgentEventKind.TOOL_CALL.value:
            continue
        name = ev.payload.get("name", "unknown")
        inp = ev.payload.get("input")
        key = (name, _hashable_input(inp))
        counter[key] += 1
        payload_lookup[key] = inp
    return [
        (key[0], payload_lookup[key], count)
        for key, count in counter.items() if count > threshold
    ]


def tool_hash(name: str, inp: Any) -> str:
    """Stable short fingerprint for (tool_name, input)."""
    raw = f"{name}\0{_hashable_input(inp)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def loop_stuck_from_fingerprints(
    fingerprints: list[str] | tuple[str, ...],
    *,
    threshold: int = 3,
) -> list[tuple[str, str, int]]:
    """Detect repeats from opaque tool-hash strings (TaskTool result path).

    Returns [(fingerprint, fingerprint, count), ...] over threshold.
    """
    if not fingerprints:
        return []
    counter: Counter[str] = Counter(fp for fp in fingerprints if fp)
    return [
        (fp, fp, count) for fp, count in counter.items() if count > threshold
    ]


def fingerprints_from_result_payload(payload: dict[str, Any]) -> list[str]:
    """Extract tool hashes from a TaskTool result JSON (best-effort)."""
    found: list[str] = []
    raw = payload.get("tool_hashes") or payload.get("tool_fingerprints")
    if isinstance(raw, list):
        found.extend(str(item) for item in raw if item)
    events = payload.get("events") or payload.get("tool_calls")
    if isinstance(events, list):
        for item in events:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("tool") or "unknown")
            inp = item.get("input") if "input" in item else item.get("arguments")
            found.append(tool_hash(name, inp))
    return found


def _hashable_input(inp: Any) -> str:
    """Сериализовать input в hashable строку для Counter."""
    if inp is None:
        return ""
    if isinstance(inp, (str, int, float, bool)):
        return str(inp)
    try:
        import json  # noqa: PLC0415 (локальный импорт)
        return json.dumps(inp, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(inp)
