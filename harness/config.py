"""Конфигурация harness. Значения читаются из окружения (как в scripts/lib.sh)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from harness.domain.enums import Role, RunnerKind
from harness.tasktool.resources import ResourcePolicy, default_mem_thresholds


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _resource_env(name: str, default: str) -> str:
    """Accept the harness prefix while retaining the concise deployment names."""
    return os.environ.get(f"HARNESS_{name}") or os.environ.get(name, default)


def _env_runner(role_specific_var: str, default: str) -> str:
    """v2-022c: `HARNESS_RUNNER` — master switch на все роли (для лёгкого
    переключения run-режим <-> chat-режим одной переменной, см. `harness mode`).
    Приоритет: явный `ROLE_RUNNER` > `HARNESS_RUNNER` > дефолт роли. Точечная
    настройка (например ревьюер остаётся на sdk, воркер на cursor_task) всё
    ещё работает — просто задай `ROLE_RUNNER` явно, он выигрывает.
    """
    return os.environ.get(role_specific_var) or os.environ.get("HARNESS_RUNNER") or default


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
    root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("HARNESS_ROOT")
            or os.environ.get("HARNESS_HOME")
            or "."
        ).resolve()
    )

    base_branch: str = field(default_factory=lambda: _env("BASE_BRANCH", "main"))
    max_attempts: int = field(default_factory=_default_max_attempts)
    max_parallel: int = field(default_factory=lambda: int(_env("MAX_PARALLEL", "6")))

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

    # TaskTool isolation: worktrees (safe parallel merges) vs in-place shared checkout.
    # V4 clone default ON (Honesty/isolation path). Prod `/home/1.harness` stays 0
    # until cutover — set HARNESS_USE_WORKTREES=0 explicitly to match legacy.
    use_worktrees: bool = field(
        default_factory=lambda: _env("HARNESS_USE_WORKTREES", "1") == "1"
    )

    # Escape hatch for optional/soft behaviour gates (phpunit-optional etc.).
    # Default off in v4; Phase 1.5 will refuse soft sensors unless this is 1.
    allow_soft_gates: bool = field(
        default_factory=lambda: _env("HARNESS_ALLOW_SOFT_GATES", "0") == "1"
    )

    # Design-on multi-tenant lease index (prod scale gated by Honesty-MVP).
    multi_tenant: bool = field(
        default_factory=lambda: _env("HARNESS_MULTI_TENANT", "1") == "1"
    )

    # ContextPack pointer-only brain policy (Phase 0.5) — enforced in build_context_pack.
    context_pointer_only: bool = field(
        default_factory=lambda: _env("HARNESS_CONTEXT_POINTER_ONLY", "1") == "1"
    )

    # Per-run SoT under .harness/runs/<id>/ (Phase 1). Legacy tasktool/ spool
    # still read when that is the only existing tree for a run_id.
    use_run_roots: bool = field(
        default_factory=lambda: _env("HARNESS_USE_RUN_ROOTS", "1") == "1"
    )
    run_root: str = field(
        default_factory=lambda: _env("HARNESS_RUN_ROOT", ".harness/runs")
    )
    tenant_id: str = field(
        default_factory=lambda: _env(
            "HARNESS_TENANT_ID", _env("USER", "anonymous")
        )
    )

    # Phase 1.5 Honesty: DONE requires sealed acceptance + attested ledger.
    # V4 default ON; emergency escape hatch HARNESS_EVIDENCE_GATE=0.
    evidence_gate: bool = field(
        default_factory=lambda: _env("HARNESS_EVIDENCE_GATE", "1") == "1"
    )
    # Semantic AC↔evidence 1:1 bind (default ON). Escape: ALLOW_EVIDENCE_REUSE=1.
    evidence_exclusive_bind: bool = field(
        default_factory=lambda: _env("HARNESS_EVIDENCE_EXCLUSIVE_BIND", "1") == "1"
    )
    # Require nested Task workers (distinct worker_ids) for medium+; default ON.
    require_nested_workers: bool = field(
        default_factory=lambda: _env("HARNESS_REQUIRE_NESTED_WORKERS", "1") == "1"
    )
    # Refuse DONE/advance on cosplay_risk for medium+ (default ON; set 0 to warn-only).
    nested_workers_hard_fail: bool = field(
        default_factory=lambda: _env("HARNESS_NESTED_WORKERS_HARD_FAIL", "1") == "1"
    )

    # Phase 2 governors (solo-safe defaults: aggressive flags OFF).
    precall_budget: bool = field(
        default_factory=lambda: _env("HARNESS_PRECALL_BUDGET", "0") == "1"
    )
    max_steps: int | None = field(
        default_factory=lambda: (
            int(v) if (v := _env("HARNESS_MAX_STEPS", "")).strip() else None
        )
    )
    max_wall_clock_sec: int | None = field(
        default_factory=lambda: (
            int(v) if (v := _env("HARNESS_MAX_WALL_CLOCK_SEC", "")).strip() else None
        )
    )
    max_tokens_est: int | None = field(
        default_factory=lambda: (
            int(v) if (v := _env("HARNESS_MAX_TOKENS_EST", "")).strip() else None
        )
    )
    loop_detect: bool = field(
        default_factory=lambda: _env("HARNESS_LOOP_DETECT", "1") == "1"
    )
    tool_hash_repeat: int = field(
        default_factory=lambda: int(_env("HARNESS_TOOL_HASH_REPEAT", "3"))
    )
    sprint_contract: bool = field(
        default_factory=lambda: _env("HARNESS_SPRINT_CONTRACT", "0") == "1"
    )
    # Observation masking hard cap inside feedback (P2.4); 0 = use section fraction only.
    observation_mask_chars: int = field(
        default_factory=lambda: int(_env("HARNESS_OBSERVATION_MASK_CHARS", "2400"))
    )

    # TaskTool ship: "manual" marks tasks DONE after review (no git merge/push);
    # "merge" keeps the MERGE_QUEUE + --approved-merge path.
    ship_mode: str = field(
        default_factory=lambda: _env("HARNESS_SHIP_MODE", "manual")
    )

    # После успешного мержа в base — git push (чтобы видеть прогресс на remote).
    push_after_merge: bool = field(
        default_factory=lambda: _env("HARNESS_PUSH_AFTER_MERGE", "0") == "1"
    )
    git_remote: str = field(default_factory=lambda: _env("HARNESS_GIT_REMOTE", "origin"))

    # Anti-gaming: зона спеков/acceptance, которую воркеру править запрещено.
    protected_paths: list[str] = field(
        default_factory=lambda: _csv(_env("PROTECTED_PATHS", "tests/spec/**"))
    )

    # Re-plan «умного лида»: сколько раз оркестратор может переразбить/уточнить задачу,
    # прежде чем она уйдёт в BLOCKED (защита от бесконечного цикла re-plan).
    max_replans: int = field(default_factory=lambda: int(_env("MAX_REPLANS", "1")))

    # Agent-in-agent (TaskTool): large-задачи сперва получают sub-orchestrator
    # dispatch, который декомпозирует работу на дочерние задачи (fan-out внутри
    # run'а). Спек-уровневый override: строка `Decompose: yes|no` в задаче.
    decompose_enabled: bool = field(
        default_factory=lambda: _env("HARNESS_DECOMPOSE", "1") == "1"
    )
    decompose_max_children: int = field(
        default_factory=lambda: int(_env("HARNESS_DECOMPOSE_MAX_CHILDREN", "12"))
    )

    # Phase 3 economics (solo-safe defaults: aggressive OFF).
    # When on: apply fan-out profile caps + breadth gate → DECOMPOSE_DENIED.
    economics_enabled: bool = field(
        default_factory=lambda: _env("HARNESS_ECONOMICS", "0") == "1"
    )
    fanout_profile: str = field(
        default_factory=lambda: _env("HARNESS_FANOUT_PROFILE", "solo")
    )
    # Human canon (read-only sync source) vs agent layer (HARNESS_BRAIN_ROOT).
    brain_canon: str = field(
        default_factory=lambda: _env("HARNESS_BRAIN_CANON", "/home/brain")
    )
    brain_root: str = field(
        default_factory=lambda: _env("HARNESS_BRAIN_ROOT", "/home/brain-agents")
    )

    # Phase 4 meta (AHE-lite). CLI always available; flag documents opt-in for
    # orch prompts / dogfood habit. Default off = solo-safe.
    meta_enabled: bool = field(
        default_factory=lambda: _env("HARNESS_META", "0") == "1"
    )

    # Context sizing. The host has ~125 GiB and the whole control-plane state is a
    # few MB, so the only scarce budget here is the agent's context window — these
    # defaults spend it deliberately rather than rationing it.
    # ContextPack render cap (acceptance, provides, brief, checkpoint, feedback,
    # lessons). Sub-budgets inside the pack scale from this value.
    context_char_budget: int = field(
        default_factory=lambda: int(_env("HARNESS_CONTEXT_CHAR_BUDGET", "30000"))
    )
    # Real unified diff handed to reviewer/goal-judge. Without it those roles can
    # only grade the worker's own prose about itself.
    review_diff_bytes: int = field(
        default_factory=lambda: int(_env("HARNESS_REVIEW_DIFF_BYTES", "40000"))
    )

    # Agent review threshold: complexities strictly below this skip the reviewer
    # (gates → ship/DONE). Default ``small`` preserves historical behavior
    # (only ``trivial`` auto-skips). Speed mode: ``large`` (review only large/high).
    review_min_complexity: str = field(
        default_factory=lambda: _env("HARNESS_REVIEW_MIN_COMPLEXITY", "small")
    )

    # When dependents become READY: ``done`` (after review/ship) or ``post_gate``
    # (as soon as parent passed local gates / entered REVIEW). post_gate shortens
    # the critical path; risk: parent may later get CHANGES while child already ran.
    dag_unlock: str = field(
        default_factory=lambda: _env("HARNESS_DAG_UNLOCK", "done")
    )

    # ADR-0013: пер-проектный журнал работ — после каждой DONE-задачи и по
    # завершении run'а запись (решения / что сделано / итерации) кладётся в
    # сам целевой репозиторий.
    project_journal_enabled: bool = field(
        default_factory=lambda: _env("HARNESS_PROJECT_JOURNAL", "1") == "1"
    )
    journal_dir: str = field(
        default_factory=lambda: _env("HARNESS_JOURNAL_DIR", "docs/history")
    )

    # Песочница исполнения гейтов: local (на хосте) | docker (изолированный контейнер).
    sandbox_kind: str = field(default_factory=lambda: _env("HARNESS_SANDBOX", "local"))

    # Pull TaskTool admission control. Defaults sized for /home host
    # (24 CPU / ~125 GiB): 12 parallel jobs, hard-clamped at 16 in controller.
    max_tasktool_jobs: int = field(
        default_factory=lambda: int(_resource_env("MAX_TASKTOOL_JOBS", "12"))
    )
    max_agent_slots: int = field(
        default_factory=lambda: int(_resource_env("MAX_AGENT_SLOTS", "12"))
    )
    max_heavy_jobs: int = field(
        default_factory=lambda: int(_resource_env("MAX_HEAVY_JOBS", "5"))
    )
    max_gates: int = field(default_factory=lambda: int(_resource_env("MAX_GATES", "4")))
    # Defaults scale to the machine's real capacity (cgroup limit when there is
    # one, else MemTotal); the tuned 16/8 GiB survive on the 125 GiB host. An
    # explicit env value always wins.
    soft_mem_available_bytes: int = field(
        default_factory=lambda: int(
            _resource_env("SOFT_MEM_AVAILABLE_BYTES", str(default_mem_thresholds()[0]))
        )
    )
    hard_mem_available_bytes: int = field(
        default_factory=lambda: int(
            _resource_env("HARD_MEM_AVAILABLE_BYTES", str(default_mem_thresholds()[1]))
        )
    )
    load_per_cpu_threshold: float = field(
        default_factory=lambda: float(_resource_env("LOAD_PER_CPU_THRESHOLD", "1.1"))
    )

    # Нотификации (Telegram). Если токен и чат заданы — шлём в Telegram, иначе в консоль.
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))

    roles: dict[Role, RoleConfig] = field(
        default_factory=lambda: {
            Role.ORCHESTRATOR: RoleConfig(
                model=_env("ORCH_MODEL", "claude-opus-4-8"),
                runner=RunnerKind(_env_runner("ORCH_RUNNER", "sdk")),
            ),
            Role.WORKER: RoleConfig(
                model=_env("WORKER_MODEL", "kimi-k2.5"),
                runner=RunnerKind(_env_runner("WORKER_RUNNER", "sdk")),
            ),
            Role.REVIEWER: RoleConfig(
                model=_env("REVIEWER_MODEL", "glm-5.2"),
                runner=RunnerKind(_env_runner("REVIEWER_RUNNER", "sdk")),
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

    # v2-022b: file-spool bridge для chat-режима (CursorTaskRunner).
    # Директория запросов/ответов между harness process и Cursor IDE оркестратором.
    # По умолчанию <root>/.harness/bridge; переопределяется через HARNESS_CHAT_BRIDGE.
    @property
    def bridge_dir(self) -> Path:
        explicit = os.environ.get("HARNESS_CHAT_BRIDGE", "")
        return Path(explicit) if explicit else self.root / ".harness" / "bridge"

    @property
    def resource_policy(self) -> ResourcePolicy:
        jobs, slots, heavy, gates, _children = self.effective_fanout_limits()
        return ResourcePolicy(
            max_tasktool_jobs=jobs,
            max_agent_slots=slots,
            max_heavy_jobs=heavy,
            max_gates=gates,
            soft_mem_available_bytes=self.soft_mem_available_bytes,
            hard_mem_available_bytes=self.hard_mem_available_bytes,
            load_per_cpu_threshold=self.load_per_cpu_threshold,
        )

    def effective_fanout_limits(self) -> tuple[int, int, int, int, int]:
        """(jobs, slots, heavy, gates, max_children) after economics profile."""
        from harness.policy.economics import (  # noqa: PLC0415
            effective_resource_limits,
            resolve_fanout_profile,
        )

        profile = resolve_fanout_profile(self.fanout_profile)
        return effective_resource_limits(
            profile=profile,
            force_economics=self.economics_enabled,
            baseline_jobs=self.max_tasktool_jobs,
            baseline_slots=self.max_agent_slots,
            baseline_heavy=self.max_heavy_jobs,
            baseline_gates=self.max_gates,
            baseline_children=self.decompose_max_children,
        )

    @property
    def effective_decompose_max_children(self) -> int:
        return self.effective_fanout_limits()[4]

    def role(self, role: Role) -> RoleConfig:
        return self.roles[role]
