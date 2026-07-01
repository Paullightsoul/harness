"""v2-013: LOOP_STUCK — детектор зацикливания воркера на одном tool-call.

Если за попытку один и тот же tool_call (same name + same input) повторяется
больше `threshold` раз — воркер застрял. Источник: `AgentResult.events`
(транскрипт из `run.messages()`).
"""

from __future__ import annotations

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
