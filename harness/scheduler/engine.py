"""Движок-автомат: исполняет конечный автомат задач поверх store/graph/runner/gates.

Цикл на один Run:
  - вычисляет готовые узлы DAG (зависимости DONE),
  - запускает до MAX_PARALLEL воркеров, каждый в своём worktree,
  - на каждую задачу: worker -> gates -> reviewer -> APPROVE? merge-queue : feedback,
  - при исчерпании попыток включает эскалацию модели, затем ESCALATE/BLOCKED,
  - бюджет проверяется перед каждым дорогим вызовом (иначе Run -> PAUSED).

Состояние и журнал событий пишутся в store, поэтому Run переживает падение
процесса и продолжается с того же места (resume).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType, Role, RunStatus, TaskStatus, Verdict
from harness.domain.models import Review, Task
from harness.domain.state_machine import RECOVERABLE_STATUSES
from harness.gates.profile_gate import ProfileGate
from harness.interface.notify import build_notifier
from harness.policy.budget import BudgetExceeded, CostGovernor
from harness.policy.escalation import EscalationLadder
from harness.policy.guard import protected_violations
from harness.profile import load_profile
from harness.replan import ReplanAction, ReplanDecision, SubtaskSpec, parse_replan
from harness.runner.base import AgentResult
from harness.runner.factory import build_runner
from harness.sandbox.factory import build_sandbox
from harness.scheduler.dag import TaskGraph
from harness.store.repository import Store
from harness.worktree.manager import WorktreeManager

_VERDICT_RE = "VERDICT:"


class Engine:
    def __init__(self, settings: Settings, store: Store, repo_root: Path | None = None) -> None:
        self._s = settings
        self._store = store
        # repo_root — целевой репозиторий (мульти-проект); по умолчанию = control root.
        # settings.root остаётся домом harness (prompts, tasks, .harness/state.db, logs).
        self._repo_root = repo_root or settings.root
        self._profile = load_profile(self._repo_root)
        self._sandbox = build_sandbox(settings.sandbox_kind, self._profile)
        self._gate = ProfileGate(self._profile, self._sandbox)
        self._governor = CostGovernor(settings.run_budget)
        worker_cfg = settings.role(Role.WORKER)
        self._ladder = EscalationLadder(
            base_model=worker_cfg.model,
            rungs=settings.escalation_models,
            escalate_after=settings.escalate_after,
        )
        self._worker_runner = build_runner(worker_cfg.runner)
        self._reviewer_runner = build_runner(settings.role(Role.REVIEWER).runner)
        self._orchestrator_runner = build_runner(settings.role(Role.ORCHESTRATOR).runner)
        self._notifier = build_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self._worktrees: WorktreeManager | None = None
        self._replans: dict[str, int] = {}  # сколько раз задача уже переразбивалась

    # ── публичный вход ──────────────────────────────────────────────────────
    async def run(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"run {run_id} не найден")

        self._worktrees = WorktreeManager(
            repo_root=self._repo_root,
            worktrees_dir=self._repo_root / ".worktrees",
            base_branch=run.base_branch,
        )
        self._store.set_run_status(run_id, RunStatus.RUNNING.value)
        self._recover_interrupted(run_id)

        running: dict[str, asyncio.Task[None]] = {}

        while True:
            # Граф пересобирается каждый цикл: re-plan/SPLIT может добавить новые задачи.
            graph = TaskGraph(self._store.list_tasks(run_id))
            statuses = {t.id: TaskStatus(t.status) for t in self._store.list_tasks(run_id)}
            if graph.all_done(statuses):
                break

            for tid in graph.ready(statuses):
                if tid not in running and len(running) < self._s.max_parallel:
                    running[tid] = asyncio.create_task(self._process_task(run_id, tid))

            if not running:
                break  # ничего не готово и ничего не крутится => остались blocked/escalate

            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for tid, atask in list(running.items()):
                if not atask.done():
                    continue
                running.pop(tid)
                exc = atask.exception()
                if isinstance(exc, BudgetExceeded):
                    await self._pause_on_budget(run_id, running, exc)
                    return
                if exc is not None:
                    self._store.add_event(
                        run_id, EventType.ERROR, task_id=tid, detail={"error": str(exc)}
                    )

        await self._finalize(run_id)

    # ── human-gate: подтверждение мержа человеком ─────────────────────────────
    async def approve_merge(self, run_id: str, task_id: str) -> None:
        """Подтвердить отложенный мерж (HUMAN_GATE_MERGE=1) и продолжить Run.

        Задача в MERGE_QUEUE сливается в base, помечается DONE; затем Run продолжается
        обычным циклом (следующие готовые задачи). Удобно дёргать из CLI/Telegram.
        """
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"run {run_id} не найден")
        task = self._require_task(run_id, task_id)
        if TaskStatus(task.status) != TaskStatus.MERGE_QUEUE:
            raise ValueError(f"task {task_id} не в MERGE_QUEUE (текущий: {task.status})")

        self._worktrees = WorktreeManager(
            repo_root=self._repo_root,
            worktrees_dir=self._repo_root / ".worktrees",
            base_branch=run.base_branch,
        )
        outcome = await self._worktrees.merge_to_base(task.branch, f"task({task_id})")
        if not outcome.merged:
            self._advance(run_id, task_id, TaskStatus.READY, note="merge conflict при approve")
            await self._notifier.notify("Конфликт мержа", f"run {run_id}, task-{task_id}")
            return
        gate = await self._gate.check(self._repo_root)
        if not gate.passed:
            self._store.add_event(run_id, EventType.ERROR, task_id=task_id,
                                  detail={"reason": "post-merge gate failed"})
        self._advance(run_id, task_id, TaskStatus.DONE, note="approved")
        self._store.add_event(run_id, EventType.MERGED, task_id=task_id,
                              detail={"branch": task.branch, "approved": True})
        await self._worktrees.remove(task_id)
        self._store.set_run_status(run_id, RunStatus.RUNNING.value)
        await self.run(run_id)  # продолжить остаток DAG

    # ── обработка одной задачи (петля попыток) ───────────────────────────────
    async def _process_task(self, run_id: str, task_id: str) -> None:
        assert self._worktrees is not None
        task = self._require_task(run_id, task_id)

        if TaskStatus(task.status) == TaskStatus.PENDING:
            task = self._store.transition_task(task, TaskStatus.READY)

        worktree, branch = await self._worktrees.create(task_id)
        task.branch, task.worktree_path = branch, str(worktree)
        self._store.update_task_fields(task)

        feedback = ""
        for attempt_no in range(1, self._s.max_attempts + 1):
            self._governor.check(self._spent(run_id))

            task = self._require_task(run_id, task_id)
            task = self._store.transition_task(task, TaskStatus.RUNNING)
            task.attempts = attempt_no
            self._store.update_task_fields(task)

            start_index = self._start_rung(task)
            model = self._ladder.model_for_attempt(attempt_no, start_index)
            attempt_id = self._store.start_attempt(run_id, task_id, attempt_no, model)

            # 1) ВОРКЕР
            wprompt = self._worker_prompt(task, feedback)
            wlog = self._s.root / "logs" / f"worker-{task_id}-a{attempt_no}.log"
            wres = await self._worker_runner.run(
                wprompt, model=model, cwd=worktree, log_path=wlog
            )
            self._account(run_id, wres.cost_credits)

            await self._worktrees.commit_all(worktree, f"task({task_id}): attempt {attempt_no}")

            # 2) ГЕЙТЫ
            task = self._advance(run_id, task_id, TaskStatus.GATING)
            gate = await self._gate.check(worktree)
            self._store.add_event(
                run_id, EventType.GATE_RESULT, task_id=task_id,
                detail={"passed": gate.passed},
            )

            # 3) ANTI-GAMING GUARD: воркер не должен править зону спеков/acceptance
            task = self._advance(run_id, task_id, TaskStatus.REVIEW)
            diff = await self._worktrees.diff_against_base(branch)
            changed = await self._worktrees.changed_files(branch)
            violations = protected_violations(changed, self._s.protected_paths)

            # 4) РЕВЬЮЕР (пропускаем, если guard уже сработал — это детерминированный отказ)
            if violations:
                self._store.add_event(
                    run_id, EventType.GUARD_VIOLATION, task_id=task_id,
                    detail={"files": violations},
                )
                rres = AgentResult(
                    ok=True,
                    text="VERDICT: CHANGES\nИзменена защищённая зона (spec/acceptance): "
                    + ", ".join(violations)
                    + ". Откати эти изменения — приёмку правит только оркестратор/ревьюер.",
                )
                verdict = Verdict.CHANGES
            else:
                rprompt = self._reviewer_prompt(task, gate.passed, gate.output, diff)
                rlog = self._s.root / "logs" / f"reviewer-{task_id}-a{attempt_no}.log"
                reviewer_model = self._profile.review_model or self._s.role(Role.REVIEWER).model
                rres = await self._reviewer_runner.run(
                    rprompt, model=reviewer_model, cwd=self._s.root, log_path=rlog
                )
                self._account(run_id, rres.cost_credits)
                verdict = _parse_verdict(rres.text)

            self._store.save_review(Review(
                attempt_id=attempt_id, task_id=task_id, verdict=verdict.value,
                report=rres.text, feedback="" if verdict == Verdict.APPROVE else rres.text,
            ))
            self._store.finish_attempt(
                attempt_id, run_id=run_id, task_id=task_id, worker_output=wres.text,
                gates_passed=gate.passed, verdict=verdict.value, cost_credits=wres.cost_credits,
            )
            self._store.add_event(
                run_id, EventType.REVIEW_RESULT, task_id=task_id,
                detail={
                    "verdict": verdict.value,
                    "model": model,
                    "escalated": self._ladder.is_escalated(attempt_no, start_index),
                },
            )

            # 5) РЕШЕНИЕ
            if verdict == Verdict.APPROVE and gate.passed:
                await self._merge(run_id, task_id, branch)
                return

            feedback = (
                f"Гейты: {'PASS' if gate.passed else 'FAIL'}.\n{rres.text}\n\n"
                f"--- хвост гейтов ---\n{gate.output}"
            )
            # На последней попытке НЕ возвращаем в READY: оставляем в REVIEW, чтобы
            # эскалация была валидным переходом REVIEW -> ESCALATE.
            if attempt_no < self._s.max_attempts:
                task = self._require_task(run_id, task_id)
                self._store.transition_task(task, TaskStatus.READY, note="на доработку")

        # попытки исчерпаны -> re-plan «умного лида» (оркестратор)
        await self._escalate(run_id, task_id, feedback)

    # ── мерж (через сериализованную очередь + повторные гейты) ───────────────
    async def _merge(self, run_id: str, task_id: str, branch: str) -> None:
        assert self._worktrees is not None
        task = self._require_task(run_id, task_id)
        self._store.transition_task(task, TaskStatus.MERGE_QUEUE)

        if self._s.human_gate_merge:
            self._store.add_event(run_id, EventType.HUMAN_GATE_WAIT, task_id=task_id,
                                  detail={"reason": "approve merge"})
            self._store.set_run_status(run_id, RunStatus.PAUSED.value)
            await self._notifier.notify(
                "Нужен аппрув мержа",
                f"run {run_id}, task-{task_id} ждёт.\n"
                f"Подтвердить: harness approve {run_id} {task_id}",
            )
            return

        outcome = await self._worktrees.merge_to_base(branch, f"task({task_id})")
        if not outcome.merged:
            # конфликт интеграции -> назад в работу с пометкой
            task = self._require_task(run_id, task_id)
            self._store.transition_task(task, TaskStatus.READY, note="merge conflict")
            return

        # повторный гейт на интегрированном base: «зелёные по отдельности» != зелёный base
        gate = await self._gate.check(self._repo_root)
        if not gate.passed:
            self._store.add_event(run_id, EventType.ERROR, task_id=task_id,
                                  detail={"reason": "post-merge gate failed"})
        task = self._require_task(run_id, task_id)
        self._store.transition_task(task, TaskStatus.DONE)
        self._store.add_event(run_id, EventType.MERGED, task_id=task_id, detail={"branch": branch})
        await self._worktrees.remove(task_id)

    async def _escalate(self, run_id: str, task_id: str, feedback: str = "") -> None:
        """Re-plan «умного лида»: оркестратор уточняет/дробит задачу вместо тихого BLOCKED."""
        self._advance(run_id, task_id, TaskStatus.ESCALATE, note="попытки исчерпаны")
        self._store.add_event(run_id, EventType.ESCALATED, task_id=task_id, detail={})

        if self._replans.get(task_id, 0) >= self._s.max_replans:
            self._advance(run_id, task_id, TaskStatus.BLOCKED, note="re-plan budget исчерпан")
            self._store.add_event(run_id, EventType.BLOCKED, task_id=task_id,
                                  detail={"reason": "replan budget"})
            await self._notifier.notify(
                "Задача заблокирована",
                f"run {run_id}, task-{task_id}: исчерпан бюджет re-plan. Нужен человек.",
            )
            return

        decision = await self._run_replan(run_id, task_id, feedback)
        self._replans[task_id] = self._replans.get(task_id, 0) + 1
        self._apply_replan(run_id, task_id, decision)
        if TaskStatus(self._require_task(run_id, task_id).status) == TaskStatus.BLOCKED:
            await self._notifier.notify(
                "Задача заблокирована",
                f"run {run_id}, task-{task_id}: re-plan={decision.action.value}. {decision.reason}",
            )

    async def _run_replan(self, run_id: str, task_id: str, feedback: str) -> ReplanDecision:
        task = self._require_task(run_id, task_id)
        role = (self._s.prompts_dir / "replan.md").read_text(encoding="utf-8")
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        prompt = (
            f"{role}\n\n=== ТЕКУЩАЯ СПЕКА ===\n{spec}\n\n"
            f"=== ПОЧЕМУ ВОРКЕР НЕ СПРАВИЛСЯ (фидбэк/гейты последней попытки) ===\n{feedback}\n"
        )
        log = self._s.root / "logs" / f"replan-{task_id}.log"
        res = await self._orchestrator_runner.run(
            prompt, model=self._s.role(Role.ORCHESTRATOR).model, cwd=self._s.root, log_path=log
        )
        self._account(run_id, res.cost_credits)
        return parse_replan(res.text)

    def _apply_replan(self, run_id: str, task_id: str, decision: ReplanDecision) -> None:
        if decision.action == ReplanAction.REFINE and decision.spec:
            task = self._require_task(run_id, task_id)
            Path(task.spec_path).write_text(decision.spec, encoding="utf-8")
            task.attempts = 0
            self._store.update_task_fields(task)
            self._advance(run_id, task_id, TaskStatus.READY, note="re-plan: refine")
            self._store.add_event(run_id, EventType.REPLANNED, task_id=task_id,
                                  detail={"action": "refine", "reason": decision.reason})
            return

        if decision.action == ReplanAction.SPLIT and decision.subtasks:
            ids = self._create_subtasks(run_id, decision.subtasks)
            self._advance(run_id, task_id, TaskStatus.BLOCKED,
                          note=f"split → {', '.join(ids)}")
            self._store.add_event(run_id, EventType.REPLANNED, task_id=task_id,
                                  detail={"action": "split", "subtasks": ids,
                                          "reason": decision.reason})
            return

        self._advance(run_id, task_id, TaskStatus.BLOCKED, note="re-plan: block")
        self._store.add_event(run_id, EventType.BLOCKED, task_id=task_id,
                              detail={"reason": decision.reason})

    def _create_subtasks(self, run_id: str, subtasks: list[SubtaskSpec]) -> list[str]:
        self._s.tasks_dir.mkdir(parents=True, exist_ok=True)
        created: list[str] = []
        for st in subtasks:
            spec_path = self._s.tasks_dir / f"task-{st.id}.md"
            spec_path.write_text(st.spec, encoding="utf-8")
            self._store.upsert_task(Task(
                id=st.id, run_id=run_id, title=st.title, spec_path=str(spec_path),
                status=TaskStatus.PENDING.value, depends_on=st.depends_on,
                complexity=st.complexity,
            ))
            self._store.add_event(run_id, EventType.TASK_CREATED, task_id=st.id,
                                  detail={"title": st.title, "from_replan": True})
            created.append(st.id)
        return created

    # ── промпты ──────────────────────────────────────────────────────────────
    def _worker_prompt(self, task: Task, feedback: str) -> str:
        role = (self._s.prompts_dir / "worker.md").read_text(encoding="utf-8")
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        provides = self._collect_provides(task)
        parts = [role, self._gates_brief(), "\n=== СПЕКА ЗАДАЧИ ===\n" + spec]
        if provides:
            parts.append("\n=== ИНТЕРФЕЙСЫ ЗАВИСИМОСТЕЙ (готовый код) ===\n" + provides)
        if feedback:
            parts.append("\n=== ФИДБЭК РЕВЬЮЕРА (исправь это) ===\n" + feedback)
        return "\n".join(parts)

    def _gates_brief(self) -> str:
        """Список гейтов проекта — воркер/ревьюер гоняют именно их, не зашитый make."""
        cmds = "\n".join(f"  - {c}" for c in self._profile.gate_commands)
        return (
            f"\n=== ГЕЙТЫ ПРОЕКТА (язык: {self._profile.language}) ===\n"
            f"Прогоняй эти команды сам, пока все не станут зелёными:\n{cmds}\n"
        )

    def _reviewer_prompt(self, task: Task, gates_ok: bool, gate_tail: str, diff: str) -> str:
        role = (self._s.prompts_dir / "reviewer.md").read_text(encoding="utf-8")
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        return (
            f"{role}\n{self._gates_brief()}\n=== СПЕКА ЗАДАЧИ ===\n{spec}\n\n"
            f"=== ГЕЙТЫ ПРОЕКТА: {'PASS' if gates_ok else 'FAIL'} ===\n{gate_tail}\n\n"
            f"=== GIT DIFF относительно base ===\n{diff}\n"
        )

    def _collect_provides(self, task: Task) -> str:
        chunks: list[str] = []
        for dep_id in task.depends_on:
            dep = self._store.get_task(task.run_id, dep_id)
            if dep and dep.provides:
                chunks.append(f"# из task-{dep_id}:\n{dep.provides}")
        return "\n\n".join(chunks)

    # ── вспомогательное ────────────────────────────────────────────────────────
    def _require_task(self, run_id: str, task_id: str) -> Task:
        task = self._store.get_task(run_id, task_id)
        if task is None:
            raise ValueError(f"task {task_id} не найдена в run {run_id}")
        return task

    def _advance(self, run_id: str, task_id: str, dst: TaskStatus, note: str = "") -> Task:
        """Прочитать актуальную задачу и сделать валидируемый переход в dst."""
        return self._store.transition_task(self._require_task(run_id, task_id), dst, note=note)

    @staticmethod
    def _start_rung(task: Task) -> int:
        """Стартовая ступень лестницы: high-сложность пропускает auto и стартует с kimi."""
        return 1 if task.complexity == "high" else 0

    def _recover_interrupted(self, run_id: str) -> None:
        """Resume: задачи, бывшие in-flight на момент падения, возвращаем в READY.

        Без этого задача в RUNNING/GATING/REVIEW/MERGE_QUEUE после рестарта зависла бы
        (ready() её не берёт). Сброс идёт через RECOVERING, чтобы переход был
        валидируемым и попал в журнал (ROADMAP 3.3).
        """
        for task in self._store.list_tasks(run_id):
            if TaskStatus(task.status) not in RECOVERABLE_STATUSES:
                continue
            origin = task.status
            recovering = self._store.transition_task(task, TaskStatus.RECOVERING, note="resume")
            self._store.transition_task(recovering, TaskStatus.READY, note="recovered")
            self._store.add_event(
                run_id, EventType.RECOVERED, task_id=task.id, detail={"from": origin}
            )

    def _spent(self, run_id: str) -> float:
        run = self._store.get_run(run_id)
        return run.spent_credits if run else 0.0

    def _account(self, run_id: str, credits: float) -> None:
        if credits > 0:
            spent = self._store.add_spend(run_id, credits)
            try:
                self._governor.check(spent)
            except BudgetExceeded:
                self._store.add_event(run_id, EventType.BUDGET_EXCEEDED,
                                      detail={"spent": spent})
                raise

    async def _pause_on_budget(
        self, run_id: str, running: dict[str, asyncio.Task[None]], exc: BudgetExceeded
    ) -> None:
        for atask in running.values():
            atask.cancel()
        self._store.set_run_status(run_id, RunStatus.PAUSED.value)
        self._store.add_event(run_id, EventType.BUDGET_EXCEEDED,
                              detail={"spent": exc.spent, "budget": exc.budget})
        await self._notifier.notify(
            "Бюджет исчерпан — Run на паузе",
            f"run {run_id}: потрачено {exc.spent:.2f} / бюджет {exc.budget:.2f}",
        )

    async def _finalize(self, run_id: str) -> None:
        tasks = self._store.list_tasks(run_id)
        all_done = all(TaskStatus(t.status) == TaskStatus.DONE for t in tasks)
        status = RunStatus.DONE if all_done else RunStatus.PAUSED
        self._store.set_run_status(run_id, status.value)
        blocked = [t.id for t in tasks if TaskStatus(t.status) == TaskStatus.BLOCKED]
        if all_done:
            await self._notifier.notify("Run завершён ✅", f"run {run_id}: все задачи DONE")
        else:
            await self._notifier.notify(
                "Run на паузе",
                f"run {run_id}: не все задачи готовы. BLOCKED: {', '.join(blocked) or '—'}",
            )


def _parse_verdict(text: str) -> Verdict:
    """Берёт последнюю строку 'VERDICT: APPROVE|CHANGES'. По умолчанию CHANGES (строго)."""
    last = Verdict.CHANGES
    for line in text.splitlines():
        s = line.strip().upper()
        if s.startswith(_VERDICT_RE):
            if "APPROVE" in s:
                last = Verdict.APPROVE
            elif "CHANGES" in s:
                last = Verdict.CHANGES
    return last
