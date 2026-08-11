"""v2-022 + v2-022b: CursorTaskRunner — runner-adapter для chat-режима.

Chat-режим: текущий чат Cursor IDE становится оркестратором, воркеры — Task tool
сабагенты в отдельных окнах IDE. Harness остаётся control plane
(гейты/мерж/бюджет/DAG) — меняется только runner.

Реализация v2-022b — file-spool bridge (см. `harness/runner/bridge.py`):
  1. harness пишет `requests/<id>.json` (atomic) в spool-директорию;
  2. ждёт `responses/<id>.json` (poll, timeout);
  3. парсит → `AgentResult`, cleanup.

Spool-директорию задаёт `HARNESS_CHAT_BRIDGE` env (путь). Если env пустой —
runner работает в stub-режиме и возвращает понятную ошибку (back-compat с v2-022
stub'ом — существующий вызов без bridge-конфига не падает, а объясняет).

Контракт `AgentRunner` (см. `harness/runner/base.py`) не меняется — движок
`scheduler/engine.py` остаётся нетронутым.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from harness.domain.enums import Role
from harness.runner.base import AgentResult
from harness.runner.bridge import (
    BridgeProtocolError,
    BridgeRequest,
    BridgeResponse,
    cleanup,
    new_request_id,
    wait_response,
    write_request,
)

# По умолчанию ждём ответа от IDE-сабагента 30 минут. Chat-режим — interactive,
# но воркер может делать длинную задачу (тесты, рефакторинг). Настраивается env.
_DEFAULT_TIMEOUT_SEC = float(os.environ.get("HARNESS_CHAT_BRIDGE_TIMEOUT", "1800"))
_DEFAULT_POLL_SEC = float(os.environ.get("HARNESS_CHAT_BRIDGE_POLL", "2"))


class CursorTaskRunner:
    """Runner для chat-режима: оркестратор в Cursor IDE, воркеры через Task tool.

    Конструктор принимает `bridge_path` — путь к spool-директории (исторически
    назывался bridge_path из v2-022 stub'а; в v2-022b это директория file-spool,
    не сокет). None → stub-режим с понятной ошибкой (back-compat).
    """

    def __init__(
        self,
        bridge_path: str | None = None,
        *,
        role: Role | None = None,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self._bridge = bridge_path  # back-compat attr (используется в тестах factory)
        # v2-022c: роль вызывающего — прокидывается в BridgeRequest.role, чтобы
        # root chat (единственная точка вызова Task tool) знал, какой протокол
        # применить: у orchestrator'а вопросы разрешаются живьём через `resume`
        # ДО отправки response; у worker/reviewer — прозрачно транслируются
        # (существующий question-protocol v2-020 разберётся на python-стороне).
        self._role = role
        self._timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT_SEC
        self._poll_interval = (
            poll_interval if poll_interval is not None else _DEFAULT_POLL_SEC
        )

    async def run(
        self,
        prompt: str,
        *,
        model: str,
        cwd: Path,
        log_path: Path | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> AgentResult:
        if progress_callback:
            progress_callback("started")
        if self._bridge is None:
            # back-compat: без bridge env — stub с понятной ошибкой (v2-022 behavior).
            return AgentResult(
                ok=False,
                text="",
                error=(
                    "CursorTaskRunner требует IDE-bridge. Установи "
                    "HARNESS_CHAT_BRIDGE=<spool_dir> (см. `harness chat`) и "
                    "запусти оркестратор-чат в Cursor IDE со skill "
                    "`harness-bridge-worker`. Либо используй WORKER_RUNNER=cli "
                    "или sdk."
                ),
            )

        spool_dir = Path(self._bridge)
        req = BridgeRequest(
            id=new_request_id(),
            prompt=prompt,
            model=model,
            cwd=str(cwd),
            created_at=datetime.now(tz=UTC).isoformat(),
            log_path=str(log_path) if log_path else "",
            timeout_hint_sec=int(self._timeout),
            role=self._role.value if self._role is not None else "worker",
        )

        try:
            write_request(spool_dir, req)
        except OSError as exc:
            return AgentResult(
                ok=False, text="",
                error=f"bridge: cannot write request to {spool_dir}: {exc}",
            )
        if progress_callback:
            progress_callback(f"bridge: request queued (id={req.id})")

        try:
            resp: BridgeResponse | None = await wait_response(
                spool_dir,
                req.id,
                timeout=self._timeout,
                poll_interval=self._poll_interval,
                progress_callback=(
                    lambda elapsed: progress_callback(
                        f"bridge: waiting for response (id={req.id}, elapsed={elapsed:.1f}s)"
                    )
                    if progress_callback
                    else None
                ),
            )
        except BridgeProtocolError as exc:
            if progress_callback:
                progress_callback(f"bridge: invalid response (id={req.id})")
            return AgentResult(
                ok=False,
                text="",
                error=f"bridge: invalid response for request id={req.id}: {exc}",
            )

        if resp is None:
            # Timeout — сабагент не успел или IDE-сторона не запущена. Request
            # оставляем в spool (не cleanup) — skill может подобрать позже, а
            # пользователь увидит orphan-файл и поймёт, что响应 не пришёл.
            return AgentResult(
                ok=False, text="",
                error=(
                    f"bridge: timeout {self._timeout}s waiting for response "
                    f"(request id={req.id}, spool={spool_dir}). Проверь, что "
                    "оркестратор-чат в Cursor IDE запущен и skill "
                    "`harness-bridge-worker` активен."
                ),
            )

        # Ответ проверен на id и proto в read_response; только после этого
        # безопасно удалять обе стороны записи.
        cleanup(spool_dir, req.id)

        if log_path is not None and resp.text:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(resp.text, encoding="utf-8")
            except OSError:
                pass  # лог-файл не должен валить прогон

        cost = float(resp.cost_credits or 0.0)
        cost_kind = "actual" if cost > 0 else "estimated"
        if progress_callback:
            progress_callback("done")
        return AgentResult(
            ok=resp.ok,
            text=resp.text,
            cost_credits=cost,
            cost_kind=cost_kind,
            tokens_in=int(resp.tokens_in or 0),
            tokens_out=int(resp.tokens_out or 0),
            agent_id=resp.agent_id,
            error=resp.error,
            events=[],  # Task-tool не отдаёт транскрипт; live events — future work
        )
