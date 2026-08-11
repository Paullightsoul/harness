"""Harness V4 Phase 3 — economics breadth gate + brain-agents sync/query."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.brain_agents.cards import BrainCard, load_card, write_card
from harness.brain_agents.query import query_cards
from harness.brain_agents.sync import sync_brain_agents
from harness.config import Settings
from harness.policy.economics import (
    DENY_CODE,
    assess_breadth,
    effective_resource_limits,
    resolve_fanout_profile,
)
from harness.tasktool.expansion import ExpansionDecision, SubtaskBrief, parse_expansion


def _brief(
    child_id: str,
    *owned: str,
) -> SubtaskBrief:
    return SubtaskBrief(
        child_id=child_id,
        title=child_id,
        summary="x",
        files_owned=owned,
        depends_on=(),
        acceptance=("ok",),
        complexity="small",
    )


def test_economics_off_always_allows_breadth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ECONOMICS", "0")
    decision = ExpansionDecision(
        decompose=True,
        reason="overlap ok when off",
        subtasks=(
            _brief("a", "src/foo/**"),
            _brief("b", "src/foo/**"),  # identical — would deny if on
        ),
    )
    verdict = assess_breadth(decision, force=False)
    assert verdict.ok


def test_economics_denies_identical_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ECONOMICS", "1")
    decision = ExpansionDecision(
        decompose=True,
        reason="tax",
        subtasks=(
            _brief("a", "admin/**"),
            _brief("b", "admin/**"),
        ),
    )
    verdict = assess_breadth(decision, force=True)
    assert verdict.denied
    assert verdict.code == DENY_CODE
    assert "identical" in verdict.reason or "not disjoint" in verdict.reason


def test_economics_denies_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ECONOMICS", "1")
    decision = ExpansionDecision(
        decompose=True,
        subtasks=(
            _brief("a", "backend/app/**"),
            _brief("b", "backend/app/Http/**"),
        ),
    )
    verdict = assess_breadth(decision, force=True)
    assert verdict.denied
    assert any("not disjoint" in e for e in verdict.errors)


def test_economics_allows_disjoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ECONOMICS", "1")
    decision = ExpansionDecision(
        decompose=True,
        subtasks=(
            _brief("a", "backend/app/Models/**"),
            _brief("b", "admin/app/MoonShine/**"),
        ),
    )
    verdict = assess_breadth(
        decision,
        parent_files_owned=["backend/**", "admin/**"],
        force=True,
    )
    assert verdict.ok


def test_solo_profile_clamps_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ECONOMICS", "1")
    monkeypatch.setenv("HARNESS_FANOUT_PROFILE", "solo")
    profile = resolve_fanout_profile("solo")
    jobs, slots, heavy, gates, children = effective_resource_limits(
        profile=profile,
        force_economics=True,
        baseline_jobs=12,
        baseline_slots=12,
        baseline_heavy=5,
        baseline_gates=4,
        baseline_children=12,
    )
    assert jobs == 4
    assert slots == 4
    assert heavy == 2
    assert gates == 2
    assert children == 4


def test_economics_off_keeps_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_ECONOMICS", "0")
    jobs, *_rest, children = effective_resource_limits(
        force_economics=False,
        baseline_jobs=12,
        baseline_children=12,
    )
    assert jobs == 12
    assert children == 12


def test_settings_effective_children(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_ROOT", str(tmp_path))
    monkeypatch.setenv("HARNESS_ECONOMICS", "1")
    monkeypatch.setenv("HARNESS_FANOUT_PROFILE", "solo")
    monkeypatch.setenv("HARNESS_DECOMPOSE_MAX_CHILDREN", "12")
    s = Settings()
    assert s.effective_decompose_max_children == 4
    assert s.resource_policy.max_tasktool_jobs == 4


def test_sync_and_query_brain_agents(tmp_path: Path) -> None:
    canon = tmp_path / "brain"
    agent = tmp_path / "brain-agents"
    (canon / "decisions").mkdir(parents=True)
    (canon / "architecture").mkdir(parents=True)
    (canon / "incidents").mkdir(parents=True)
    (canon / "templates").mkdir(parents=True)
    (canon / "decisions" / "ADR-0099-test-harness.md").write_text(
        "# ADR-0099 Test Harness\n\ntags: [adr, harness]\n\nDecision about TaskTool.\n",
        encoding="utf-8",
    )
    (canon / "architecture" / "zy-backend.md").write_text(
        "# ZY Backend\n\nPHP Laravel service.\n",
        encoding="utf-8",
    )
    result = sync_brain_agents(canon=canon, agent_root=agent)
    assert not result.errors
    assert result.cards_written >= 2
    assert (agent / "index.json").is_file()
    assert (agent / "lessons").is_dir()
    assert (agent / "decisions" / "ADR-0099-test-harness.md").is_file()
    # Human canon untouched beyond what we wrote.
    assert list(canon.rglob("*"))  # still exists

    hits = query_cards("TaskTool harness ADR", agent_root=agent, limit=3)
    assert hits
    assert "0099" in hits[0].card.id or "harness" in hits[0].card.title.lower()


def test_sync_refuses_human_canon_as_target(tmp_path: Path) -> None:
    # Simulate pointing agent_root at a path named like canon under /home —
    # use identical paths which the sync also rejects.
    canon = tmp_path / "brain"
    canon.mkdir()
    result = sync_brain_agents(canon=canon, agent_root=canon)
    assert result.errors
    assert any("differ" in e for e in result.errors)


def test_write_card_agent_layer(tmp_path: Path) -> None:
    cards = tmp_path / "cards"
    path = write_card(
        cards,
        BrainCard(
            id="lesson-demo",
            title="Demo",
            kind="lesson",
            path=str(tmp_path / "lessons" / "x.md"),
            summary="pointer only",
            source="agent",
        ),
    )
    loaded = load_card(path)
    assert loaded is not None
    assert loaded.id == "lesson-demo"
    assert loaded.source == "agent"


def test_parse_expansion_still_works_with_economics_helpers() -> None:
    text = (
        '```json\n{"decompose": true, "subtasks": [\n'
        '{"id": "a", "title": "A", "files_owned": ["a/**"], '
        '"acceptance": ["ok"], "complexity": "small"},\n'
        '{"id": "b", "title": "B", "files_owned": ["b/**"], '
        '"acceptance": ["ok"], "complexity": "small"}\n'
        "]}\n```\n"
    )
    decision = parse_expansion(text)
    assert decision is not None
    assert decision.decompose
    assert assess_breadth(decision, force=True).ok
