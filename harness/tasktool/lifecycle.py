"""Deterministic TaskTool lifecycle operations, independent of agent runners."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType, RunStatus, TaskStatus, Verdict
from harness.domain.models import Task
from harness.domain.state_machine import RECOVERABLE_STATUSES
from harness.evidence.acceptance import auto_flip_gate_items
from harness.evidence.ledger import done_allowed, record_gate_run
from harness.gates.base import GateResult
from harness.gates.profile_gate import ProfileGate
from harness.policy.guard import protected_violations
from harness.profile import load_profile
from harness.sandbox.factory import build_sandbox
from harness.store.repository import Store
from harness.tenant.run_layout import resolve_run_root
from harness.worktree.manager import WorktreeManager


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    action: str
    task_status: str
    changed_files: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    provides: str = ""
    detail: str = ""
    diff: str = ""


def owned_scope_violations(
    changed_files: Sequence[str],
    files_owned: Sequence[str],
) -> list[str]:
    """Return changed paths not covered by frozen files_owned (glob-aware).

    Empty ``files_owned`` means ownership was not declared — return no violations
    so the control plane does not infinite-retry every dirty path in the tree.
    """
    patterns = tuple(files_owned)
    if not patterns:
        return []
    violations: list[str] = []
    for changed in changed_files:
        if not changed:
            continue
        if not any(_match_owned(changed, pattern) for pattern in patterns):
            violations.append(changed)
    return violations


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob so ``*``/``?`` do not cross ``/``; ``**`` may."""
    out: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        char = pattern[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    out.append("$")
    return re.compile("".join(out))


def _match_owned(path: str, pattern: str) -> bool:
    """Match a changed path against one ownership pattern.

    Exact file patterns match only that path (``allowed.py`` does not cover
    ``allowed.py.evil``). Directory intent requires a trailing slash or an
    explicit glob (``src/pkg/``, ``src/pkg/**``, ``src/*.py``).
    """
    normalized = path.replace("\\", "/").lstrip("./")
    owned = pattern.replace("\\", "/")
    if any(char in owned for char in "*?["):
        return bool(_glob_to_regex(owned).match(normalized))
    if owned.endswith("/"):
        prefix = owned.rstrip("/")
        return normalized == prefix or normalized.startswith(prefix + "/")
    return normalized == owned


class TaskLifecycleService:
    def __init__(self, settings: Settings, store: Store, repo_root: Path) -> None:
        self._settings = settings
        self._store = store
        self._repo_root = Path(repo_root).resolve()
        profile = load_profile(self._repo_root)
        self._gate = ProfileGate(
            profile,
            build_sandbox(settings.sandbox_kind, profile),
            allow_soft_gates=settings.allow_soft_gates,
        )

    def _run_root(self, run_id: str) -> Path:
        return resolve_run_root(
            self._repo_root, run_id, prefer_modern=self._settings.use_run_roots
        ).root

    def _refuse_done_if_needed(
        self, run_id: str, task_id: str
    ) -> tuple[bool, str]:
        """Return (allowed, reason). When evidence gate off, always allowed."""
        ok, reason = done_allowed(self._run_root(run_id), task_id=task_id)
        if not ok:
            self._store.add_event(
                run_id,
                EventType.DONE_REFUSED,
                task_id=task_id,
                detail={"reason": reason, "evidence_gate": True},
            )
        return ok, reason

    def _record_gate_evidence(
        self, run_id: str, task_id: str, result: GateResult, *, source: str
    ) -> list[str]:
        """Append ledger rows for each gate run; auto-flip ac-gate-* items."""
        run_root = self._run_root(run_id)
        green_pairs: list[tuple[str, str]] = []
        evidence_ids: list[str] = []
        for gate_run in getattr(result, "gate_runs", None) or []:
            record = record_gate_run(
                run_root,
                run_id=run_id,
                task_id=task_id,
                gate_id=gate_run.gate_id,
                exit_code=gate_run.exit_code,
                log_path=gate_run.log_path,
                source=source,
            )
            evidence_ids.append(record.id)
            self._store.add_event(
                run_id,
                EventType.EVIDENCE_RECORDED,
                task_id=task_id,
                detail={
                    "evidence_id": record.id,
                    "gate_id": gate_run.gate_id,
                    "exit_code": gate_run.exit_code,
                    "log_path": gate_run.log_path,
                },
            )
            if gate_run.exit_code == 0:
                green_pairs.append((gate_run.gate_id, record.id))
        acceptance_path = run_root / "acceptance.json"
        flipped = auto_flip_gate_items(
            acceptance_path, run_root=run_root, gate_evidence=green_pairs
        )
        for item_id in flipped:
            self._store.add_event(
                run_id,
                EventType.ACCEPTANCE_FLIPPED,
                task_id=task_id,
                detail={"item_id": item_id, "source": "gate_auto"},
            )
        return evidence_ids

    def _task(self, run_id: str, task_id: str) -> Task:
        task = self._store.get_task(run_id, task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found in run {run_id}")
        return task

    def _manager(self, run_id: str) -> WorktreeManager:
        run = self._store.get_run(run_id)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        return WorktreeManager(
            self._repo_root, self._repo_root / ".worktrees", run.base_branch
        )

    async def prepare_task(self, run_id: str, task_id: str) -> Task:
        task = self._task(run_id, task_id)
        if TaskStatus(task.status) is TaskStatus.PENDING:
            task = self._store.transition_task(task, TaskStatus.READY)
        if TaskStatus(task.status) is not TaskStatus.READY:
            raise ValueError(f"task {task_id} cannot be prepared from {task.status}")
        if self._settings.use_worktrees:
            worktree, branch = await self._manager(run_id).create(task_id)
            task.branch = branch
            task.worktree_path = str(worktree)
        else:
            # In-place mode: agents share the target checkout; no git worktree.
            manager = self._manager(run_id)
            current = await manager.current_branch(self._repo_root)
            run = self._store.get_run(run_id)
            task.branch = current or (run.base_branch if run else "")
            task.worktree_path = str(self._repo_root)
        self._store.update_task_fields(task)
        return self._store.transition_task(task, TaskStatus.RUNNING)

    async def finalize_worker(
        self,
        run_id: str,
        task_id: str,
        final_text: str,
        *,
        files_owned: Sequence[str] | None = None,
    ) -> LifecycleResult:
        task = self._task(run_id, task_id)
        if TaskStatus(task.status) is not TaskStatus.RUNNING:
            raise ValueError(f"task {task_id} is not running")
        manager = self._manager(run_id)
        worktree = Path(task.worktree_path or self._repo_root)
        if self._settings.use_worktrees:
            await manager.commit_all(worktree, f"task({task_id}): worker result")
            changed = await manager.changed_files(task.branch)
            # Empty ownership means "scope not declared" — do not treat every path
            # as a violation (that caused infinite worker retries in dogfood).
            scope = (
                owned_scope_violations(changed, files_owned)
                if files_owned
                else []
            )
        else:
            # Shared checkout (in-place): attribute only this task's owned dirty
            # paths for the checkpoint. Do NOT treat non-owned dirty paths as
            # scope exits — sibling WIP, pre-existing branch dirt, and pending
            # tasks without dispatches yet are expected under HARNESS_USE_WORKTREES=0.
            # Ownership is enforced by ReviewBundle / worker briefs, not whole-tree
            # dirty scans (orphan scope_exit caused infinite worker retries).
            dirty = await manager.dirty_files(worktree)
            if files_owned:
                changed = [
                    path
                    for path in dirty
                    if any(_match_owned(path, pattern) for pattern in files_owned)
                ]
            else:
                # No ownership declared: attribute nothing for checkpoint; skip scope.
                changed = []
            scope = []
        # Snapshot the diff now: siblings keep editing the shared checkout, so a
        # reviewer that diffed later would grade someone else's work too.
        diff = await manager.review_diff(
            worktree,
            changed,
            branch=task.branch if self._settings.use_worktrees else "",
            max_bytes=self._settings.review_diff_bytes,
        )
        protected = protected_violations(changed, self._settings.protected_paths)
        violations = list(dict.fromkeys([*protected, *scope]))
        provides = _extract_provides(final_text)
        if provides:
            task.provides = provides
            self._store.update_task_fields(task)
        if protected:
            self._store.add_event(
                run_id,
                EventType.GUARD_VIOLATION,
                task_id=task_id,
                detail={"files": protected},
            )
        if scope:
            self._store.add_event(
                run_id,
                EventType.SCOPE_EXIT,
                task_id=task_id,
                detail={"files": scope, "files_owned": list(files_owned or ())},
            )
        if violations:
            note = f"scope/protected violations: {violations}"
            task = self._store.transition_task(task, TaskStatus.READY, note=note[:500])
            return LifecycleResult(
                action="scope_violation",
                task_status=task.status,
                changed_files=changed,
                violations=violations,
                provides=provides,
                detail=note,
                diff=diff,
            )
        task = self._store.transition_task(task, TaskStatus.GATING)
        return LifecycleResult(
            action="gates_required",
            task_status=task.status,
            changed_files=changed,
            violations=[],
            provides=provides,
            diff=diff,
            detail="",
        )

    async def run_gates(
        self, run_id: str, task_id: str, *, full: bool = False
    ) -> GateResult:
        task = self._task(run_id, task_id)
        status = TaskStatus(task.status)
        if status not in {TaskStatus.GATING, TaskStatus.MERGE_QUEUE}:
            raise ValueError(f"task {task_id} cannot run gates from {task.status}")
        # `full=True` selects every profile gate but still runs against the
        # task branch. The post-integration full gate is run explicitly after merge.
        cwd = Path(task.worktree_path)
        log_dir = (
            self._run_root(run_id) / "evidence" / "gates" / task_id / ("full" if full else "local")
        )
        try:
            result = await self._gate.check(
                cwd, task_id=task_id, full=full, log_dir=log_dir
            )
        except TypeError:
            # Test doubles / older Gate Protocol implementations.
            result = await self._gate.check(cwd, task_id=task_id, full=full)
        evidence_ids = self._record_gate_evidence(
            run_id, task_id, result, source="advance" if not full else "post_merge"
        )
        self._store.add_event(
            run_id,
            EventType.GATE_RESULT,
            task_id=task_id,
            detail={
                "passed": result.passed,
                "full": full,
                "evidence_ids": evidence_ids,
                "gate_ids": [g.gate_id for g in result.gate_runs],
            },
        )
        if status is TaskStatus.GATING:
            if result.passed:
                self._store.transition_task(task, TaskStatus.REVIEW)
            else:
                self._store.transition_task(
                    task, TaskStatus.READY, note=(result.output or "local gate failed")[:500]
                )
        return result

    def apply_review(
        self, run_id: str, task_id: str, review_text: str
    ) -> LifecycleResult:
        task = self._task(run_id, task_id)
        if TaskStatus(task.status) is not TaskStatus.REVIEW:
            raise ValueError(f"task {task_id} is not awaiting review")
        verdict = parse_review_verdict(review_text)
        if verdict is None:
            return LifecycleResult(
                "review_unparsed", task.status, detail="review lacks APPROVE or CHANGES"
            )
        if verdict is Verdict.APPROVE:
            # Judge seat is read-only w.r.t. acceptance: APPROVE cannot flip
            # evidence and is a no-op when the honesty gate says evidence is red.
            allowed, reason = self._refuse_done_if_needed(run_id, task_id)
            if not allowed:
                self._store.add_event(
                    run_id,
                    EventType.JUDGE_APPROVE_BLOCKED,
                    task_id=task_id,
                    detail={
                        "reason": reason,
                        "verdict": verdict.value,
                        "note": "judge cannot flip acceptance; APPROVE ignored",
                    },
                )
                self._store.add_event(
                    run_id,
                    EventType.REVIEW_RESULT,
                    task_id=task_id,
                    detail={
                        "verdict": verdict.value,
                        "blocked": True,
                        "reason": reason,
                    },
                )
                return LifecycleResult(
                    "judge_approve_blocked",
                    task.status,
                    detail=(
                        f"APPROVE no-op: evidence red ({reason}); "
                        "judge cannot flip acceptance"
                    ),
                )
            if self._settings.ship_mode == "manual":
                task = self._store.transition_task(
                    task, TaskStatus.DONE, note="manual ship — human commits/merges"
                )
                action = "manual_ship"
            else:
                task = self._store.transition_task(task, TaskStatus.MERGE_QUEUE)
                action = "merge_required"
        else:
            task = self._store.transition_task(task, TaskStatus.READY, note=review_text)
            task.completion_signals = 0
            self._store.update_task_fields(task)
            action = "changes_required"
        self._store.add_event(
            run_id,
            EventType.REVIEW_RESULT,
            task_id=task_id,
            detail={"verdict": verdict.value},
        )
        return LifecycleResult(action, task.status, detail=review_text)

    async def merge(
        self, run_id: str, task_id: str, *, approved: bool = False
    ) -> LifecycleResult:
        task = self._task(run_id, task_id)
        if TaskStatus(task.status) is not TaskStatus.MERGE_QUEUE:
            raise ValueError(f"task {task_id} is not in merge queue")
        if self._settings.ship_mode == "manual":
            # Defensive: old runs may still sit in MERGE_QUEUE after a mode flip.
            allowed, reason = self._refuse_done_if_needed(run_id, task_id)
            if not allowed:
                return LifecycleResult(
                    "done_refused",
                    task.status,
                    detail=f"DONE blocked by evidence gate: {reason}",
                )
            task = self._store.transition_task(
                task, TaskStatus.DONE, note="manual ship — skip git merge"
            )
            self._store.add_event(
                run_id,
                EventType.MERGED,
                task_id=task_id,
                detail={"branch": task.branch, "approved": approved, "manual_ship": True},
            )
            return LifecycleResult(
                "manual_ship",
                task.status,
                detail="ship_mode=manual — working tree left for human",
            )
        if not approved:
            self._store.set_run_status(run_id, RunStatus.PAUSED.value)
            self._store.add_event(
                run_id,
                EventType.HUMAN_GATE_WAIT,
                task_id=task_id,
                detail={"reason": "approve merge"},
            )
            return LifecycleResult("merge_required", task.status, detail="human approval required")

        manager = self._manager(run_id)
        outcome = await manager.merge_to_base(task.branch, f"task({task_id})")
        if not outcome.merged:
            task = self._store.transition_task(
                task, TaskStatus.READY, note="merge conflict"
            )
            return LifecycleResult(
                "merge_conflict",
                task.status,
                changed_files=outcome.conflicting_files,
                detail=outcome.output,
            )
        log_dir = self._run_root(run_id) / "evidence" / "gates" / task_id / "post_merge"
        try:
            gate = await self._gate.check(
                self._repo_root, task_id=task_id, full=True, log_dir=log_dir
            )
        except TypeError:
            gate = await self._gate.check(
                self._repo_root, task_id=task_id, full=True
            )
        evidence_ids = self._record_gate_evidence(
            run_id, task_id, gate, source="post_merge"
        )
        self._store.add_event(
            run_id,
            EventType.GATE_RESULT,
            task_id=task_id,
            detail={
                "passed": gate.passed,
                "full": True,
                "post_integration": True,
                "evidence_ids": evidence_ids,
            },
        )
        if not gate.passed:
            task = self._store.transition_task(
                task,
                TaskStatus.POST_MERGE_FIX,
                note=(gate.output or "post-integration gate failed")[:500],
            )
            self._store.set_run_status(run_id, RunStatus.PAUSED.value)
            return LifecycleResult(
                "post_integration_gate_failed",
                task.status,
                detail=gate.output,
            )
        allowed, reason = self._refuse_done_if_needed(run_id, task_id)
        if not allowed:
            task = self._store.transition_task(
                task,
                TaskStatus.MERGE_QUEUE,
                note=f"DONE blocked by evidence gate: {reason}"[:500],
            )
            return LifecycleResult(
                "done_refused",
                task.status,
                detail=f"DONE blocked by evidence gate: {reason}",
            )
        task = self._task(run_id, task_id)
        task = self._store.transition_task(
            task, TaskStatus.DONE, note="approved" if approved else ""
        )
        self._store.add_event(
            run_id,
            EventType.MERGED,
            task_id=task_id,
            detail={"branch": task.branch, "approved": approved, "gates_passed": gate.passed},
        )
        await manager.remove(task_id)
        return LifecycleResult(
            "merged",
            task.status,
            detail="post-integration full gates passed",
        )

    async def prepare_post_merge_repair(
        self, run_id: str, task_id: str
    ) -> LifecycleResult:
        """Protocol hook: worktree stream reconciles integrated base for repair.

        Default implementation only advances FSM to READY so the controller can
        enqueue a repair worker. Worktree managers may override/reset branches.
        """
        task = self._task(run_id, task_id)
        if TaskStatus(task.status) is not TaskStatus.POST_MERGE_FIX:
            raise ValueError(
                f"task {task_id} cannot prepare post-merge repair from {task.status}"
            )
        # Idempotent git reconcile hook — no-op when worktree already matches base.
        if self._settings.use_worktrees and task.worktree_path:
            worktree = Path(task.worktree_path)
            if worktree.exists():
                await self._manager(run_id).commit_all(
                    worktree, f"task({task_id}): reconcile before post-merge repair"
                )
        task = self._store.transition_task(
            task, TaskStatus.READY, note="post-merge repair approved"
        )
        return LifecycleResult(
            "post_merge_repair_ready",
            task.status,
            detail="repair dispatch may proceed on integrated base",
        )

    def recover(self, run_id: str) -> None:
        for task in self._store.list_tasks(run_id):
            if TaskStatus(task.status) not in RECOVERABLE_STATUSES:
                continue
            origin = task.status
            recovering = self._store.transition_task(
                task, TaskStatus.RECOVERING, note="resume"
            )
            self._store.transition_task(
                recovering, TaskStatus.READY, note="recovered"
            )
            self._store.add_event(
                run_id, EventType.RECOVERED, task_id=task.id, detail={"from": origin}
            )


_VERDICT_PATTERN = re.compile(
    r"(?:\b(?:VERDICT|DECISION)\s*:\s*|"
    r"^\s*(?:[#>*_-]+\s*)?(?:\*\*)?)(APPROVE|CHANGES)\b",
    re.IGNORECASE | re.MULTILINE,
)


def parse_review_verdict(text: str) -> Verdict | None:
    """Return the last unambiguous review decision, including markdown forms."""
    matches = _VERDICT_PATTERN.findall(text)
    return Verdict(matches[-1].lower()) if matches else None


def _extract_provides(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lstrip("#").strip().rstrip(":").lower() != "provides":
            continue
        body: list[str] = []
        for raw in lines[index + 1 :]:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                break
            body.append(stripped)
        return "\n".join(body)
    return ""


def _sibling_ownership(
    store: Store,
    run_id: str,
    task_id: str,
    *,
    repo_root: Path | None = None,
) -> tuple[str, ...]:
    """Ownership patterns of other non-done tasks — used to ignore sibling dirt.

    Prefer frozen ``plan.json`` ownership (all tasks) so pending siblings without
    dispatches yet still cover pre-existing WIP. Fall back to dispatch rows.
    """
    patterns: list[str] = []
    if repo_root is not None:
        plan_path = Path(repo_root) / ".harness" / "tasktool" / run_id / "plan.json"
        if plan_path.is_file():
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                for item in plan.get("tasks") or []:
                    other_id = str(item.get("task_id") or "")
                    if not other_id or other_id == task_id:
                        continue
                    patterns.extend(str(p) for p in (item.get("files_owned") or []))
            except (OSError, ValueError, TypeError):
                pass
    for task in store.list_tasks(run_id):
        if task.id == task_id:
            continue
        if TaskStatus(task.status) is TaskStatus.DONE:
            continue
        for dispatch in store.list_dispatches(run_id, task_id=task.id):
            patterns.extend(dispatch.files_owned)
    return tuple(dict.fromkeys(patterns))
