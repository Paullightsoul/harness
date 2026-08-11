"""Phase 0.5: curated skill catalog select 1–3."""

from __future__ import annotations

from pathlib import Path

from harness.skills.catalog import load_catalog, select_skills


def test_load_and_select_worker_max_3(tmp_path: Path) -> None:
    # Use real catalog from harness root if present; else write minimal.
    root = Path(__file__).resolve().parents[1]
    catalog_path = root / "skills" / "catalog.json"
    catalog = load_catalog(catalog_path)
    paths = select_skills(catalog, role="worker", stage="implement")
    assert 1 <= len(paths) <= 3
    assert all(p.endswith("SKILL.md") for p in paths)
    # human_only must not appear
    joined = " ".join(paths)
    assert "grill-me" not in joined
    assert "firecrawl" not in joined


def test_select_reviewer_pack() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = load_catalog(root / "skills" / "catalog.json")
    paths = select_skills(catalog, role="reviewer", stage="review")
    assert len(paths) <= 3
    assert any("security-review" in p or "code-quality" in p for p in paths)


def test_gate_fail_prefers_debug() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = load_catalog(root / "skills" / "catalog.json")
    paths = select_skills(
        catalog, role="worker", stage="fix", gate_fail=True, limit=3
    )
    assert any("systematic-debugging" in p for p in paths)


def test_skills_cli_registered() -> None:
    from harness.interface import cli as cli_module  # noqa: PLC0415

    parser = cli_module.build_parser()
    args = parser.parse_args(["skills", "select", "--role", "worker", "--stage", "implement"])
    assert args.skills_action == "select"
    assert args.role == "worker"
