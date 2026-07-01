"""v2-015: парсер pre-flight understanding-brief.

Воркер перед основным прогоном возвращает машиночитаемый JSON:
  {"understand": "...", "files_i_will_touch": [...],
   "acceptance_i_will_satisfy": ["AC1", "AC2"],
   "assumptions": [...], "missing_context": ["..."]}

Если `missing_context` непустой → задача уходит в `NEEDS_CLARIFICATION`,
движок зовёт question-protocol (v2-020, follow-up) или ставит на паузу.

Поддерживает два формата: чистый JSON или fenced-блок ```brief ... ```.
Не падает на незнакомых полях — берёт только известные, остальные игнорирует.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_BRIEF_FENCE = re.compile(r"```brief\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class UnderstandingBrief:
    """Maшиночитный pre-flight ответ воркера перед кодом."""

    understand: str = ""
    files_i_will_touch: list[str] = field(default_factory=list)
    acceptance_i_will_satisfy: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    missing_context: list[str] = field(default_factory=list)
    raw: str = ""           # исходный текст агента (для аудита)
    parsed: bool = False    # True если удалось достать JSON

    @property
    def needs_clarification(self) -> bool:
        """True если воркер заявил missing_context — нужен вопрос пользователю."""
        return bool(self.missing_context)


def parse_brief(text: str) -> UnderstandingBrief:
    """Достать understanding-brief из вывода агента.

    Ищет fenced-блок ```brief ... ``` или сырой JSON. Возвращает UnderstandingBrief;
    `parsed=False` если ничего не нашли (воркер не выдал brief).
    """
    raw_json = _extract_brief_json(text)
    if raw_json is None:
        return UnderstandingBrief(raw=text, parsed=False)
    try:
        data: Any = json.loads(raw_json)
    except json.JSONDecodeError:
        return UnderstandingBrief(raw=text, parsed=False)
    if not isinstance(data, dict):
        return UnderstandingBrief(raw=text, parsed=False)
    return UnderstandingBrief(
        understand=str(data.get("understand", "")),
        files_i_will_touch=_as_str_list(data.get("files_i_will_touch")),
        acceptance_i_will_satisfy=_as_str_list(data.get("acceptance_i_will_satisfy")),
        assumptions=_as_str_list(data.get("assumptions")),
        missing_context=_as_str_list(data.get("missing_context")),
        raw=text,
        parsed=True,
    )


def _extract_brief_json(text: str) -> str | None:
    """Найти JSON в fenced-блоке ```brief ... ``` или как сырой JSON объект."""
    m = _BRIEF_FENCE.search(text)
    if m:
        return m.group(1).strip()
    # Fallback: первый { ... } блок, выглядящий как JSON объект.
    start = text.find("{")
    if start == -1:
        return None
    # Грубое извлечение — балансировка скобок.
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _as_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return [v]
    return [str(v)]
