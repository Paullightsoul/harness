"""Конфигурация прогона. Всё переопределяется переменными окружения.

Совместимо с переменными из scripts/lib.sh, плюс новые для параллелизма:
  MAX_WORKERS         глобальный потолок одновременных воркеров (default 5)
  WORKER_TYPE_LIMITS  пер-типовые лимиты, напр. "codegen=100,backend=3"
  INTEGRATION_BRANCH  ветка-интегратор (default integration)
  GATES_CMD           команда гейтов (default "make check")
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


def _parse_type_limits(raw: str) -> Dict[str, int]:
    """`"codegen=100,backend=3"` -> {"codegen": 100, "backend": 3}."""
    limits: Dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        try:
            limits[name] = int(value.strip())
        except ValueError:
            continue
    return limits


@dataclass
class Config:
    root: Path

    orch_model: str = "claude-opus-4-8-thinking-high"
    worker_model: str = "auto"
    reviewer_model: str = "glm-5.2-high"

    max_workers: int = 5
    type_limits: Dict[str, int] = field(default_factory=dict)
    max_attempts: int = 3

    base_branch: str = "main"
    integration_branch: str = "integration"

    gates_cmd: List[str] = field(default_factory=lambda: ["make", "check"])
    cursor_flags: List[str] = field(default_factory=list)

    diff_max_lines: int = 400

    @classmethod
    def from_env(cls, root: Path) -> "Config":
        return cls(
            root=root,
            orch_model=os.environ.get("ORCH_MODEL", "claude-opus-4-8-thinking-high"),
            worker_model=os.environ.get("WORKER_MODEL", "auto"),
            reviewer_model=os.environ.get("REVIEWER_MODEL", "glm-5.2-high"),
            max_workers=int(os.environ.get("MAX_WORKERS", "5")),
            type_limits=_parse_type_limits(os.environ.get("WORKER_TYPE_LIMITS", "")),
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
            base_branch=os.environ.get("BASE_BRANCH", "main"),
            integration_branch=os.environ.get("INTEGRATION_BRANCH", "integration"),
            gates_cmd=shlex.split(os.environ.get("GATES_CMD", "make check")),
            cursor_flags=shlex.split(os.environ.get("CURSOR_FLAGS", "")),
            diff_max_lines=int(os.environ.get("DIFF_MAX_LINES", "400")),
        )

    def type_limit(self, worker_type: str) -> int:
        """Лимит конкурентности для типа воркера (или глобальный потолок)."""
        return self.type_limits.get(worker_type, self.max_workers)

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def reviews_dir(self) -> Path:
        return self.root / "reviews"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / ".worktrees"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def state_db(self) -> Path:
        return self.root / "state.db"
