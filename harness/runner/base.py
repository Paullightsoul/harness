"""Абстракция исполнителя агента. Прячет различие SDK vs CLI от движка.

Главный архитектурный приём: дорогие роли можно держать на SDK (пул API),
а массового воркера — на cursor-agent CLI (подписка), не меняя движок.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from harness.domain.models import AgentEvent


@dataclass
class AgentResult:
    """Итог одного прогона агента."""

    ok: bool                       # завершился ли успешно (не путать с качеством работы)
    text: str                      # финальный ответ агента
    cost_credits: float = 0.0      # потрачено кредитов (если рантайм отдаёт; иначе 0)
    agent_id: str = ""             # id для resume/инспекции
    error: str = ""
    # v2-003: тип стоимости. "actual" — провайдер отдал реальную, "estimated" —
    # эвристика (SDK вернул 0 для подписочной модели, или CLI-раннер).
    cost_kind: str = "estimate"
    tokens_in: int = 0             # prompt tokens (если рантайм отдаёт usage)
    tokens_out: int = 0            # completion tokens
    context_window_remaining: int | None = None  # None если рантайм не отдал
    # v2-009: транскрипт агент-событий (tool_call / assistant_msg / ...) из
    # `run.messages()`. Пусто для CLI-раннера и для SDK без messages().
    events: list[AgentEvent] = field(default_factory=list)


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
