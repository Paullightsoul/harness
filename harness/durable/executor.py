"""Контракт исполнителя задачи для durable-плейна.

Durable-workflow детерминирован и состоит из шагов (DBOS steps). Все побочные
эффекты (запуск агента, гейты, git-merge) и обновления домена живут за этим
протоколом — так workflow можно тестировать с фейковым исполнителем, а в
проде подменять на реальный (worktree + runner + ProfileGate).

Методы синхронные: каждый DBOS-шаг исполняется в своём worker-потоке, и реальная
реализация может мостить async-вызовы через asyncio.run внутри шага.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from harness.config import Settings
from harness.domain.enums import Role, TaskStatus, Verdict
from harness.domain.models import Task
from harness.gates.profile_gate import ProfileGate
from harness.profile import load_profile
from harness.runner.base import AgentRunner
from harness.runner.factory import build_runner
from harness.sandbox.factory import build_sandbox
from harness.scheduler.engine import _parse_verdict
from harness.store.repository import Store
from harness.worktree.manager import WorktreeManager


@dataclass
class WorkerStepResult:
    ok: bool
    cost_credits: float = 0.0


@dataclass
class ReviewStepResult:
    verdict: str  # значение Verdict ("approve" | "changes")
    feedback: str = ""


@runtime_checkable
class TaskExecutor(Protocol):
    """Гранулярные операции одной задачи. Возвраты — простые сериализуемые данные."""

    @property
    def max_attempts(self) -> int: ...

    def setup(self, run_id: str, task_id: str) -> None:
        """Подготовить изоляцию (worktree/ветку) перед первой попыткой."""

    def worker(self, run_id: str, task_id: str, attempt: int, feedback: str) -> WorkerStepResult:
        """Прогнать воркера на попытке attempt с фидбэком прошлой итерации."""

    def gate(self, run_id: str, task_id: str) -> bool:
        """Машинные гейты (computational sensors). True = зелёные."""

    def review(
        self, run_id: str, task_id: str, attempt: int, gates_passed: bool
    ) -> ReviewStepResult:
        """LLM-ревью. Возвращает вердикт и фидбэк на доработку."""

    def merge(self, run_id: str, task_id: str) -> bool:
        """Слить ветку задачи в base. True = слито (False = конфликт)."""

    def teardown(self, run_id: str, task_id: str, *, merged: bool) -> None:
        """Снять worktree после успешного мержа."""


# ── v2-034: реальный исполнитель (worktree + runner + ProfileGate) ──────────
class EngineTaskExecutor:
    """`TaskExecutor` на реальной инфраструктуре — durable-аналог `Engine._process_task`.

    Контракт `TaskExecutor` синхронный (DBOS-шаги исполняются в worker-потоках
    без активного event loop) — каждый метод мостит в async через `asyncio.run()`.
    Это безопасно именно потому, что DBOS-шаг не исполняется внутри уже
    запущенного event loop (в отличие от `Engine`, который сам — asyncio-цикл).

    Сознательно НЕ портирует весь ADR-0005 пайплайн `Engine` (de-sloppify,
    eviction context, completion-signal, tiered pipeline, ...) — это базовый
    ADR-0003 контракт (worker→gate→review→merge), качественно связанный с
    durable-гарантиями (crash-safe resume), а не с полным набором v2-фич.
    Вердикт парсится тем же `_parse_verdict`, что и `Engine` (DRY, единая логика).
    """

    def __init__(
        self,
        settings: Settings,
        store: Store,
        repo_root: Path | None = None,
        base_branch: str = "main",
        worker_runner: AgentRunner | None = None,
        reviewer_runner: AgentRunner | None = None,
        gate: ProfileGate | None = None,
    ) -> None:
        self._s = settings
        self._store = store
        self._repo_root = repo_root or settings.root
        profile = load_profile(self._repo_root)
        self._worker_runner = worker_runner or build_runner(settings.role(Role.WORKER).runner)
        self._reviewer_runner = reviewer_runner or build_runner(
            settings.role(Role.REVIEWER).runner
        )
        self._gate = gate or ProfileGate(profile, build_sandbox(settings.sandbox_kind, profile))
        self._gates_brief = _gates_brief_text(profile.language, profile.gate_commands)
        self._review_model = profile.review_model or settings.role(Role.REVIEWER).model
        self._worktrees = WorktreeManager(
            repo_root=self._repo_root,
            worktrees_dir=self._repo_root / ".worktrees",
            base_branch=base_branch,
        )

    @property
    def max_attempts(self) -> int:
        return self._s.max_attempts

    def setup(self, run_id: str, task_id: str) -> None:
        """Подготовить worktree/ветку. Идемпотентно: если worktree с прошлого
        (незакеченного DBOS) прогона уже существует на диске — не пересоздаём
        (`WorktreeManager.create` использует `-B`, который иначе стирает уже
        сделанные коммиты попыток на этой ветке)."""
        task = self._require(run_id, task_id)
        if TaskStatus(task.status) == TaskStatus.PENDING:
            self._store.transition_task(task, TaskStatus.READY)
            task = self._require(run_id, task_id)
        existing = Path(task.worktree_path) if task.worktree_path else None
        if existing is not None and existing.exists():
            return
        worktree, branch = asyncio.run(self._worktrees.create(task_id))
        task.branch, task.worktree_path = branch, str(worktree)
        self._store.update_task_fields(task)

    def worker(self, run_id: str, task_id: str, attempt: int, feedback: str) -> WorkerStepResult:
        task = self._require(run_id, task_id)
        worktree = Path(task.worktree_path)
        prompt = self._worker_prompt(task, feedback)
        model = self._s.role(Role.WORKER).model
        log = self._s.root / "logs" / f"durable-worker-{task_id}-a{attempt}.log"
        wres = asyncio.run(
            self._worker_runner.run(prompt, model=model, cwd=worktree, log_path=log)
        )
        # git commit no-op'ится (returncode игнорируется), если правок нет — тот
        # же механизм даёт идемпотентность при переигрывании шага после краха.
        asyncio.run(
            self._worktrees.commit_all(worktree, f"task({task_id}): durable attempt {attempt}")
        )
        return WorkerStepResult(ok=wres.ok, cost_credits=wres.cost_credits)

    def gate(self, run_id: str, task_id: str) -> bool:
        task = self._require(run_id, task_id)
        worktree = Path(task.worktree_path)
        result = asyncio.run(self._gate.check(worktree))
        return result.passed

    def review(
        self, run_id: str, task_id: str, attempt: int, gates_passed: bool
    ) -> ReviewStepResult:
        task = self._require(run_id, task_id)
        worktree = Path(task.worktree_path)
        diff = asyncio.run(self._worktrees.diff_against_base(task.branch))
        prompt = self._reviewer_prompt(task, gates_passed, diff)
        log = self._s.root / "logs" / f"durable-reviewer-{task_id}-a{attempt}.log"
        rres = asyncio.run(
            self._reviewer_runner.run(prompt, model=self._review_model, cwd=worktree, log_path=log)
        )
        verdict = _parse_verdict(rres.text)
        if verdict is None:
            # Базовый ADR-0003 контракт (без v2-006 unparsed-эскалации): malformed
            # вывод ревьюера безопасно трактуем как CHANGES, не как APPROVE.
            return ReviewStepResult(verdict=Verdict.CHANGES.value, feedback=rres.text)
        return ReviewStepResult(
            verdict=verdict.value,
            feedback="" if verdict == Verdict.APPROVE else rres.text,
        )

    def merge(self, run_id: str, task_id: str) -> bool:
        """Слить ветку в base. Идемпотентно через саму git-семантику: повторный
        мерж уже влитой ветки — `Already up to date`, `merged=True`, без ошибки."""
        task = self._require(run_id, task_id)
        outcome = asyncio.run(self._worktrees.merge_to_base(task.branch, f"task({task_id})"))
        if not outcome.merged:
            return False
        gate_result = asyncio.run(self._gate.check(self._repo_root))
        return gate_result.passed

    def teardown(self, run_id: str, task_id: str, *, merged: bool) -> None:
        """Снять worktree. Идемпотентно: `git worktree remove` на уже убранном
        пути — не бросает исключение (`_Git.run` не проверяет returncode)."""
        asyncio.run(self._worktrees.remove(task_id))

    # ── промпты (упрощённая копия Engine._worker_prompt/_reviewer_prompt —
    # durable-плейн намеренно не тянет provides/SHARED_TASK_NOTES/tiered-pipeline
    # из ADR-0005, см. докстринг класса) ────────────────────────────────────
    def _worker_prompt(self, task: Task, feedback: str) -> str:
        role = (self._s.prompts_dir / "worker.md").read_text(encoding="utf-8")
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        parts = [role, self._gates_brief, "\n=== СПЕКА ЗАДАЧИ ===\n" + spec]
        if feedback:
            parts.append("\n=== ФИДБЭК РЕВЬЮЕРА (исправь это) ===\n" + feedback)
        return "\n".join(parts)

    def _reviewer_prompt(self, task: Task, gates_ok: bool, diff: str) -> str:
        role = (self._s.prompts_dir / "reviewer.md").read_text(encoding="utf-8")
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        return (
            f"{role}\n{self._gates_brief}\n=== СПЕКА ЗАДАЧИ ===\n{spec}\n\n"
            f"=== ГЕЙТЫ ПРОЕКТА: {'PASS' if gates_ok else 'FAIL'} ===\n\n"
            f"=== GIT DIFF относительно base ===\n{diff}\n"
        )

    def _require(self, run_id: str, task_id: str) -> Task:
        task = self._store.get_task(run_id, task_id)
        if task is None:
            raise ValueError(f"task {task_id} не найдена в run {run_id}")
        return task


def _gates_brief_text(language: str, gate_commands: list[str]) -> str:
    cmds = "\n".join(f"  - {c}" for c in gate_commands)
    return (
        f"\n=== ГЕЙТЫ ПРОЕКТА (язык: {language}) ===\n"
        f"Прогоняй эти команды сам, пока все не станут зелёными:\n{cmds}\n"
    )
