"""File-backed prompts for TaskTool pull agents."""

from __future__ import annotations

from collections.abc import Sequence

from harness.domain.enums import ResourceClass
from harness.domain.models import Run, Task
from harness.tasktool.context import ContextPack
from harness.tasktool.models import TaskStage
from harness.tasktool.review_bundle import ReviewBundle

_REPORT_FORMAT = (
    "changed files; evidence; gates; provides; deviations/questions/risks"
)

# The frozen spec is the contract the worker is graded against, so clipping it
# hides acceptance criteria the agent is still held to. Prior-stage text matters
# most on retries, where the previous failure explains what to do differently.
_SPEC_LIMIT = 12_000
_PRIOR_RESULT_LIMIT = 8_000
_JUDGE_EVIDENCE_LIMIT = 8_000


def build_prompt(
    run: Run,
    task: Task,
    stage: TaskStage,
    *,
    spec_text: str,
    prior_result: str = "",
    kind: str = "",
    worktree: str = "",
    files_owned: Sequence[str] = (),
    read_only_paths: Sequence[str] = (),
    frozen_contracts: Sequence[str] = (),
    acceptance: Sequence[str] = (),
    resource_class: ResourceClass | str = ResourceClass.STANDARD,
    canon_pointer: str = "",
    context_pack: ContextPack | None = None,
    review_bundle: ReviewBundle | None = None,
    skill_paths: Sequence[str] = (),
) -> str:
    """Build a compact prompt with the exact Task Brief sections from harness-orchestrate."""
    role_kind = kind or _default_kind(stage)
    absolute_worktree = worktree or task.worktree_path or ""
    owned = ", ".join(files_owned) if files_owned else "(none)"
    read_only = ", ".join(read_only_paths) if read_only_paths else "(none)"
    contracts = (
        " | ".join(item.strip() for item in frozen_contracts if item.strip())
        or "TaskTool protocol 3.0"
    )
    acceptance_text = (
        "; ".join(item.strip() for item in acceptance if item.strip()) or "(none)"
    )
    resource = (
        resource_class.value
        if isinstance(resource_class, ResourceClass)
        else str(resource_class or ResourceClass.STANDARD.value)
    )
    pointer = canon_pointer or (
        context_pack.canon_pointer if context_pack is not None else "canon:unknown"
    )
    skills = tuple(p.strip() for p in skill_paths if p and str(p).strip())[:3]
    skills_line = ", ".join(skills) if skills else "(none — follow repo AGENTS.md)"

    brief = (
        "Task Brief\n"
        f"Worktree absolute path: {absolute_worktree}\n"
        f"Owned files only: {owned}\n"
        f"Read-only paths: {read_only}\n"
        f"Frozen contracts (verbatim): {contracts}\n"
        f"Acceptance: {acceptance_text}\n"
        f"Resource class: {resource}\n"
        f"Relevant canon pointer: {pointer}\n"
        f"Skills to apply (read SKILL.md, max 3): {skills_line}\n"
        f"Report format: {_REPORT_FORMAT}\n"
    )

    header = (
        f"# TaskTool {stage.value} ({role_kind})\n\n"
        f"Run: `{run.id}`\nTask: `{task.id}` — {task.title}\n"
        f"Repository goal: {run.goal}\nBase branch: `{run.base_branch}`\n\n"
        f"{brief}\n"
    )

    compact_spec = _compact_spec(spec_text)
    body = f"## Frozen task specification\n\n{compact_spec}\n"

    context_block = ""
    if context_pack is not None and context_pack.rendered.strip():
        context_block = f"\n{context_pack.rendered}"

    skills_block = ""
    if skills:
        listed = "\n".join(f"- `{path}`" for path in skills)
        skills_block = (
            "\n## Injected skills (progressive disclosure)\n"
            "Read each SKILL.md before acting. Do not load additional skills mid-task.\n"
            f"{listed}\n"
        )

    prior = ""
    if prior_result.strip() and role_kind != "reviewer":
        prior = f"\n## Prior stage result\n\n{_clip(prior_result.strip(), _PRIOR_RESULT_LIMIT)}\n"

    if role_kind == "sub-orchestrator" or stage is TaskStage.PLAN:
        instruction = (
            "\n## Assignment (sub-orchestrator, read-only)\n"
            "You are the decomposition agent for this single task. Study the frozen "
            "spec, the project context file from Read-only paths, and the repository "
            "(`rg`, `graphify query` when a graph exists). Do NOT edit any files and "
            "do NOT start other agents.\n\n"
            "Decide: fan the task out into 2+ parallelizable subtasks, or implement "
            "directly. Reply with exactly ONE fenced ```json block:\n\n"
            "```json\n"
            "{\n"
            '  "decompose": true,\n'
            '  "reason": "why this split",\n'
            '  "subtasks": [\n'
            "    {\n"
            '      "id": "api", "title": "…", "summary": "self-contained brief",\n'
            '      "files_owned": ["src/api/**"], "depends_on": [],\n'
            '      "acceptance": ["pytest tests/test_api.py -q"],\n'
            '      "complexity": "small"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "or `{\"decompose\": false, \"reason\": \"…\"}` — then include your "
            "analysis (key files, approach, risks) as plain text for the worker.\n\n"
            "Hard rules: every subtask's `files_owned` stays INSIDE this task's "
            "'Owned files only'; subtask ownerships are pairwise disjoint; "
            "`depends_on` refers to sibling ids only (no cycles); complexity is "
            "trivial|small|medium (never large); each summary must be self-contained "
            "— the child agent sees nothing else. If blocked on user input, return "
            "a fenced `question` JSON block instead.\n"
        )
    elif role_kind == "sprint-contract" or stage is TaskStage.SPRINT_CONTRACT:
        instruction = (
            "\n## Assignment (sprint-contract, read-only)\n"
            "Freeze the product acceptance for this medium/large task BEFORE coding. "
            "Do NOT edit application code, acceptance.json, or evidence ledger. "
            "Confirm the acceptance criteria are complete and unambiguous; list gaps "
            "as questions if needed. End with `SPRINT_CONTRACT_READY` when the "
            "acceptance text in the brief is frozen as-is, or return a fenced "
            "`question` JSON block if a human decision is required.\n"
        )
    elif role_kind == "worker" or stage is TaskStage.IMPLEMENT:
        instruction = (
            "\n## Assignment\nImplement only this task in the working directory "
            f"(`{absolute_worktree or 'repo root'}`). "
            "Do not start another agent or runner. Run targeted checks where practical.\n\n"
            "End with `HARNESS_DONE` only when the goal is complete. Include a `Provides:` "
            "section listing durable interfaces for dependent tasks. If blocked on user input, "
            "return a fenced `question` JSON block.\n"
        )
    elif role_kind == "goal-judge":
        instruction = (
            "\n## Assignment (judge seat — read-only)\n"
            "Independently judge whether the implementation satisfies the "
            "original repository goal and acceptance. Judge the diff in the ReviewBundle, "
            "not the worker's description of it — a confident summary over an empty or "
            "unrelated diff is a CHANGES verdict. "
            "You CANNOT flip acceptance.json or evidence ledger; APPROVE is ignored by "
            "the control plane when evidence is red. "
            "End with exactly `VERDICT: APPROVE` or `VERDICT: CHANGES`.\n"
        )
        if review_bundle is not None:
            instruction += f"\n{review_bundle.render()}"
        if prior_result.strip():
            claims = _clip(prior_result.strip(), _JUDGE_EVIDENCE_LIMIT)
            prior = f"\n## Worker's own claims (unverified)\n\n{claims}\n"
    elif role_kind == "reviewer" or stage is TaskStage.REVIEW:
        bundle = (
            review_bundle.render()
            if review_bundle is not None
            else (
                "## ReviewBundle (authoritative; ignore worker-claimed paths)\n"
                "Changed files (0):\n- (none)\n"
                "Findings must use `path:line:severity: message` (one finding per line).\n"
            )
        )
        instruction = (
            "\n## Assignment (judge seat — read-only)\n"
            "Independently review only the authoritative ReviewBundle below. "
            "Never trust changed paths from worker text. Emit findings as "
            "`path:line:severity: message`. "
            "You CANNOT flip acceptance.json or evidence ledger; "
            "VERDICT: APPROVE is a no-op when evidence is red. "
            "End with exactly `VERDICT: APPROVE` or `VERDICT: CHANGES`.\n\n"
            f"{bundle}"
        )
        prior = ""
    elif stage is TaskStage.GATE:
        instruction = (
            "\n## Assignment\nIndependently inspect and run the scoped acceptance checks. "
            "Report factual outcomes; do not claim changed files from memory.\n"
        )
    else:
        instruction = "\n## Assignment\nEvaluate whether this task is ready to integrate.\n"

    return header + body + skills_block + context_block + prior + instruction


def _default_kind(stage: TaskStage) -> str:
    if stage is TaskStage.IMPLEMENT:
        return "worker"
    if stage is TaskStage.REVIEW:
        return "reviewer"
    if stage is TaskStage.GATE:
        return "gate"
    if stage is TaskStage.PLAN:
        return "sub-orchestrator"
    if stage is TaskStage.SPRINT_CONTRACT:
        return "sprint-contract"
    return stage.value


def _compact_spec(spec_text: str) -> str:
    return _clip(spec_text.strip(), _SPEC_LIMIT)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
