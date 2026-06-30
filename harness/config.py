"""Конфигурация harness. Значения читаются из окружения (как в scripts/lib.sh)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from harness.domain.enums import Role, RunnerKind


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_escalation_models() -> list[str]:
    return _csv(_env("ESCALATION_MODELS", "kimi-k2.5,glm-5.2-high"))


def _default_max_attempts() -> int:
    """Без явного MAX_ATTEMPTS — хватает на всю лестницу: escalate_after + число ступеней."""
    if os.environ.get("MAX_ATTEMPTS"):
        return int(os.environ["MAX_ATTEMPTS"])
    return int(_env("ESCALATE_AFTER", "2")) + len(_default_escalation_models())


@dataclass
class RoleConfig:
    """Модель и рантайм одной роли. Дорогие роли — SDK, массовый воркер — CLI/подписка."""

    model: str
    runner: RunnerKind


@dataclass
class Settings:
    root: Path = field(default_factory=lambda: Path(os.environ.get("HARNESS_ROOT", ".")).resolve())

    base_branch: str = field(default_factory=lambda: _env("BASE_BRANCH", "main"))
    max_attempts: int = field(default_factory=_default_max_attempts)
    max_parallel: int = field(default_factory=lambda: int(_env("MAX_PARALLEL", "3")))

    # Бюджет на один Run в кредитах/$ (None = без потолка). Жёсткий стоп при превышении.
    run_budget: float | None = field(
        default_factory=lambda: (
            float(os.environ["RUN_BUDGET"]) if os.environ.get("RUN_BUDGET") else None
        )
    )

    # Эскалация: сколько попыток даётся стартовой ступени (auto), прежде чем подниматься
    # по лестнице. Китайские модели подключаются только после провалов auto.
    escalate_after: int = field(default_factory=lambda: int(_env("ESCALATE_AFTER", "2")))
    # Ступени эскалации после базовой (auto). Дефолт — китайские модели: kimi -> glm.
    escalation_models: list[str] = field(default_factory=_default_escalation_models)

    # Требовать ли ручной аппрув перед мержем в base.
    human_gate_merge: bool = field(
        default_factory=lambda: _env("HUMAN_GATE_MERGE", "0") == "1"
    )

    # Anti-gaming: зона спеков/acceptance, которую воркеру править запрещено.
    protected_paths: list[str] = field(
        default_factory=lambda: _csv(_env("PROTECTED_PATHS", "tests/spec/**"))
    )

    # Re-plan «умного лида»: сколько раз оркестратор может переразбить/уточнить задачу,
    # прежде чем она уйдёт в BLOCKED (защита от бесконечного цикла re-plan).
    max_replans: int = field(default_factory=lambda: int(_env("MAX_REPLANS", "1")))

    # Песочница исполнения гейтов: local (на хосте) | docker (изолированный контейнер).
    sandbox_kind: str = field(default_factory=lambda: _env("HARNESS_SANDBOX", "local"))

    # Нотификации (Telegram). Если токен и чат заданы — шлём в Telegram, иначе в консоль.
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))

    roles: dict[Role, RoleConfig] = field(
        default_factory=lambda: {
            Role.ORCHESTRATOR: RoleConfig(
                model=_env("ORCH_MODEL", "opus-4.8"),
                runner=RunnerKind(_env("ORCH_RUNNER", "sdk")),
            ),
            Role.WORKER: RoleConfig(
                model=_env("WORKER_MODEL", "auto"),
                runner=RunnerKind(_env("WORKER_RUNNER", "cli")),
            ),
            Role.REVIEWER: RoleConfig(
                model=_env("REVIEWER_MODEL", "glm-5.2-high"),
                runner=RunnerKind(_env("REVIEWER_RUNNER", "sdk")),
            ),
        }
    )

    @property
    def db_path(self) -> Path:
        return self.root / ".harness" / "state.db"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def reviews_dir(self) -> Path:
        return self.root / "reviews"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / ".worktrees"

    def role(self, role: Role) -> RoleConfig:
        return self.roles[role]
