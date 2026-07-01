"""v2-016 + v2-017: iterative-retrieval скилл + graphify rule + секции в worker-промпте.

Регрессии: наличие обязательной секции iterative-retrieval в `prompts/worker.md`,
скилла `iterative-retrieval/SKILL.md`, правила `30-graphify.mdc`, и упоминания
`graphify query` в промпте.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_worker_prompt_has_iterative_retrieval_section() -> None:
    """v2-016: prompts/worker.md содержит обязательный шаг iterative-retrieval."""
    text = (_ROOT / "prompts" / "worker.md").read_text(encoding="utf-8")
    assert "Iterative retrieval" in text
    assert "DISPATCH" in text and "EVALUATE" in text and "REFINE" in text and "LOOP" in text
    assert "retrieval_result:" in text  # требование вернуть в output


def test_iterative_retrieval_skill_exists() -> None:
    skill = _ROOT / ".cursor" / "skills" / "iterative-retrieval" / "SKILL.md"
    assert skill.exists(), "missing .cursor/skills/iterative-retrieval/SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert "harness_role: worker" in text
    assert "DISPATCH" in text and "EVALUATE" in text


def test_worker_prompt_mentions_graphify() -> None:
    """v2-017: worker-промпт упоминает graphify query для архитектурных вопросов."""
    text = (_ROOT / "prompts" / "worker.md").read_text(encoding="utf-8")
    assert "graphify query" in text


def test_graphify_rule_exists() -> None:
    rule = _ROOT / ".cursor" / "rules" / "30-graphify.mdc"
    assert rule.exists(), "missing .cursor/rules/30-graphify.mdc"
    text = rule.read_text(encoding="utf-8")
    assert "graphify query" in text
    assert "rg" in text or "Grep" in text  # когда graphify vs rg
