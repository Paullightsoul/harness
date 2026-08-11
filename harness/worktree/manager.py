"""Изоляция задач через git worktree + сериализованный merge queue.

Worktree даёт каждой параллельной задаче свою рабочую копию (нет конфликтов
рабочего дерева). Мерж в base сериализован общим asyncio.Lock: две «зелёные
по отдельности» ветки не должны давать красный base — после интеграции
гейты прогоняются повторно (это делает движок).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Deterministic excludes for every worktree commit/stage — even when the target
# repo has no .gitignore. Prevents harness git pollution (venv/__pycache__/caches
# sucked in by bare `git add -A`). Do NOT write these into the target .gitignore.
_GENERATED_DIR_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "cache",
    "htmlcov",
    ".tox",
    ".nox",
    ".hypothesis",
    ".eggs",
})
_GENERATED_DIR_SUFFIXES = (".egg-info",)
_GENERATED_FILE_SUFFIXES = (".pyc", ".pyo", ".pyd")
_GENERATED_FILE_NAMES = frozenset({
    ".coverage",
    "coverage.xml",
    ".DS_Store",
})

# Pathspecs for `git add -A -- . <excludes>` (glob magic required for **).
# Every directory exclude uses `**/…` so nested paths (nested/venv, …) match.
_GIT_ADD_EXCLUDES: tuple[str, ...] = (
    ":(glob,exclude)**/__pycache__/**",
    ":(glob,exclude)**/*.py[cod]",
    ":(glob,exclude)**/.pytest_cache/**",
    ":(glob,exclude)**/.mypy_cache/**",
    ":(glob,exclude)**/.ruff_cache/**",
    ":(glob,exclude)**/.venv/**",
    ":(glob,exclude)**/venv/**",
    ":(glob,exclude)**/node_modules/**",
    ":(glob,exclude)**/dist/**",
    ":(glob,exclude)**/build/**",
    ":(glob,exclude)**/cache/**",
    ":(glob,exclude)**/htmlcov/**",
    ":(glob,exclude)**/.tox/**",
    ":(glob,exclude)**/.nox/**",
    ":(glob,exclude)**/.hypothesis/**",
    ":(glob,exclude)**/.eggs/**",
    ":(glob,exclude)**/*.egg-info/**",
    ":(glob,exclude)**/.coverage/**",
    ":(glob,exclude)**/.coverage",
    ":(glob,exclude)**/coverage.xml",
    ":(glob,exclude)**/.DS_Store",
)

# Positive pathspecs to unstage previously staged generated artifacts
# (e.g. after a legacy `git add -A` in uncommitted_diff).
_GIT_RESET_GENERATED: tuple[str, ...] = (
    ":(glob)**/__pycache__/**",
    ":(glob)**/*.py[cod]",
    ":(glob)**/.pytest_cache/**",
    ":(glob)**/.mypy_cache/**",
    ":(glob)**/.ruff_cache/**",
    ":(glob)**/.venv/**",
    ":(glob)**/venv/**",
    ":(glob)**/node_modules/**",
    ":(glob)**/dist/**",
    ":(glob)**/build/**",
    ":(glob)**/cache/**",
    ":(glob)**/htmlcov/**",
    ":(glob)**/.tox/**",
    ":(glob)**/.nox/**",
    ":(glob)**/.hypothesis/**",
    ":(glob)**/.eggs/**",
    ":(glob)**/*.egg-info/**",
    ":(glob)**/.coverage/**",
    ":(glob)**/.coverage",
    ":(glob)**/coverage.xml",
    ":(glob)**/.DS_Store",
)


def is_generated_artifact(rel_path: str) -> bool:
    """True for known generated/heavy paths that must never enter a harness commit."""
    posix = rel_path.replace("\\", "/")
    while posix.startswith("./"):
        posix = posix[2:]
    posix = posix.lstrip("/")
    if not posix:
        return False
    name = posix.rsplit("/", 1)[-1]
    if name in _GENERATED_FILE_NAMES:
        return True
    if name.endswith(_GENERATED_FILE_SUFFIXES):
        return True
    for part in posix.split("/"):
        if part in _GENERATED_DIR_NAMES:
            return True
        if part.endswith(_GENERATED_DIR_SUFFIXES):
            return True
    return False


def _clip_diff(text: str, max_bytes: int) -> str:
    """Trim an oversized diff on a line boundary and say so explicitly.

    A silently cut diff reads as a complete one, so a reviewer would approve code
    they never saw; the marker turns that into a visible gap instead.
    """
    if max_bytes <= 0 or len(text) <= max_bytes:
        return text
    head = text[:max_bytes]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    omitted = len(text) - len(head)
    return (
        f"{head}\n"
        f"[diff truncated: {omitted} of {len(text)} chars omitted — "
        f"read the files directly to review the rest]\n"
    )


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
        if self._repo.resolve() == Path("/home"):
            raise ValueError("/home is the harness meta-root, not a target repository")
        code, _ = await self._git.run("rev-parse", "--verify", self._base)
        if code != 0:
            raise ValueError(f"base branch does not exist: {self._base}")
        branch = f"task/{task_id}"
        path = self._dir / f"task-{task_id}"
        if path.is_dir() and (path / ".git").exists():
            code, current = await self._git.run(
                "branch", "--show-current", cwd=path
            )
            if code != 0 or current.strip() != branch:
                raise RuntimeError(
                    f"existing worktree {path} is not on expected branch {branch}"
                )
            return path, branch
        self._dir.mkdir(parents=True, exist_ok=True)
        await self._git.run("worktree", "prune")
        branch_code, _ = await self._git.run(
            "rev-parse", "--verify", f"refs/heads/{branch}"
        )
        args = (
            ("worktree", "add", "--force", str(path), branch)
            if branch_code == 0
            else ("worktree", "add", "-b", branch, str(path), self._base)
        )
        code, out = await self._git.run(
            *args
        )
        if code != 0:
            raise RuntimeError(f"git worktree add failed: {out.strip()}")
        return path, branch

    async def diff_against_base(self, branch: str) -> str:
        _, out = await self._git.run("diff", f"{self._base}...{branch}")
        return out

    async def changed_files(self, branch: str) -> list[str]:
        """Список путей, изменённых веткой относительно base (для guard/anti-gaming)."""
        _, out = await self._git.run("diff", "--name-only", f"{self._base}...{branch}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def dirty_files(self, worktree: Path) -> list[str]:
        """Uncommitted + untracked source paths in a shared checkout (no commit).

        Used when ``HARNESS_USE_WORKTREES=0``: agents edit in-place and the human
        commits later, so finalize must not ``git add -A`` / commit the whole tree.
        """
        _, status = await self._git.run(
            "status", "--porcelain=v1", "--untracked-files=all", cwd=worktree
        )
        paths: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            rel = line[3:]
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            rel = rel.strip().strip('"')
            if not rel or is_generated_artifact(rel):
                continue
            paths.append(rel)
        return list(dict.fromkeys(paths))

    async def review_diff(
        self,
        worktree: Path,
        paths: Sequence[str],
        *,
        branch: str = "",
        max_bytes: int = 40_000,
    ) -> str:
        """Unified diff of ``paths`` for the reviewer, in either isolation mode.

        Committed worktree branches diff against base; a shared in-place checkout
        has nothing committed yet, so tracked edits come from ``git diff HEAD`` and
        new files are rendered via ``--no-index`` (never staged — the human owns
        the index in this mode).
        """
        scoped = [
            path for path in dict.fromkeys(paths)
            if path and not is_generated_artifact(path)
        ]
        if not scoped:
            return ""
        if branch:
            _, out = await self._git.run(
                "diff", f"{self._base}...{branch}", "--", *scoped
            )
            return _clip_diff(out, max_bytes)
        _, tracked_out = await self._git.run(
            "ls-files", "--", *scoped, cwd=worktree
        )
        tracked = {line.strip() for line in tracked_out.splitlines() if line.strip()}
        chunks: list[str] = []
        size = 0
        if tracked:
            _, out = await self._git.run(
                "diff", "HEAD", "--", *sorted(tracked), cwd=worktree
            )
            if out.strip():
                chunks.append(out)
                size += len(out)
        for path in scoped:
            if path in tracked:
                continue
            # Stop before reading further new files: one oversized addition would
            # otherwise be buffered whole just to be clipped away afterwards.
            if size > max_bytes:
                chunks.append(f"[diff truncated: remaining new files not read: {path} …]")
                break
            # --no-index exits 1 when files differ; that is the normal "added" case.
            _, out = await self._git.run(
                "diff", "--no-index", "--", os.devnull, path, cwd=worktree
            )
            if out.strip():
                chunks.append(out)
                size += len(out)
        return _clip_diff("\n".join(chunks), max_bytes)

    async def current_branch(self, cwd: Path | None = None) -> str:
        code, out = await self._git.run("branch", "--show-current", cwd=cwd)
        if code != 0:
            return ""
        return out.strip()

    async def commit_all(self, worktree: Path, message: str) -> None:
        await self.validate_pending_commit(worktree)
        await self._stage_worktree(worktree)
        code, out = await self._git.run("commit", "-q", "-m", message, cwd=worktree)
        if code != 0 and "nothing to commit" not in out.lower():
            raise RuntimeError(f"git commit failed: {out.strip()}")

    async def _stage_worktree(self, worktree: Path) -> None:
        """Stage tracked changes + safe untracked sources; never stage generated caches."""
        code, out = await self._git.run(
            "add", "-A", "--", ".", *_GIT_ADD_EXCLUDES, cwd=worktree,
        )
        if code != 0:
            raise RuntimeError(f"git add failed: {out.strip()}")
        # Drop generated paths that were already in the index (e.g. after a prior
        # full `git add -A`) without touching the working tree.
        await self._git.run("reset", "-q", "--", *_GIT_RESET_GENERATED, cwd=worktree)

    async def validate_pending_commit(self, worktree: Path) -> None:
        """Reject oversized changes before staging can mutate the index."""
        max_diff = int(os.environ.get("HARNESS_MAX_DIFF_BYTES", "1048576"))
        max_commit = int(os.environ.get("HARNESS_MAX_COMMIT_BYTES", "10485760"))
        _, diff = await self._git.run("diff", "--binary", "HEAD", cwd=worktree)
        _, status = await self._git.run(
            "status", "--porcelain=v1", "--untracked-files=all", cwd=worktree
        )
        untracked: list[Path] = []
        for line in status.splitlines():
            if not line.startswith("?? "):
                continue
            rel = line[3:]
            if is_generated_artifact(rel):
                continue
            untracked.append(worktree / rel)
        untracked_size = sum(path.stat().st_size for path in untracked if path.is_file())
        diff_size = len(diff.encode("utf-8"))
        if diff_size > max_diff:
            raise ValueError(f"pending diff exceeds {max_diff} bytes")
        if diff_size + untracked_size > max_commit:
            raise ValueError(f"pending commit exceeds {max_commit} bytes")

    async def uncommitted_diff(self, worktree: Path) -> str:
        """v2-028: diff рабочего дерева относительно HEAD (незакоммиченные правки).

        Используется de-sloppify pass'ом: агент правит файлы в worktree без
        коммита, движок проверяет объём правок и решает — коммитить или
        откатывать (`discard_uncommitted`).
        """
        await self._stage_worktree(worktree)  # untracked sources visible; caches excluded
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
            checkout_code, checkout_output = await self._git.run("checkout", self._base)
            if checkout_code != 0:
                return MergeOutcome(
                    merged=False,
                    conflict=False,
                    output=f"git checkout {self._base} failed: {checkout_output}",
                )
            branch_code, current = await self._git.run("branch", "--show-current")
            if branch_code != 0 or current.strip() != self._base:
                return MergeOutcome(
                    merged=False,
                    conflict=False,
                    output=f"base checkout verification failed: {current.strip()}",
                )
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

    async def cleanup_orphans(self, active_paths: set[Path] | list[Path]) -> None:
        """Remove stale task worktrees while preserving every active path."""
        active = {path.resolve() for path in active_paths}
        if not self._dir.exists():
            return
        for path in self._dir.glob("task-*"):
            if path.resolve() in active:
                continue
            await self._git.run("worktree", "remove", "--force", str(path))
            if path.exists():
                shutil.rmtree(path)
        await self._git.run("worktree", "prune")
