"""v2-008: Канон промптов — `prompts/<role>.md`. `.cursor/agents/<role>.md` — только
frontmatter + ссылка на канон (для Cursor IDE). Тест фиксирует инвариант, чтобы
файлы не разошлись снова.

Гарантирует:
  1. Каждый `.cursor/agents/<role>.md` имеет frontmatter с `name` и `model`.
  2. Тело `.cursor/agents/<role>.md` ссылается на `prompts/<role>.md`.
  3. `prompts/<role>.md` существует и непустой.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ROLES = ("orchestrator", "reviewer", "worker")

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(\S+)", re.MULTILINE)
_MODEL_RE = re.compile(r"^model:\s*(\S+)", re.MULTILINE)


def _frontmatter(text: str) -> dict[str, str]:
    m = _FM_RE.search(text)
    if not m:
        return {}
    fm = m.group(1)
    out: dict[str, str] = {}
    name = _NAME_RE.search(fm)
    model = _MODEL_RE.search(fm)
    if name:
        out["name"] = name.group(1)
    if model:
        out["model"] = model.group(1)
    return out


def test_agents_directory_has_all_roles() -> None:
    for role in _ROLES:
        agent = _ROOT / ".cursor" / "agents" / f"{role}.md"
        assert agent.exists(), f"missing .cursor/agents/{role}.md"


def test_agents_have_frontmatter_with_name_and_model() -> None:
    for role in _ROLES:
        text = (_ROOT / ".cursor" / "agents" / f"{role}.md").read_text(encoding="utf-8")
        fm = _frontmatter(text)
        assert fm.get("name") == role, f"{role}: frontmatter name должен быть {role}"
        assert "model" in fm, f"{role}: frontmatter должен содержать model"


def test_agents_reference_canonical_prompt() -> None:
    """Тело .cursor/agents/<role>.md ссылается на prompts/<role>.md — канон."""
    for role in _ROLES:
        text = (_ROOT / ".cursor" / "agents" / f"{role}.md").read_text(encoding="utf-8")
        assert f"prompts/{role}.md" in text, (
            f".cursor/agents/{role}.md должен ссылаться на prompts/{role}.md (канон)"
        )


def test_canonical_prompts_exist_and_nonempty() -> None:
    for role in _ROLES:
        prompt = _ROOT / "prompts" / f"{role}.md"
        assert prompt.exists(), f"missing canonical prompts/{role}.md"
        text = prompt.read_text(encoding="utf-8")
        assert len(text.strip()) > 100, (
            f"prompts/{role}.md слишком короткий ({len(text)} chars) — вероятно заглушка"
        )
