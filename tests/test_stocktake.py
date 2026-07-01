"""v2-035: skill-stocktake — аудит prompts/.cursor/skills (размер, свежесть, usage)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import AgentEventKind
from harness.domain.models import AgentEvent, Run
from harness.interface import cli as cli_module
from harness.skills.stocktake import SkillUsage, run_stocktake
from harness.store.repository import Store


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_finds_prompts_and_skills(tmp_path: Path) -> None:
    _write(tmp_path / "prompts" / "worker.md", "# worker\n")
    _write(tmp_path / "prompts" / "reviewer.md", "# reviewer\n")
    _write(tmp_path / ".cursor" / "skills" / "tdd-workflow" / "SKILL.md", "# tdd\n")
    store = Store(tmp_path / "state.db")

    report = run_stocktake(tmp_path, store)

    paths = {item.path for item in report.items}
    assert str(tmp_path / "prompts" / "worker.md") in paths
    assert str(tmp_path / "prompts" / "reviewer.md") in paths
    assert str(tmp_path / ".cursor" / "skills" / "tdd-workflow" / "SKILL.md") in paths
    kinds = {item.kind for item in report.items}
    assert kinds == {"prompt", "skill"}


def test_empty_directories_returns_empty_report(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    report = run_stocktake(tmp_path, store)
    assert report.items == []
    assert report.heavy_and_unmentioned == []


def test_size_bytes_and_approx_tokens(tmp_path: Path) -> None:
    content = "x" * 4000
    _write(tmp_path / "prompts" / "big.md", content)
    store = Store(tmp_path / "state.db")

    report = run_stocktake(tmp_path, store)

    item = report.items[0]
    assert item.size_bytes == 4000
    assert item.approx_tokens == 1000
    assert item.is_heavy is True


def test_small_file_is_not_heavy(tmp_path: Path) -> None:
    _write(tmp_path / "prompts" / "tiny.md", "hi\n")
    store = Store(tmp_path / "state.db")

    report = run_stocktake(tmp_path, store)

    assert report.items[0].is_heavy is False


def test_mentions_counted_from_agent_events_across_runs(tmp_path: Path) -> None:
    _write(tmp_path / ".cursor" / "skills" / "search-first" / "SKILL.md", "# search-first\n")
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.create_run(Run(id="r2", project="p", goal="g2", status="running"))
    store.add_agent_events("r1", "001", 1, [
        AgentEvent(kind=AgentEventKind.ASSISTANT_MSG.value,
                   payload={"text": "следуя скиллу search-first, я поискал rg"}),
    ])
    store.add_agent_events("r2", "002", 1, [
        AgentEvent(kind=AgentEventKind.ASSISTANT_MSG.value,
                   payload={"text": "search-first: ищу существующую реализацию"}),
    ])

    report = run_stocktake(tmp_path, store)

    skill = next(i for i in report.items if i.kind == "skill")
    assert skill.mentions == 2
    assert skill.is_unmentioned is False


def test_unmentioned_skill_has_zero_mentions(tmp_path: Path) -> None:
    _write(tmp_path / ".cursor" / "skills" / "never-used" / "SKILL.md", "# never used\n")
    store = Store(tmp_path / "state.db")

    report = run_stocktake(tmp_path, store)

    assert report.items[0].mentions == 0
    assert report.items[0].is_unmentioned is True


def test_heavy_and_unmentioned_is_the_intersection(tmp_path: Path) -> None:
    # Тяжёлый и упомянутый — не кандидат.
    _write(tmp_path / "prompts" / "heavy-used.md", "x" * 5000)
    # Тяжёлый и НЕ упомянутый — кандидат.
    _write(tmp_path / "prompts" / "heavy-unused.md", "y" * 5000)
    # Лёгкий и не упомянутый — не кандидат (мал, не жалко).
    _write(tmp_path / "prompts" / "light-unused.md", "z" * 10)
    store = Store(tmp_path / "state.db")
    store.create_run(Run(id="r1", project="p", goal="g", status="running"))
    store.add_agent_events("r1", "001", 1, [
        AgentEvent(kind=AgentEventKind.ASSISTANT_MSG.value,
                   payload={"text": "используя heavy-used прочитал спеку"}),
    ])

    report = run_stocktake(tmp_path, store)
    candidates = {i.path for i in report.heavy_and_unmentioned}

    assert str(tmp_path / "prompts" / "heavy-unused.md") in candidates
    assert str(tmp_path / "prompts" / "heavy-used.md") not in candidates
    assert str(tmp_path / "prompts" / "light-unused.md") not in candidates


def test_markdown_report_contains_sections(tmp_path: Path) -> None:
    _write(tmp_path / "prompts" / "big.md", "w" * 5000)
    store = Store(tmp_path / "state.db")

    report = run_stocktake(tmp_path, store)
    md = report.markdown()

    assert "# Skill stocktake report" in md
    assert "## Все файлы" in md
    assert "## Кандидаты на ревизию" in md
    assert "big.md" in md


def test_markdown_clean_when_no_heavy_unmentioned(tmp_path: Path) -> None:
    _write(tmp_path / "prompts" / "tiny.md", "hi\n")
    store = Store(tmp_path / "state.db")

    report = run_stocktake(tmp_path, store)
    md = report.markdown()

    assert "Нет тяжёлых неиспользуемых файлов" in md


def test_skill_usage_dataclass_defaults() -> None:
    item = SkillUsage(path="x", kind="prompt", size_bytes=0, modified_at="", mentions=0)
    assert item.approx_tokens == 0
    assert item.is_heavy is False
    assert item.is_unmentioned is True


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_stocktake_subparser_registered() -> None:
    parser = cli_module.build_parser()
    args = parser.parse_args(["stocktake", "--write", "/tmp/st.md"])
    assert args.command == "stocktake"
    assert args.write == "/tmp/st.md"


def test_cmd_stocktake_prints_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path / "prompts" / "worker.md", "# worker\n")
    settings = Settings(root=tmp_path)

    rc = cli_module.cmd_stocktake(settings, argparse.Namespace(write=None))

    assert rc == 0
    captured = capsys.readouterr()
    assert "Skill stocktake report" in captured.out


def test_cmd_stocktake_write_to_file(tmp_path: Path) -> None:
    _write(tmp_path / "prompts" / "worker.md", "# worker\n")
    settings = Settings(root=tmp_path)
    target = tmp_path / "out" / "stocktake.md"

    rc = cli_module.cmd_stocktake(settings, argparse.Namespace(write=str(target)))

    assert rc == 0
    assert target.exists()
    assert "Skill stocktake report" in target.read_text(encoding="utf-8")
