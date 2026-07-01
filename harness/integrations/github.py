"""v2-033: обёртка над GitHub CLI (`gh`) — PR checks polling + логи упавших ранов.

Оборачиваем `gh`, а не бьём в REST API напрямую — тот же принцип, что и с
`cursor-agent`: используем уже настроенную на VPS аутентификацию (`gh auth
login`), не заводим отдельный GH_TOKEN в конфиге harness.

Статус относительно ROADMAP 3.14 (GitHub PR-интеграция вместо локального
мержа): 3.14 сама ещё не реализована — harness мержит задачи локально
(`WorktreeManager.merge_to_base`) и пушит `base` напрямую, PR на задачу
никто не создаёт автоматически. `pr_for_branch` в этом случае просто вернёт
`None` для ветки `task/<id>` — это ожидаемо, не ошибка. `Engine._maybe_run_ci_recovery`
использует это как safe no-op seam: как только 3.14 начнёт создавать PR на
каждую задачу, весь цикл recovery заработает без изменений в этом модуле.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_FAIL_RE = re.compile(r"\bfail(?:ed|ure)?\b", re.IGNORECASE)


@dataclass
class PullRequest:
    number: int
    url: str = ""


@dataclass
class CheckStatus:
    """Итог одного опроса `gh pr checks`."""

    all_passed: bool
    failed_run_ids: list[str]
    raw: str


class GitHubCli:
    """Тонкая асинхронная обёртка: один метод = один subprocess-вызов `gh`."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd

    async def _run(self, *args: str) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", *args, cwd=str(self._cwd),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return 127, "gh CLI не найден в PATH"
        out_bytes, _ = await proc.communicate()
        return proc.returncode or 0, out_bytes.decode("utf-8", errors="replace")

    async def pr_for_branch(self, branch: str) -> PullRequest | None:
        """Открытый PR для ветки. `None`, если такого нет — обычный случай, пока
        ROADMAP 3.14 не создаёт PR автоматически на каждую задачу."""
        code, out = await self._run("pr", "view", branch, "--json", "number,url")
        if code != 0:
            return None
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return None
        number = data.get("number")
        if not isinstance(number, int):
            return None
        return PullRequest(number=number, url=str(data.get("url", "")))

    async def create_pr(
        self, branch: str, base: str, title: str, body: str = "",
    ) -> PullRequest | None:
        """`gh pr create` — минимальный enabler для ручного/будущего PR-flow
        (не замена локального мержа целиком — это ROADMAP 3.14). `None` при
        неудаче (напр. `gh` не аутентифицирован) — не должен валить Run."""
        code, _ = await self._run(
            "pr", "create", "--head", branch, "--base", base,
            "--title", title, "--body", body or title,
        )
        if code != 0:
            return None
        return await self.pr_for_branch(branch)

    async def check_status(self, pr_number: int) -> CheckStatus:
        """`gh pr checks <number>` без `--watch` — движок сам управляет интервалом
        поллинга вызывающим кодом (упрощает тестирование, не блокирует event loop)."""
        code, out = await self._run("pr", "checks", str(pr_number))
        failed = _parse_failed_run_ids(out)
        return CheckStatus(all_passed=(code == 0 and not failed), failed_run_ids=failed, raw=out)

    async def failed_run_log(self, run_id: str) -> str:
        """`gh run view <id> --log-failed` — логи только упавших джобов/степов."""
        _, out = await self._run("run", "view", run_id, "--log-failed")
        return out


def _parse_failed_run_ids(checks_output: str) -> list[str]:
    """`gh pr checks` печатает построчно `name  state  ...  URL`; URL содержит
    `/actions/runs/<id>/job/<job_id>` — достаём run id из строк со статусом fail."""
    ids: list[str] = []
    for line in checks_output.splitlines():
        if not _FAIL_RE.search(line):
            continue
        m = _RUN_ID_RE.search(line)
        if m:
            ids.append(m.group(1))
    return ids
