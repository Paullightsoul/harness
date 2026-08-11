"""v2-022b: File-spool bridge между harness process и Cursor IDE оркестратором.

Протокол двунаправленной связи через файловую систему:

    <spool_dir>/
      requests/<id>.json    ← harness пишет (atomic: .tmp + rename), IDE читает
      responses/<id>.json   ← IDE пишет (atomic), harness читает

Почему файлы, а не FIFO/MCP/SDK:
- Task tool вызывается *агентом внутри Cursor IDE*, не внешним процессом.
  cursor_sdk.Agent.create создаёт топ-level агента (это делает SdkRunner), а не
  Task-tool-сабагент в отдельном окне текущего чата. MCP-сервера, оборачивающего
  IDE-ный Task tool для внешних клиентов, нет.
- File-spool: атомарный write→rename даёт single-writer/multi-reader семантику,
  переживает рестарт любой стороны, дебаггаемо (файлы видны), тривиально тестировать
  на tmp_path. Cursor-side poller — тонкий skill в `.cursor/skills/harness-bridge-worker/`.

Схема request/response — JSON с фиксированными полями. Версия — в поле `proto`,
чтобы будущие изменения определять явно.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROTO_VERSION = "1"


@dataclass
class BridgeRequest:
    """harness → IDE: «вызови Task tool с этим prompt»."""

    id: str
    prompt: str
    model: str
    cwd: str
    created_at: str
    subagent_type: str = "generalPurpose"
    log_path: str = ""
    proto: str = PROTO_VERSION
    # Подсказка для IDE-стороны — сколько ждать сабагента (не обязательное поле,
    # harness всё равно имеет свой timeout). Используется skill'ом для информации.
    timeout_hint_sec: int = 1800
    # v2-022c: роль вызывающего ("orchestrator" | "worker" | "reviewer") — root chat
    # использует это, чтобы выбрать протокол обработки: у orchestrator'а вопросы
    # (question-block) разрешаются ЖИВЬЁМ через Task tool `resume` до отправки
    # финального response; у worker/reviewer вопрос прозрачно долетает до harness
    # (существующий question-protocol v2-020 ставит NEEDS_CLARIFICATION). Значение
    # не влияет на сам bridge-транспорт — это подсказка для IDE-стороны (skill).
    role: str = "worker"


@dataclass
class BridgeResponse:
    """IDE → harness: финальный message от Task-tool сабагента."""

    id: str
    ok: bool
    text: str = ""
    error: str = ""
    cost_credits: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    agent_id: str = ""
    proto: str = PROTO_VERSION
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SpoolRecord:
    """Safe diagnostic view of a bridge file without exposing prompt contents."""

    id: str
    path: Path
    age_seconds: float
    role: str = ""


class BridgeProtocolError(ValueError):
    """A response exists, but cannot be accepted under the bridge protocol."""


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def new_request_id() -> str:
    """Короткий request id: timestamp + 8 hex chars — достаточно для уникальности
    в пределах spool-директории и читаемо в именах файлов."""
    return f"{int(time.time())}-{secrets.token_hex(4)}"


def _requests_dir(spool_dir: Path) -> Path:
    return spool_dir / "requests"


def _responses_dir(spool_dir: Path) -> Path:
    return spool_dir / "responses"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Атомарная запись JSON: пишем в .tmp, потом rename. Читающая сторона никогда
    не видит partial-файл — только полное содержимое или его отсутствие."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def write_request(spool_dir: Path, req: BridgeRequest) -> Path:
    """Записать request в spool. Возвращает путь к файлу (для логов/диагностики)."""
    path = _requests_dir(spool_dir) / f"{req.id}.json"
    _atomic_write_json(path, asdict(req))
    return path


def write_response(spool_dir: Path, resp: BridgeResponse) -> Path:
    """Atomically write an IDE response using the canonical serializer."""
    path = _responses_dir(spool_dir) / f"{resp.id}.json"
    _atomic_write_json(path, response_to_dict(resp))
    return path


def read_response(spool_dir: Path, request_id: str) -> BridgeResponse | None:
    """Прочитать response, если он уже есть. None — ещё не готов."""
    path = _responses_dir(spool_dir) / f"{request_id}.json"
    if not path.exists():
        return None
    return parse_response_file(path, expected_id=request_id)


def parse_response_file(path: Path, *, expected_id: str | None = None) -> BridgeResponse:
    """Разобрать response-файл в BridgeResponse. Не падает на лишних полях —
    игнорирует unknown keys. Некорректная схема, id или версия протокола
    отклоняются до того, как ответ попадёт в runner.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeProtocolError(f"malformed response JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BridgeProtocolError("malformed response: expected a JSON object")

    response_id = data.get("id")
    proto = data.get("proto")
    ok = data.get("ok")
    if not isinstance(response_id, str) or not response_id:
        raise BridgeProtocolError("malformed response: id must be a non-empty string")
    if expected_id is not None and response_id != expected_id:
        raise BridgeProtocolError(
            f"response id mismatch: expected {expected_id!r}, got {response_id!r}"
        )
    if proto != PROTO_VERSION:
        raise BridgeProtocolError(
            f"response protocol mismatch: expected {PROTO_VERSION!r}, got {proto!r}"
        )
    if not isinstance(ok, bool):
        raise BridgeProtocolError("malformed response: ok must be a boolean")

    try:
        return BridgeResponse(
            id=response_id,
            ok=ok,
            text=str(data.get("text", "")),
            error=str(data.get("error", "")),
            cost_credits=float(data.get("cost_credits", 0.0) or 0.0),
            tokens_in=int(data.get("tokens_in", 0) or 0),
            tokens_out=int(data.get("tokens_out", 0) or 0),
            agent_id=str(data.get("agent_id", "")),
            proto=proto,
            extra=data.get("extra", {}) if isinstance(data.get("extra"), dict) else {},
        )
    except (TypeError, ValueError) as exc:
        raise BridgeProtocolError(f"malformed response fields: {exc}") from exc


def response_to_dict(resp: BridgeResponse) -> dict[str, object]:
    """Сериализация BridgeResponse в dict для IDE-стороны (skill пишет это в файл).
    Вынесено отдельно, чтобы skill мог использовать тот же формат, что и runner —
    нет риска рассинхронизации схем."""
    return asdict(resp)


async def wait_response(
    spool_dir: Path,
    request_id: str,
    *,
    timeout: float,
    poll_interval: float = 2.0,
    progress_callback: Callable[[float], None] | None = None,
) -> BridgeResponse | None:
    """Ждать response файл до timeout. Возвращает BridgeResponse или None (timeout).

    Использует asyncio.sleep (не time.sleep) — в async-коде это hard rule.
    Poll-интервал намеренно небольшой (default 2s): chat-режим — interactive,
    задержка заметна пользователю в IDE-окне сабагента.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout
    while loop.time() < deadline:
        resp = read_response(spool_dir, request_id)
        if resp is not None:
            return resp
        if progress_callback is not None:
            progress_callback(loop.time() - started)
        await asyncio.sleep(poll_interval)
    return None


def cleanup(spool_dir: Path, request_id: str) -> None:
    """Удалить request и response файлы после обработки. Best-effort: ошибки
    подавляем (cleanup не должен валить прогон)."""
    with suppress(OSError):
        (_requests_dir(spool_dir) / f"{request_id}.json").unlink(missing_ok=True)
    with suppress(OSError):
        (_responses_dir(spool_dir) / f"{request_id}.json").unlink(missing_ok=True)


def _parse_request_file(path: Path) -> BridgeRequest | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return BridgeRequest(
            id=str(data.get("id", path.stem)),
            prompt=str(data.get("prompt", "")),
            model=str(data.get("model", "auto")),
            cwd=str(data.get("cwd", "")),
            created_at=str(data.get("created_at", "")),
            subagent_type=str(data.get("subagent_type", "generalPurpose")),
            log_path=str(data.get("log_path", "")),
            proto=str(data.get("proto", PROTO_VERSION)),
            timeout_hint_sec=int(data.get("timeout_hint_sec", 1800) or 1800),
            role=str(data.get("role", "worker")),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def list_pending_requests(spool_dir: Path) -> list[BridgeRequest]:
    """IDE-сторона: список необслуженных запросов (для poller-цикла skill'а).

    Сортировка по created_at — старые сначала (FIFO). Пропускает файлы без .json
    и .tmp-хвосты (atomic write ещё в процессе). Пропускает запросы, у которых
    уже есть response (обработанные, но не убранные cleanup'ом).
    """
    req_dir = _requests_dir(spool_dir)
    if not req_dir.exists():
        return []
    out: list[BridgeRequest] = []
    for entry in sorted(req_dir.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".json"):
            continue
        resp_path = _responses_dir(spool_dir) / entry.name
        if resp_path.exists():
            continue  # уже обработан
        request = _parse_request_file(entry)
        if request is not None:
            out.append(request)
    return out


def _file_age_seconds(path: Path, now: float) -> float | None:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def inspect_pending_requests(
    spool_dir: Path,
    *,
    now: float | None = None,
) -> list[SpoolRecord]:
    """List pending requests with age and role, safely skipping bad files."""
    observed_at = time.time() if now is None else now
    records: list[SpoolRecord] = []
    request_dir = _requests_dir(spool_dir)
    if not request_dir.exists():
        return records
    for path in sorted(request_dir.glob("*.json"), key=lambda item: item.name):
        if (_responses_dir(spool_dir) / path.name).exists():
            continue
        request = _parse_request_file(path)
        if request is None:
            continue
        age = _file_age_seconds(path, observed_at)
        if age is not None:
            records.append(
                SpoolRecord(
                    id=request.id,
                    path=path,
                    age_seconds=age,
                    role=request.role,
                )
            )
    return records


def list_orphan_responses(
    spool_dir: Path,
    *,
    now: float | None = None,
) -> list[SpoolRecord]:
    """List response files that have no matching request, without parsing payloads."""
    response_dir = _responses_dir(spool_dir)
    if not response_dir.exists():
        return []
    observed_at = time.time() if now is None else now
    records: list[SpoolRecord] = []
    for path in sorted(response_dir.glob("*.json"), key=lambda item: item.name):
        if (_requests_dir(spool_dir) / path.name).exists():
            continue
        age = _file_age_seconds(path, observed_at)
        if age is not None:
            records.append(SpoolRecord(id=path.stem, path=path, age_seconds=age))
    return records


def cleanup_stale(
    spool_dir: Path,
    *,
    max_age_seconds: float,
    now: float | None = None,
) -> list[Path]:
    """Remove only stale spool records, retaining every fresh side of a pair."""
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")

    observed_at = time.time() if now is None else now
    request_dir = _requests_dir(spool_dir)
    response_dir = _responses_dir(spool_dir)
    request_paths = {path.stem: path for path in request_dir.glob("*.json")}
    response_paths = {path.stem: path for path in response_dir.glob("*.json")}
    removed: list[Path] = []

    for record_id in sorted(request_paths.keys() | response_paths.keys()):
        paths = [
            path
            for path in (request_paths.get(record_id), response_paths.get(record_id))
            if path is not None
        ]
        ages = [_file_age_seconds(path, observed_at) for path in paths]
        if any(age is None or age <= max_age_seconds for age in ages):
            continue
        for path in paths:
            age = _file_age_seconds(path, observed_at)
            if age is None or age <= max_age_seconds:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path)
    return removed
