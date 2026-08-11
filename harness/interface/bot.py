"""Двухсторонний Telegram-бот: уведомления уже шлёт Notifier, бот добавляет команды.

Команды (только из разрешённого чата TELEGRAM_CHAT_ID):
    /runs                 — последние Run'ы
    /status [run_id]      — статусы задач (по умолчанию последний Run)
    /approve <run> <task> — подтвердить отложенный мерж (human-gate) и продолжить
    /help                 — помощь

Запуск: `harness bot` (нужен extra `telegram`: httpx; и TELEGRAM_BOT_TOKEN/CHAT_ID).
Long-polling через getUpdates; httpx импортируется лениво.
"""

from __future__ import annotations

from harness.config import Settings
from harness.scheduler.engine import Engine
from harness.store.repository import Store

_HELP = (
    "Команды harness:\n"
    "/runs — последние Run'ы\n"
    "/status [run_id] — статусы задач\n"
    "/approve <run_id> <task_id> — подтвердить мерж и продолжить\n"
)


def parse_command(text: str) -> tuple[str, list[str]]:
    """'/approve r1 003@bot' -> ('approve', ['r1','003']). Без команды -> ('', [])."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", []
    cmd = parts[0].lstrip("/").split("@", 1)[0].lower()
    return cmd, parts[1:]


def _latest_run_id(store: Store) -> str | None:
    return store.latest_run_id()


def format_runs(store: Store, limit: int = 10) -> str:
    runs = store.list_runs(limit=limit)
    if not runs:
        return "Run'ов нет."
    return "\n".join(
        f"{run.id} [{run.status}] потрачено {run.spent_credits:.2f}" for run in runs
    )


def format_status(store: Store, run_id: str | None) -> str:
    run_id = run_id or _latest_run_id(store)
    if not run_id:
        return "нет Run"
    run = store.get_run(run_id)
    if run is None:
        return f"run {run_id} не найден"
    lines = [f"run {run_id} [{run.status}] потрачено {run.spent_credits:.2f}"]
    for t in store.list_tasks(run_id):
        lines.append(f"  task-{t.id} {t.status} attempts={t.attempts} {t.title}")
    return "\n".join(lines)


class TelegramBot:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._token = settings.telegram_bot_token
        self._chat_id = str(settings.telegram_chat_id)
        self._base = f"https://api.telegram.org/bot{self._token}"

    async def run(self) -> None:
        if not self._token or not self._chat_id:
            raise RuntimeError("заданы не все: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")
        import httpx  # noqa: PLC0415

        offset = 0
        async with httpx.AsyncClient(timeout=40.0) as client:
            await self._send(client, "harness bot запущен. /help")
            while True:
                updates = await self._get_updates(client, offset)
                for upd in updates:
                    uid = upd.get("update_id", 0)
                    if isinstance(uid, int):
                        offset = max(offset, uid + 1)
                    await self._handle(client, upd)

    async def _get_updates(self, client: object, offset: int) -> list[dict[str, object]]:
        try:
            resp = await client.get(  # type: ignore[attr-defined]
                f"{self._base}/getUpdates", params={"timeout": 30, "offset": offset}
            )
            data = resp.json()
        except Exception:  # noqa: BLE001  (сетевые сбои — просто ретраим)
            return []
        result = data.get("result", []) if isinstance(data, dict) else []
        return result if isinstance(result, list) else []

    async def _handle(self, client: object, upd: dict[str, object]) -> None:
        message = upd.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat", {})
        chat_id = str(chat.get("id")) if isinstance(chat, dict) else ""
        if chat_id != self._chat_id:  # отвечаем только в разрешённом чате
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        await self._send(client, await self._dispatch(text))

    async def _dispatch(self, text: str) -> str:
        cmd, args = parse_command(text)
        store = Store(self._s.db_path)
        if cmd in {"help", "start", ""}:
            return _HELP
        if cmd == "runs":
            return format_runs(store)
        if cmd == "status":
            return format_status(store, args[0] if args else None)
        if cmd == "approve":
            return await self._approve(store, args)
        return f"неизвестная команда: /{cmd}\n{_HELP}"

    async def _approve(self, store: Store, args: list[str]) -> str:
        if len(args) < 2:
            return "использование: /approve <run_id> <task_id>"
        run_id, task_id = args[0], args[1]
        try:
            await Engine(self._s, store).approve_merge(run_id, task_id)
        except (ValueError, RuntimeError) as exc:
            return f"approve не удался: {exc}"
        return f"approve ок: run {run_id}, task-{task_id} продолжаем"

    async def _send(self, client: object, text: str) -> None:
        try:
            await client.post(  # type: ignore[attr-defined]
                f"{self._base}/sendMessage", json={"chat_id": self._chat_id, "text": text}
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[bot] send error: {exc}")  # noqa: T201
