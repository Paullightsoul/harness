"""Абстракция исполнителя агента. Прячет различие SDK vs CLI от движка.

Главный архитектурный приём: дорогие роли можно держать на SDK (пул API),
а массового воркера — на cursor-agent CLI (подписка), не меняя движок.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class AgentResult:
    """Итог одного прогона агента."""

    ok: bool                       # завершился ли успешно (не путать с качеством работы)
    text: str                      # финальный ответ агента
    cost_credits: float = 0.0      # потрачено кредитов (если рантайм отдаёт; иначе 0)
    agent_id: str = ""             # id для resume/инспекции
    error: str = ""


@runtime_checkable
class AgentRunner(Protocol):
    """Контракт исполнителя. Реализации: SdkRunner, CliRunner."""

    async def run(
        self,
        prompt: str,
        *,
        model: str,
        cwd: Path,
        log_path: Path | None = None,
    ) -> AgentResult: ...
