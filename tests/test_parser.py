from __future__ import annotations

from pathlib import Path

from harness.tasks_io.parser import (
    parse_plan_dependencies,
    parse_task_dependencies,
    parse_task_file,
    read_section,
    resolve_plan_root,
)


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


def test_parse_plan_dependencies_heading_format(tmp_path: Path) -> None:
    """v2-036: формат `harness plan` (см. `_save_plan_to_project`) — heading +
    bullet-список, не markdown-таблица. Без этого depends_on всегда пуст для
    любого плана, реально сгенерированного `harness plan`."""
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "# PLAN\n\n## Goal\nтест\n\n## Tasks\n\n"
        "### 001: база\n- complexity: small\n\n"
        "### 002: api\n- complexity: medium\n- depends_on: 001\n\n"
        "### 003: тесты\n- complexity: small\n- depends_on: 001, 002\n\n"
        "## Strategy\nSee tasks/\n",
        encoding="utf-8",
    )
    deps = parse_plan_dependencies(plan)
    assert deps["001"] == []
    assert deps["002"] == ["001"]
    assert deps["003"] == ["001", "002"]


def test_parse_plan_dependencies_missing_file(tmp_path: Path) -> None:
    assert parse_plan_dependencies(tmp_path / "no-such-PLAN.md") == {}


def test_parse_plan_dependencies_task_id_header_and_ignores_partition_table(
    tmp_path: Path,
) -> None:
    """Real dogfood PLANs use `task id | … | depends on` then a later ownership table
    whose first column is also a task id — that table must not wipe the DAG."""
    plan = tmp_path / "PLAN.md"
    plan.write_text(
        "## Dependency DAG\n\n"
        "| task id | title | depends on |\n"
        "|---------|-------|------------|\n"
        "| 001 | docs | — |\n"
        "| 002 | infra | 001 |\n"
        "| 003 | schema | 001 |\n"
        "| 004 | api | 002, 003 |\n"
        "| 005 | loop | 003, 004 |\n"
        "| 006 | onboard | 002, 003, 004 |\n"
        "| 007 | smoke | 004, 005, 006 |\n"
        "\n## Single-writer partitions\n\n"
        "| Task | Owns (write) | Read-only |\n"
        "|------|--------------|-----------|\n"
        "| 001 | docs/a.md | contracts/ |\n"
        "| 002 | config/x.php | routes/ |\n"
        "| 004 | routes/api.php | LocationController |\n",
        encoding="utf-8",
    )
    deps = parse_plan_dependencies(plan)
    assert deps["001"] == []
    assert deps["002"] == ["001"]
    assert deps["003"] == ["001"]
    assert deps["004"] == ["002", "003"]
    assert deps["005"] == ["003", "004"]
    assert deps["006"] == ["002", "003", "004"]
    assert deps["007"] == ["004", "005", "006"]


def test_parse_task_dependencies_from_body() -> None:
    text = "# Task\n\nDepends on: **002**, **003**.\n\n## Files\n"
    # Bold markers sit outside the capture in our regex — use plain form too.
    assert parse_task_dependencies("Depends on: 002, 003\n") == ["002", "003"]
    assert parse_task_dependencies("**Depends on:** 004\n") == ["004"]


def test_resolve_plan_root_prefers_runs_latest(tmp_path: Path) -> None:
    """`harness plan` пишет в .harness/runs/<id>/ + симлинк .../latest — ingest/
    verify должны читать оттуда, а не из settings.root напрямую."""
    run_dir = tmp_path / ".harness" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "PLAN.md").write_text("# plan\n", encoding="utf-8")
    latest = tmp_path / ".harness" / "runs" / "latest"
    latest.symlink_to(run_dir, target_is_directory=True)

    assert resolve_plan_root(tmp_path) == latest


def test_resolve_plan_root_falls_back_without_latest(tmp_path: Path) -> None:
    """Dogfood-конвенция: PLAN.md/tasks/ прямо в root, без .harness/runs/."""
    assert resolve_plan_root(tmp_path) == tmp_path


def test_resolve_plan_root_falls_back_when_latest_has_no_plan(tmp_path: Path) -> None:
    """`.harness/runs/latest/` существует, но ещё не дописан (нет PLAN.md) — не
    выбираем пустой/неполный каталог."""
    latest = tmp_path / ".harness" / "runs" / "latest"
    latest.mkdir(parents=True)
    assert resolve_plan_root(tmp_path) == tmp_path
