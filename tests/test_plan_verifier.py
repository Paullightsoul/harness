"""v2-018: детерминированный plan-verifier.

Проверяет PLAN.md + tasks/*.md без LLM: frontmatter, depends_on, Provides для
задач с dependents, acceptance criteria — исполняемые команды. Красный →
ingest отказывает без --force.
"""
from __future__ import annotations

from pathlib import Path

from harness.tasks_io.verifier import verify_plan


def _write_plan(tmp_path: Path, body: str) -> None:
    (tmp_path / "PLAN.md").write_text(body, encoding="utf-8")


def _write_task(
    tmp_path: Path, task_id: str, *,
    title: str = "stub",
    complexity: str = "normal",
    provides: str = "",
    ac_lines: list[str] | None = None,
    depends_on: str = "",
) -> None:
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    ac = "\n".join(ac_lines or ["- [ ] `pytest tests/test_x.py` зелёный"])
    provides_block = f"\n# Provides\n{provides}\n" if provides else "\n# Provides\n\n"
    content = f"""---
id: "{task_id}"
title: "{title}"
status: "todo"
complexity: "{complexity}"
attempts: 0
---

# Контекст
stub

# Файлы
- src/x.py

# Что нужно сделать
1. step

# Acceptance criteria
{ac}
{provides_block}
# Запрещено
-
"""
    (tmp_path / "tasks" / f"task-{task_id}.md").write_text(content, encoding="utf-8")


def test_verify_good_plan_passes(tmp_path: Path) -> None:
    _write_plan(tmp_path, "# Plan\n\n| id | фаза | название | depends_on | complexity | status |\n"
                          "|---|---|---|---|---|---|\n| 001 | A | X | — | normal | todo |\n")
    _write_task(tmp_path, "001", provides="- src/x.py::foo() -> int")
    report = verify_plan(tmp_path)
    assert report.ok, [f.issue for f in report.findings]
    assert report.errors == []


def test_verify_missing_plan_fails(tmp_path: Path) -> None:
    report = verify_plan(tmp_path)
    assert not report.ok
    assert any("PLAN.md не найден" in f.issue for f in report.errors)


def test_verify_missing_tasks_dir_fails(tmp_path: Path) -> None:
    _write_plan(tmp_path, "# plan\n")
    report = verify_plan(tmp_path)
    assert not report.ok
    assert any("tasks/" in f.issue for f in report.errors)


def test_verify_no_task_files_fails(tmp_path: Path) -> None:
    _write_plan(tmp_path, "# plan\n")
    (tmp_path / "tasks").mkdir(parents=True)
    report = verify_plan(tmp_path)
    assert not report.ok
    assert any("нет файлов" in f.issue for f in report.errors)


def test_verify_depends_on_missing_id_is_error(tmp_path: Path) -> None:
    _write_plan(tmp_path, "| id | название | depends_on | status |\n|---|---|---|---|\n"
                          "| 001 | X | 099 | todo |\n")
    _write_task(tmp_path, "001")
    report = verify_plan(tmp_path)
    assert not report.ok
    assert any("несуществующий id '099'" in f.issue for f in report.errors)


def test_verify_provides_missing_for_task_with_dependents_is_warn(tmp_path: Path) -> None:
    """Задача имеет dependents, но Provides пуст — warn (не error, мягкая миграция)."""
    _write_plan(tmp_path, "| id | название | depends_on | status |\n|---|---|---|---|\n"
                          "| 001 | X | — | todo |\n| 002 | Y | 001 | todo |\n")
    _write_task(tmp_path, "001", provides="")  # нет Provides
    _write_task(tmp_path, "002")
    report = verify_plan(tmp_path)
    # warn не блокирует
    assert report.ok
    assert any("dependents" in f.issue and "Provides" in f.issue for f in report.warnings)


def test_verify_non_executable_ac_is_warn(tmp_path: Path) -> None:
    _write_plan(tmp_path, "| id | название | depends_on | status |\n|---|---|---|---|\n"
                          "| 001 | X | — | todo |\n")
    _write_task(
        tmp_path, "001",
        ac_lines=["- [ ] работает корректно", "- [ ] код красивый"],
        provides="- x.py::foo() -> int",
    )
    report = verify_plan(tmp_path)
    assert report.ok  # warn, не error
    assert any("исполняемыми" in f.issue for f in report.warnings)


def test_verify_executable_ac_no_warning(tmp_path: Path) -> None:
    _write_plan(tmp_path, "| id | название | depends_on | status |\n|---|---|---|---|\n"
                          "| 001 | X | — | todo |\n")
    _write_task(
        tmp_path, "001",
        ac_lines=[
            "- [ ] `pytest tests/test_x.py` зелёный",
            "- [ ] `mypy src/x.py` без ошибок",
            "- [ ] `make lint` exit 0",
        ],
        provides="- x.py::foo() -> int",
    )
    report = verify_plan(tmp_path)
    assert not [f for f in report.warnings if "исполняемыми" in f.issue]


def test_verify_markdown_report_format(tmp_path: Path) -> None:
    _write_plan(tmp_path, "# plan\n")
    (tmp_path / "tasks").mkdir(parents=True)
    report = verify_plan(tmp_path)
    md = report.markdown()
    assert "Plan verification report" in md or "no findings" in md


def test_verify_plan_with_good_provides_no_warn(tmp_path: Path) -> None:
    """Задача с dependents и заполненным Provides — нет warn."""
    _write_plan(tmp_path, "| id | название | depends_on | status |\n|---|---|---|---|\n"
                          "| 001 | X | — | todo |\n| 002 | Y | 001 | todo |\n")
    _write_task(tmp_path, "001", provides="- src/x.py::foo() -> int\n- src/x.py::bar() -> str")
    _write_task(tmp_path, "002")
    report = verify_plan(tmp_path)
    assert not [f for f in report.warnings if "dependents" in f.issue]
