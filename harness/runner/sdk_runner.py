"""Исполнитель через Cursor SDK (Python). Тарифицируется из пула API (CURSOR_API_KEY).

cursor_sdk импортируется ЛЕНИВО: пакет harness должен импортироваться даже там,
где SDK не установлен (массовый воркер может работать только на CLI).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from harness.domain.enums import AgentEventKind
from harness.domain.models import AgentEvent
from harness.runner.base import AgentResult


def _estimate_cost(model: str) -> float:
    """Эвристика для fallback когда SDK отдал cost_credits=0 (подписочная модель).

    Реэкспорт из cli_runner чтобы не дублировать таблицу. Если SDK действительно
    тарифицирует 0 (бесплатный tier) — мы всё равно считаем оценку, чтобы
    `RUN_BUDGET` не стал иллюзией. Помечаем `cost_kind="estimated"` в вызывающем коде.
    """
    from harness.runner.cli_runner import _estimate_cost as _cli_est  # noqa: PLC0415
    return _cli_est(model)


class SdkRunner:
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("CURSOR_API_KEY", "")

    def _load_sdk(self) -> Any:
        try:
            import cursor_sdk  # noqa: PLC0415  (ленивый импорт by design)
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "cursor-sdk не установлен. Поставь `pip install cursor-sdk` "
                "или переведи роль на runner=cli."
            ) from exc
        return cursor_sdk

    async def run(
        self,
        prompt: str,
        *,
        model: str,
        cwd: Path,
        log_path: Path | None = None,
    ) -> AgentResult:
        sdk = self._load_sdk()
        Agent = sdk.Agent
        AgentOptions = sdk.AgentOptions
        LocalAgentOptions = sdk.LocalAgentOptions

        def _execute() -> tuple[Any, Any, list[AgentEvent]]:
            # cursor-sdk 0.1.x: Agent.create — sync context manager, не coroutine.
            with Agent.create(
                AgentOptions(
                    api_key=self._api_key,
                    model=model,
                    local=LocalAgentOptions(cwd=str(cwd)),
                ),
            ) as agent:
                run = agent.send(prompt)
                # v2-009: собираем транскрипт из run.messages() (sync iterator).
                # Если метод отсутствует (старый SDK) — fallback на пустой список.
                events: list[AgentEvent] = []
                messages_iter = getattr(run, "messages", None)
                if callable(messages_iter):
                    try:
                        for msg in messages_iter():
                            events.extend(_agent_events_from_message(msg))
                    except Exception:  # noqa: BLE001 (транскрипт не должен валить прогон)
                        pass
                result = run.wait()
                return agent, result, events

        try:
            agent, result, agent_events = await asyncio.to_thread(_execute)
        except sdk.CursorAgentError as exc:  # запуск не состоялся (auth/config/network)
            return AgentResult(ok=False, text="", error=str(exc))

        text = getattr(result, "result", "") or ""
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(text, encoding="utf-8")

        ok = getattr(result, "status", "error") == "finished"
        cost = float(getattr(result, "cost_credits", 0.0) or 0.0)

        # v2-003: SDK иногда отдаёт cost_credits=0 для подписочных/китайских моделей
        # (подтверждено на glm-5.2-high в run-20260630-093910). Без fallback бюджет
        # становится иллюзией — 7 ревьюеров и 7 эскалаций не попадают в учёт.
        # Если SDK отдал 0 — используем эвристику и помечаем `estimated`.
        cost_kind = "actual" if cost > 0 else "estimated"
        if cost <= 0:
            cost = _estimate_cost(model)

        # usage-поля — пробуем достать через getattr, не падаем если их нет.
        usage = getattr(result, "usage", None)
        tokens_in = 0
        tokens_out = 0
        ctx_remaining: int | None = None
        if usage is not None:
            tokens_in = int(
                getattr(usage, "input_tokens", 0)
                or getattr(usage, "prompt_tokens", 0) or 0
            )
            tokens_out = int(
                getattr(usage, "output_tokens", 0)
                or getattr(usage, "completion_tokens", 0) or 0
            )
            ctx_remaining = getattr(usage, "context_window_remaining", None)
            if ctx_remaining is not None:
                ctx_remaining = int(ctx_remaining)

        return AgentResult(
            ok=ok, text=text, cost_credits=cost, cost_kind=cost_kind,
            tokens_in=tokens_in, tokens_out=tokens_out,
            context_window_remaining=ctx_remaining,
            agent_id=getattr(agent, "agent_id", ""),
            error="" if ok else f"status={getattr(result, 'status', '?')}",
            events=agent_events,
        )


def _agent_events_from_message(msg: Any) -> list[AgentEvent]:
    """Преобразовать SDK-сообщение в список AgentEvent.

    SDK message structure (Python): `msg.type` ("assistant" / "tool_use" / ...) и
    `msg.message.content` — список блоков. Извлекаем text/tool_use/file_edit и
    складываем в события. Не падаем на незнакомых структурах — возвращаем пусто.
    """
    out: list[AgentEvent] = []
    content = getattr(getattr(msg, "message", None), "content", None)
    if not content:
        # Может быть message с прямым content
        content = getattr(msg, "content", None)
    if not content:
        return out

    for block in content:
        block_type = getattr(block, "type", "other")
        if block_type == "text":
            text = getattr(block, "text", "") or ""
            if text:
                out.append(AgentEvent(
                    kind=AgentEventKind.ASSISTANT_MSG.value,
                    payload={"text": text[:2000]},  # обрезаем для объёма
                ))
        elif block_type == "tool_use":
            out.append(AgentEvent(
                kind=AgentEventKind.TOOL_CALL.value,
                payload={
                    "name": getattr(block, "name", "unknown"),
                    "input": _safe_input(getattr(block, "input", None)),
                },
            ))
        elif block_type in ("tool_result", "tool_result_file"):
            # tool_result — не отдельное событие, а ответ; логируем как usage/other
            continue
        else:
            out.append(AgentEvent(
                kind=AgentEventKind.OTHER.value,
                payload={"block_type": block_type},
            ))
    return out


def _safe_input(inp: Any) -> Any:
    """Безопасно сериализовать input tool_call'а в JSON-совместимое значение."""
    if inp is None:
        return {}
    if isinstance(inp, (str, int, float, bool)):
        return inp
    if isinstance(inp, dict):
        # Обрезаем длинные значения
        return {k: (str(v)[:500] if not isinstance(v, (dict, list)) else v) for k, v in inp.items()}
    try:
        return str(inp)[:500]
    except Exception:  # noqa: BLE101
        return "<unrepresentable>"
