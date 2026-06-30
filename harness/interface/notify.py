"""Нотификации о ключевых событиях (blocked, бюджет, human-gate, готово).

Notifier — async-протокол. Реализации:
- NullNotifier   — тишина (для тестов/выключено);
- ConsoleNotifier — в stderr (дефолт, всегда видно);
- TelegramNotifier — в чат через Bot API (httpx, ленивый импорт).

Ошибка доставки никогда не валит Run: TelegramNotifier ловит сбой и пишет в stderr.
"""

from __future__ import annotations

import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, title: str, body: str) -> None: ...


class NullNotifier:
    async def notify(self, title: str, body: str) -> None:
        return None


class ConsoleNotifier:
    async def notify(self, title: str, body: str) -> None:
        print(f"[notify] {title}: {body}", file=sys.stderr)


class TelegramNotifier:
    """Шлёт сообщение в Telegram-чат. httpx импортируется лениво (опц. зависимость)."""

    def __init__(self, token: str, chat_id: str, timeout: float = 10.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    async def notify(self, title: str, body: str) -> None:
        text = f"\U0001f9ed *{_escape(title)}*\n{_escape(body)}"
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "MarkdownV2"}
        try:
            import httpx  # noqa: PLC0415  (ленивый импорт опц. зависимости)

            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    print(
                        f"[notify:telegram] HTTP {resp.status_code}: {resp.text}", file=sys.stderr
                    )
        except Exception as exc:  # noqa: BLE001  (нотификация не должна валить Run)
            print(f"[notify:telegram] ошибка доставки: {exc}\n{title}: {body}", file=sys.stderr)


def build_notifier(token: str, chat_id: str) -> Notifier:
    """Telegram, если заданы токен и чат; иначе — консоль."""
    if token and chat_id:
        return TelegramNotifier(token, chat_id)
    return ConsoleNotifier()


_MD_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def _escape(text: str) -> str:
    out = []
    for ch in text:
        if ch in _MD_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# Обратная совместимость: прежний log_notifier.
log_notifier: Notifier = ConsoleNotifier()
