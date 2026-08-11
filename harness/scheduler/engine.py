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
import os
from pathlib import Path
from typing import Any

from harness.config import Settings
from harness.domain.enums import EventType, Role, RunStatus, TaskStatus, Verdict
from harness.domain.models import AgentEvent, Review, Task
from harness.gates.base import GateResult
from harness.gates.profile_gate import ProfileGate
from harness.integrations.github import GitHubCli, PullRequest
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
from harness.tasktool.lifecycle import TaskLifecycleService, parse_review_verdict
from harness.worktree.manager import MergeOutcome, WorktreeManager

# v2-029: конфликтный diff может быть большим (combined-diff по нескольким файлам) —
# обрезаем хвостом, чтобы не раздувать контекст следующей попытки.
_CONFLICT_DIFF_MAX_LINES = 200


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
        self._worker_runner = build_runner(worker_cfg.runner, role=Role.WORKER)
        self._reviewer_runner = build_runner(
            settings.role(Role.REVIEWER).runner, role=Role.REVIEWER,
        )
        self._orchestrator_runner = build_runner(
            settings.role(Role.ORCHESTRATOR).runner, role=Role.ORCHESTRATOR,
        )
        self._notifier = build_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
        self._lifecycle = TaskLifecycleService(settings, store, self._repo_root)
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
            self._record_merge_eviction(run_id, task_id, outcome)
            self._advance(run_id, task_id, TaskStatus.READY, note="merge conflict при approve")
            await self._notifier.notify("Конфликт мержа", f"run {run_id}, task-{task_id}")
            return
        gate = await self._check_gates(self._repo_root, task_id, full=True)
        if not gate.passed:
            self._store.add_event(run_id, EventType.ERROR, task_id=task_id,
                                  detail={"reason": "post-merge gate failed"})
            self._store.set_run_status(run_id, RunStatus.PAUSED.value)
            return
        await self._complete_merge(run_id, task_id, task.branch, approved=True)
        self._store.set_run_status(run_id, RunStatus.RUNNING.value)
        await self.run(run_id)  # продолжить остаток DAG

    # ── обработка одной задачи (петля попыток) ───────────────────────────────
    async def _process_task(self, run_id: str, task_id: str) -> None:  # noqa: PLR0915, PLR0912
        assert self._worktrees is not None
        task = self._require_task(run_id, task_id)

        if TaskStatus(task.status) == TaskStatus.PENDING:
            task = self._store.transition_task(task, TaskStatus.READY)

        worktree, branch = await self._worktrees.create(task_id)
        task.branch, task.worktree_path = branch, str(worktree)
        self._store.update_task_fields(task)

        # v2-015: pre-flight understanding-brief — воркер даёт машиночитаемый
        # self-check перед кодом. Если missing_context непустой → NEEDS_CLARIFICATION.
        # Opt-in через HARNESS_PREFLIGHT=1 (для trivial-задач — overhead).
        if os.environ.get("HARNESS_PREFLIGHT", "0") == "1":
            clarification = await self._run_preflight(run_id, task_id, task, worktree)
            if clarification is not None:
                # brief с missing_context → ставим паузу, ждём ответа пользователя.
                return

        # v2-029: если предыдущий заход упал на merge-конфликте, первая попытка
        # этого захода получает eviction context (файлы+diff конфликта) как
        # стартовый feedback — не слепой retry с чистого листа.
        feedback = self._consume_eviction_context(task_id)
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
            # v2-024: SHARED_TASK_NOTES.md в worktree — мост между попытками.
            notes_path = worktree / "SHARED_TASK_NOTES.md"
            notes = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
            wprompt = self._worker_prompt(task, feedback, notes=notes)
            wlog = self._s.root / "logs" / f"worker-{task_id}-a{attempt_no}.log"
            wres = await self._worker_runner.run(
                wprompt, model=model, cwd=worktree, log_path=wlog
            )
            self._account(run_id, wres.cost_credits, cost_kind=wres.cost_kind)

            # v2-024: обновить SHARED_TASK_NOTES.md из output воркера (=== NOTES UPDATE ===).
            self._update_shared_notes(worktree, task_id, attempt_no, wres.text)

            # v2-009: сохранить транскрипт агент-событий в store + ndjson-лог.
            if wres.events:
                self._store.add_agent_events(run_id, task_id, attempt_no, wres.events)
                self._write_transcript_ndjson(task_id, attempt_no, wres.events, "worker")

            # v2-013: контекст-монитор — warnings в event-log.
            self._check_context_low(run_id, task_id, wres)
            self._check_loop_stuck(run_id, task_id, wres)

            # v2-020: question-protocol — воркер вернул question-блок с need_input.
            # Ставим NEEDS_CLARIFICATION, пишем INPUT_REQUESTED, выходим из цикла.
            from harness.tasks_io.question import parse_question_block  # noqa: PLC0415
            qblock = parse_question_block(wres.text)
            if qblock.has_questions:
                self._handle_input_request(run_id, task_id, qblock)
                return

            # v2-021: completion signal — воркер выдал HARNESS_DONE magic-phrase.
            # +1 к consecutive counter; при >= threshold и зелёных гейтах → DONE
            # без reviewer. CHANGES сбрасывает counter (ниже в блоке решения).
            if "HARNESS_DONE" in wres.text:
                task = self._require_task(run_id, task_id)
                task.completion_signals += 1
                self._store.update_task_fields(task)
                self._store.add_event(
                    run_id, EventType.COMPLETION_SIGNAL, task_id=task_id,
                    detail={"consecutive": task.completion_signals},
                )
            else:
                # Не выдал signal — сбросить counter (consecutive сломан).
                task = self._require_task(run_id, task_id)
                if task.completion_signals > 0:
                    task.completion_signals = 0
                    self._store.update_task_fields(task)

            await self._worktrees.commit_all(worktree, f"task({task_id}): attempt {attempt_no}")

            # v2-004: извлечь Provides: из вывода воркера и сохранить в task.provides —
            # зависимые воркеры получат этот блок как «ИНТЕРФЕЙСЫ ЗАВИСИМОСТЕЙ».
            extracted = self._extract_provides(wres.text)
            if extracted:
                task = self._require_task(run_id, task_id)
                task.provides = extracted
                self._store.update_task_fields(task)

            # 2) ГЕЙТЫ
            task = self._advance(run_id, task_id, TaskStatus.GATING)
            gate = await self._check_gates(worktree, task_id)
            self._store.add_event(
                run_id, EventType.GATE_RESULT, task_id=task_id,
                detail={"passed": gate.passed},
            )

            # v2-028: de-sloppify — отдельный focused cleanup-pass после зелёных
            # гейтов, перед reviewer. Ревьюер увидит уже очищенный diff (diff/changed
            # ниже читаются ПОСЛЕ этого вызова).
            gate = await self._run_de_sloppify(run_id, task_id, task, worktree, branch, gate)

            # 3) ANTI-GAMING GUARD: воркер не должен править зону спеков/acceptance
            task = self._advance(run_id, task_id, TaskStatus.REVIEW)
            diff = await self._worktrees.diff_against_base(branch)
            changed = await self._worktrees.changed_files(branch)
            violations = protected_violations(changed, self._s.protected_paths)

            # v2-013: SCOPE_EXIT — воркер тронул файлы вне списка «Файлы:» из спеки.
            self._check_scope_exit(run_id, task_id, task, changed)

            # v2-019: trivial — пропускаем reviewer (гейты как оракул истины).
            # guard violation всё равно ловится (защищённая зона).
            is_trivial = task.complexity == "trivial"

            # v2-021: completion signal threshold — N consecutive HARNESS_DONE с
            # зелёными гейтами → auto-APPROVE без reviewer. Threshold через env.
            threshold = int(os.environ.get("HARNESS_COMPLETION_THRESHOLD", "3"))
            signals_ok = task.completion_signals >= threshold

            # 4) РЕВЬЮЕР (пропускаем, если guard уже сработал — это детерминированный отказ;
            #    или если trivial — гейты достаточно; или completion signals >= threshold)
            provides_missing = (
                not task.provides and self._has_dependents(run_id, task_id)
            )
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
                verdict: Verdict | None = Verdict.CHANGES
            elif provides_missing:
                # v2-004: воркер не заявил Provides для задачи с dependents — зависимые
                # воркеры получат пустой «ИНТЕРФЕЙСЫ ЗАВИСИМОСТЕЙ» и работают вслепую.
                self._store.add_event(
                    run_id, EventType.PROVIDES_MISSING, task_id=task_id,
                    detail={"reason": "no Provides: block for task with dependents"},
                )
                rres = AgentResult(
                    ok=True,
                    text=(
                        "VERDICT: CHANGES\nНе заявлен Provides: блок, а от задачи "
                        "зависят другие. Перечисли интерфейсы (сигнатуры, пути "
                        "файлов, DTO), которые зависимые воркеры будут использовать."
                    ),
                )
                verdict = Verdict.CHANGES
            elif is_trivial and gate.passed:
                # v2-019: trivial задача с зелёными гейтами — auto-APPROVE без reviewer.
                # Гейты как оракул истины; guard уже проверил protected paths.
                self._store.add_event(
                    run_id, EventType.REVIEW_RESULT, task_id=task_id,
                    detail={"verdict": "approve", "tier": "trivial", "skipped_reviewer": True},
                )
                rres = AgentResult(
                    ok=True,
                    text="VERDICT: APPROVE\ntrivial tier — auto-approve on green gates",
                )
                verdict = Verdict.APPROVE
            elif signals_ok and gate.passed:
                # v2-021: N consecutive HARNESS_DONE + зелёные гейты → auto-APPROVE.
                self._store.add_event(
                    run_id, EventType.REVIEW_RESULT, task_id=task_id,
                    detail={"verdict": "approve", "reason": "completion_signals",
                            "signals": task.completion_signals, "threshold": threshold,
                            "skipped_reviewer": True},
                )
                rres = AgentResult(
                    ok=True,
                    text=f"VERDICT: APPROVE\n{task.completion_signals} consecutive "
                         "HARNESS_DONE signals — auto-approve",
                )
                verdict = Verdict.APPROVE
            else:
                rprompt = self._reviewer_prompt(task, gate.passed, gate.output, diff, worktree)
                rlog = self._s.root / "logs" / f"reviewer-{task_id}-a{attempt_no}.log"
                reviewer_model = self._profile.review_model or self._s.role(Role.REVIEWER).model
                # v2-001: ревьюер запускается в worktree воркера, не в доме harness —
                # иначе он не имеет кода в CWD и не может гонять гейты самостоятельно.
                rres = await self._reviewer_runner.run(
                    rprompt, model=reviewer_model, cwd=worktree, log_path=rlog
                )
                self._account(run_id, rres.cost_credits, cost_kind=rres.cost_kind)
                verdict = _parse_verdict(rres.text)

                # v2-009: транскрипт ревьюера — в store + ndjson.
                if rres.events:
                    self._store.add_agent_events(run_id, task_id, attempt_no, rres.events)
                    self._write_transcript_ndjson(task_id, attempt_no, rres.events, "reviewer")

                # Malformed-вывод ревьюера: вердикта нет → не сжигаем попытку на
                # CHANGES, а сразу эскалируем (событие VERDICT_UNPARSED для аудита).
                if verdict is None:
                    await self._handle_unparsed_verdict(
                        run_id, task_id, attempt_id, wres, rres, gate, reviewer_model
                    )
                    return

            # После guard-violation (verdict=CHANGES) или else+None-check verdict не None.
            assert verdict is not None
            self._store.save_review(Review(
                attempt_id=attempt_id, task_id=task_id, verdict=verdict.value,
                report=rres.text, feedback="" if verdict == Verdict.APPROVE else rres.text,
            ))
            self._store.finish_attempt(
                attempt_id, run_id=run_id, task_id=task_id, worker_output=wres.text,
                gates_passed=gate.passed, verdict=verdict.value,
                cost_credits=wres.cost_credits + rres.cost_credits,
                cost_kind=wres.cost_kind if wres.cost_kind == "actual" else rres.cost_kind,
                tokens_in=wres.tokens_in + rres.tokens_in,
                tokens_out=wres.tokens_out + rres.tokens_out,
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
            # v2-032: strategic compaction — если контекст этой попытки заканчивался,
            # следующая попытка получает подсказку скомпактить его самой (не авто).
            feedback += self._strategic_compact_hint(wres)
            # На последней попытке НЕ возвращаем в READY: оставляем в REVIEW, чтобы
            # эскалация была валидным переходом REVIEW -> ESCALATE.
            if attempt_no < self._s.max_attempts:
                task = self._require_task(run_id, task_id)
                self._store.transition_task(task, TaskStatus.READY, note="на доработку")

        # попытки исчерпаны -> re-plan «умного лида» (оркестратор)
        await self._escalate(run_id, task_id, feedback)

    # ── мерж (через сериализованную очередь + повторные гейты) ───────────────
    async def _handle_unparsed_verdict(
        self,
        run_id: str,
        task_id: str,
        attempt_id: int,
        wres: AgentResult,
        rres: AgentResult,
        gate: GateResult,
        reviewer_model: str,
    ) -> None:
        """v2-006: ревьюер не выдал VERDICT — эскалация без сжигания попытки.

        Пишем `VERDICT_UNPARSED` event с хвостом вывода (для аудита), сохраняем
        attempt/review с пометкой `unparsed`, зовём `_escalate` сразу. Так
        malformed-вывод ревьюера не конвертируется в доработку воркера на CHANGES.
        """
        self._store.add_event(
            run_id, EventType.VERDICT_UNPARSED, task_id=task_id,
            detail={"model": reviewer_model, "tail": rres.text[-300:]},
        )
        self._store.save_review(Review(
            attempt_id=attempt_id, task_id=task_id, verdict="unparsed",
            report=rres.text, feedback=rres.text,
        ))
        self._store.finish_attempt(
            attempt_id, run_id=run_id, task_id=task_id,
            worker_output=wres.text, gates_passed=gate.passed,
            verdict="unparsed", cost_credits=wres.cost_credits + rres.cost_credits,
            cost_kind=wres.cost_kind if wres.cost_kind == "actual" else rres.cost_kind,
            tokens_in=wres.tokens_in + rres.tokens_in,
            tokens_out=wres.tokens_out + rres.tokens_out,
        )
        await self._escalate(run_id, task_id, feedback=rres.text)

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
            # v2-029: eviction context — конфликт интеграции больше не «слепой retry»
            # с пустым feedback; следующая попытка получит файлы+diff конфликта.
            self._record_merge_eviction(run_id, task_id, outcome)
            task = self._require_task(run_id, task_id)
            self._store.transition_task(task, TaskStatus.READY, note="merge conflict")
            return

        # повторный гейт на интегрированном base: «зелёные по отдельности» != зелёный base
        gate = await self._check_gates(self._repo_root, task_id, full=True)
        if not gate.passed:
            self._store.add_event(run_id, EventType.ERROR, task_id=task_id,
                                  detail={"reason": "post-merge gate failed"})
            self._store.set_run_status(run_id, RunStatus.PAUSED.value)
            return
        await self._complete_merge(run_id, task_id, branch)

    # ── v2-029: merge queue eviction context ─────────────────────────────────
    def _eviction_context_path(self, task_id: str) -> Path:
        """Файл вне worktree (дом harness) — переживает пересоздание worktree/ветки
        следующей попытки (`WorktreeManager.create` пересоздаёт ветку от base)."""
        return self._s.root / "tasks" / f"task-{task_id}.eviction.md"

    def _record_merge_eviction(
        self, run_id: str, task_id: str, outcome: MergeOutcome,
    ) -> None:
        """Записать eviction context на диск + событие `MERGE_EVICTED`.

        Файл читается один раз `_consume_eviction_context` в начале следующего
        `_process_task` (attempt 1) и становится стартовым feedback воркеру.
        """
        path = self._eviction_context_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_format_eviction_context(outcome), encoding="utf-8")
        self._store.add_event(
            run_id, EventType.MERGE_EVICTED, task_id=task_id,
            detail={"conflicting_files": outcome.conflicting_files},
        )

    def _consume_eviction_context(self, task_id: str) -> str:
        """Прочитать и удалить (one-shot) eviction context, если он есть.

        Возвращает пустую строку, если конфликтов не было — обычный путь без
        дополнительного feedback на первую попытку.
        """
        path = self._eviction_context_path(task_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        path.unlink()
        return text

    async def _run_de_sloppify(
        self,
        run_id: str,
        task_id: str,
        task: Task,
        worktree: Path,
        branch: str,
        gate: GateResult,
    ) -> GateResult:
        """v2-028: отдельный cleanup-pass после зелёных гейтов, перед reviewer.

        ECC autonomous-loops §5: «два focused агента лучше одного constrained» —
        воркер спешит закрыть acceptance criteria и оставляет мусор (debug-print,
        мёртвый код, over-defensive checks); отдельный дешёвый агент в своём
        контексте чистит diff, не отвлекаясь на бизнес-логику. Ревьюер получает
        уже очищенный diff.

        Пропускается: trivial tier (там reviewer вообще не участвует — гейты
        достаточно), красные гейты (нечего чистить, пока код не рабочий), пустой
        diff, и полностью через `HARNESS_DESLOPPIFY=0`. Если cleanup ломает гейты —
        откат (де-слоппификация не должна влиять на бизнес-исход задачи).
        """
        if task.complexity == "trivial" or not gate.passed:
            return gate
        if os.environ.get("HARNESS_DESLOPPIFY", "1") != "1":
            return gate
        assert self._worktrees is not None

        diff = await self._worktrees.diff_against_base(branch)
        if not diff.strip():
            return gate  # воркер ничего не менял — нечего чистить

        prompt = self._de_sloppify_prompt(task, diff)
        model = os.environ.get("HARNESS_DESLOPPIFY_MODEL", "auto")
        log = self._s.root / "logs" / f"desloppify-{task_id}.log"
        dres = await self._worker_runner.run(prompt, model=model, cwd=worktree, log_path=log)
        self._account(run_id, dres.cost_credits, cost_kind=dres.cost_kind)

        cleanup_diff = await self._worktrees.uncommitted_diff(worktree)
        if not cleanup_diff.strip():
            self._store.add_event(
                run_id, EventType.DE_SLOPPIFIED, task_id=task_id,
                detail={"applied": False, "reason": "no changes"},
            )
            return gate

        new_gate = await self._gate.check(worktree)
        if new_gate.passed:
            await self._worktrees.commit_all(worktree, f"task({task_id}): de-sloppify cleanup")
            self._store.add_event(
                run_id, EventType.DE_SLOPPIFIED, task_id=task_id,
                detail={"applied": True, "reverted": False},
            )
            return new_gate

        # Cleanup сломал гейты — откатываем, задача продолжает как будто
        # де-слоппификации не было (её цель — гигиена, не риск для исхода).
        await self._worktrees.discard_uncommitted(worktree)
        self._store.add_event(
            run_id, EventType.DE_SLOPPIFIED, task_id=task_id,
            detail={"applied": False, "reverted": True, "reason": "gates failed after cleanup"},
        )
        return gate

    async def _extract_lesson(
        self, run_id: str, task_id: str, outcome: str,
    ) -> None:
        """v2-025: после терминального статуса — оркестратор пишет lesson в brain/.

        Только для medium/large задач (не trivial — шум). LLM-call через
        orchestrator runner; fallback — тихо пропустить если не вышло.
        """
        task = self._require_task(run_id, task_id)
        if task.complexity not in {"medium", "large"}:
            return
        try:
            role = (self._s.prompts_dir / "lesson.md").read_text(encoding="utf-8")
            spec = Path(task.spec_path).read_text(encoding="utf-8")[:1500]
            attempts = self._store.list_attempts(run_id, task_id)
            models = [a.model for a in attempts]
            prompt = (
                f"{role}\n\n=== СПЕКА ===\n{spec}\n\n"
                f"=== ИСХОД ===\n{outcome}\n\n"
                f"=== ПОПЫТКИ ===\n{len(attempts)}, модели: {', '.join(models)}\n"
            )
            log = self._s.root / "logs" / f"lesson-{task_id}.log"
            res = await self._orchestrator_runner.run(
                prompt, model=self._s.role(Role.ORCHESTRATOR).model,
                cwd=self._s.root, log_path=log,
            )
            self._account(run_id, res.cost_credits, cost_kind=res.cost_kind)
            if res.ok and res.text.strip():
                from harness.brain_agents.sync import (  # noqa: PLC0415
                    DEFAULT_AGENT_ROOT,
                    is_human_canon,
                )
                from harness.lessons.extractor import write_lesson_file  # noqa: PLC0415
                run = self._store.get_run(run_id)
                project = run.project if run else "default"
                # V4: agent layer only — never auto-write human /home/brain.
                brain_root = Path(
                    os.environ.get("HARNESS_BRAIN_ROOT", str(DEFAULT_AGENT_ROOT))
                ).expanduser()
                if is_human_canon(brain_root):
                    self._store.add_event(
                        run_id, EventType.ERROR, task_id=task_id,
                        detail={
                            "where": "lesson extraction",
                            "error": (
                                "refusing write to human brain canon; "
                                "set HARNESS_BRAIN_ROOT=/home/brain-agents"
                            ),
                        },
                    )
                    return
                write_lesson_file(brain_root, project, task_id, res.text)
                self._store.add_event(
                    run_id, EventType.TASK_CREATED, task_id=task_id,
                    detail={"lesson": "extracted", "outcome": outcome},
                )
        except Exception as exc:  # noqa: BLE001 (lesson extraction не должен валить Run)
            self._store.add_event(
                run_id, EventType.ERROR, task_id=task_id,
                detail={"where": "lesson extraction", "error": str(exc)[:200]},
            )

    async def _complete_merge(
        self, run_id: str, task_id: str, branch: str, *, approved: bool = False
    ) -> None:
        """Пометить задачу DONE, опционально push base на remote, убрать worktree."""
        assert self._worktrees is not None
        note = "approved" if approved else ""
        self._advance(run_id, task_id, TaskStatus.DONE, note=note)
        detail: dict[str, object] = {"branch": branch}
        if approved:
            detail["approved"] = True
        self._store.add_event(run_id, EventType.MERGED, task_id=task_id, detail=detail)
        await self._push_base_if_enabled(run_id, task_id)
        # v2-033: CI failure recovery — no-op пока задачные PR не создаются
        # автоматически (ROADMAP 3.14); safe seam на будущее.
        await self._maybe_run_ci_recovery(run_id, task_id, branch)
        await self._worktrees.remove(task_id)
        # v2-025: lesson extraction для medium/large.
        await self._extract_lesson(run_id, task_id, outcome="DONE")

    async def _push_base_if_enabled(self, run_id: str, task_id: str) -> None:
        if not self._s.push_after_merge:
            return
        assert self._worktrees is not None
        run = self._store.get_run(run_id)
        branch = run.base_branch if run else self._worktrees.base_branch
        outcome = await self._worktrees.push_base(self._s.git_remote)
        if outcome.pushed:
            self._store.add_event(
                run_id, EventType.GIT_PUSH, task_id=task_id,
                detail={"remote": self._s.git_remote, "branch": branch},
            )
        else:
            self._store.add_event(
                run_id, EventType.GIT_PUSH_FAILED, task_id=task_id,
                detail={
                    "remote": self._s.git_remote,
                    "branch": branch,
                    "output": outcome.output[:500],
                },
            )
            await self._notifier.notify(
                "Git push не удался",
                f"run {run_id}, task-{task_id}: {outcome.output[:300]}",
            )

    # ── v2-033: CI failure recovery ──────────────────────────────────────────
    async def _maybe_run_ci_recovery(self, run_id: str, task_id: str, branch: str) -> None:
        """Если для ветки задачи есть открытый GitHub PR — poll CI, fix-pass на fail.

        Отключено, если `HARNESS_PUSH_AFTER_MERGE` выключен (нечего PR-ить, если
        мы даже не пушим) или `CI_RETRY_MAX=0`. Seam для ROADMAP 3.14: сейчас
        harness мержит локально и пушит `base` напрямую — PR на ветку `task/<id>`
        никто не создаёт автоматически, `pr_for_branch` вернёт `None`, метод
        no-op'ится. Когда 3.14 начнёт создавать PR на каждую задачу — этот путь
        заработает без изменений.
        """
        if not self._s.push_after_merge:
            return
        max_retries = int(os.environ.get("CI_RETRY_MAX", "1"))
        if max_retries <= 0:
            return
        assert self._worktrees is not None

        gh = GitHubCli(self._repo_root)
        pr = await gh.pr_for_branch(branch)
        if pr is None:
            return

        worktree = self._repo_root / ".worktrees" / f"task-{task_id}"
        if not worktree.exists():
            return

        await self._run_ci_recovery(run_id, task_id, gh, pr, branch, worktree, max_retries)

    async def _run_ci_recovery(
        self,
        run_id: str,
        task_id: str,
        gh: GitHubCli,
        pr: PullRequest,
        branch: str,
        worktree: Path,
        max_retries: int,
    ) -> None:
        """poll → (fail) fetch логи → fix-pass → re-push → re-poll, до `max_retries` раз.

        ECC Continuous Claude §"CI Failure Recovery". Не трогает статус задачи в
        автомате (она уже DONE с точки зрения harness) — это дополнительная
        сеть безопасности на уровне git/GitHub, а не часть DAG-цикла.
        """
        assert self._worktrees is not None
        result = await gh.check_status(pr.number)
        self._store.add_event(
            run_id, EventType.CI_CHECK_RESULT, task_id=task_id,
            detail={"pr": pr.number, "passed": result.all_passed,
                    "failed_runs": result.failed_run_ids, "attempt": 0},
        )
        if result.all_passed:
            return

        for attempt in range(1, max_retries + 1):
            logs = "\n\n".join(
                [await gh.failed_run_log(rid) for rid in result.failed_run_ids[:3]]
            ) or result.raw
            feedback = (
                f"=== CI FAILED (PR #{pr.number}, попытка {attempt}/{max_retries}) ===\n"
                f"{logs[-4000:]}\n"
            )
            task = self._require_task(run_id, task_id)
            fix_prompt = self._worker_prompt(task, feedback)
            fix_log = self._s.root / "logs" / f"ci-fix-{task_id}-a{attempt}.log"
            wres = await self._worker_runner.run(
                fix_prompt, model=self._s.role(Role.WORKER).model,
                cwd=worktree, log_path=fix_log,
            )
            self._account(run_id, wres.cost_credits, cost_kind=wres.cost_kind)
            await self._worktrees.commit_all(worktree, f"task({task_id}): ci fix attempt {attempt}")
            await self._worktrees.push_branch(branch, self._s.git_remote)

            result = await gh.check_status(pr.number)
            self._store.add_event(
                run_id, EventType.CI_CHECK_RESULT, task_id=task_id,
                detail={"pr": pr.number, "passed": result.all_passed,
                        "failed_runs": result.failed_run_ids, "attempt": attempt},
            )
            if result.all_passed:
                return

        self._store.add_event(
            run_id, EventType.CI_RECOVERY_EXHAUSTED, task_id=task_id,
            detail={"pr": pr.number, "retries": max_retries},
        )

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
            await self._extract_lesson(run_id, task_id, outcome="BLOCKED")
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
        self._account(run_id, res.cost_credits, cost_kind=res.cost_kind)

        # v2-020: оркестратор тоже может вернуть question-блок вместо replan-решения.
        from harness.tasks_io.question import parse_question_block  # noqa: PLC0415
        qblock = parse_question_block(res.text)
        if qblock.has_questions:
            await self._handle_input_request_async(run_id, task_id, qblock)
            # Возвращаем decision-action=block как сигнал «ждём пользователя».
            return ReplanDecision(action=ReplanAction.BLOCK,
                                  reason="awaiting user input via question-protocol")
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
    def _worker_prompt(self, task: Task, feedback: str, notes: str = "") -> str:
        role = (self._s.prompts_dir / "worker.md").read_text(encoding="utf-8")
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        provides = self._collect_provides(task)
        parts = [role, self._gates_brief(), "\n=== СПЕКА ЗАДАЧИ ===\n" + spec]
        if provides:
            parts.append("\n=== ИНТЕРФЕЙСЫ ЗАВИСИМОСТЕЙ (готовый код) ===\n" + provides)
        if notes:
            parts.append("\n=== SHARED TASK NOTES (прогресс с прошлых попыток) ===\n" + notes)
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

    def _reviewer_prompt(
        self, task: Task, gates_ok: bool, gate_tail: str, diff: str, worktree: Path | None = None
    ) -> str:
        role = (self._s.prompts_dir / "reviewer.md").read_text(encoding="utf-8")
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        worktree_block = (
            f"\n=== WORKTREE (твой CWD) ===\n{worktree}\n"
            "Гейты можно гонять прямо тут — ты в рабочей копии воркера.\n"
            if worktree is not None else ""
        )
        return (
            f"{role}\n{self._gates_brief()}\n=== СПЕКА ЗАДАЧИ ===\n{spec}\n\n"
            f"=== ГЕЙТЫ ПРОЕКТА: {'PASS' if gates_ok else 'FAIL'} ===\n{gate_tail}\n\n"
            f"=== GIT DIFF относительно base ===\n{diff}\n{worktree_block}"
        )

    def _de_sloppify_prompt(self, task: Task, diff: str) -> str:
        """v2-028: промпт cleanup-агента — роль + гейты + diff воркера."""
        role = (self._s.prompts_dir / "de-sloppify.md").read_text(encoding="utf-8")
        return (
            f"{role}\n{self._gates_brief()}\n"
            f"=== GIT DIFF (код воркера, относительно base) ===\n{diff}\n"
        )

    def _collect_provides(self, task: Task) -> str:
        chunks: list[str] = []
        for dep_id in task.depends_on:
            dep = self._store.get_task(task.run_id, dep_id)
            if dep and dep.provides:
                chunks.append(f"# из task-{dep_id}:\n{dep.provides}")
        return "\n\n".join(chunks)

    @staticmethod
    def _extract_provides(worker_output: str) -> str:
        """v2-004: достать `Provides:` блок из вывода воркера.

        Поддерживает два формата:
          - свёрнутый: строки после маркера `Provides:` до пустой строки/след.
            секции;
          - markdown: тело секции `# Provides` до следующего заголовка.
        Возвращает очищенный текст (без маркера и trailer). Пусто — если блока нет.
        """
        text = worker_output
        # Свёрнутый формат: `Provides:` в начале строки, тело до пустой строки.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip().rstrip(":").lower() == "provides":
                body: list[str] = []
                for raw in lines[i + 1:]:
                    s = raw.strip()
                    if not s:
                        break
                    body.append(s)
                if body:
                    return "\n".join(body)
        # Markdown-секция `# Provides`
        from harness.tasks_io.parser import read_section  # noqa: PLC0415 (локальный импорт)
        section = read_section(text, "Provides")
        return section

    def _has_dependents(self, run_id: str, task_id: str) -> bool:
        """Есть ли в DAG задачи, зависящие от `task_id`."""
        return any(task_id in t.depends_on for t in self._store.list_tasks(run_id))

    # ── v2-013: контекст-монитор ──────────────────────────────────────────────
    async def _run_preflight(
        self, run_id: str, task_id: str, task: Task, worktree: Path,
    ) -> str | None:
        """v2-015: pre-flight understanding-brief.

        Воркер получает упрощённый промпт «дай brief, не пиши код» и возвращает
        JSON с understand/files/assumptions/missing_context. Если missing_context
        непустой — задача переводится в `NEEDS_CLARIFICATION`, пишется event, и
        движок выходит из _process_task (ждёт ответа пользователя).

        Возвращает строку-clarification (для будущего v2-020 question-protocol)
        или None если brief чистый (можно идти в worker-loop).
        """
        from harness.tasks_io.brief import parse_brief  # noqa: PLC0415
        prompt = self._preflight_prompt(task)
        plog = self._s.root / "logs" / f"preflight-{task_id}.log"
        # Pre-flight — на дешёвой модели (auto), не жжём Opus на self-check.
        pre_model = self._s.role(Role.WORKER).model
        pres = await self._worker_runner.run(
            prompt, model=pre_model, cwd=worktree, log_path=plog,
        )
        self._account(run_id, pres.cost_credits, cost_kind=pres.cost_kind)
        brief = parse_brief(pres.text)
        self._store.add_event(
            run_id, EventType.UNDERSTANDING_BRIEF, task_id=task_id,
            detail={
                "parsed": brief.parsed,
                "missing_context": brief.missing_context[:5],
                "files_i_will_touch": brief.files_i_will_touch[:10],
                "acceptance_count": len(brief.acceptance_i_will_satisfy),
            },
        )
        if brief.needs_clarification:
            t = self._require_task(run_id, task_id)
            self._store.transition_task(t, TaskStatus.NEEDS_CLARIFICATION,
                                        note="pre-flight: missing_context")
            await self._notifier.notify(
                "Нужно уточнение задачи",
                f"run {run_id}, task-{task_id}: воркер заявил missing_context — "
                f"{', '.join(brief.missing_context[:3])}",
            )
            return ", ".join(brief.missing_context)
        return None

    def _preflight_prompt(self, task: Task) -> str:
        """Промпт для pre-flight brief — без TDD/гейтов, только self-check."""
        role = (
            "Ты делаешь pre-flight self-check перед написанием кода. "
            "НЕ пиши код. Прочитай спеку и верни машиночитаемый brief.\n\n"
            "Формат — строго fenced-блок:\n"
            "```brief\n"
            '{"understand": "я понял задачу как ...", '
            '"files_i_will_touch": ["..."], '
            '"acceptance_i_will_satisfy": ["AC1", "AC2"], '
            '"assumptions": ["..."], '
            '"missing_context": ["чего не хватает, чтобы начать"]}\n'
            "```\n"
            "Если контекста достаточно — `missing_context` пустой []. "
            "Если не хватает — перечисли конкретно что нужно от человека.\n"
        )
        spec = Path(task.spec_path).read_text(encoding="utf-8")
        return f"{role}\n=== СПЕКА ЗАДАЧИ ===\n{spec}\n"

    # ── v2-020: question-protocol ─────────────────────────────────────────────
    def _handle_input_request(
        self, run_id: str, task_id: str, qblock: Any,
    ) -> None:
        """Synchronous часть: записать INPUT_REQUESTED event, поставить статус.

        Telegram-нотификация — async (через `_handle_input_request_async`).
        """
        from harness.tasks_io.question import QuestionBlock  # noqa: PLC0415
        assert isinstance(qblock, QuestionBlock)
        questions_payload = [
            {"id": q.id, "question": q.question, "options": q.options[:5], "why": q.why[:200]}
            for q in qblock.questions
        ]
        self._store.add_event(
            run_id, EventType.INPUT_REQUESTED, task_id=task_id,
            detail={"questions": questions_payload},
        )
        t = self._require_task(run_id, task_id)
        self._store.transition_task(t, TaskStatus.NEEDS_CLARIFICATION,
                                    note="question-protocol: awaiting /answer")

    async def _handle_input_request_async(
        self, run_id: str, task_id: str, qblock: Any,
    ) -> None:
        """Async-обёртка: sync-часть + Telegram-нотификация с inline-кнопками."""
        self._handle_input_request(run_id, task_id, qblock)
        # Формируем текст для Telegram. Inline-кнопки — в bot.py (нужен httpx).
        from harness.tasks_io.question import QuestionBlock  # noqa: PLC0415
        assert isinstance(qblock, QuestionBlock)
        lines = [f"run {run_id}, task-{task_id} — нужны ответы:"]
        for q in qblock.questions:
            opts = " / ".join(q.options[:4]) if q.options else "<свободный ответ>"
            lines.append(f"  • {q.id}: {q.question} [{opts}]")
        lines.append(f"Ответить: harness answer {run_id} {task_id} q1=\"...\"")
        await self._notifier.notify("Нужен input", "\n".join(lines))

    async def answer(
        self, run_id: str, task_id: str, answers: dict[str, str],
    ) -> None:
        """v2-020: применить ответы пользователя и продолжить Run.

        `answers` — {question_id: answer_text}. Записывает `CLARIFICATION_ANSWERED`
        event, переводит задачу из `NEEDS_CLARIFICATION` → `READY`, и продолжат
        основной цикл (новый worker-call с answers в feedback).
        """
        from harness.tasks_io.question import format_answers_for_feedback  # noqa: PLC0415
        task = self._require_task(run_id, task_id)
        if TaskStatus(task.status) != TaskStatus.NEEDS_CLARIFICATION:
            raise ValueError(
                f"task {task_id} не в NEEDS_CLARIFICATION (текущий: {task.status})"
            )
        self._store.add_event(
            run_id, EventType.CLARIFICATION_ANSWERED, task_id=task_id,
            detail={"answers": answers},
        )
        # Инжектим ответы в spec_path (или в note) — воркер получит их в feedback.
        # Простейший путь — добавить answers в note задачи; движок читает note в
        # _worker_prompt? Нет — читает только feedback. Положим answers в отдельный
        # файл, который _worker_prompt подхватит.
        answers_file = self._s.root / "tasks" / f"task-{task_id}.answers.md"
        answers_file.parent.mkdir(parents=True, exist_ok=True)
        answers_file.write_text(format_answers_for_feedback(answers), encoding="utf-8")
        self._store.transition_task(task, TaskStatus.READY, note="answered, resuming")
        # Продолжаем Run — основной цикл подхватит READY-задачу.
        await self.run(run_id)

    def _strategic_compact_hint(self, wres: AgentResult) -> str:
        """v2-032: strategic compaction — подсказка `/compact` в feedback следующей
        попытки, если контекст этой попытки был близок к исчерпанию.

        ECC `strategic-compact`: ручной compact на logical breakpoint дешевле, чем
        ждать автоматический ~95%-компакт посреди правки файла. Порог выше, чем у
        `CONTEXT_LOW` (v2-013, ~20%) — компакт предлагается раньше, превентивно.
        Только подсказка: воркер сам решает, звать `/compact` или нет (не авто).
        """
        remaining = wres.context_window_remaining
        if remaining is None:
            return ""
        threshold = int(os.environ.get("HARNESS_COMPACT_THRESHOLD", "60000"))
        if remaining >= threshold:
            return ""
        return (
            f"\n=== КОНТЕКСТ ЗАКАНЧИВАЕТСЯ (осталось ~{remaining} токенов) ===\n"
            "Рассмотри `/compact` на логичной точке (между acceptance criteria, "
            "не посреди правки файла). Сохрани: спеку задачи, SHARED_TASK_NOTES.md, "
            "уже сделанные шаги и их результат. Снеси: exploration тупиковых "
            "подходов, неудачные попытки, длинные промежуточные выводы инструментов.\n"
        )

    def _check_context_low(
        self, run_id: str, task_id: str, wres: AgentResult,
    ) -> None:
        """CONTEXT_LOW event при context_window_remaining < порога (default 40k = 20% от 200k)."""
        remaining = wres.context_window_remaining
        if remaining is None:
            return
        threshold = int(os.environ.get("HARNESS_CONTEXT_LOW_THRESHOLD", "40000"))
        if remaining < threshold:
            self._store.add_event(
                run_id, EventType.CONTEXT_LOW, task_id=task_id,
                detail={"remaining": remaining, "threshold": threshold},
            )

    def _check_loop_stuck(
        self, run_id: str, task_id: str, wres: AgentResult,
    ) -> None:
        """LOOP_STUCK event если один tool-call повторён > threshold раз за попытку."""
        from harness.policy.loop_detect import loop_stuck  # noqa: PLC0415
        stuck = loop_stuck(wres.events)
        for name, inp, count in stuck:
            self._store.add_event(
                run_id, EventType.LOOP_STUCK, task_id=task_id,
                detail={"tool": name, "input": str(inp)[:200], "count": count},
            )

    def _check_scope_exit(
        self, run_id: str, task_id: str, task: Task, changed: list[str],
    ) -> list[str]:
        """SCOPE_EXIT event если воркер тронул файлы вне списка «Файлы:» из спеки.

        Возвращает список нарушений. Не блокирует (только observability) —
        guard_violations отдельно ловит protected paths (tests/spec/**).
        """
        from harness.policy.scope import scope_violations  # noqa: PLC0415
        try:
            spec_text = Path(task.spec_path).read_text(encoding="utf-8")
        except OSError:
            return []
        viol = scope_violations(changed, spec_text, spec_path=task.spec_path)
        # Не считаем нарушением файлы из protected_paths — их ловит guard.
        viol = [v for v in viol if not protected_violations([v], self._s.protected_paths)]
        if viol:
            self._store.add_event(
                run_id, EventType.SCOPE_EXIT, task_id=task_id,
                detail={"files": viol[:20]},
            )
        return viol

    def _update_shared_notes(
        self, worktree: Path, task_id: str, attempt: int, worker_output: str,
    ) -> None:
        """v2-024: append `=== NOTES UPDATE ===` блок из output воркера в SHARED_TASK_NOTES.md.

        Воркер в output может содержать секцию `=== NOTES UPDATE ===` с обновлением
        прогресса. Append в `worktree/SHARED_TASK_NOTES.md` — следующая попытка
        прочитает. Если секции нет — не трогаем файл.
        """
        marker = "=== NOTES UPDATE ==="
        idx = worker_output.find(marker)
        if idx == -1:
            return
        body = worker_output[idx + len(marker):].strip()
        # Обрезаем до следующего === маркера если есть
        next_mark = body.find("===")
        if next_mark != -1:
            body = body[:next_mark].strip()
        if not body:
            return
        notes_path = worktree / "SHARED_TASK_NOTES.md"
        header = f"\n## Attempt {attempt}\n"
        notes_path.write_text(
            (notes_path.read_text(encoding="utf-8") if notes_path.exists() else "")
            + header + body + "\n",
            encoding="utf-8",
        )

    def _write_transcript_ndjson(
        self, task_id: str, attempt: int, events: list[AgentEvent], role: str,
    ) -> None:
        """v2-009: написать транскрипт агент-событий в logs/<role>-<task>-a<N>.ndjson.

        По строке на событие (JSONL) — для `harness tail` и дебага.
        """
        import json  # noqa: PLC0415 (локальный импорт, не критично)
        path = self._s.root / "logs" / f"{role}-{task_id}-a{attempt}.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for ev in events:
                line = json.dumps(
                    {"kind": ev.kind, "payload": ev.payload, "at": ev.at},
                    ensure_ascii=False,
                )
                f.write(line + "\n")

    # ── вспомогательное ────────────────────────────────────────────────────────
    def _require_task(self, run_id: str, task_id: str) -> Task:
        task = self._store.get_task(run_id, task_id)
        if task is None:
            raise ValueError(f"task {task_id} не найдена в run {run_id}")
        return task

    async def _check_gates(
        self, cwd: Path, task_id: str, *, full: bool = False
    ) -> GateResult:
        """Keep legacy injected gates compatible while ProfileGate supports scoping."""
        if isinstance(self._gate, ProfileGate):
            return await self._gate.check(cwd, task_id=task_id, full=full)
        return await self._gate.check(cwd)

    def _advance(self, run_id: str, task_id: str, dst: TaskStatus, note: str = "") -> Task:
        """Прочитать актуальную задачу и сделать валидируемый переход в dst."""
        return self._store.transition_task(self._require_task(run_id, task_id), dst, note=note)

    @staticmethod
    def _start_rung(task: Task) -> int:
        """Стартовая ступень лестницы по complexity (v2-019: tiered).

        - trivial/small — auto (ступень 0)
        - medium        — kimi (ступень 1, auto пропускается)
        - large         — glm (ступень 2, сразу сильная)
        Обратная совместимость: old `high` → `medium`.
        """
        return {"trivial": 0, "small": 0, "medium": 1, "large": 2}.get(task.complexity, 0)

    def _recover_interrupted(self, run_id: str) -> None:
        """Resume: задачи, бывшие in-flight на момент падения, возвращаем в READY.

        Без этого задача в RUNNING/GATING/REVIEW/MERGE_QUEUE после рестарта зависла бы
        (ready() её не берёт). Сброс идёт через RECOVERING, чтобы переход был
        валидируемым и попал в журнал (ROADMAP 3.3).
        """
        self._lifecycle.recover(run_id)

    def _spent(self, run_id: str) -> float:
        run = self._store.get_run(run_id)
        return run.spent_credits if run else 0.0

    def _account(self, run_id: str, credits: float, cost_kind: str = "estimate") -> None:
        if credits > 0:
            spent = self._store.add_spend(run_id, credits, cost_kind=cost_kind)
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


def _format_eviction_context(outcome: MergeOutcome) -> str:
    """v2-029: полный контекст merge-конфликта для feedback следующей попытки.

    Ralphinho "Merge Queue with Eviction": слепой retry (`note="merge conflict"`,
    пустой feedback) заставляет воркера гадать. Даём конкретику — какие файлы
    конфликтуют и как выглядит конфликт (diff с conflict-маркерами) — и явно
    говорим, что base мог уйти вперёд, поэтому чинить нужно поверх свежего base,
    а не пытаться руками резолвить конфликт старого коммита (его уже нет — worktree
    следующей попытки создаётся заново от base).
    """
    diff = outcome.conflict_diff
    lines = diff.splitlines()
    if len(lines) > _CONFLICT_DIFF_MAX_LINES:
        tail = "\n".join(lines[-_CONFLICT_DIFF_MAX_LINES:])
        diff = f"... (обрезано, показаны последние {_CONFLICT_DIFF_MAX_LINES} строк)\n{tail}"
    files = "\n".join(f"- {f}" for f in outcome.conflicting_files) or "(не определены)"
    return (
        "=== MERGE CONFLICT (прошлая попытка не влилась в base) ===\n"
        f"Конфликтующие файлы:\n{files}\n\n"
        f"--- diff с conflict-маркерами (<<<<<<< / ======= / >>>>>>>) ---\n{diff}\n"
        "---\n"
        "base мог измениться с момента прошлой попытки (другие задачи влились "
        "раньше тебя). Реализуй задачу заново поверх актуального base — не пытайся "
        "восстановить старый коммит, его больше нет в твоём worktree.\n"
    )


def _parse_verdict(text: str) -> Verdict | None:
    """Compatibility wrapper around the shared deterministic lifecycle parser."""
    return parse_review_verdict(text)
