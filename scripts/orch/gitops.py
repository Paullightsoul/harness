"""Git-операции: integration-ветка, worktree-изоляция, последовательный мерж.

Функции синхронные (subprocess.run); в асинхронном планировщике их вызывают
через asyncio.to_thread. Все команды выполняются с явным cwd.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


@dataclass
class GitResult:
    code: int
    out: str


def _run(args: List[str], cwd: Path) -> GitResult:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return GitResult(code=proc.returncode, out=proc.stdout)


def is_repo(root: Path) -> bool:
    return _run(["git", "rev-parse", "--is-inside-work-tree"], root).code == 0


def ensure_integration_branch(root: Path, base: str, integration: str) -> GitResult:
    """Переключает root на чистую integration-ветку, созданную от base."""
    _run(["git", "checkout", base], root)
    # -B: создать или жёстко переустановить на base.
    return _run(["git", "checkout", "-B", integration, base], root)


def head(root: Path, ref: str) -> str:
    return _run(["git", "rev-parse", ref], root).out.strip()


def add_worktree(root: Path, path: Path, branch: str, base_ref: str) -> GitResult:
    """Создаёт изолированный worktree с веткой branch от base_ref."""
    # Чистим возможные остатки прошлого прогона.
    _run(["git", "worktree", "remove", "--force", str(path)], root)
    _run(["git", "worktree", "prune"], root)
    _run(["git", "branch", "-D", branch], root)
    return _run(
        ["git", "worktree", "add", "-B", branch, str(path), base_ref], root
    )


def remove_worktree(root: Path, path: Path, branch: str, delete_branch: bool) -> None:
    _run(["git", "worktree", "remove", "--force", str(path)], root)
    _run(["git", "worktree", "prune"], root)
    if delete_branch:
        _run(["git", "branch", "-D", branch], root)


def commit_all(worktree: Path, message: str) -> GitResult:
    _run(["git", "add", "-A"], worktree)
    # Может вернуть ненулевой код, если коммитить нечего — это не ошибка.
    return _run(["git", "commit", "-m", message], worktree)


def has_changes(worktree: Path, base_ref: str) -> bool:
    res = _run(["git", "diff", "--name-only", f"{base_ref}...HEAD"], worktree)
    return bool(res.out.strip())


def diff(worktree: Path, base_ref: str, max_lines: int) -> str:
    res = _run(["git", "diff", f"{base_ref}...HEAD"], worktree)
    lines = res.out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (дифф обрезан, всего {len(lines)} строк)"]
    return "\n".join(lines)


def merge_into_integration(
    root: Path, integration: str, branch: str, message: str
) -> Tuple[bool, str]:
    """Последовательный мерж ветки задачи в integration. (ok, log).

    При конфликте откатывает merge и возвращает (False, log) — задача уходит
    на доработку/блокировку, integration остаётся консистентной.
    """
    co = _run(["git", "checkout", integration], root)
    if co.code != 0:
        return False, co.out
    res = _run(["git", "merge", "--no-ff", "-m", message, branch], root)
    if res.code != 0:
        _run(["git", "merge", "--abort"], root)
        return False, res.out
    return True, res.out
