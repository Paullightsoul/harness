from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIMARY = ROOT / ".cursor" / "skills" / "harness-orchestrate" / "SKILL.md"
REFERENCE = ROOT / ".cursor" / "skills" / "harness-orchestrate" / "reference.md"
LEGACY_CHAT = ROOT / ".cursor" / "skills" / "harness-chat-orchestrator" / "SKILL.md"
LEGACY_BRIDGE = ROOT / ".cursor" / "skills" / "harness-bridge-worker" / "SKILL.md"
BRIDGE_RULE = ROOT / ".cursor" / "rules" / "40-chat-bridge.mdc"

VERBS = ("start", "next", "report", "advance", "status", "abort", "resume")


def _text(path: Path) -> str:
    assert path.exists(), f"missing {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_primary_skill_is_tasktool_entrypoint() -> None:
    text = _text(PRIMARY)
    assert "name: harness-orchestrate" in text
    assert "используй harness" in text
    assert "use harness" in text
    # Checklist lists the verb family; full CLI forms live in reference.md.
    assert "harness tasktool start|next|report|advance|status|abort|resume" in text
    for verb in VERBS:
        assert verb in text


def test_primary_skill_enforces_pull_dispatch_and_approvals() -> None:
    text = _text(PRIMARY)
    assert "--limit 12" in text
    assert "next(≤12)" in text or "continuous refill" in text.lower()
    assert "continuous refill" in text.lower()
    assert "run_in_background" in text
    assert "only Task Tool dispatcher" in text
    assert "AskQuestion" in text
    assert "plan approval" in text
    assert "risk" in text.lower()
    assert "ship" in text.lower()
    assert "Atomically write" in text or "atomically write" in text
    assert "prompt_path" in text
    assert "result_path" in text
    assert "HARNESS_SHIP_MODE=manual" in text
    assert "HARNESS_USE_WORKTREES" in text


def test_primary_skill_forbids_full_wave_batch_wait() -> None:
    text = _text(PRIMARY).lower()
    assert "never wait for the whole wave" in text or "do **not** batch-wait" in _text(PRIMARY).lower() or "never** batch-wait" in text
    ref = _text(REFERENCE).lower()
    assert "continuous refill" in ref
    assert "run_in_background" in ref
    assert "wait for all" in ref  # in the Don't column


def test_primary_skill_plans_and_verifies_before_start() -> None:
    text = _text(PRIMARY)
    assert "invoke **one** planner" in text
    assert "harness verify --project" in text
    assert "harness tasktool start" in text
    assert "PLAN.md" in text
    assert "tasks/task-*.md" in text
    assert "--approve-plan" in text
    assert "--spec-source" in text
    assert "never creates a planner dispatch" in text
    # Plan section precedes Start section.
    assert text.index("## 2. Plan") < text.index("## 3. Start")
    assert text.index("## 3. Start") < text.index("## 4. Pull-dispatch loop")


def test_primary_skill_and_reference_use_implemented_cli_shapes() -> None:
    skill = _squash(_text(PRIMARY))
    reference = _squash(_text(REFERENCE))
    assert '--limit 12' in skill
    assert '--agent-id "<root-chat-id>" --limit 12' in reference
    for fragment in (
        'harness tasktool start "<goal>"',
        'harness tasktool next "<run-id>"',
        'harness tasktool report "<dispatch-id>"',
        'harness tasktool advance "<run-id>"',
        'harness tasktool status "<run-id>"',
        'harness tasktool abort "<run-id>"',
        'harness tasktool resume "<run-id>"',
        "--approve-plan",
        "--spec-source",
        "--result-file",
    ):
        assert fragment in reference


def test_gate_dispatch_is_a_protocol_error() -> None:
    text = _text(PRIMARY)
    assert "stage: gate" in text
    assert "protocol error" in text
    assert "do **not** call Task Tool" in text or "do not call Task Tool" in text
    assert "Gates are control-plane work" in text


def test_primary_skill_contains_exact_minimal_brief_and_tree_guards() -> None:
    text = _text(PRIMARY)
    for heading in (
        "Worktree absolute path:",
        "Owned files only:",
        "Read-only paths:",
        "Frozen contracts (verbatim):",
        "Acceptance:",
        "Resource class:",
        "Relevant canon pointer:",
        "Report format:",
    ):
        assert heading in text
    assert "depth ≤ 2" in text
    assert "must not invoke Task Tool children" in text
    assert "Medium/large" in text


def test_primary_skill_single_dispatcher_semantics() -> None:
    """Root chat owns every Task Tool call; depth is conceptual, not nested dispatch."""
    text = _text(PRIMARY)
    assert "only Task Tool dispatcher" in text
    assert "returns leaf briefs" in text
    assert "must not invoke Task Tool children" in text
    assert "Max depth is **not** nested" in text
    assert "A sub-orchestrator can dispatch only the" not in text
    assert "explicitly makes it a medium/large sub-orchestrator" not in text


def test_primary_skill_forbids_legacy_execution_patterns() -> None:
    text = _text(PRIMARY)
    forbidden = (
        "harness plan &",
        "harness run &",
        "subprocess.Popen",
        "subprocess.run",
        "requests/*.json",
        "sleep(1",
        "sleep(2",
    )
    for pattern in forbidden:
        assert pattern not in text
    assert not re.search(r"\bwhile\s+true\b", text, flags=re.IGNORECASE)


def test_legacy_surfaces_point_to_primary_and_cap_parallelism() -> None:
    primary_pointer = ".cursor/skills/harness-orchestrate/SKILL.md"
    for path in (LEGACY_CHAT, LEGACY_BRIDGE, BRIDGE_RULE):
        text = _text(path)
        assert "legacy" in text.lower()
        assert primary_pointer in text
    assert "max 2" in _text(LEGACY_BRIDGE).lower()
    assert "write_response" in _text(LEGACY_BRIDGE)


def test_primary_skill_documents_approve_risk_separately_from_ship() -> None:
    text = _text(PRIMARY)
    assert "--approve-risk" in text
    assert "never conflate" in text.lower()
    assert "--answers-file" in text
