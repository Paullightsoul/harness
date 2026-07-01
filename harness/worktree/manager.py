"""Изоляция задач через git worktree + сериализованный merge queue.

Worktree даёт каждой параллельной задаче свою рабочую копию (нет конфликтов
рабочего дерева). Мерж в base сериализован общим asyncio.Lock: две «зелёные
по отдельности» ветки не должны давать красный base — после интеграции
гейты прогоняются повторно (это делает движок).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MergeOutcome:
    merged: bool
    conflict: bool
    output: str
    # v2-029: eviction context — конфликтующие файлы + diff с conflict-маркерами,
    # собранные ДО отката. Ralphinho "Merge Queue with Eviction": следующая попытка
    # получает полный контекст в feedback, а не слепой retry с note="merge conflict".
    conflicting_files: list[str] = field(default_factory=list)
    conflict_diff: str = ""


@dataclass
class PushOutcome:
    pushed: bool
    output: str


class _Git:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    async def run(self, *args: str, cwd: Path | None = None) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd or self._repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_bytes, _ = await proc.communicate()
        return proc.returncode or 0, out_bytes.decode("utf-8", errors="replace")


class WorktreeManager:
    def __init__(self, repo_root: Path, worktrees_dir: Path, base_branch: str) -> None:
        self._git = _Git(repo_root)
        self._repo = repo_root
        self._dir = worktrees_dir
        self._base = base_branch
        self._merge_lock = asyncio.Lock()  # сериализует мержи в base

    @property
    def base_branch(self) -> str:
        return self._base

    async def create(self, task_id: str) -> tuple[Path, str]:
        """Готовит worktree и ветку task/<id> от base. Возвращает (путь, ветка)."""
        branch = f"task/{task_id}"
        path = self._dir / f"task-{task_id}"
        self._dir.mkdir(parents=True, exist_ok=True)
        await self._git.run("worktree", "prune")
        # -B пересоздаёт ветку от base, --force переиспользует путь, если остался.
        await self._git.run(
            "worktree", "add", "--force", "-B", branch, str(path), self._base
        )
        return path, branch

    async def diff_against_base(self, branch: str) -> str:
        _, out = await self._git.run("diff", f"{self._base}...{branch}")
        return out

    async def changed_files(self, branch: str) -> list[str]:
        """Список путей, изменённых веткой относительно base (для guard/anti-gaming)."""
        _, out = await self._git.run("diff", "--name-only", f"{self._base}...{branch}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def commit_all(self, worktree: Path, message: str) -> None:
        await self._git.run("add", "-A", cwd=worktree)
        await self._git.run("commit", "-q", "-m", message, cwd=worktree)

    async def uncommitted_diff(self, worktree: Path) -> str:
        """v2-028: diff рабочего дерева относительно HEAD (незакоммиченные правки).

        Используется de-sloppify pass'ом: агент правит файлы в worktree без
        коммита, движок проверяет объём правок и решает — коммитить или
        откатывать (`discard_uncommitted`).
        """
        await self._git.run("add", "-A", cwd=worktree)  # untracked тоже видны в diff --cached
        _, out = await self._git.run("diff", "--cached", cwd=worktree)
        return out

    async def discard_uncommitted(self, worktree: Path) -> None:
        """v2-028: откатить незакоммиченные правки в worktree (staged + working tree)."""
        await self._git.run("reset", "--hard", "HEAD", cwd=worktree)
        await self._git.run("clean", "-fd", cwd=worktree)

    async def merge_to_base(self, branch: str, title: str) -> MergeOutcome:
        """Сериализованный мерж ветки в base. Конфликт откатывается.

        v2-029: перед `merge --abort` собираем eviction context (какие файлы
        конфликтуют + diff с conflict-маркерами) — иначе информация теряется
        безвозвратно после отката.
        """
        async with self._merge_lock:
            await self._git.run("checkout", self._base)
            code, out = await self._git.run(
                "merge", "--no-ff", "-m", f"merge {title}", branch
            )
            if code != 0:
                conflicting_files = await self._unmerged_files()
                conflict_diff = await self._unmerged_diff()
                await self._git.run("merge", "--abort")
                return MergeOutcome(
                    merged=False, conflict=True, output=out,
                    conflicting_files=conflicting_files, conflict_diff=conflict_diff,
                )
            return MergeOutcome(merged=True, conflict=False, output=out)

    async def _unmerged_files(self) -> list[str]:
        """v2-029: пути в состоянии конфликта (до `merge --abort`)."""
        _, out = await self._git.run("diff", "--name-only", "--diff-filter=U")
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def _unmerged_diff(self) -> str:
        """v2-029: diff рабочего дерева с conflict-маркерами (<<<<<<</=======/>>>>>>>)."""
        _, out = await self._git.run("diff")
        return out

    async def push_base(self, remote: str = "origin") -> PushOutcome:
        """Push base branch to remote so progress is visible on GitHub."""
        return await self.push_branch(self._base, remote)

    async def push_branch(self, branch: str, remote: str = "origin") -> PushOutcome:
        """v2-033: push произвольной ветки (не только base) — нужен для re-push
        задачной ветки после CI fix-pass (правим ветку PR, не base)."""
        code, out = await self._git.run("push", remote, branch)
        return PushOutcome(pushed=code == 0, output=out)

    async def remove(self, task_id: str) -> None:
        path = self._dir / f"task-{task_id}"
        await self._git.run("worktree", "remove", "--force", str(path))
