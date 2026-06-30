from __future__ import annotations

from pathlib import Path

from harness.tasks_io.parser import parse_plan_dependencies, parse_task_file, read_section


def test_parse_task_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "task-003.md"
    f.write_text(
        '---\nid: "003"\ntitle: "REST users"\nstatus: "todo"\nattempts: 1\n---\n'
        "# Контекст\nтекст\n",
        encoding="utf-8",
    )
    parsed = parse_task_file(f)
    assert parsed.id == "003"
    assert parsed.title == "REST users"
    assert parsed.attempts == 1


def test_read_section() -> None:
    text = "# Контекст\nполезное\n# Файлы\n- src/x.py\n"
    assert "полезное" in read_section(text, "Контекст")
    assert "src/x.py" in read_section(text, "Файлы")


def test_parse_plan_dependencies(tmp_path: Path) -> None:
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "| id | название | зависит от | статус |\n"
        "|----|----|----|----|\n"
        "| 001 | база | — | todo |\n"
        "| 002 | api | 001 | todo |\n"
        "| 003 | тесты | 001, 002 | todo |\n",
        encoding="utf-8",
    )
    deps = parse_plan_dependencies(plan)
    assert deps["001"] == []
    assert deps["002"] == ["001"]
    assert deps["003"] == ["001", "002"]
