"""Tests for deterministic ReviewBundle grouping."""

from __future__ import annotations

from pathlib import Path

from harness.tasktool.review_bundle import build_review_bundle


def test_grouping_is_deterministic_by_package() -> None:
    files = [
        "tests/test_b.py",
        "harness/tasktool/z.py",
        "harness/tasktool/a.py",
        "src/pkg/one.py",
        "README.md",
    ]
    first = build_review_bundle(files)
    second = build_review_bundle(list(reversed(files)))
    assert first.changed_files == second.changed_files
    assert [group.key for group in first.groups] == [group.key for group in second.groups]
    assert first.changed_files == (
        "README.md",
        "harness/tasktool/a.py",
        "harness/tasktool/z.py",
        "src/pkg/one.py",
        "tests/test_b.py",
    )
    keys = {group.key: group.files for group in first.groups}
    assert keys["harness/tasktool"] == (
        "harness/tasktool/a.py",
        "harness/tasktool/z.py",
    )
    assert keys["src/pkg"] == ("src/pkg/one.py",)
    assert keys["tests"] == ("tests/test_b.py",)
    assert "2 file(s)" in first.groups[0].impact_hint or any(
        "file(s)" in group.impact_hint for group in first.groups
    )


def test_graphify_hint_only_when_graph_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    without = build_review_bundle(["a.py"], repo_root=repo, project="missing-proj")
    assert without.graphify_hint == ""

    graph = repo / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text("{}", encoding="utf-8")
    with_graph = build_review_bundle(["a.py"], repo_root=repo, project="missing-proj")
    assert "graphify query" in with_graph.graphify_hint
    assert str(graph) in with_graph.graphify_hint
    assert "do not run automatically" in with_graph.render().lower() or (
        "On-demand graphify" in with_graph.render()
    )


def test_render_requires_path_line_severity_findings() -> None:
    bundle = build_review_bundle(["pkg/mod.py"])
    text = bundle.render()
    assert "pkg/mod.py" in text
    assert "path:line:severity: message" in text
    assert "ignore worker-claimed paths" in text


def test_render_embeds_diff_in_fenced_block() -> None:
    diff = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-old\n+new\n"
    bundle = build_review_bundle(["pkg/mod.py"], diff=diff)
    text = bundle.render()
    assert "```diff" in text
    assert "+new" in text
    assert "captured at worker finalize" in text


def test_render_flags_missing_diff_instead_of_staying_silent() -> None:
    """A reviewer must not mistake "no diff shown" for "nothing to check"."""
    text = build_review_bundle(["pkg/mod.py"]).render()
    assert "```diff" not in text
    assert "No diff captured" in text
