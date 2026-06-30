"""Re-plan «умного лида» (ROADMAP 3.6): что делать, когда лестница исчерпана.

Когда воркер не справился даже на сильной модели, задача не уходит молча в BLOCKED —
её получает оркестратор (Opus) с историей попыток и фидбэком ревьюера и решает:

- REFINE — уточнить/переписать спеку и дать задаче ещё один заход (свежие попытки);
- SPLIT  — раздробить на под-задачи (их подхватит DAG), исходную закрыть как superseded;
- BLOCK  — действительно нужен человек.

Оркестратор обязан вернуть один fenced-блок ```replan с JSON-решением. Если распарсить
не удалось — консервативно считаем это BLOCK (человек разберётся).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

_BLOCK_RE = re.compile(r"```(?:replan|json)?\s*\n(.*?)\n```", re.DOTALL)


class ReplanAction(StrEnum):
    REFINE = "refine"
    SPLIT = "split"
    BLOCK = "block"


@dataclass
class SubtaskSpec:
    id: str
    title: str
    spec: str
    depends_on: list[str] = field(default_factory=list)
    complexity: str = "normal"


@dataclass
class ReplanDecision:
    action: ReplanAction
    reason: str = ""
    spec: str | None = None             # для REFINE — новый текст спеки
    subtasks: list[SubtaskSpec] = field(default_factory=list)  # для SPLIT


def parse_replan(text: str) -> ReplanDecision:
    """Парсит решение оркестратора. Любая ошибка/мусор => BLOCK (безопасный дефолт)."""
    payload = _extract_json(text)
    if payload is None:
        return ReplanDecision(ReplanAction.BLOCK, reason="не удалось распарсить решение re-plan")

    action = _parse_action(payload.get("action"))
    reason = str(payload.get("reason", ""))

    if action == ReplanAction.REFINE:
        spec = payload.get("spec")
        if not isinstance(spec, str) or not spec.strip():
            return ReplanDecision(ReplanAction.BLOCK, reason="refine без текста спеки")
        return ReplanDecision(ReplanAction.REFINE, reason=reason, spec=spec)

    if action == ReplanAction.SPLIT:
        subtasks = _parse_subtasks(payload.get("subtasks"))
        if not subtasks:
            return ReplanDecision(ReplanAction.BLOCK, reason="split без под-задач")
        return ReplanDecision(ReplanAction.SPLIT, reason=reason, subtasks=subtasks)

    return ReplanDecision(ReplanAction.BLOCK, reason=reason or "block")


def _extract_json(text: str) -> dict[str, object] | None:
    match = _BLOCK_RE.search(text)
    raw = match.group(1) if match else text
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parse_action(raw: object) -> ReplanAction:
    if isinstance(raw, str):
        try:
            return ReplanAction(raw.strip().lower())
        except ValueError:
            return ReplanAction.BLOCK
    return ReplanAction.BLOCK


def _parse_subtasks(raw: object) -> list[SubtaskSpec]:
    if not isinstance(raw, list):
        return []
    out: list[SubtaskSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        spec = item.get("spec")
        task_id = item.get("id")
        if not (isinstance(spec, str) and spec.strip() and isinstance(task_id, str) and task_id):
            continue
        deps_raw = item.get("depends_on", [])
        deps = [str(d) for d in deps_raw] if isinstance(deps_raw, list) else []
        complexity = "high" if str(item.get("complexity", "normal")).lower() == "high" else "normal"
        out.append(
            SubtaskSpec(
                id=task_id,
                title=str(item.get("title", task_id)),
                spec=spec,
                depends_on=deps,
                complexity=complexity,
            )
        )
    return out
