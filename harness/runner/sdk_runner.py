"""Исполнитель через Cursor SDK (Python). Тарифицируется из пула API (CURSOR_API_KEY).

cursor_sdk импортируется ЛЕНИВО: пакет harness должен импортироваться даже там,
где SDK не установлен (массовый воркер может работать только на CLI).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from harness.runner.base import AgentResult


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

        try:
            # Локальный рантайм: агент работает в cwd (наш git worktree).
            async with await Agent.create(
                AgentOptions(
                    api_key=self._api_key,
                    model=model,
                    local=LocalAgentOptions(cwd=str(cwd)),
                )
            ) as agent:
                run = await agent.send(prompt)
                result = await run.wait()
        except sdk.CursorAgentError as exc:  # запуск не состоялся (auth/config/network)
            return AgentResult(ok=False, text="", error=str(exc))

        text = getattr(result, "result", "") or ""
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(text, encoding="utf-8")

        ok = getattr(result, "status", "error") == "finished"
        cost = float(getattr(result, "cost_credits", 0.0) or 0.0)
        return AgentResult(
            ok=ok, text=text, cost_credits=cost,
            agent_id=getattr(agent, "agent_id", ""),
            error="" if ok else f"status={getattr(result, 'status', '?')}",
        )
