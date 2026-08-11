"""Durable pull controller for TaskTool agents.

The controller only exchanges file-backed work.  It never constructs a runner,
starts a subprocess, or invokes an SDK.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from harness import __version__ as HARNESS_VERSION
from harness.config import Settings
from harness.domain.enums import (
    DispatchStatus,
    EventType,
    ModelTier,
    ResourceClass,
    Role,
    RunStatus,
    TaskStatus,
    Verdict,
)
from harness.domain.models import Run, Task
from harness.evidence.acceptance import (
    create_acceptance,
    evidence_pct_for_run,
    honesty_pcts_for_run,
)
from harness.evidence.binding import (
    nested_workers_hard_fail,
    nested_workers_required,
)
from harness.evidence.ledger import done_allowed, evidence_gate_enabled
from harness.evidence.youtrack import readiness_for_run
from harness.ingest import ingest_run, new_run_id
from harness.journal import JournalEntry, write_entry
from harness.policy.economics import assess_breadth
from harness.policy.loop_detect import (
    fingerprints_from_result_payload,
    loop_stuck_from_fingerprints,
)
from harness.policy.phase import derive_phase
from harness.policy.precall import PrecallGovernor, PrecallHit, PrecallLimits
from harness.policy.scope import parse_spec_files
from harness.policy.sprint_contract import (
    freeze_contract,
    is_frozen,
    needs_sprint_contract,
)
from harness.profile import load_profile
from harness.skills.catalog import select_for_dispatch
from harness.store.repository import APPLICATION_APPLIED, Store
from harness.tasks_io.parser import read_section, resolve_plan_root, resolve_staging_plan_root
from harness.tasks_io.question import format_answers_for_feedback, parse_question_block
from harness.tasks_io.verifier import verify_plan
from harness.tasktool.checkpoint import CheckpointWriter
from harness.tasktool.context import build_context_pack, resolve_brain_root
from harness.tasktool.expansion import (
    ExpansionDecision,
    ExpansionStore,
    build_child_plan_tasks,
    new_artifact,
    parse_expansion,
    validate_expansion,
)
from harness.tasktool.model_map import model_for, resource_for
from harness.tasktool.models import (
    ContractError,
    DispatchEnvelope,
    PlanArtifact,
    PlanTask,
    TaskStage,
    normalize_owned_path,
)
from harness.tasktool.project_context import ProjectContext, load_or_build
from harness.tasktool.prompt_builder import build_prompt
from harness.tasktool.resources import (
    ResourceDisposition,
    ResourcePolicy,
    ResourceSnapshot,
)
from harness.tasktool.review_bundle import build_review_bundle
from harness.tasktool.run_meta import RunMetaStore
from harness.tasktool.run_meta import atomic_write as run_meta_atomic_write
from harness.tenant.leases import acquire_lease, default_tenant_id, release_lease
from harness.tenant.run_layout import (
    ensure_run_layout,
    point_latest_symlink,
    resolve_run_root,
)

_ACTIVE_DISPATCH = {DispatchStatus.CLAIMED, DispatchStatus.RUNNING}
_TERMINAL_RUN = {
    RunStatus.DONE.value,
    RunStatus.FAILED.value,
    RunStatus.ABORTED.value,
}
# Absolute max parallel Task Tool jobs. Sized for the shared /home host
# (24 CPU / ~125 GiB). Practical sweet spot is Settings/env default (12);
# hard ceiling 16 — above that Cursor IDE fan-out + in-place ownership collide.
_HARD_JOB_CEILING = 16
_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|CHANGES)", re.IGNORECASE)


class Lifecycle(Protocol):
    async def prepare_task(self, run_id: str, task_id: str) -> object: ...

    async def finalize_worker(
        self,
        run_id: str,
        task_id: str,
        final_text: str,
        *,
        files_owned: Sequence[str] | None = None,
    ) -> object: ...

    async def run_gates(
        self, run_id: str, task_id: str, *, full: bool = False
    ) -> object: ...

    def apply_review(self, run_id: str, task_id: str, review_text: str) -> object: ...

    async def merge(
        self, run_id: str, task_id: str, *, approved: bool = False
    ) -> object: ...

    async def prepare_post_merge_repair(self, run_id: str, task_id: str) -> object: ...

    def recover(self, run_id: str) -> object: ...


class TaskToolController:
    """Runner-free coordinator whose durable state lives beside the repository."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        repo_root: Path,
        *,
        resource_policy: ResourcePolicy | None = None,
        lifecycle: Lifecycle | None = None,
        snapshot: Callable[[], ResourceSnapshot] | ResourceSnapshot | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.repo_root = repo_root.resolve()
        self.resource_policy = _clamp_resource_policy(
            resource_policy or settings.resource_policy
        )
        self.lifecycle = lifecycle or _default_lifecycle(settings, store, self.repo_root)
        self._snapshot = snapshot
        self._root = self.repo_root / ".harness" / "tasktool"
        # controller.json lives behind its own collaborator so locking and
        # atomic writes have one owner (harness/tasktool/run_meta.py).
        self.run_meta = RunMetaStore(
            self.repo_root, use_run_roots=settings.use_run_roots
        )
        self._checkpoints = CheckpointWriter(self.repo_root)
        # Lazy per-project context (structure, stack, brain pointers) shared by
        # every dispatch of the run; cached on disk keyed by git HEAD.
        self._project_ctx: tuple[ProjectContext, Path] | None = None
        self._tenant_user = default_tenant_id(settings.tenant_id)

    async def start(
        self,
        *,
        project: str,
        goal: str,
        base_branch: str | None = None,
        approve_plan: bool = False,
        force: bool = False,
        spec_sources: tuple[str, ...] = (),
        user: str | None = None,
    ) -> dict[str, object]:
        """Verify and freeze an existing plan, then enqueue dependency-ready workers."""
        branch = base_branch or self.settings.base_branch
        self._tenant_user = default_tenant_id(user or self.settings.tenant_id)
        self._validate_base_branch(branch)
        report = verify_plan(self.repo_root)
        if not report.ok:
            return {
                "ok": False,
                "status": "verification_failed",
                "errors": [_finding_dict(item) for item in report.errors],
                "warnings": [_finding_dict(item) for item in report.warnings],
            }

        self._root.mkdir(parents=True, exist_ok=True)
        self._refuse_other_active_run(
            project=project,
            goal=goal,
            base_branch=branch,
        )
        tenant = getattr(self, "_tenant_user", "") or self.settings.tenant_id
        # Isolation: freeze PLAN/tasks into an exclusive run root *before* ingest
        # so a concurrent planner/start cannot clobber this run's plan surface.
        if self.settings.use_run_roots:
            run_id = new_run_id()
            source = resolve_staging_plan_root(self.repo_root)
            run_paths = ensure_run_layout(
                self.repo_root,
                run_id,
                tenant_id=tenant,
                goal=goal,
                project=project,
                harness_version=HARNESS_VERSION,
                source_plan_root=source,
                force_snapshot=True,
                prefer_modern=True,
            )
            point_latest_symlink(self.repo_root, run_id)
            run_id = ingest_run(
                self.settings,
                self.store,
                project=project,
                goal=goal,
                base_branch=branch,
                force=force,
                plan_root=run_paths.root,
                run_id=run_id,
            )
            # Reuse path: ingest may return an existing active run_id.
            if run_id != run_paths.run_id:
                run_paths = resolve_run_root(
                    self.repo_root, run_id, prefer_modern=True
                )
        else:
            run_id = ingest_run(
                self.settings,
                self.store,
                project=project,
                goal=goal,
                base_branch=branch,
                force=force,
                plan_root=self.repo_root,
            )
            run_paths = ensure_run_layout(
                self.repo_root,
                run_id,
                tenant_id=tenant,
                goal=goal,
                project=project,
                harness_version=HARNESS_VERSION,
                prefer_modern=False,
            )
        existing_meta = self._read_meta(run_id, missing_ok=True)
        if existing_meta:
            return self.status(run_id)

        run = self._require_run(run_id)
        if run.status == RunStatus.PLAN_DRAFT.value and not approve_plan:
            self._acquire_active(
                run_id, allow_same=True, goal=goal, project=project
            )
            return {
                "ok": True,
                "ready": False,
                "run_id": run_id,
                "status": RunStatus.PLAN_DRAFT.value,
                "requires": "approve_plan",
                "run_root": str(run_paths.root),
                "warnings": [_finding_dict(item) for item in report.warnings],
            }

        self._acquire_active(run_id, allow_same=True, goal=goal, project=project)
        try:
            spool = self._run_dir(run_id)
            (spool / "prompts").mkdir(parents=True, exist_ok=True)
            (spool / "results").mkdir(parents=True, exist_ok=True)
            (spool / "specs").mkdir(parents=True, exist_ok=True)
            (spool / "pending").mkdir(parents=True, exist_ok=True)
            plan = self._build_plan(run, spec_sources=spec_sources)
            plan_json = plan.to_json()
            self._atomic_write(spool / "plan.json", plan_json)
            ac_lines: list[str] = []
            for plan_task in plan.tasks:
                ac_lines.extend(plan_task.acceptance)
            gate_ids = [g.id for g in load_profile(self.repo_root).gates]
            create_acceptance(
                spool / "acceptance.json",
                run_id=run_id,
                acceptance_lines=ac_lines or None,
                gate_ids=gate_ids or None,
            )
            meta: dict[str, object] = {
                "protocol_version": plan.protocol_version,
                "run_id": run_id,
                "repo": str(self.repo_root),
                "status": RunStatus.RUNNING.value,
                "consumed": [],
                "dispatch_kinds": {},
                "full_gates": [],
                "plan_hash": plan.plan_hash,
                "run_root": str(spool),
                "tenant_id": getattr(self, "_tenant_user", "") or self.settings.tenant_id,
                "frozen_specs": {
                    task.task_id: {
                        "path": task.frozen_spec_path,
                        "sha256": task.spec_sha256,
                        "files_owned": list(task.files_owned),
                    }
                    for task in plan.tasks
                },
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._write_meta(run_id, meta)
            self.store.set_run_status(run_id, RunStatus.RUNNING.value)
            await self._enqueue_ready_workers(run_id)
        except BaseException:
            self._release_active(run_id)
            raise
        return self.status(run_id)

    def next(self, run_id: str, agent_id: str, limit: int = 2) -> dict[str, object]:
        """Recover stale leases, apply admission policy, and atomically claim work."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit > _HARD_JOB_CEILING:
            raise ValueError(
                f"limit must be <= {_HARD_JOB_CEILING} (hard TaskTool ceiling)"
            )
        run = self._require_run(run_id)
        if run.status in _TERMINAL_RUN:
            return {
                "ok": False,
                "run_id": run_id,
                "status": run.status,
                "error": f"run is terminal ({run.status}); next is rejected",
                "dispatches": [],
            }
        # Reject mutated frozen plans before handing work to agents.
        try:
            plan = self._load_plan(run_id)
            self._verify_plan_hash(run_id, plan)
        except (OSError, ContractError, ValueError, RuntimeError) as exc:
            return {
                "ok": False,
                "run_id": run_id,
                "status": run.status,
                "error": str(exc),
                "dispatches": [],
            }
        if run.status != RunStatus.RUNNING.value:
            return {
                "ok": False,
                "run_id": run_id,
                "status": run.status,
                "error": "next claims only while run status is running",
                "dispatches": [],
            }
        if self._has_blocking_pause(run_id):
            return {
                "ok": False,
                "run_id": run_id,
                "status": run.status,
                "error": "questions/risk/gate pause blocks pending dispatches",
                "dispatches": [],
            }
        hit = self._precall_check(run_id, run)
        if hit is not None:
            return self._refuse_precall(run_id, hit)
        # This call is proof the dispatcher is alive: renew its in-flight leases
        # before reclaiming anything, so long-running Task Tool jobs are not
        # handed to a second agent mid-flight.
        self.store.renew_leases(agent_id=agent_id, run_id=run_id)
        self.store.recover_stale_dispatches(run_id=run_id)
        claimed: list[DispatchEnvelope] = []
        for candidate in self.store.list_dispatches(
            run_id, status=DispatchStatus.PENDING
        )[:limit]:
            counters = self._active_counters()
            decision = self.resource_policy.decide(
                snapshot=self._capture_snapshot(),
                tasktool_jobs=counters["jobs"],
                agent_slots=counters["slots"],
                heavy_jobs=counters["heavy"],
                gates=counters["gates"],
                requested_class=candidate.resource_class,
            )
            if decision.disposition is not ResourceDisposition.ALLOW:
                event = (
                    EventType.RESOURCE_PAUSED
                    if decision.disposition is ResourceDisposition.PAUSE
                    else EventType.RESOURCE_THROTTLED
                )
                self.store.add_event(
                    run_id,
                    event,
                    task_id=candidate.task_id,
                    detail={"reason": decision.reason, "dispatch_id": candidate.dispatch_id},
                )
                if decision.disposition is ResourceDisposition.PAUSE:
                    self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                    self._set_meta_status(run_id, RunStatus.PAUSED.value)
                    self._set_phase(
                        run_id,
                        "waiting-on-human",
                        stall_reason=f"resource:{decision.reason}",
                    )
                return {
                    "ok": True,
                    "run_id": run_id,
                    "status": decision.disposition.value,
                    "reason": decision.reason,
                    "dispatches": [item.to_dict() for item in claimed],
                }
            # Claim exactly the dispatch admission just decided on — see
            # claim_pending_dispatches for why an untargeted claim is unsound.
            item = self.store.claim_pending_dispatches(
                agent_id=agent_id,
                limit=1,
                run_id=run_id,
                dispatch_id=candidate.dispatch_id,
            )
            if not item:
                # Someone else took it between the snapshot and the claim.
                continue
            claimed.extend(item)
            self.run_meta.bump_step_counter(run_id)
        phase = self._phase_snapshot(run_id)
        return {
            "ok": True,
            "run_id": run_id,
            "status": "claimed" if claimed else "idle",
            "phase": phase.ux_phase.value,
            "stall_reason": phase.stall_reason,
            "dispatches": [item.to_dict() for item in claimed],
        }

    def report(
        self,
        dispatch_id: str,
        result_file: str | Path,
        agent_id: str,
        *,
        ok: bool,
        error: str = "",
        worker_id: str = "",
    ) -> dict[str, object]:
        """Validate a result artifact and finish its lease idempotently.

        ``agent_id`` is the *lease* identity — it must match the agent that
        claimed the dispatch (the root chat under ADR-0011). ``worker_id`` is the
        *execution* identity of the Task Tool job that actually did the work; it
        defaults to ``agent_id`` for legacy callers. Keeping them separate is what
        lets a single dispatcher hold leases while the worker-boundary check
        (`_cosplay_snapshot`) still sees distinct executors.
        """
        dispatch = self.store.get_dispatch(dispatch_id)
        if dispatch is None:
            raise KeyError(f"unknown dispatch: {dispatch_id}")
        source = Path(result_file)
        payload = _load_result(source, dispatch_id)
        target = self._require_spool_path(dispatch.run_id, Path(dispatch.result_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload["dispatch_id"] = dispatch_id
        payload["final_text"] = _result_text(payload)
        # Worker boundary attestation — distinct worker_id vs orch cosplay.
        kind = self.run_meta.dispatch_kind(self._read_meta(dispatch.run_id), dispatch_id)
        # Explicit flag wins, then an identity the worker itself wrote into the
        # result artifact, then the lease holder.
        executor = (
            worker_id.strip()
            or str(payload.get("worker_id") or "").strip()
            or agent_id
        )
        payload["worker_id"] = executor
        payload["agent_kind"] = kind
        payload["agent_id"] = agent_id
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        if target.exists():
            current = _load_result(target, dispatch_id)
            current["dispatch_id"] = dispatch_id
            current["final_text"] = _result_text(current)
            # Preserve identity fields on idempotent re-report.
            for key in ("worker_id", "agent_kind", "agent_id"):
                if key not in current and key in payload:
                    current[key] = payload[key]
            if current != payload and source.resolve() != target.resolve():
                # Allow identity enrichment on first rewrite when missing.
                stripped_current = {
                    k: v for k, v in current.items() if k not in {"worker_id", "agent_kind", "agent_id"}
                }
                stripped_payload = {
                    k: v for k, v in payload.items() if k not in {"worker_id", "agent_kind", "agent_id"}
                }
                if stripped_current != stripped_payload:
                    raise ValueError(
                        f"result for {dispatch_id} already exists with other content"
                    )
                payload = {**current, **{k: payload[k] for k in ("worker_id", "agent_kind", "agent_id")}}
                canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        self._atomic_write(target, canonical)

        # Phase 2: tool-hash / loop detect on reported results (stall → pause).
        self._maybe_detect_loop(dispatch.run_id, dispatch.task_id, payload)

        # Track agent↔dispatch for cosplay detection.
        self.run_meta.record_dispatch_agent(
            dispatch.run_id, dispatch_id, agent_id, kind, worker_id=executor
        )

        if dispatch.status is DispatchStatus.SUCCEEDED and ok:
            return {"ok": True, "idempotent": True, "dispatch": dispatch.to_dict()}
        if dispatch.status is DispatchStatus.FAILED and not ok:
            return {"ok": True, "idempotent": True, "dispatch": dispatch.to_dict()}
        if dispatch.status is DispatchStatus.CLAIMED:
            dispatch = self.store.mark_dispatch_running(dispatch_id, agent_id=agent_id)
        if ok:
            finished = self.store.complete_dispatch(
                dispatch_id, agent_id=agent_id, result_path=str(target)
            )
        else:
            finished = self.store.fail_dispatch(
                dispatch_id, agent_id=agent_id, error=error or _result_text(payload)
            )
        return {
            "ok": True,
            "idempotent": False,
            "dispatch": finished.to_dict(),
            "worker_id": executor,
            "agent_id": agent_id,
            "agent_kind": kind,
        }

    async def advance(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        run_id: str,
        approved_merge: bool = False,
        *,
        approve_risk: bool = False,
    ) -> dict[str, object]:
        """Apply isolated approval ops and/or consume completed dispatches.

        Order is explicit:
        1. ``approve_risk`` — clear the persisted risk gate and resume that recovery only.
        2. ``approved_merge`` — merge tasks already in ``MERGE_QUEUE`` only.
        3. Normal dispatch consumption — only when the run is ``RUNNING`` and not paused.

        Approvals never consume unrelated succeeded dispatches while the run is paused.
        """
        run = self._require_run(run_id)
        if run.status in _TERMINAL_RUN:
            status = self.status(run_id)
            status["idempotent"] = True
            status["error"] = f"run is terminal ({run.status})"
            return status
        # Pre-call budgets before consuming / claiming further work (not for
        # isolated approve_risk / approved_merge human ops).
        if not approve_risk and not approved_merge:
            hit = self._precall_check(run_id, run)
            if hit is not None:
                refused = self._refuse_precall(run_id, hit)
                refused.update(self.status(run_id))
                refused["ok"] = False
                refused["error"] = hit.reason
                return refused
        meta = self._read_meta(run_id)

        if approve_risk:
            self._approve_risk_gate(run_id, meta)
            meta = self._read_meta(run_id)
            await self._apply_risk_approvals(run_id)
            run = self._require_run(run_id)
            if (
                run.status not in _TERMINAL_RUN
                and not self._pending_questions(run_id)
                and not self._risk_gate_pending(run_id)
            ):
                # Risk recovery may leave tasks READY; resume running without
                # globally bypassing other pause reasons or unrelated work.
                merge_waiting = any(
                    task.status == TaskStatus.MERGE_QUEUE.value
                    for task in self.store.list_tasks(run_id)
                )
                if not merge_waiting:
                    self.store.set_run_status(run_id, RunStatus.RUNNING.value)
                    self._set_meta_status(run_id, RunStatus.RUNNING.value)
                    await self._enqueue_ready_workers(run_id)
            self._finish_run_if_complete(run_id)
            return self.status(run_id)

        if approved_merge:
            # Complete crash-safe receipts first, then merge only MERGE_QUEUE tasks.
            plan_error = self._frozen_plan_error(run_id)
            if plan_error is not None:
                status = self.status(run_id)
                status["ok"] = False
                status["error"] = plan_error
                return status
            await self._complete_recoverable_receipts(run_id)
            meta = self._read_meta(run_id)
            await self._merge_approved(run_id, meta)
            run = self._require_run(run_id)
            # After ship merge, activate dependents — still no unrelated consume.
            if (
                run.status == RunStatus.RUNNING.value
                and not self._has_blocking_pause(run_id)
            ):
                await self._enqueue_ready_workers(run_id)
            self._finish_run_if_complete(run_id)
            return self.status(run_id)

        if run.status != RunStatus.RUNNING.value or self._has_blocking_pause(run_id):
            # While paused/not running, only finish receipts whose side effects are
            # already visible in task state (crash between transition and receipt).
            plan_error = self._frozen_plan_error(run_id)
            if plan_error is not None:
                status = self.status(run_id)
                status["ok"] = False
                status["idempotent"] = True
                status["error"] = plan_error
                return status
            await self._complete_recoverable_receipts(run_id)
            status = self.status(run_id)
            status["idempotent"] = True
            if self._has_blocking_pause(run_id):
                status["error"] = "questions/risk/gate pause blocks advance"
            return status

        # Reject mutated frozen plans before any lifecycle / scope side effects.
        plan_error = self._frozen_plan_error(run_id)
        if plan_error is not None:
            status = self.status(run_id)
            status["ok"] = False
            status["error"] = plan_error
            return status

        succeeded = [
            item
            for item in self.store.list_dispatches(run_id, status=DispatchStatus.SUCCEEDED)
            if not self._is_dispatch_applied(item.dispatch_id, meta)
        ]
        for dispatch in succeeded:
            run = self._require_run(run_id)
            if run.status != RunStatus.RUNNING.value or self._has_blocking_pause(run_id):
                break
            consumed_dispatch = await self._consume_dispatch(run_id, dispatch, meta)
            if not consumed_dispatch:
                break
            meta = self._read_meta(run_id)
            self._mark_dispatch_applied(run_id, dispatch, meta)
            meta = self._read_meta(run_id)

        run = self._require_run(run_id)
        if run.status == RunStatus.RUNNING.value and not self._has_blocking_pause(run_id):
            await self._enqueue_ready_workers(run_id)
        self._finish_run_if_complete(run_id)
        return self.status(run_id)

    async def _complete_recoverable_receipts(self, run_id: str) -> None:
        """Mark application receipts when task state already reflects consume side effects."""
        meta = self._read_meta(run_id)
        for dispatch in self.store.list_dispatches(run_id, status=DispatchStatus.SUCCEEDED):
            if self._is_dispatch_applied(dispatch.dispatch_id, meta):
                continue
            if not self._receipt_side_effects_applied(run_id, dispatch, meta):
                continue
            consumed = await self._consume_dispatch(run_id, dispatch, meta)
            if consumed:
                meta = self._read_meta(run_id)
                self._mark_dispatch_applied(run_id, dispatch, meta)
                meta = self._read_meta(run_id)

    def _receipt_side_effects_applied(
        self,
        run_id: str,
        dispatch: DispatchEnvelope,
        meta: dict[str, object],
    ) -> bool:
        kind = self.run_meta.dispatch_kind(meta, dispatch.dispatch_id)
        task = self._require_task(run_id, dispatch.task_id)
        if kind == "sub-orchestrator":
            # Expansion artifact on disk or a successor worker dispatch proves
            # the decomposition verdict was already applied before the crash.
            if ExpansionStore(self._run_dir(run_id)).path_for(task.id).is_file():
                return True
            return any(
                self.run_meta.dispatch_kind(meta, item.dispatch_id) == "worker"
                for item in self.store.list_dispatches(run_id, task_id=task.id)
            )
        if kind == "reviewer":
            return task.status in {
                TaskStatus.MERGE_QUEUE.value,
                TaskStatus.READY.value,
                TaskStatus.RUNNING.value,
                TaskStatus.DONE.value,
                TaskStatus.POST_MERGE_FIX.value,
                TaskStatus.GATING.value,
            }
        if kind == "worker":
            # Only gate/review states prove worker finalization already happened.
            # MERGE_QUEUE alone must not recover unrelated succeeded workers.
            return task.status in {
                TaskStatus.GATING.value,
                TaskStatus.REVIEW.value,
            }
        if kind == "goal-judge":
            return any(
                self.run_meta.dispatch_kind(meta, item.dispatch_id) == "reviewer"
                for item in self.store.list_dispatches(run_id, task_id=dispatch.task_id)
            ) or task.status in {
                TaskStatus.READY.value,
                TaskStatus.RUNNING.value,
                TaskStatus.MERGE_QUEUE.value,
                TaskStatus.REVIEW.value,  # blocked APPROVE stays in REVIEW
            }
        if kind == "sprint-contract":
            return is_frozen(self._run_dir(run_id), task.id) or any(
                self.run_meta.dispatch_kind(meta, item.dispatch_id) == "worker"
                for item in self.store.list_dispatches(run_id, task_id=task.id)
            )
        return False

    def status(self, run_id: str) -> dict[str, object]:
        """Reconstruct status exclusively from durable store and spool state."""
        run = self._require_run(run_id)
        dispatches = self.store.list_dispatches(run_id)
        expansions: dict[str, list[str]] = {}
        with suppress(OSError, ContractError, ValueError):
            for artifact in ExpansionStore(self._run_dir(run_id)).read_all():
                expansions[artifact.parent_task_id] = [
                    child.task_id for child in artifact.children
                ]
        tasks = self.store.list_tasks(run_id)
        done = sum(1 for task in tasks if task.status == TaskStatus.DONE.value)
        pipeline_pct = (100.0 * done / len(tasks)) if tasks else None
        run_root = self._run_dir(run_id)
        honesty = honesty_pcts_for_run(run_root)
        evidence_pct = honesty["evidence_pct"] if honesty else evidence_pct_for_run(run_root)
        ac_bound_pct = honesty["ac_bound_pct"] if honesty else None
        yt = readiness_for_run(
            run_root, pipeline_pct=pipeline_pct, repo=self.repo_root
        )
        phase = self._phase_snapshot(run_id)
        cosplay = self._cosplay_snapshot(run_id, tasks)
        return {
            "ok": True,
            "run_id": run_id,
            "status": run.status,
            "phase": phase.ux_phase.value,
            "loop_phase": phase.loop_phase.value if phase.loop_phase else None,
            "stall_reason": phase.stall_reason,
            "evidence_pct": evidence_pct,
            "ac_bound_pct": ac_bound_pct,
            "pipeline_pct": pipeline_pct,
            "evidence_gate": evidence_gate_enabled(),
            "cosplay_risk": cosplay["cosplay_risk"],
            "worker_boundary": cosplay,
            "youtrack_readiness": yt.to_dict(),
            "tasks": [
                {
                    "task_id": task.id,
                    "status": task.status,
                    "complexity": task.complexity,
                    "provides": task.provides,
                }
                for task in tasks
            ],
            "dispatches": [item.to_dict() for item in dispatches],
            "pending": sum(item.status is DispatchStatus.PENDING for item in dispatches),
            "active": sum(item.status in _ACTIVE_DISPATCH for item in dispatches),
            "expansions": expansions,
        }

    async def resume(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        run_id: str,
        *,
        approve_risk: bool = False,
        answers: dict[str, str] | None = None,
        answers_file: str | Path | None = None,
    ) -> dict[str, object]:
        """Recover lifecycle and leases after controller reconstruction."""
        run = self._require_run(run_id)
        if run.status in _TERMINAL_RUN:
            status = self.status(run_id)
            status["ok"] = False
            status["error"] = f"run is terminal ({run.status}); resume is rejected"
            return status
        if run.status == RunStatus.PLAN_DRAFT.value:
            return {
                **self.status(run_id),
                "ready": False,
                "requires": "approve_plan",
            }
        meta = self._read_meta(run_id, missing_ok=True)
        if answers is not None or answers_file is not None:
            self.submit_answers(run_id, answers=answers, answers_file=answers_file)
        if approve_risk:
            self._approve_risk_gate(run_id, meta or {})
            meta = self._read_meta(run_id, missing_ok=True)
        answer_feedback = ""
        if self._pending_questions(run_id):
            if not self._answers_present(run_id):
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                status = self.status(run_id)
                status["requires"] = "answers"
                return status
            loaded = self._load_answers(run_id)
            if not loaded:
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                status = self.status(run_id)
                status["requires"] = "answers"
                status["error"] = "answers.json present but empty or invalid"
                return status
            answer_feedback = format_answers_for_feedback(loaded)
            question_task_id = self._questions_task_id(run_id)
            self._clear_questions(run_id)
            if question_task_id:
                task = self._require_task(run_id, question_task_id)
                if task.status in {
                    TaskStatus.RUNNING.value,
                    TaskStatus.BLOCKED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.GATING.value,
                    TaskStatus.REVIEW.value,
                }:
                    with suppress(RuntimeError):
                        self.store.transition_task(
                            task, TaskStatus.READY, note="resumed with human answers"
                        )
        if self._risk_gate_pending(run_id) and not self._risk_gate_approved(run_id):
            self.store.set_run_status(run_id, RunStatus.PAUSED.value)
            self._set_meta_status(run_id, RunStatus.PAUSED.value)
            status = self.status(run_id)
            status["requires"] = "risk_approval"
            return status
        merge_waiting = any(
            task.status == TaskStatus.MERGE_QUEUE.value
            for task in self.store.list_tasks(run_id)
        )
        post_merge_waiting = any(
            task.status == TaskStatus.POST_MERGE_FIX.value
            for task in self.store.list_tasks(run_id)
        )
        gate_replay_pending = any(
            dispatch.status is DispatchStatus.SUCCEEDED
            and not self._is_dispatch_applied(dispatch.dispatch_id, meta or {})
            and self.run_meta.dispatch_kind(meta or {}, dispatch.dispatch_id) == "worker"
            and self._require_task(run_id, dispatch.task_id).status
            == TaskStatus.GATING.value
            for dispatch in self.store.list_dispatches(run_id)
        )
        if not merge_waiting and not gate_replay_pending and not post_merge_waiting:
            await _maybe_await(self.lifecycle.recover(run_id))
        # Resume is the crash path — reclaim this run's abandoned leases, but do
        # not touch other runs sharing the store.
        self.store.recover_stale_dispatches(run_id=run_id)
        self._acquire_active(run_id, allow_same=True)
        if merge_waiting:
            self.store.set_run_status(run_id, RunStatus.PAUSED.value)
            self._set_meta_status(run_id, RunStatus.PAUSED.value)
            return self.status(run_id)
        if post_merge_waiting:
            if self._risk_gate_approved(run_id):
                await self._apply_risk_approvals(run_id)
            else:
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                status = self.status(run_id)
                status["requires"] = "risk_approval"
                return status
        self.store.set_run_status(run_id, RunStatus.RUNNING.value)
        self._set_meta_status(run_id, RunStatus.RUNNING.value)
        # Acceptance-repair stall: do not re-dispatch implement workers.
        meta = self._read_meta(run_id, missing_ok=True) or {}
        if self.run_meta.acceptance_repair_pending(run_id, meta):
            allowed, reason = done_allowed(self._run_dir(run_id))
            if not allowed:
                self._set_phase(
                    run_id,
                    "stuck",
                    stall_reason=f"acceptance_repair:{reason}",
                    detail="resume skipped worker refill; flip/waive AC first",
                )
                status = self.status(run_id)
                status["requires"] = "acceptance_flip"
                status["acceptance_repair"] = True
                status["acceptance_reason"] = reason
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                return status
            self.run_meta.clear_acceptance_repair(run_id)
        await self._enqueue_ready_workers(run_id, prior_result=answer_feedback)
        return self.status(run_id)

    def submit_answers(
        self,
        run_id: str,
        *,
        answers: dict[str, str] | None = None,
        answers_file: str | Path | None = None,
    ) -> dict[str, object]:
        """Persist structured human answers under the run spool (path-validated)."""
        self._require_run(run_id)
        if not self._pending_questions(run_id):
            raise ValueError(f"run {run_id} has no pending questions to answer")
        payload: dict[str, str] = {}
        if answers_file is not None:
            path = self._require_spool_path(run_id, Path(answers_file))
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("answers file must contain a JSON object")
            nested = raw.get("answers", raw)
            if not isinstance(nested, dict) or not nested:
                raise ValueError("answers file must include a non-empty answers mapping")
            payload = {str(key): str(value) for key, value in nested.items()}
        if answers:
            payload.update({str(key): str(value) for key, value in answers.items()})
        if not payload:
            raise ValueError("answers must be a non-empty mapping")
        questions = self._load_questions(run_id)
        expected_ids = {str(item.get("id")) for item in questions if item.get("id")}
        if expected_ids:
            missing = sorted(expected_ids - set(payload))
            if missing:
                raise ValueError(f"missing answers for questions: {missing}")
        target = self._pending_dir(run_id) / "answers.json"
        self._atomic_write(
            target,
            json.dumps(
                {"answers": payload, "answered_at": _now()},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        return {"ok": True, "run_id": run_id, "answers": payload}

    def abort(self, run_id: str, reason: str = "aborted by user") -> dict[str, object]:
        """Durably cancel queued work without touching repository changes."""
        for dispatch in self.store.list_dispatches(run_id):
            if dispatch.status in {
                DispatchStatus.PENDING,
                DispatchStatus.CLAIMED,
                DispatchStatus.RUNNING,
            }:
                self.store.cancel_dispatch(dispatch.dispatch_id, error=reason)
        for task in self.store.list_tasks(run_id):
            if task.status == TaskStatus.DONE.value:
                continue
            if task.status in {
                TaskStatus.BLOCKED.value,
                TaskStatus.FAILED.value,
            }:
                continue
            with suppress(RuntimeError):
                self.store.transition_task(
                    task, TaskStatus.BLOCKED, note=reason[:500]
                )
        self.store.set_run_status(run_id, RunStatus.ABORTED.value)
        self._set_meta_status(run_id, RunStatus.ABORTED.value)
        self.store.add_event(run_id, EventType.ERROR, detail={"reason": reason, "aborted": True})
        self._release_active(run_id)
        return self.status(run_id)

    async def _consume_dispatch(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        run_id: str,
        dispatch: DispatchEnvelope,
        meta: dict[str, object],
    ) -> bool:
        self.store.begin_dispatch_application(
            dispatch_id=dispatch.dispatch_id,
            run_id=run_id,
            task_id=dispatch.task_id,
            stage=dispatch.stage.value,
        )
        result_path = self._require_spool_path(run_id, Path(dispatch.result_path))
        payload = _load_result(result_path, dispatch.dispatch_id)
        text = _result_text(payload)
        kind = self.run_meta.dispatch_kind(meta, dispatch.dispatch_id)
        task = self._require_task(run_id, dispatch.task_id)
        if kind == "sub-orchestrator":
            return await self._consume_sub_orchestrator(run_id, dispatch, task, text)
        if kind == "sprint-contract":
            return await self._consume_sprint_contract(run_id, task, text)
        if kind == "worker":
            questions = parse_question_block(text)
            if questions.has_questions:
                self._persist_questions(run_id, task.id, questions)
                self.store.add_event(
                    run_id,
                    EventType.INPUT_REQUESTED,
                    task_id=task.id,
                    detail={
                        "questions": [
                            {
                                "id": item.id,
                                "question": item.question,
                                "options": item.options,
                                "why": item.why,
                            }
                            for item in questions.questions
                        ]
                    },
                )
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                return True
            provides = _extract_provides(text)
            if provides:
                task.provides = provides
            if "HARNESS_DONE" in text:
                task.completion_signals += 1
            if provides or "HARNESS_DONE" in text:
                self.store.update_task_fields(task)
            changed_files = _changed_files(None)
            # Immutable claim-time scope — never re-read mutable PlanTask ownership.
            files_owned = dispatch.files_owned
            if task.status == TaskStatus.RUNNING.value:
                finalized = await _maybe_await(
                    self.lifecycle.finalize_worker(
                        run_id, task.id, text, files_owned=files_owned
                    )
                )
                changed_files = _changed_files(finalized)
                self._write_task_diff(run_id, task.id, _lifecycle_diff(finalized))
                self._write_checkpoint(
                    run_id,
                    task.id,
                    stage="worker",
                    status=self._require_task(run_id, task.id).status,
                    evidence_summary=_compact_result(text),
                    changed_files=changed_files,
                    next_action="run local gates",
                )
                if not _lifecycle_ok(finalized):
                    await self._queue_worker_retry(
                        run_id, task, _lifecycle_detail(finalized)
                    )
                    return True
            elif task.status == TaskStatus.GATING.value:
                prior = self._checkpoints.read(run_id, task.id)
                changed_files = prior.changed_files if prior is not None else ()
            elif task.status == TaskStatus.REVIEW.value:
                # Crash after gates advanced to REVIEW but before successor/receipt.
                self._recover_worker_post_gate(run_id, task, text)
                return True
            elif task.status == TaskStatus.MERGE_QUEUE.value:
                # Trivial path: merge pause may be missing after a crash.
                self._write_checkpoint(
                    run_id,
                    task.id,
                    stage="gate",
                    status=task.status,
                    evidence_summary="local gates passed",
                    changed_files=self._checkpoint_files(run_id, task.id),
                    next_action=self._ship_next_action(),
                )
                self._ship_or_pause(run_id, task.id)
                return True
            elif task.status in {
                TaskStatus.READY.value,
                TaskStatus.DONE.value,
                TaskStatus.POST_MERGE_FIX.value,
            }:
                # READY + succeeded worker usually means a prior fail already requeued.
                # Ack the receipt without spawning another fan-out here — advance's
                # trailing _enqueue_ready_workers honors READY + !_has_live_worker.
                return True
            else:
                raise RuntimeError(
                    f"worker result cannot advance task {task.id} from {task.status}"
                )
            result = await self._run_local_gate(run_id, task.id, full=False)
            if result is None:
                # Gate admission deferred (lock / MemAvailable). Keep the succeeded
                # worker unapplied so the next advance retries gates — never a new
                # worker for the same completion.
                self._write_checkpoint(
                    run_id,
                    task.id,
                    stage="gate",
                    status=self._require_task(run_id, task.id).status,
                    evidence_summary="local gates deferred (resource/lock)",
                    changed_files=changed_files,
                    next_action="run local gates",
                )
                return False
            refreshed = self._require_task(run_id, task.id)
            if not _lifecycle_ok(result):
                self._write_checkpoint(
                    run_id,
                    task.id,
                    stage="gate",
                    status=refreshed.status,
                    evidence_summary=_lifecycle_detail(result),
                    changed_files=changed_files,
                    next_action="retry worker",
                )
                await self._queue_worker_retry(run_id, task, _lifecycle_detail(result))
            else:
                self._complete_worker_post_gate(
                    run_id, refreshed, text, changed_files=changed_files
                )
            return True
        if kind == "goal-judge":
            self._write_checkpoint(
                run_id,
                task.id,
                stage="goal-judge",
                status=task.status,
                evidence_summary=_compact_result(text),
                changed_files=self._checkpoint_files(run_id, task.id),
                next_action=(
                    "reviewer"
                    if _parse_verdict(text) is Verdict.APPROVE
                    else "retry worker"
                ),
            )
            if _parse_verdict(text) is Verdict.APPROVE:
                # Judge seat: APPROVE with red evidence is a no-op (no reviewer).
                allowed, reason = done_allowed(self._run_dir(run_id), task_id=task.id)
                if not allowed:
                    self.store.add_event(
                        run_id,
                        EventType.JUDGE_APPROVE_BLOCKED,
                        task_id=task.id,
                        detail={
                            "reason": reason,
                            "seat": "goal-judge",
                            "note": "APPROVE ignored; evidence red",
                        },
                    )
                    self._set_phase(
                        run_id,
                        "stuck",
                        stall_reason=f"gate_red:{reason}",
                        detail="goal-judge APPROVE blocked",
                    )
                    self._mark_acceptance_repair(run_id, reason)
                    self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                    self._set_meta_status(run_id, RunStatus.PAUSED.value)
                    return True
                self._enqueue(task, TaskStage.REVIEW, "reviewer", prior_result=text)
            else:
                await self._queue_worker_retry(run_id, task, text)
            return True
        if kind == "reviewer":
            verdict = _parse_verdict(text)
            # Crash-safe replay: apply_review may have already moved the task out of
            # REVIEW (MERGE_QUEUE on APPROVE, READY/RUNNING on CHANGES) before pause
            # and/or the application receipt completed.
            if task.status != TaskStatus.REVIEW.value:
                if task.status == TaskStatus.MERGE_QUEUE.value:
                    self._write_checkpoint(
                        run_id,
                        task.id,
                        stage="reviewer",
                        status=task.status,
                        evidence_summary=_compact_result(text),
                        changed_files=self._checkpoint_files(run_id, task.id),
                        next_action=self._ship_next_action(),
                    )
                    self._ship_or_pause(run_id, task.id)
                    return True
                if task.status in {
                    TaskStatus.READY.value,
                    TaskStatus.RUNNING.value,
                }:
                    self._write_checkpoint(
                        run_id,
                        task.id,
                        stage="reviewer",
                        status=task.status,
                        evidence_summary=_compact_result(text),
                        changed_files=self._checkpoint_files(run_id, task.id),
                        next_action="retry worker",
                    )
                    await self._queue_worker_retry(run_id, task, text)
                    return True
                if task.status == TaskStatus.DONE.value:
                    # Crash after manual_ship / merge already marked DONE.
                    self._activate_dependents(run_id)
                    self._finish_run_if_complete(run_id)
                    return True
                if task.status in {
                    TaskStatus.POST_MERGE_FIX.value,
                    TaskStatus.GATING.value,
                }:
                    return True
                raise RuntimeError(
                    f"reviewer result cannot advance task {task.id} from {task.status}"
                )
            result = self.lifecycle.apply_review(run_id, task.id, text)
            refreshed = self._require_task(run_id, task.id)
            action = str(getattr(result, "action", "")).lower()
            if action in {"judge_approve_blocked", "done_refused"}:
                self._write_checkpoint(
                    run_id,
                    task.id,
                    stage="reviewer",
                    status=refreshed.status,
                    evidence_summary=_compact_result(text),
                    changed_files=self._checkpoint_files(run_id, task.id),
                    next_action="evidence required",
                )
                self._set_phase(
                    run_id,
                    "stuck",
                    stall_reason=f"gate_red:{_lifecycle_detail(result)}",
                    detail="reviewer APPROVE blocked",
                )
                self._mark_acceptance_repair(
                    run_id, _lifecycle_detail(result)
                )
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                return True
            approved = verdict is Verdict.APPROVE and _lifecycle_ok(result)
            self._write_checkpoint(
                run_id,
                task.id,
                stage="reviewer",
                status=refreshed.status,
                evidence_summary=_compact_result(text),
                changed_files=self._checkpoint_files(run_id, task.id),
                next_action=(
                    self._ship_next_action() if approved else "retry worker"
                ),
            )
            if approved:
                if refreshed.status == TaskStatus.DONE.value:
                    self._maybe_write_lesson(run_id, refreshed)
                    self._maybe_write_journal(run_id, refreshed)
                    self._activate_dependents(run_id)
                    self._finish_run_if_complete(run_id)
                else:
                    self._ship_or_pause(run_id, task.id)
            else:
                await self._queue_worker_retry(run_id, task, text)
            return True
        raise RuntimeError(f"unknown dispatch kind for {dispatch.dispatch_id}")

    async def _queue_worker_retry(
        self,
        run_id: str,
        task: Task,
        feedback: str,
        *,
        ignore_dispatch: str = "",
    ) -> None:
        current = self._require_task(run_id, task.id)
        current.attempts += 1
        self.store.update_task_fields(current)
        if current.attempts >= self.settings.max_attempts:
            with suppress(RuntimeError):
                self.store.transition_task(
                    current,
                    TaskStatus.BLOCKED,
                    note=f"retry budget exhausted ({current.attempts}): {feedback[-400:]}",
                )
            self._persist_risk_gate(
                run_id,
                task_id=task.id,
                reason="worker retry budget exhausted",
                evidence=feedback,
                kind="retry_budget",
            )
            self.store.set_run_status(run_id, RunStatus.PAUSED.value)
            self._set_meta_status(run_id, RunStatus.PAUSED.value)
            self.store.add_event(
                run_id,
                EventType.ERROR,
                task_id=task.id,
                detail={
                    "reason": "retry budget exhausted",
                    "attempts": current.attempts,
                    "max_attempts": self.settings.max_attempts,
                },
            )
            return
        if current.status in {
            TaskStatus.REVIEW.value,
            TaskStatus.GATING.value,
            TaskStatus.FAILED.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.MERGE_QUEUE.value,
            TaskStatus.RUNNING.value,
        }:
            with suppress(RuntimeError):
                self.store.transition_task(current, TaskStatus.READY, note=feedback[-500:])
        await self._enqueue_ready_workers(
            run_id, prior_result=feedback, ignore_dispatch=ignore_dispatch
        )

    async def _consume_sub_orchestrator(
        self,
        run_id: str,
        dispatch: DispatchEnvelope,
        task: Task,
        text: str,
    ) -> bool:
        """Agent-in-agent: apply the sub-orchestrator's decomposition verdict.

        Valid fan-out → freeze an ExpansionArtifact, create child tasks, park the
        parent in PENDING until children complete (it then returns as the
        integration worker with children ``Provides`` injected). ``decompose:
        false`` → hand the analysis to a direct worker. Malformed → retry with
        feedback through the normal retry budget.
        """
        questions = parse_question_block(text)
        if questions.has_questions:
            self._persist_questions(run_id, task.id, questions)
            self.store.add_event(
                run_id,
                EventType.INPUT_REQUESTED,
                task_id=task.id,
                detail={
                    "questions": [
                        {
                            "id": item.id,
                            "question": item.question,
                            "options": item.options,
                            "why": item.why,
                        }
                        for item in questions.questions
                    ]
                },
            )
            self.store.set_run_status(run_id, RunStatus.PAUSED.value)
            self._set_meta_status(run_id, RunStatus.PAUSED.value)
            return True
        decision = parse_expansion(text)
        if decision is None:
            await self._queue_worker_retry(
                run_id,
                task,
                "sub-orchestrator result lacks a valid fenced JSON decompose block "
                '({"decompose": true|false, "subtasks": [...]}) — return exactly one',
                ignore_dispatch=dispatch.dispatch_id,
            )
            return True
        if not decision.decompose:
            await self._dispatch_direct_worker(run_id, task, text, decision)
            return True
        # Phase 3 economics: breadth gate before multi-agent fan-out.
        breadth = assess_breadth(
            decision,
            parent_files_owned=dispatch.files_owned,
            max_children=self.settings.effective_decompose_max_children,
            force=self.settings.economics_enabled,
        )
        if breadth.denied:
            self.store.add_event(
                run_id,
                EventType.DECOMPOSE_DENIED,
                task_id=task.id,
                detail={
                    "code": breadth.code,
                    "reason": breadth.reason,
                    "errors": list(breadth.errors[:12]),
                    "profile": self.settings.fanout_profile,
                },
            )
            # Avoid multi-agent tax without isolation — implement directly.
            denied = ExpansionDecision(
                decompose=False,
                reason=f"DECOMPOSE_DENIED: {breadth.reason}",
            )
            await self._dispatch_direct_worker(run_id, task, text, denied)
            return True
        errors = validate_expansion(
            decision,
            parent_files_owned=dispatch.files_owned,
            existing_task_ids=[item.id for item in self.store.list_tasks(run_id)],
            parent_task_id=task.id,
            max_children=self.settings.effective_decompose_max_children,
        )
        if errors:
            await self._queue_worker_retry(
                run_id,
                task,
                "decomposition rejected by control plane: " + "; ".join(errors[:8]),
                ignore_dispatch=dispatch.dispatch_id,
            )
            return True
        self._apply_expansion(run_id, task, decision)
        return True

    async def _dispatch_direct_worker(
        self,
        run_id: str,
        task: Task,
        text: str,
        decision: ExpansionDecision,
    ) -> None:
        """decompose=false — the analysis becomes the worker's starting context."""
        current = self._require_task(run_id, task.id)
        if current.status == TaskStatus.READY.value:
            await _maybe_await(self.lifecycle.prepare_task(run_id, task.id))
            current = self._require_task(run_id, task.id)
        analysis = _compact_result(text, 1_400)
        prior = (
            f"Sub-orchestrator analysis (implement directly): {decision.reason}\n{analysis}"
        )
        self._write_checkpoint(
            run_id,
            task.id,
            stage="decompose",
            status=current.status,
            evidence_summary=f"implement directly: {decision.reason or 'no fan-out needed'}",
            next_action="worker",
        )
        self._enqueue(current, TaskStage.IMPLEMENT, "worker", prior_result=prior)

    def _apply_expansion(
        self,
        run_id: str,
        task: Task,
        decision: ExpansionDecision,
    ) -> None:
        plan = self._load_plan(run_id)
        parent_plan = next(
            (item for item in plan.tasks if item.task_id == task.id), None
        )
        if parent_plan is None:
            raise RuntimeError(f"frozen plan missing task {task.id}")
        spool = self._run_dir(run_id)

        def model_for_child(
            complexity: str,
        ) -> tuple[str, ModelTier, ResourceClass]:
            model, tier = model_for(self.settings, Role.WORKER, complexity)
            return model, tier, resource_for(TaskStage.IMPLEMENT, tier)

        children = build_child_plan_tasks(
            decision,
            parent=parent_plan,
            run_id=run_id,
            spool=spool,
            model_for_child=model_for_child,
        )
        artifact = new_artifact(
            run_id=run_id,
            parent_task_id=task.id,
            reason=decision.reason,
            children=children,
        )
        path, digest = ExpansionStore(spool).write(artifact)
        briefs = {item.child_id: item for item in decision.subtasks}
        for child_plan in children:
            brief_id = child_plan.task_id.removeprefix(f"{task.id}-")
            brief = briefs.get(brief_id)
            self.store.upsert_task(
                Task(
                    id=child_plan.task_id,
                    run_id=run_id,
                    title=brief.title if brief else child_plan.task_id,
                    spec_path=child_plan.frozen_spec_path,
                    status=TaskStatus.PENDING.value,
                    depends_on=list(child_plan.dependencies),
                    complexity=brief.complexity if brief else "small",
                )
            )
            self.store.add_event(
                run_id,
                EventType.TASK_CREATED,
                task_id=child_plan.task_id,
                detail={
                    "expanded_from": task.id,
                    "files_owned": list(child_plan.files_owned),
                },
            )
        current = self._require_task(run_id, task.id)
        merged_deps = list(
            dict.fromkeys([*current.depends_on, *(child.task_id for child in children)])
        )
        current.depends_on = merged_deps
        # upsert_task persists depends_on (update_task_fields does not).
        self.store.upsert_task(current)
        if current.status in {TaskStatus.RUNNING.value, TaskStatus.READY.value}:
            self.store.transition_task(
                current,
                TaskStatus.PENDING,
                note=f"decomposed into {len(children)} subtasks",
            )
        def _record_expansion(meta: dict[str, object]) -> None:
            expansions = dict(_dict(meta.get("expansions")))
            expansions[task.id] = {"path": str(path), "sha256": digest}
            meta["expansions"] = expansions

        self._update_meta(run_id, _record_expansion, missing_ok=False)
        self._write_checkpoint(
            run_id,
            task.id,
            stage="decompose",
            status=TaskStatus.PENDING.value,
            evidence_summary=(
                f"expanded into {len(children)} subtasks: "
                + ", ".join(child.task_id for child in children)
            ),
            next_action="await children, then integrate",
        )

    async def _merge_approved(self, run_id: str, meta: dict[str, object]) -> None:
        for task in self.store.list_tasks(run_id):
            if task.status != TaskStatus.MERGE_QUEUE.value:
                continue
            premerge_gate = await self._run_local_gate(run_id, task.id, full=True)
            if premerge_gate is None or not _lifecycle_ok(premerge_gate):
                evidence = _lifecycle_detail(premerge_gate)
                refreshed = self._require_task(run_id, task.id)
                with suppress(RuntimeError):
                    self.store.transition_task(
                        refreshed, TaskStatus.READY, note=evidence[-500:]
                    )
                self._persist_risk_gate(
                    run_id,
                    task_id=task.id,
                    reason="pre-merge full gate failed",
                    evidence=evidence,
                    kind="pre_merge_fix",
                )
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                self.store.add_event(
                    run_id,
                    EventType.ERROR,
                    task_id=task.id,
                    detail={
                        "reason": "pre-merge full gate failed",
                        "evidence": evidence,
                    },
                )
                return
            result = await _maybe_await(self.lifecycle.merge(run_id, task.id, approved=True))
            if not _lifecycle_ok(result):
                action = str(getattr(result, "action", ""))
                evidence = _lifecycle_detail(result)
                current = self._require_task(run_id, task.id)
                if action == "post_integration_gate_failed" or current.status == (
                    TaskStatus.POST_MERGE_FIX.value
                ):
                    self._persist_risk_gate(
                        run_id,
                        task_id=task.id,
                        reason="post-integration full gate failed",
                        evidence=evidence,
                        kind="post_merge_fix",
                    )
                elif current.status == TaskStatus.MERGE_QUEUE.value:
                    with suppress(RuntimeError):
                        self.store.transition_task(
                            current, TaskStatus.READY, note=evidence[-500:]
                        )
                    self._persist_risk_gate(
                        run_id,
                        task_id=task.id,
                        reason="merge failed",
                        evidence=evidence,
                        kind="pre_merge_fix",
                    )
                self._write_checkpoint(
                    run_id,
                    task.id,
                    stage="merge",
                    status=self._require_task(run_id, task.id).status,
                    evidence_summary=evidence,
                    changed_files=self._checkpoint_files(run_id, task.id),
                    next_action="fix merge failure",
                )
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                self.store.add_event(
                    run_id,
                    EventType.ERROR,
                    task_id=task.id,
                    detail={
                        "reason": "merge or post-integration full gate failed",
                        "evidence": evidence,
                    },
                )
                return
            done = self._require_task(run_id, task.id)
            self._write_checkpoint(
                run_id,
                task.id,
                stage="merge",
                status=done.status,
                evidence_summary=_lifecycle_detail(result) or "merged",
                changed_files=_changed_files(result) or self._checkpoint_files(run_id, task.id),
                next_action="done",
            )
            if done.status == TaskStatus.DONE.value:
                self._maybe_write_lesson(run_id, done)
                self._maybe_write_journal(run_id, done)
        self.store.set_run_status(run_id, RunStatus.RUNNING.value)
        self._set_meta_status(run_id, RunStatus.RUNNING.value)

    async def _enqueue_ready_workers(
        self, run_id: str, *, prior_result: str = "", ignore_dispatch: str = ""
    ) -> None:
        self._activate_dependents(run_id)
        for task in self.store.list_tasks(run_id):
            if task.status != TaskStatus.READY.value or self._has_live_worker(
                run_id, task.id, ignore_dispatch=ignore_dispatch
            ):
                continue
            await _maybe_await(self.lifecycle.prepare_task(run_id, task.id))
            prepared = self._require_task(run_id, task.id)
            if self._should_decompose(run_id, prepared):
                # Agent-in-agent: a sub-orchestrator plans the fan-out first.
                self._enqueue(
                    prepared,
                    TaskStage.PLAN,
                    "sub-orchestrator",
                    prior_result=prior_result,
                )
                continue
            if self._should_sprint_contract(run_id, prepared):
                self._enqueue(
                    prepared,
                    TaskStage.SPRINT_CONTRACT,
                    "sprint-contract",
                    prior_result=prior_result,
                )
                self._set_phase(run_id, "researching", stall_reason="none")
                continue
            self._enqueue(prepared, TaskStage.IMPLEMENT, "worker", prior_result=prior_result)

    def _should_sprint_contract(self, run_id: str, task: Task) -> bool:
        """Medium/large tasks freeze acceptance before coding when flag is on."""
        if not needs_sprint_contract(task.complexity):
            return False
        if is_frozen(self._run_dir(run_id), task.id):
            return False
        # Already have a pending/active sprint-contract dispatch.
        meta = self._read_meta(run_id, missing_ok=True)
        for item in self.store.list_dispatches(run_id, task_id=task.id):
            if self.run_meta.dispatch_kind(meta, item.dispatch_id) != "sprint-contract":
                continue
            if item.status in {
                DispatchStatus.PENDING,
                DispatchStatus.CLAIMED,
                DispatchStatus.RUNNING,
                DispatchStatus.SUCCEEDED,
            }:
                return False
        return True

    async def _consume_sprint_contract(
        self, run_id: str, task: Task, text: str
    ) -> bool:
        questions = parse_question_block(text)
        if questions.has_questions:
            self._persist_questions(run_id, task.id, questions)
            self.store.add_event(
                run_id,
                EventType.INPUT_REQUESTED,
                task_id=task.id,
                detail={"source": "sprint-contract"},
            )
            self.store.set_run_status(run_id, RunStatus.PAUSED.value)
            self._set_meta_status(run_id, RunStatus.PAUSED.value)
            self._set_phase(
                run_id,
                "waiting-on-human",
                stall_reason="waiting_human:sprint_contract",
            )
            return True
        plan_task = self._plan_task(run_id, task.id)
        acceptance = list(plan_task.acceptance) if plan_task else []
        if not acceptance:
            try:
                spec = Path(task.spec_path).read_text(encoding="utf-8")
                acceptance = list(_acceptance(spec))
            except OSError:
                acceptance = []
        contract = freeze_contract(
            self._run_dir(run_id),
            run_id=run_id,
            task_id=task.id,
            acceptance_lines=acceptance,
        )
        self.store.add_event(
            run_id,
            EventType.SPRINT_CONTRACT_FROZEN,
            task_id=task.id,
            detail=contract.to_dict(),
        )
        self._write_checkpoint(
            run_id,
            task.id,
            stage="sprint_contract",
            status=task.status,
            evidence_summary=f"acceptance frozen sha={contract.acceptance_sha256[:12]}",
            next_action="implement",
        )
        # Keep READY so worker enqueue proceeds on the next ready pass.
        if task.status != TaskStatus.READY.value:
            with suppress(RuntimeError):
                self.store.transition_task(task, TaskStatus.READY, note="sprint contract frozen")
        self._enqueue(task, TaskStage.IMPLEMENT, "worker", prior_result=text)
        self._set_phase(run_id, "coding", stall_reason="none")
        return True

    def _should_decompose(self, run_id: str, task: Task) -> bool:
        """Large tasks get a sub-orchestrator pass unless the spec opts out.

        Never re-decomposes: an existing ExpansionArtifact means children ran
        (or are running) and the parent returns only as the integration worker.
        Children themselves are exempt (their specs carry ``Decompose: no``).
        """
        if not self.settings.decompose_enabled:
            return False
        if ExpansionStore(self._run_dir(run_id)).path_for(task.id).is_file():
            return False
        spec_flag = self._spec_decompose_flag(run_id, task)
        if spec_flag is not None:
            return spec_flag
        return task.complexity.lower().strip() == "large"

    def _spec_decompose_flag(self, run_id: str, task: Task) -> bool | None:
        plan_task = self._plan_task(run_id, task.id)
        spec_path = Path(
            (plan_task.frozen_spec_path if plan_task else "") or task.spec_path
        )
        try:
            text = spec_path.read_text(encoding="utf-8")
        except OSError:
            return None
        match = re.search(r"(?im)^\s*Decompose:\s*(yes|no|true|false)\s*$", text)
        if match is None:
            return None
        return match.group(1).lower() in {"yes", "true"}

    def _enqueue(
        self,
        task: Task,
        stage: TaskStage,
        kind: str,
        *,
        prior_result: str = "",
    ) -> DispatchEnvelope:
        meta = self._read_meta(task.run_id)
        existing = [
            item
            for item in self.store.list_dispatches(task.run_id, task_id=task.id)
            if self.run_meta.dispatch_kind(meta, item.dispatch_id) == kind
        ]
        # Basic duplicate guard: do not mint another PENDING/CLAIMED/RUNNING
        # envelope of the same kind while one is already live.
        live = [
            item
            for item in existing
            if item.status
            in {
                DispatchStatus.PENDING,
                DispatchStatus.CLAIMED,
                DispatchStatus.RUNNING,
            }
        ]
        if live:
            return live[0]
        # Also refuse a fresh worker when a succeeded+unapplied worker already
        # covers the completion (resume thrash / noop re-dispatch).
        if kind == "worker":
            for item in existing:
                if (
                    item.status is DispatchStatus.SUCCEEDED
                    and not self._is_dispatch_applied(item.dispatch_id, meta)
                ):
                    return item
        sequence = len(existing) + 1
        dispatch_id = f"{task.run_id}-{task.id}-{kind}-{sequence}"
        old = self.store.get_dispatch(dispatch_id)
        if old is not None:
            return old
        run = self._require_run(task.run_id)
        plan = self._load_plan(task.run_id)
        plan_task = self._plan_task_anywhere(task.run_id, task.id, plan)
        if plan_task is None:
            raise RuntimeError(f"frozen plan missing task {task.id}")
        self._verify_plan_hash(task.run_id, plan)
        spool = self._run_dir(task.run_id)
        prompt_path = self._require_spool_path(
            task.run_id, spool / "prompts" / f"{dispatch_id}.md"
        )
        result_path = self._require_spool_path(
            task.run_id, spool / "results" / f"{dispatch_id}.json"
        )
        frozen_spec = Path(plan_task.frozen_spec_path or task.spec_path)
        if not frozen_spec.is_file():
            raise RuntimeError(f"frozen spec missing for task {task.id}: {frozen_spec}")
        if plan_task.spec_sha256:
            digest = hashlib.sha256(frozen_spec.read_bytes()).hexdigest()
            if digest != plan_task.spec_sha256:
                raise RuntimeError(f"frozen spec hash mismatch for task {task.id}")
        spec_text = frozen_spec.read_text(encoding="utf-8")
        acceptance = plan_task.acceptance or tuple(_acceptance(spec_text))
        files_owned = plan_task.files_owned
        role = _role_for_kind(kind)
        model, tier = model_for(self.settings, role, task.complexity, kind=kind)
        resource_class = resource_for(stage, tier)
        context_md_path = self._project_context_path(run)
        pack = build_context_pack(
            run=run,
            task=task,
            store=self.store,
            repo_root=self.repo_root,
            acceptance=acceptance,
            feedback=prior_result,
            checkpoint_text=self._checkpoints.summary_for_context(task.run_id, task.id),
            project_brief=self._project_brief(run),
            char_budget=self.settings.context_char_budget,
            pointer_only=self.settings.context_pointer_only,
        )
        skill_paths = tuple(
            select_for_dispatch(
                self.settings.root,
                kind=kind,
                stage=stage.value,
                gate_fail=(
                    "CHANGES" in (prior_result or "").upper()
                    or "fail" in (prior_result or "").lower()
                ),
            )
        )
        frozen = (
            "TaskTool protocol 3.0",
            f"plan_hash:{plan.plan_hash}",
            *(
                f"depends:{dep_id}: {provides}"
                for dep_id, provides in pack.dependency_provides
            ),
        )
        review_bundle = None
        if kind in {"reviewer", "goal-judge"}:
            # The goal-judge decides whether the goal was met; without the diff it
            # can only re-read the worker's own claims about itself.
            review_bundle = build_review_bundle(
                self._checkpoint_files(task.run_id, task.id),
                repo_root=self.repo_root,
                project=run.project,
                diff=self._read_task_diff(task.run_id, task.id),
            )
        worktree = task.worktree_path or str(self.repo_root)
        read_only_extra: tuple[str, ...] = ()
        if context_md_path is not None and kind == "sub-orchestrator":
            # The decomposer reads the full project context, not just the brief.
            read_only_extra = (str(context_md_path),)
        read_only = (
            str(frozen_spec),
            *read_only_extra,
            *tuple(
                path
                for path in plan_task.read_only_context
                if path != str(frozen_spec) and path not in read_only_extra
            ),
        )
        prompt = build_prompt(
            run,
            task,
            stage,
            spec_text=spec_text,
            prior_result=prior_result,
            kind=kind,
            worktree=worktree,
            files_owned=files_owned,
            read_only_paths=read_only,
            frozen_contracts=frozen,
            acceptance=acceptance,
            resource_class=resource_class,
            canon_pointer=pack.canon_pointer,
            context_pack=pack,
            review_bundle=review_bundle,
            skill_paths=skill_paths,
        )
        self._atomic_write(prompt_path, prompt)
        now = _now()
        dispatch = DispatchEnvelope(
            run_id=run.id,
            dispatch_id=dispatch_id,
            task_id=task.id,
            role=role,
            stage=stage,
            status=DispatchStatus.PENDING,
            prompt_path=str(prompt_path),
            result_path=str(result_path),
            model=model,
            model_tier=tier,
            repo=str(self.repo_root),
            worktree=worktree,
            files_owned=files_owned,
            read_only_context=read_only,
            frozen_contracts=frozen,
            acceptance=acceptance,
            resource_class=resource_class,
            source_document_paths=(str(frozen_spec),),
            dependencies=tuple(task.depends_on),
            created_at=now,
            updated_at=now,
            skill_paths=skill_paths,
        )
        self.store.create_or_assert_dispatch(dispatch)
        if skill_paths:
            self.store.add_event(
                task.run_id,
                EventType.SKILLS_INJECTED,
                task_id=task.id,
                detail={
                    "dispatch_id": dispatch_id,
                    "skill_paths": list(skill_paths),
                    "kind": kind,
                },
            )
        self.run_meta.record_dispatch_kind(task.run_id, dispatch_id, kind)
        return dispatch

    def _build_plan(self, run: Run, *, spec_sources: tuple[str, ...]) -> PlanArtifact:
        now = _now()
        plan_root = resolve_plan_root(self.repo_root)
        plan_path = plan_root / "PLAN.md"
        spool = self._run_dir(run.id)
        specs_dir = spool / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        tasks: list[PlanTask] = []
        for task in self.store.list_tasks(run.id):
            source = Path(task.spec_path)
            text = source.read_text(encoding="utf-8")
            raw_owned = parse_spec_files(text)
            try:
                files_owned = tuple(normalize_owned_path(path) for path in raw_owned)
            except ContractError as exc:
                raise ValueError(f"task {task.id}: {exc}") from exc
            self._assert_owned_paths_safe(files_owned, task_id=task.id)
            frozen_spec = specs_dir / f"{task.id}.md"
            shutil.copyfile(source, frozen_spec)
            digest = hashlib.sha256(frozen_spec.read_bytes()).hexdigest()
            model, tier = model_for(self.settings, Role.WORKER, task.complexity)
            tasks.append(
                PlanTask(
                    task_id=task.id,
                    role=Role.WORKER,
                    stage=TaskStage.IMPLEMENT,
                    prompt_path=str(spool / "prompts" / f"{task.id}.md"),
                    result_path=str(spool / "results" / f"{task.id}.json"),
                    model=model,
                    model_tier=tier,
                    repo=str(self.repo_root),
                    worktree=task.worktree_path or str(self.repo_root),
                    files_owned=files_owned,
                    read_only_context=(str(frozen_spec),),
                    frozen_contracts=("TaskTool protocol 3.0",),
                    acceptance=tuple(_acceptance(text)),
                    resource_class=resource_for(TaskStage.IMPLEMENT, tier),
                    source_document_paths=(str(frozen_spec),),
                    dependencies=tuple(task.depends_on),
                    spec_sha256=digest,
                    frozen_spec_path=str(frozen_spec),
                )
            )
        unsigned = PlanArtifact(
            run_id=run.id,
            goal=run.goal,
            project=run.project,
            repo=str(self.repo_root),
            base_branch=run.base_branch,
            spec_sources=tuple(dict.fromkeys((str(plan_path), *spec_sources))),
            tasks=tuple(tasks),
            created_at=now,
            updated_at=now,
        )
        plan_hash = hashlib.sha256(unsigned.to_json().encode("utf-8")).hexdigest()
        return PlanArtifact(
            run_id=unsigned.run_id,
            goal=unsigned.goal,
            project=unsigned.project,
            repo=unsigned.repo,
            base_branch=unsigned.base_branch,
            spec_sources=unsigned.spec_sources,
            tasks=unsigned.tasks,
            created_at=unsigned.created_at,
            updated_at=unsigned.updated_at,
            protocol_version=unsigned.protocol_version,
            plan_hash=plan_hash,
        )

    def _complete_worker_post_gate(
        self,
        run_id: str,
        task: Task,
        text: str,
        *,
        changed_files: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        """Restore post-gate checkpoint and enqueue the next successor attempt."""
        files = (
            tuple(changed_files)
            if changed_files is not None
            else self._checkpoint_files(run_id, task.id)
        )
        if self._needs_goal_judge(task):
            self._write_checkpoint(
                run_id,
                task.id,
                stage="gate",
                status=task.status,
                evidence_summary="local gates passed",
                changed_files=files,
                next_action="goal-judge",
            )
            self._enqueue(task, TaskStage.REVIEW, "goal-judge", prior_result=text)
        elif not self._needs_agent_review(task):
            self._write_checkpoint(
                run_id,
                task.id,
                stage="gate",
                status=task.status,
                evidence_summary="local gates passed",
                changed_files=files,
                next_action=self._ship_next_action(),
            )
            self._ship_or_pause(run_id, task.id)
        else:
            self._write_checkpoint(
                run_id,
                task.id,
                stage="gate",
                status=task.status,
                evidence_summary="local gates passed",
                changed_files=files,
                next_action="reviewer",
            )
            self._enqueue(task, TaskStage.REVIEW, "reviewer", prior_result=text)

    def _needs_agent_review(self, task: object) -> bool:
        """Whether this task gets a Task Tool reviewer after green gates."""
        complexity = str(getattr(task, "complexity", "medium") or "medium")
        if complexity.lower().strip() == "trivial":
            return False
        return _complexity_rank(complexity) >= _complexity_rank(
            self.settings.review_min_complexity
        )

    def _needs_goal_judge(self, task: object) -> bool:
        complexity = str(getattr(task, "complexity", "") or "").lower().strip()
        return complexity in {"large", "high"} and self._needs_agent_review(task)

    def _recover_worker_post_gate(self, run_id: str, task: Task, text: str) -> None:
        """Crash recovery: restore checkpoint and create successor only if missing."""
        files = self._checkpoint_files(run_id, task.id)
        if self._needs_goal_judge(task):
            kind = "goal-judge"
            next_action = "goal-judge"
        elif not self._needs_agent_review(task):
            self._write_checkpoint(
                run_id,
                task.id,
                stage="gate",
                status=task.status,
                evidence_summary="local gates passed",
                changed_files=files,
                next_action=self._ship_next_action(),
            )
            self._ship_or_pause(run_id, task.id)
            return
        else:
            kind = "reviewer"
            next_action = "reviewer"
        self._write_checkpoint(
            run_id,
            task.id,
            stage="gate",
            status=task.status,
            evidence_summary="local gates passed",
            changed_files=files,
            next_action=next_action,
        )
        meta = self._read_meta(run_id)
        existing = [
            item
            for item in self.store.list_dispatches(run_id, task_id=task.id)
            if self.run_meta.dispatch_kind(meta, item.dispatch_id) == kind
        ]
        if existing:
            return
        self._enqueue(task, TaskStage.REVIEW, kind, prior_result=text)

    def _ship_next_action(self) -> str:
        if self.settings.ship_mode == "manual":
            return "manual handoff — human commits/merges"
        return "await merge approval"

    def _ship_or_pause(self, run_id: str, task_id: str) -> None:
        """Complete (manual ship) or pause for merge approval depending on ship_mode."""
        if self.settings.ship_mode == "manual":
            self._complete_manual_ship(run_id, task_id)
            return
        self._pause_for_merge(run_id, task_id)

    def _complete_manual_ship(self, run_id: str, task_id: str) -> None:
        task = self._require_task(run_id, task_id)
        if task.status in {
            TaskStatus.REVIEW.value,
            TaskStatus.MERGE_QUEUE.value,
        }:
            allowed, reason = done_allowed(self._run_dir(run_id), task_id=task_id)
            if not allowed:
                self.store.add_event(
                    run_id,
                    EventType.DONE_REFUSED,
                    task_id=task_id,
                    detail={"reason": reason, "path": "complete_manual_ship"},
                )
                # Keep task in REVIEW / MERGE_QUEUE — do not mark DONE.
                self._mark_acceptance_repair(run_id, reason)
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                return
            with suppress(RuntimeError):
                self.store.transition_task(
                    task, TaskStatus.DONE, note="manual ship — human commits/merges"
                )
        elif task.status != TaskStatus.DONE.value:
            return
        self.store.add_event(
            run_id,
            EventType.MERGED,
            task_id=task_id,
            detail={"manual_ship": True, "ship_mode": "manual"},
        )
        done = self._require_task(run_id, task_id)
        if done.status == TaskStatus.DONE.value:
            self._maybe_write_lesson(run_id, done)
            self._maybe_write_journal(run_id, done)
        self._activate_dependents(run_id)
        self._finish_run_if_complete(run_id)

    def _pause_for_merge(self, run_id: str, task_id: str) -> None:
        task = self._require_task(run_id, task_id)
        if task.status == TaskStatus.REVIEW.value:
            self.store.transition_task(task, TaskStatus.MERGE_QUEUE)
        already_waiting = any(
            event.type == EventType.HUMAN_GATE_WAIT.value
            and event.task_id == task_id
            for event in self.store.list_events(run_id)
        )
        if not already_waiting:
            self.store.add_event(
                run_id,
                EventType.HUMAN_GATE_WAIT,
                task_id=task_id,
                detail={"reason": "approve merge"},
            )
        self.store.set_run_status(run_id, RunStatus.PAUSED.value)
        self._set_meta_status(run_id, RunStatus.PAUSED.value)

    def _finish_run_if_complete(self, run_id: str) -> None:
        tasks = self.store.list_tasks(run_id)
        if tasks and all(task.status == TaskStatus.DONE.value for task in tasks):
            cosplay = self._cosplay_snapshot(run_id, tasks)
            if cosplay["cosplay_risk"] and nested_workers_hard_fail():
                self._set_phase(
                    run_id,
                    "stuck",
                    stall_reason=f"cosplay:{cosplay['reason']}",
                    detail="HARNESS_NESTED_WORKERS_HARD_FAIL blocks DONE",
                )
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
                self.store.add_event(
                    run_id,
                    EventType.DONE_REFUSED,
                    detail={
                        "reason": "cosplay_risk",
                        "worker_boundary": cosplay,
                    },
                )
                return
            if cosplay["cosplay_risk"]:

                def _flag_cosplay(meta: dict[str, object]) -> None:
                    meta["cosplay_risk"] = True
                    meta["worker_boundary"] = cosplay

                self._update_meta(run_id, _flag_cosplay)
            self.store.set_run_status(run_id, RunStatus.DONE.value)
            self._set_meta_status(run_id, RunStatus.DONE.value)
            self._maybe_write_run_journal(run_id)
            self._release_active(run_id)

    def _has_live_worker(
        self, run_id: str, task_id: str, *, ignore_dispatch: str = ""
    ) -> bool:
        """Live = unapplied worker or sub-orchestrator dispatch for the task.

        ``ignore_dispatch`` excludes the dispatch currently being consumed so a
        retry queued from inside its own consumption is not self-blocked.
        """
        meta = self._read_meta(run_id)
        return any(
            item.dispatch_id != ignore_dispatch
            and self.run_meta.dispatch_kind(meta, item.dispatch_id)
            in {"worker", "sub-orchestrator", "sprint-contract"}
            and item.status
            in {
                DispatchStatus.PENDING,
                DispatchStatus.CLAIMED,
                DispatchStatus.RUNNING,
                DispatchStatus.SUCCEEDED,
            }
            and not self._is_dispatch_applied(item.dispatch_id, meta)
            for item in self.store.list_dispatches(run_id, task_id=task_id)
        )

    def _active_counters(self) -> dict[str, int]:
        active = [
            item for item in self.store.list_dispatches() if item.status in _ACTIVE_DISPATCH
        ]
        return {
            "jobs": len(active),
            "slots": len(active),
            "heavy": sum(item.resource_class.value == "heavy" for item in active),
            "gates": sum(item.resource_class.value == "gate" for item in active),
        }

    def _capture_snapshot(self) -> ResourceSnapshot:
        if isinstance(self._snapshot, ResourceSnapshot):
            return self._snapshot
        if self._snapshot is not None:
            return self._snapshot()
        return ResourceSnapshot.capture()

    def _validate_base_branch(self, expected: str) -> None:
        if not _branch_exists(self.repo_root, expected):
            raise ValueError(f"base branch does not exist: {expected!r}")

    async def _run_local_gate(
        self,
        run_id: str,
        task_id: str,
        *,
        full: bool,
    ) -> object | None:
        lock_dir = self.settings.root / ".harness"
        lock_dir.mkdir(parents=True, exist_ok=True)
        # Slot 0 keeps the historical lock name; extra slots allow overlapping
        # scoped gates from independent tasks (max_gates > 1 on big hosts).
        # Full-suite gates and in-place shared checkouts share one working
        # directory, so they stay serialized regardless of max_gates.
        slots = (
            max(1, self.resource_policy.max_gates)
            if self.settings.use_worktrees and not full
            else 1
        )
        lock: Path | None = None
        descriptor: int | None = None
        for slot in range(slots):
            candidate = lock_dir / (
                "tasktool-gate.lock" if slot == 0 else f"tasktool-gate-{slot}.lock"
            )
            descriptor = _claim_gate_lock(candidate, run_id, task_id)
            if descriptor is not None:
                lock = candidate
                break
        if descriptor is None or lock is None:
            self.store.add_event(
                run_id,
                EventType.RESOURCE_THROTTLED,
                task_id=task_id,
                detail={"reason": "all deterministic gate slots are active"},
            )
            return None
        counters = self._active_counters()
        decision = self.resource_policy.decide(
            snapshot=self._capture_snapshot(),
            tasktool_jobs=counters["jobs"],
            agent_slots=counters["slots"],
            heavy_jobs=counters["heavy"],
            gates=0,
            requested_class=ResourceClass.GATE,
        )
        if decision.disposition is not ResourceDisposition.ALLOW:
            os.close(descriptor)
            lock.unlink(missing_ok=True)
            event = (
                EventType.RESOURCE_PAUSED
                if decision.disposition is ResourceDisposition.PAUSE
                else EventType.RESOURCE_THROTTLED
            )
            self.store.add_event(
                run_id,
                event,
                task_id=task_id,
                detail={"reason": decision.reason, "full": full},
            )
            if decision.disposition is ResourceDisposition.PAUSE:
                self.store.set_run_status(run_id, RunStatus.PAUSED.value)
                self._set_meta_status(run_id, RunStatus.PAUSED.value)
            return None
        try:
            return await _maybe_await(
                self.lifecycle.run_gates(run_id, task_id, full=full)
            )
        finally:
            os.close(descriptor)
            lock.unlink(missing_ok=True)

    def _activate_dependents(self, run_id: str) -> None:
        tasks = self.store.list_tasks(run_id)
        unlock = (self.settings.dag_unlock or "done").strip().lower()
        if unlock == "post_gate":
            satisfied = {
                task.id
                for task in tasks
                if task.status in _POST_GATE_UNLOCK_STATUSES
            }
        else:
            satisfied = {
                task.id for task in tasks if task.status == TaskStatus.DONE.value
            }
        for task in tasks:
            if task.status != TaskStatus.PENDING.value:
                continue
            if all(dependency in satisfied for dependency in task.depends_on):
                self.store.transition_task(task, TaskStatus.READY)

    def _refuse_other_active_run(
        self,
        *,
        project: str,
        goal: str,
        base_branch: str,
    ) -> None:
        # Multi-tenant: concurrent runs allowed (lease conflict policy is separate).
        if self.settings.multi_tenant:
            return
        active = self._root / ".active"
        if not active.exists():
            return
        run_id = active.read_text(encoding="utf-8").strip()
        run = self.store.get_run(run_id)
        if run is not None and run.status not in _TERMINAL_RUN:
            if (
                run.project == project
                and run.goal == goal
                and run.base_branch == base_branch
            ):
                return
            raise RuntimeError(f"repository already has active TaskTool run {run_id}")
        active.unlink(missing_ok=True)

    def _acquire_active(
        self,
        run_id: str,
        *,
        allow_same: bool = False,
        goal: str = "",
        project: str = "",
    ) -> None:
        if self.settings.multi_tenant:
            lease = acquire_lease(
                self.repo_root,
                run_id,
                user=getattr(self, "_tenant_user", None) or self.settings.tenant_id,
                goal=goal,
                project=project,
                use_worktrees=self.settings.use_worktrees,
                allow_same=allow_same,
            )
            self.store.add_event(
                run_id,
                EventType.TENANT_LEASE_ACQUIRED,
                detail={"user": lease.user, "worktrees": lease.use_worktrees},
            )
            # Also write run-local .active for discoverability.
            paths = resolve_run_root(
                self.repo_root, run_id, prefer_modern=self.settings.use_run_roots
            )
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.active_lock.write_text(run_id + "\n", encoding="utf-8")
            return
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / ".active"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            current = path.read_text(encoding="utf-8").strip()
            if allow_same and current == run_id:
                return
            raise RuntimeError(f"repository already has active TaskTool run {current}") from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(run_id + "\n")

    def _release_active(self, run_id: str) -> None:
        if self.settings.multi_tenant:
            if release_lease(self.repo_root, run_id):
                self.store.add_event(
                    run_id, EventType.TENANT_LEASE_RELEASED, detail={}
                )
            paths = resolve_run_root(
                self.repo_root, run_id, prefer_modern=self.settings.use_run_roots
            )
            if paths.active_lock.exists():
                current = paths.active_lock.read_text(encoding="utf-8").strip()
                if current == run_id:
                    paths.active_lock.unlink(missing_ok=True)
            return
        path = self._root / ".active"
        if path.exists() and path.read_text(encoding="utf-8").strip() == run_id:
            path.unlink()

    # ── Run metadata (controller.json) — see harness/tasktool/run_meta.py ────
    # These stay as thin delegators: they are called from ~40 places inside this
    # class and from tests, and the point of the split is the seam, not churn.

    def _run_dir(self, run_id: str) -> Path:
        return self.run_meta.run_dir(run_id)

    def _meta_path(self, run_id: str) -> Path:
        return self.run_meta.path(run_id)

    def _meta_lock(self, run_id: str) -> AbstractContextManager[None]:
        return self.run_meta.lock(run_id)

    def _update_meta(
        self,
        run_id: str,
        mutate: Callable[[dict[str, object]], None],
        *,
        missing_ok: bool = True,
        skip_if_missing: bool = False,
    ) -> dict[str, object]:
        return self.run_meta.update(
            run_id, mutate, missing_ok=missing_ok, skip_if_missing=skip_if_missing
        )

    def _read_meta(self, run_id: str, *, missing_ok: bool = False) -> dict[str, object]:
        return self.run_meta.read(run_id, missing_ok=missing_ok)

    def _write_meta(self, run_id: str, meta: dict[str, object]) -> None:
        self.run_meta.write(run_id, meta)

    def _set_meta_status(self, run_id: str, status: str) -> None:
        self.run_meta.set_status(run_id, status)

    def _set_phase(
        self,
        run_id: str,
        phase: str,
        *,
        stall_reason: str = "none",
        detail: str = "",
    ) -> None:
        previous = self.run_meta.set_phase_fields(
            run_id, phase, stall_reason=stall_reason, detail=detail
        )
        if previous != phase:
            self.store.add_event(
                run_id,
                EventType.PHASE_CHANGED,
                detail={
                    "from": previous or None,
                    "to": phase,
                    "stall_reason": stall_reason,
                    "detail": detail,
                },
            )

    def _mark_acceptance_repair(self, run_id: str, reason: str) -> None:
        """Persist the repair flag, then move the run to a visible stuck phase."""
        self.run_meta.mark_acceptance_repair(run_id, reason)
        self._set_phase(
            run_id,
            "stuck",
            stall_reason=f"acceptance_repair:{reason}",
            detail="flip/waive acceptance before re-dispatching workers",
        )

    def _cosplay_snapshot(
        self, run_id: str, tasks: list[Task] | None = None
    ) -> dict[str, object]:
        """Detect single-agent orch==worker cosplay on implement dispatches."""
        task_list = tasks if tasks is not None else self.store.list_tasks(run_id)
        medium_plus = [
            t
            for t in task_list
            if _complexity_rank(t.complexity) >= _complexity_rank("medium")
        ]
        meta = self._read_meta(run_id, missing_ok=True)
        agents_map = dict(_dict(meta.get("dispatch_agents")))
        worker_ids: list[str] = []
        worker_task_ids: list[str] = []
        for dispatch in self.store.list_dispatches(run_id):
            kind = self.run_meta.dispatch_kind(meta, dispatch.dispatch_id)
            if kind != "worker":
                continue
            if dispatch.status is not DispatchStatus.SUCCEEDED:
                continue
            info = dict(_dict(agents_map.get(dispatch.dispatch_id)))
            wid = str(info.get("worker_id") or info.get("agent_id") or "")
            if not wid:
                # Fallback: result JSON identity fields.
                with suppress(OSError, ValueError, KeyError, json.JSONDecodeError):
                    path = self._require_spool_path(
                        run_id, Path(dispatch.result_path)
                    )
                    if path.is_file():
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        wid = str(
                            payload.get("worker_id")
                            or payload.get("agent_id")
                            or ""
                        )
            if wid:
                worker_ids.append(wid)
                worker_task_ids.append(dispatch.task_id)
        distinct = sorted(set(worker_ids))
        required = nested_workers_required() and len(medium_plus) >= 1
        cosplay = False
        reason = "ok"
        if required and len(worker_task_ids) >= 2 and len(distinct) <= 1:
            cosplay = True
            reason = (
                "same_agent_reported_all_workers"
                if distinct
                else "missing_distinct_worker_ids"
            )
        elif required and len(worker_task_ids) >= 1 and not distinct:
            cosplay = True
            reason = "missing_worker_id_on_reports"
        return {
            "cosplay_risk": cosplay,
            "require_nested_workers": required,
            "hard_fail": nested_workers_hard_fail(),
            "distinct_worker_ids": distinct,
            "worker_report_count": len(worker_ids),
            "medium_plus_tasks": len(medium_plus),
            "reason": reason,
        }

    def _precall_check(self, run_id: str, run: Run) -> PrecallHit | None:
        meta = self._read_meta(run_id, missing_ok=True)
        limits = PrecallLimits(
            max_steps=self.settings.max_steps,
            max_wall_clock_sec=self.settings.max_wall_clock_sec,
            max_tokens_est=self.settings.max_tokens_est,
            max_tool_hash_repeat=self.settings.tool_hash_repeat,
            run_budget_credits=run.budget_credits,
        )
        tokens_est = 0
        for task in self.store.list_tasks(run_id):
            for attempt in self.store.list_attempts(run_id, task.id):
                tokens_est += int(getattr(attempt, "tokens_in", 0) or 0)
                tokens_est += int(getattr(attempt, "tokens_out", 0) or 0)
        # Fallback: sum from events when attempt rows lack tokens.
        if tokens_est == 0:
            for event in self.store.list_events(run_id)[-200:]:
                detail = event.detail if isinstance(event.detail, dict) else {}
                tokens_est += int(detail.get("tokens_in") or 0)
                tokens_est += int(detail.get("tokens_out") or 0)
                tokens_est += int(detail.get("tokens") or 0)
        governor = PrecallGovernor(limits)
        return governor.check(
            steps=int(meta.get("steps") or 0),
            started_at=str(run.created_at or meta.get("created_at") or ""),
            tokens_est=tokens_est,
            spent_credits=float(run.spent_credits or 0.0),
        )

    def _refuse_precall(self, run_id: str, hit: PrecallHit) -> dict[str, object]:
        self.store.add_event(
            run_id,
            EventType.BUDGET_PREDICATE_HIT,
            detail=hit.to_dict(),
        )
        if hit.predicate == "tool_hash_repeat":
            self.store.add_event(
                run_id,
                EventType.LOOP_STUCK,
                detail=hit.detail or {},
            )
        self.store.set_run_status(run_id, RunStatus.PAUSED.value)
        self._set_meta_status(run_id, RunStatus.PAUSED.value)
        self._set_phase(
            run_id,
            hit.phase,
            stall_reason=(
                f"budget:{hit.predicate}"
                if hit.phase == "budget_exhausted"
                else f"stuck:{hit.predicate}"
            ),
            detail=hit.reason,
        )
        if hit.predicate in {"max_steps", "max_wall_clock", "max_tokens_est", "max_credits"}:
            self.store.add_event(
                run_id,
                EventType.BUDGET_EXCEEDED,
                detail=hit.to_dict(),
            )
        return {
            "ok": False,
            "run_id": run_id,
            "status": hit.phase,
            "phase": hit.phase,
            "stall_reason": (
                f"budget:{hit.predicate}"
                if hit.phase == "budget_exhausted"
                else f"stuck:{hit.predicate}"
            ),
            "error": hit.reason,
            "predicate": hit.predicate,
            "dispatches": [],
        }

    def _maybe_detect_loop(
        self, run_id: str, task_id: str, payload: dict[str, object]
    ) -> None:
        if not self.settings.loop_detect:
            return
        fps = fingerprints_from_result_payload(payload)
        stuck = loop_stuck_from_fingerprints(
            fps, threshold=self.settings.tool_hash_repeat
        )
        if not stuck:
            return
        fp, _same, count = stuck[0]
        self.store.add_event(
            run_id,
            EventType.LOOP_STUCK,
            task_id=task_id,
            detail={"tool_hash": fp, "count": count},
        )
        hit = PrecallHit(
            predicate="tool_hash_repeat",
            reason=f"tool_hash {fp} repeated {count}x",
            phase="stuck",
            detail={"tool_hash": fp, "count": count},
        )
        self._refuse_precall(run_id, hit)

    def _phase_snapshot(self, run_id: str):
        run = self._require_run(run_id)
        tasks = self.store.list_tasks(run_id)
        meta = self._read_meta(run_id, missing_ok=True)
        active_kinds: list[str] = []
        for item in self.store.list_dispatches(run_id):
            if item.status in _ACTIVE_DISPATCH or item.status is DispatchStatus.PENDING:
                kind = self.run_meta.dispatch_kind(meta, item.dispatch_id)
                if kind:
                    active_kinds.append(kind)
        recent = [
            event.type
            for event in self.store.list_events(run_id)[-40:]
        ]
        return derive_phase(
            run_status=run.status,
            task_statuses=[t.status for t in tasks],
            active_kinds=active_kinds,
            recent_event_types=recent,
            stall_override=str(meta.get("stall_reason") or "") or None,
            phase_override=str(meta.get("phase") or "") or None,
        )

    def _write_checkpoint(
        self,
        run_id: str,
        task_id: str,
        *,
        stage: str,
        status: str,
        evidence_summary: str = "",
        changed_files: tuple[str, ...] | list[str] = (),
        next_action: str = "",
    ) -> None:
        self._checkpoints.write(
            run_id,
            task_id,
            stage=stage,
            status=status,
            evidence_summary=evidence_summary,
            changed_files=changed_files,
            next_action=next_action,
        )

    def _checkpoint_files(self, run_id: str, task_id: str) -> tuple[str, ...]:
        checkpoint = self._checkpoints.read(run_id, task_id)
        return checkpoint.changed_files if checkpoint is not None else ()

    def _diff_path(self, run_id: str, task_id: str) -> Path:
        return self._run_dir(run_id) / "diffs" / f"{task_id}.patch"

    def _write_task_diff(self, run_id: str, task_id: str, diff: str) -> None:
        """Persist the finalize-time diff so later dispatches review the same code."""
        if not diff.strip():
            return
        self._atomic_write(
            self._require_spool_path(run_id, self._diff_path(run_id, task_id)), diff
        )

    def _read_task_diff(self, run_id: str, task_id: str) -> str:
        path = self._diff_path(run_id, task_id)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _maybe_write_lesson(self, run_id: str, task: Task) -> None:
        """Best-effort compact lesson via harness.lessons; never fails the run.

        Phase 3: write only to agent layer (``HARNESS_BRAIN_ROOT``, default
        ``/home/brain-agents``). Never auto-write into human canon ``/home/brain``.
        """
        brain = resolve_brain_root()
        if brain is None:
            return
        try:
            resolved = brain.resolve()
            canon = Path(
                self.settings.brain_canon or os.environ.get("HARNESS_BRAIN_CANON", "/home/brain")
            ).expanduser().resolve()
            if resolved == canon:
                self.store.add_event(
                    run_id,
                    EventType.ERROR,
                    task_id=task.id,
                    detail={
                        "where": "lesson extraction",
                        "error": "refusing write to human brain canon; set HARNESS_BRAIN_ROOT=/home/brain-agents",
                    },
                )
                return
            if not os.access(brain, os.W_OK):
                return
            from harness.lessons.extractor import write_lesson_file  # noqa: PLC0415

            run = self._require_run(run_id)
            checkpoint = self._checkpoints.read(run_id, task.id)
            evidence = checkpoint.evidence_summary if checkpoint is not None else ""
            files = (
                ", ".join(checkpoint.changed_files[:20])
                if checkpoint is not None and checkpoint.changed_files
                else "(none)"
            )
            content = (
                f"# Lesson: {task.title}\n\n"
                f"- task: `{task.id}`\n"
                f"- complexity: `{task.complexity}`\n"
                f"- outcome: DONE\n"
                f"- evidence: {evidence or 'merged'}\n"
                f"- changed_files: {files}\n"
            )
            path = write_lesson_file(brain, run.project, task.id, content)
            self.store.add_event(
                run_id,
                EventType.LESSON_WRITTEN,
                task_id=task.id,
                detail={"lesson": "extracted", "path": str(path), "layer": "brain-agents"},
            )
        except Exception as exc:  # noqa: BLE001 — lessons must not fail the run
            self.store.add_event(
                run_id,
                EventType.ERROR,
                task_id=task.id,
                detail={"where": "lesson extraction", "error": str(exc)[:200]},
            )

    def _maybe_write_journal(self, run_id: str, task: Task) -> None:
        """ADR-0013: per-project history entry on task DONE; never fails the run.

        Idempotent across crash replays — one JOURNAL_WRITTEN event per task.
        """
        if not self.settings.project_journal_enabled:
            return
        if any(
            event.type == EventType.JOURNAL_WRITTEN.value and event.task_id == task.id
            for event in self.store.list_events(run_id)
        ):
            return
        try:
            run = self._require_run(run_id)
            entry = JournalEntry(
                title=f"{task.id} — {task.title}",
                source="harness-task",
                project=run.project,
                run_id=run_id,
                task_id=task.id,
                decisions=tuple(self._journal_decisions(run_id, task)),
                done=tuple(self._journal_done(run_id, task)),
                iterations=tuple(self._journal_iterations(run_id, task)),
            )
            path = write_entry(
                self.repo_root, entry, rel_dir=self.settings.journal_dir
            )
            self.store.add_event(
                run_id,
                EventType.JOURNAL_WRITTEN,
                task_id=task.id,
                detail={"path": str(path), "kind": "task"},
            )
        except Exception as exc:  # noqa: BLE001 — журнал не должен ронять run
            self.store.add_event(
                run_id,
                EventType.ERROR,
                task_id=task.id,
                detail={"where": "project journal", "error": str(exc)[:200]},
            )

    def _journal_decisions(self, run_id: str, task: Task) -> list[str]:
        decisions: list[str] = []
        expansions = ExpansionStore(self._run_dir(run_id))
        own = expansions.read(task.id)
        if own is not None:
            children = ", ".join(child.task_id for child in own.children)
            reason = f": {own.reason}" if own.reason else ""
            decisions.append(
                f"декомпозирована на {len(own.children)} подзадач ({children}){reason}"
            )
        else:
            with suppress(OSError, ContractError, ValueError):
                for artifact in expansions.read_all():
                    if any(child.task_id == task.id for child in artifact.children):
                        decisions.append(
                            f"подзадача декомпозиции {artifact.parent_task_id}"
                        )
                        break
        events = [
            event
            for event in self.store.list_events(run_id)
            if event.task_id == task.id
        ]
        verdicts = [
            str(event.detail.get("verdict") or "")
            for event in events
            if event.type == EventType.REVIEW_RESULT.value
        ]
        if verdicts:
            decisions.append("review: " + " → ".join(v for v in verdicts if v))
        for event in events:
            if event.type != EventType.INPUT_REQUESTED.value:
                continue
            questions = event.detail.get("questions")
            if isinstance(questions, list) and questions:
                first = questions[0]
                text = str(first.get("question") or "") if isinstance(first, dict) else ""
                if text:
                    decisions.append(f"уточнение у пользователя: {text[:200]}")
        return decisions

    def _journal_done(self, run_id: str, task: Task) -> list[str]:
        done: list[str] = []
        checkpoint = self._checkpoints.read(run_id, task.id)
        if checkpoint is not None:
            if checkpoint.changed_files:
                done.append("файлы: " + ", ".join(checkpoint.changed_files[:20]))
            if checkpoint.evidence_summary:
                done.append(checkpoint.evidence_summary)
        if task.provides:
            done.append("Provides: " + " ".join(task.provides.split())[:400])
        if not done:
            done.append("задача завершена (детали в event-log run'а)")
        return done

    def _journal_iterations(self, run_id: str, task: Task) -> list[str]:
        iterations = [f"попыток: {max(task.attempts, 0) + 1}"]
        retry_notes = [
            str(event.detail.get("note") or "")
            for event in self.store.list_events(run_id)
            if event.type == EventType.TASK_TRANSITION.value
            and event.task_id == task.id
            and str(event.detail.get("to")) == TaskStatus.READY.value
            and event.detail.get("note")
        ]
        for note in retry_notes[:4]:
            iterations.append(f"доработка: {note[:200]}")
        return iterations

    def _maybe_write_run_journal(self, run_id: str) -> None:
        """Run-level summary entry when the whole run flips to DONE."""
        if not self.settings.project_journal_enabled:
            return
        if any(
            event.type == EventType.JOURNAL_WRITTEN.value
            and event.detail.get("kind") == "run"
            for event in self.store.list_events(run_id)
        ):
            return
        try:
            run = self._require_run(run_id)
            tasks = self.store.list_tasks(run_id)
            done_lines = [
                f"{task.id} — {task.title} (попыток: {max(task.attempts, 0) + 1})"
                for task in tasks
            ]
            entry = JournalEntry(
                title=f"Run завершён: {run.goal}",
                source="harness-run",
                project=run.project,
                run_id=run_id,
                decisions=(f"задач выполнено: {len(tasks)}",),
                done=tuple(done_lines),
            )
            path = write_entry(
                self.repo_root, entry, rel_dir=self.settings.journal_dir
            )
            self.store.add_event(
                run_id,
                EventType.JOURNAL_WRITTEN,
                detail={"path": str(path), "kind": "run"},
            )
        except Exception as exc:  # noqa: BLE001 — журнал не должен ронять run
            self.store.add_event(
                run_id,
                EventType.ERROR,
                detail={"where": "run journal", "error": str(exc)[:200]},
            )

    def _is_dispatch_applied(self, dispatch_id: str, meta: dict[str, object]) -> bool:
        application = self.store.get_dispatch_application(dispatch_id)
        if application is not None:
            return application.status == APPLICATION_APPLIED
        return dispatch_id in set(_string_list(meta.get("consumed")))

    def _mark_dispatch_applied(
        self,
        run_id: str,
        dispatch: DispatchEnvelope,
        meta: dict[str, object],
    ) -> None:
        self.store.begin_dispatch_application(
            dispatch_id=dispatch.dispatch_id,
            run_id=run_id,
            task_id=dispatch.task_id,
            stage=dispatch.stage.value,
        )
        self.store.complete_dispatch_application(dispatch.dispatch_id)
        def _mark_consumed(meta: dict[str, object]) -> None:
            consumed = set(_string_list(meta.get("consumed")))
            consumed.add(dispatch.dispatch_id)
            meta["consumed"] = sorted(consumed)

        self._update_meta(run_id, _mark_consumed, missing_ok=False)

    def _load_plan(self, run_id: str) -> PlanArtifact:
        path = self._run_dir(run_id) / "plan.json"
        return PlanArtifact.from_json(path.read_text(encoding="utf-8"))

    def _plan_task(self, run_id: str, task_id: str) -> PlanTask | None:
        try:
            plan = self._load_plan(run_id)
        except (OSError, ContractError, ValueError):
            return None
        return self._plan_task_anywhere(run_id, task_id, plan)

    def _plan_task_anywhere(
        self, run_id: str, task_id: str, plan: PlanArtifact
    ) -> PlanTask | None:
        """Frozen task lookup: root PlanArtifact first, then ExpansionArtifacts."""
        direct = next((item for item in plan.tasks if item.task_id == task_id), None)
        if direct is not None:
            return direct
        try:
            for artifact in ExpansionStore(self._run_dir(run_id)).read_all():
                for child in artifact.children:
                    if child.task_id == task_id:
                        return child
        except (OSError, ContractError, ValueError):
            return None
        return None

    def _project_context(self, run: Run) -> tuple[ProjectContext, Path] | None:
        """Best-effort cached project context; never blocks a dispatch."""
        cached = self._project_ctx
        if cached is not None and cached[0].project == run.project:
            return cached
        try:
            built = load_or_build(
                self.repo_root,
                run.project,
                brain_root=resolve_brain_root(),
            )
        except (OSError, ValueError, RuntimeError):
            return None
        self._project_ctx = built
        return built

    def _project_brief(self, run: Run) -> str:
        context = self._project_context(run)
        return context[0].brief() if context is not None else ""

    def _project_context_path(self, run: Run) -> Path | None:
        context = self._project_context(run)
        return context[1] if context is not None else None

    def _verify_plan_hash(self, run_id: str, plan: PlanArtifact) -> None:
        """Recompute canonical hash with plan_hash blank; never trust the field."""
        meta = self._read_meta(run_id)
        expected = str(meta.get("plan_hash") or "")
        if not expected:
            return
        unsigned = PlanArtifact(
            run_id=plan.run_id,
            goal=plan.goal,
            project=plan.project,
            repo=plan.repo,
            base_branch=plan.base_branch,
            spec_sources=plan.spec_sources,
            tasks=plan.tasks,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
            protocol_version=plan.protocol_version,
            plan_hash="",
        )
        digest = hashlib.sha256(unsigned.to_json().encode("utf-8")).hexdigest()
        if digest != expected:
            raise RuntimeError(f"plan hash mismatch for run {run_id}")

    def _assert_frozen_plan_intact(self, run_id: str) -> PlanArtifact:
        """Load plan.json and canonically verify plan hash + per-task spec hashes."""
        plan = self._load_plan(run_id)
        self._verify_plan_hash(run_id, plan)
        meta = self._read_meta(run_id)
        frozen_specs = _dict(meta.get("frozen_specs"))
        for task in plan.tasks:
            frozen_path = Path(task.frozen_spec_path or "")
            if not frozen_path.is_file():
                raise RuntimeError(
                    f"frozen spec missing for task {task.task_id}: {frozen_path}"
                )
            digest = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
            if task.spec_sha256 and digest != task.spec_sha256:
                raise RuntimeError(f"frozen spec hash mismatch for task {task.task_id}")
            meta_entry = _dict(frozen_specs.get(task.task_id))
            expected = str(meta_entry.get("sha256") or "")
            if expected and digest != expected:
                raise RuntimeError(
                    f"frozen spec hash mismatch for task {task.task_id}"
                )
        for parent_id, raw_entry in _dict(meta.get("expansions")).items():
            entry = _dict(raw_entry)
            path = Path(str(entry.get("path") or ""))
            if not path.is_file():
                raise RuntimeError(f"expansion artifact missing for task {parent_id}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = str(entry.get("sha256") or "")
            if expected and digest != expected:
                raise RuntimeError(
                    f"expansion artifact hash mismatch for task {parent_id}"
                )
        return plan

    def _frozen_plan_error(self, run_id: str) -> str | None:
        try:
            self._assert_frozen_plan_intact(run_id)
        except (OSError, ContractError, ValueError, RuntimeError) as exc:
            return str(exc)
        return None

    def _assert_owned_paths_safe(
        self, files_owned: Sequence[str], *, task_id: str
    ) -> None:
        for pattern in files_owned:
            # Glob meta-roots and empty segments already rejected by normalize.
            if pattern in {"*", "**", "**/*"}:
                raise ValueError(f"task {task_id}: owned path is too broad: {pattern!r}")
            concrete = pattern.split("*", 1)[0].rstrip("/")
            if not concrete:
                continue
            candidate = self.repo_root / concrete
            if candidate.is_symlink():
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(self.repo_root)
                except ValueError as exc:
                    raise ValueError(
                        f"task {task_id}: owned path symlink escapes repo: {pattern}"
                    ) from exc
            elif candidate.exists():
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(self.repo_root.resolve())
                except ValueError as exc:
                    raise ValueError(
                        f"task {task_id}: owned path resolves outside repo: {pattern}"
                    ) from exc

    def _require_spool_path(self, run_id: str, path: Path) -> Path:
        spool = self._run_dir(run_id).resolve()
        resolved = path if path.is_absolute() else (self.repo_root / path)
        try:
            resolved = resolved.resolve()
            resolved.relative_to(spool)
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"path must remain under .harness/runs/{run_id}: {path}"
            ) from exc
        return resolved

    def _pending_dir(self, run_id: str) -> Path:
        path = self._run_dir(run_id) / "pending"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persist_questions(self, run_id: str, task_id: str, questions: object) -> None:
        payload = {
            "task_id": task_id,
            "questions": [
                {
                    "id": item.id,
                    "question": item.question,
                    "options": item.options,
                    "why": item.why,
                }
                for item in getattr(questions, "questions", ())
            ],
        }
        self._atomic_write(
            self._pending_dir(run_id) / "questions.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _pending_questions(self, run_id: str) -> bool:
        path = self._run_dir(run_id) / "pending" / "questions.json"
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        return bool(data.get("questions"))

    def _answers_present(self, run_id: str) -> bool:
        return bool(self._load_answers(run_id))

    def _load_answers(self, run_id: str) -> dict[str, str]:
        path = self._run_dir(run_id) / "pending" / "answers.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        nested = data.get("answers", data)
        if not isinstance(nested, dict) or not nested:
            return {}
        return {str(key): str(value) for key, value in nested.items() if key}

    def _load_questions(self, run_id: str) -> list[dict[str, object]]:
        path = self._run_dir(run_id) / "pending" / "questions.json"
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []
        questions = data.get("questions")
        if not isinstance(questions, list):
            return []
        return [item for item in questions if isinstance(item, dict)]

    def _questions_task_id(self, run_id: str) -> str:
        path = self._run_dir(run_id) / "pending" / "questions.json"
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("task_id") or "")

    def _clear_questions(self, run_id: str) -> None:
        pending = self._run_dir(run_id) / "pending"
        for name in ("questions.json", "answers.json"):
            path = pending / name
            if path.exists():
                path.unlink()

    def _persist_risk_gate(
        self,
        run_id: str,
        *,
        task_id: str,
        reason: str,
        evidence: str,
        kind: str,
    ) -> None:
        payload = {
            "task_id": task_id,
            "reason": reason,
            "evidence": evidence,
            "kind": kind,
            "approved": False,
        }
        self._atomic_write(
            self._pending_dir(run_id) / "risk_gate.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        def _apply(meta: dict[str, object]) -> None:
            if not meta:
                meta.update({"run_id": run_id, "dispatch_kinds": {}, "consumed": []})
            meta["risk_gate"] = payload

        self._update_meta(run_id, _apply)

    def _risk_gate_pending(self, run_id: str) -> bool:
        path = self._run_dir(run_id) / "pending" / "risk_gate.json"
        if not path.is_file():
            meta = self._read_meta(run_id, missing_ok=True)
            risk = _dict((meta or {}).get("risk_gate"))
            return bool(risk) and not bool(risk.get("approved"))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        return not bool(data.get("approved"))

    def _risk_gate_approved(self, run_id: str) -> bool:
        path = self._run_dir(run_id) / "pending" / "risk_gate.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            return bool(data.get("approved"))
        meta = self._read_meta(run_id, missing_ok=True)
        risk = _dict((meta or {}).get("risk_gate"))
        return bool(risk.get("approved"))

    def _approve_risk_gate(self, run_id: str, meta: dict[str, object]) -> None:
        """Approve only an exact persisted risk gate; never invent a bypass."""
        path = self._pending_dir(run_id) / "risk_gate.json"
        data: dict[str, object] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid risk_gate.json for run {run_id}") from exc
        else:
            data = dict(_dict(meta.get("risk_gate")))
        if not data or not (data.get("task_id") or data.get("reason") or data.get("kind")):
            raise ValueError(f"no persisted risk gate to approve for run {run_id}")
        if bool(data.get("approved")):
            return
        data["approved"] = True
        data["approved_at"] = _now()
        self._atomic_write(
            path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )
        def _apply(refreshed: dict[str, object]) -> None:
            if not refreshed:
                refreshed.update(meta)
            refreshed["risk_gate"] = data

        self._update_meta(run_id, _apply)

    async def _apply_risk_approvals(self, run_id: str) -> None:
        if not self._risk_gate_approved(run_id):
            return
        path = self._run_dir(run_id) / "pending" / "risk_gate.json"
        kind = ""
        task_id = ""
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                kind = str(data.get("kind") or "")
                task_id = str(data.get("task_id") or "")
            except (OSError, json.JSONDecodeError):
                kind = ""
        for task in self.store.list_tasks(run_id):
            if task_id and task.id != task_id:
                continue
            if task.status == TaskStatus.POST_MERGE_FIX.value:
                await _maybe_await(
                    self.lifecycle.prepare_post_merge_repair(run_id, task.id)
                )
            elif kind == "pre_merge_fix" and task.status == TaskStatus.READY.value:
                # Fix worker is enqueued by _enqueue_ready_workers after resume.
                continue
        # Clear the gate after scheduling so subsequent next/advance are unblocked.
        if path.exists():
            path.unlink()
        with self._meta_lock(run_id):
            meta = self._read_meta(run_id, missing_ok=True)
            if meta and "risk_gate" in meta:
                meta.pop("risk_gate", None)
                meta["updated_at"] = _now()
                self._write_meta(run_id, meta)

    def _has_blocking_pause(self, run_id: str) -> bool:
        return (
            self._pending_questions(run_id) and not self._answers_present(run_id)
        ) or (self._risk_gate_pending(run_id) and not self._risk_gate_approved(run_id))

    def _require_run(self, run_id: str) -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        return run

    def _require_task(self, run_id: str, task_id: str) -> Task:
        task = self.store.get_task(run_id, task_id)
        if task is None:
            raise KeyError(f"unknown task: {run_id}/{task_id}")
        return task

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        run_meta_atomic_write(path, text)


def _default_lifecycle(settings: Settings, store: Store, repo_root: Path) -> Lifecycle:
    try:
        from harness.tasktool.lifecycle import TaskLifecycleService  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "TaskLifecycleService is unavailable; inject lifecycle during construction"
        ) from exc
    return TaskLifecycleService(settings, store, repo_root)


_COMPLEXITY_RANK: dict[str, int] = {
    "trivial": 0,
    "small": 1,
    "medium": 2,
    "normal": 2,
    "large": 3,
    "high": 3,
}
# Parent statuses that mean "implements Provides are on disk / gates green".
_POST_GATE_UNLOCK_STATUSES = frozenset(
    {
        TaskStatus.REVIEW.value,
        TaskStatus.MERGE_QUEUE.value,
        TaskStatus.DONE.value,
    }
)


def _complexity_rank(value: str) -> int:
    return _COMPLEXITY_RANK.get(value.lower().strip(), 2)


def _role_for_kind(kind: str) -> Role:
    if kind == "worker":
        return Role.WORKER
    if kind in {"sub-orchestrator", "sprint-contract"}:
        return Role.ORCHESTRATOR
    return Role.REVIEWER


def _clamp_resource_policy(policy: ResourcePolicy) -> ResourcePolicy:
    """Hard-ceiling agent/job slots while preserving lower configured values."""
    return ResourcePolicy(
        max_tasktool_jobs=min(policy.max_tasktool_jobs, _HARD_JOB_CEILING),
        max_agent_slots=min(policy.max_agent_slots, _HARD_JOB_CEILING),
        max_heavy_jobs=policy.max_heavy_jobs,
        max_gates=policy.max_gates,
        soft_mem_available_bytes=policy.soft_mem_available_bytes,
        hard_mem_available_bytes=policy.hard_mem_available_bytes,
        load_per_cpu_threshold=policy.load_per_cpu_threshold,
    )


async def _maybe_await(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_result(path: Path, dispatch_id: str) -> dict[str, object]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid result file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("result file must contain a JSON object")
    result_id = value.get("dispatch_id", value.get("id"))
    if result_id is not None and result_id != dispatch_id:
        raise ValueError(f"result id {result_id!r} does not match {dispatch_id!r}")
    _result_text(value)
    return value


def _result_text(payload: dict[str, object]) -> str:
    for key in ("final_text", "text", "result"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    raise ValueError("result file must contain string final_text")


def _parse_verdict(text: str) -> Verdict | None:
    matches = list(_VERDICT_RE.finditer(text))
    return Verdict(matches[-1].group(1).lower()) if matches else None


def _extract_provides(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lstrip("#").strip().rstrip(":").lower() != "provides":
            continue
        body: list[str] = []
        for raw in lines[index + 1 :]:
            if not raw.strip() or raw.lstrip().startswith("#"):
                break
            body.append(raw.strip())
        return "\n".join(body)
    return ""


def _acceptance(spec_text: str) -> list[str]:
    section = read_section(spec_text, "Acceptance criteria")
    return [
        line.lstrip("-* [xX]").strip(" `")
        for line in section.splitlines()
        if line.strip().startswith(("-", "*"))
    ]


def _finding_dict(finding: object) -> dict[str, object]:
    return {
        "severity": getattr(finding, "severity", ""),
        "task_id": getattr(finding, "task_id", ""),
        "issue": getattr(finding, "issue", ""),
        "detail": getattr(finding, "detail", ""),
    }


def _lifecycle_ok(result: object) -> bool:
    passed = getattr(result, "passed", None)
    if passed is False:
        return False
    violations = getattr(result, "violations", ())
    action = str(getattr(result, "action", "")).lower()
    status = str(getattr(result, "task_status", "")).lower()
    detail = str(getattr(result, "detail", "")).lower()
    blocked_actions = (
        "fail",
        "blocked",
        "changes",
        "violation",
        "error",
        "refused",
        "judge_approve_blocked",
        "done_refused",
    )
    return not violations and not any(
        marker in action or marker in status or marker in detail
        for marker in blocked_actions
    )


def _lifecycle_detail(result: object) -> str:
    return str(getattr(result, "detail", "") or "lifecycle stage requested changes")


def _changed_files(result: object | None) -> tuple[str, ...]:
    if result is None:
        return ()
    raw = getattr(result, "changed_files", ()) or ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    return tuple(str(item) for item in raw if str(item).strip())


def _lifecycle_diff(result: object | None) -> str:
    if result is None:
        return ""
    return str(getattr(result, "diff", "") or "")


def _compact_result(text: str, limit: int = 500) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _git_dir(repo_root: Path) -> Path:
    git = repo_root / ".git"
    if git.is_file():
        marker = git.read_text(encoding="utf-8").strip()
        if marker.startswith("gitdir:"):
            target = Path(marker.partition(":")[2].strip())
            git = target if target.is_absolute() else (repo_root / target).resolve()
    return git


def _branch_exists(repo_root: Path, branch: str) -> bool:
    git = _git_dir(repo_root)
    if (git / "refs" / "heads" / branch).is_file():
        return True
    try:
        packed = (git / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return False
    suffix = f" refs/heads/{branch}"
    return any(line.endswith(suffix) for line in packed.splitlines())


def _claim_gate_lock(lock: Path, run_id: str, task_id: str) -> int | None:
    for _ in range(2):
        try:
            descriptor = os.open(
                lock,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            if _gate_lock_owner_alive(lock):
                return None
            lock.unlink(missing_ok=True)
            continue
        os.write(descriptor, f"{os.getpid()}:{run_id}:{task_id}\n".encode())
        return descriptor
    return None


def _gate_lock_owner_alive(lock: Path) -> bool:
    try:
        raw_pid = lock.read_text(encoding="utf-8").partition(":")[0]
        pid = int(raw_pid)
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _string_list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
