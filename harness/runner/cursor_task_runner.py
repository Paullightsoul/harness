"""v2-022: CursorTaskRunner — runner-adapter для chat-режима (Task tool в Cursor IDE).

Идея: вместо subprocess (CLI) или API (SDK) — оркестратор в Cursor IDE вызывает
Task tool для каждой задачи, и сабагент получает отдельное окно в IDE. Движок
harness остаётся control plane (гейты/мерж/бюджет/DAG), только runner другой.

Реальная интеграция требует двунаправленного канала между harness process и IDE
оркестратором (через cursor-sdk AsyncAgent или MCP-bridge). Здесь — stub, который
возвращает понятную ошибку если вызван вне IDE-bridge. Архитектурный seam
(RunnerKind + factory + AgentRunner contract) закрыт; рабочая интеграция —
follow-up через `harness chat` в Cursor IDE.
"""

from __future__ import annotations

from pathlib import Path

from harness.runner.base import AgentResult


class CursorTaskRunner:
    """Stub runner для chat-режима. Реальная интеграция — через IDE-bridge.

    Возвращает AgentResult с ошибкой и инструкцией. Когда `harness chat`
    запускается из Cursor IDE с подключённым bridge — этот runner заменяется
    на live-implementation, которая зовёт Task tool оркестратора.
    """

    def __init__(self, bridge_path: str | None = None) -> None:
        # bridge_path — путь к FIFO/файлу для двунаправленной связи с IDE.
        # None — stub-режим (возвращает ошибку).
        self._bridge = bridge_path

    async def run(
        self,
        prompt: str,
        *,
        model: str,
        cwd: Path,
        log_path: Path | None = None,
    ) -> AgentResult:
        if self._bridge is None:
            return AgentResult(
                ok=False,
                text="",
                error=(
                    "CursorTaskRunner требует IDE-bridge. Запусти `harness chat` из "
                    "Cursor IDE с подключённым bridge (follow-up v2-022b), либо "
                    "используй WORKER_RUNNER=cli или sdk."
                ),
            )
        # Реальная интеграция: записать Task Brief в bridge-файл, ждать результат.
        # Здесь — placeholder для follow-up.
        return AgentResult(
            ok=False, text="",
            error=f"bridge {self._bridge} — live integration is v2-022b (follow-up)",
        )
