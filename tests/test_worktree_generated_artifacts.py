"""Regression: commit_all must not stage pytest/venv caches (harness git pollution).

Dogfood incident: bare `git add -A` in a target repo without .gitignore pulled
`__pycache__/*.pyc` (and previously whole venvs) into task commits → huge diffs /
E2BIG in CliRunner. Staging must deterministically exclude generated paths without
touching the target .gitignore or deleting user files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.worktree.manager import WorktreeManager, is_generated_artifact


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_without_gitignore(repo: Path) -> None:
    """Target repo with NO .gitignore — the pollution failure mode."""
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@harness.local")
    _run_git(repo, "config", "user.name", "harness-test")
    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    (repo / "keep_me.py").write_text("KEEP = 1\n", encoding="utf-8")
    _run_git(repo, "add", "app.py", "keep_me.py")
    _run_git(repo, "commit", "-q", "-m", "init")
    assert not (repo / ".gitignore").exists()


def _committed_files(repo: Path, rev: str = "HEAD") -> set[str]:
    out = _run_git(repo, "ls-tree", "-r", "--name-only", rev).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def _simulate_pytest_caches(root: Path) -> None:
    """Create artifacts typical of running pytest / compiling Python."""
    pycache = root / "pkg" / "__pycache__"
    pycache.mkdir(parents=True, exist_ok=True)
    (pycache / "mod.cpython-312.pyc").write_bytes(b"\0" * 64)
    (root / "app.pyc").write_bytes(b"\0" * 32)
    pytest_cache = root / ".pytest_cache"
    pytest_cache.mkdir(parents=True, exist_ok=True)
    (pytest_cache / "v" / "cache" / "nodeids").parent.mkdir(parents=True, exist_ok=True)
    (pytest_cache / "v" / "cache" / "nodeids").write_text("[]\n", encoding="utf-8")
    mypy = root / ".mypy_cache" / "3.12"
    mypy.mkdir(parents=True, exist_ok=True)
    (mypy / "data.json").write_text("{}\n", encoding="utf-8")
    ruff = root / ".ruff_cache"
    ruff.mkdir(parents=True, exist_ok=True)
    (ruff / "CACHEDIR.TAG").write_text("tag\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pkg/__pycache__/x.pyc", True),
        ("app.pyc", True),
        (".pytest_cache/v/cache", True),
        (".venv/lib/python3.12/site.py", True),
        ("venv/bin/python", True),
        ("nested/venv/lib/junk.py", True),
        ("nested/.venv/lib/site.py", True),
        ("node_modules/leftpad/index.js", True),
        ("apps/web/node_modules/x.js", True),
        ("dist/pkg-1.0.whl", True),
        ("packages/ui/dist/index.js", True),
        ("build/lib/app.py", True),
        ("services/api/build/out.js", True),
        ("cache/pip/wheels.db", True),
        ("nested/cache/y/c.txt", True),
        ("htmlcov/index.html", True),
        ("pkg.egg-info/PKG-INFO", True),
        ("src/feature.py", False),
        ("app.py", False),
    ],
)
def test_is_generated_artifact(path: str, expected: bool) -> None:
    assert is_generated_artifact(path) is expected


@pytest.mark.asyncio
async def test_commit_all_excludes_pytest_like_caches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_without_gitignore(repo)
    wm = WorktreeManager(repo, repo / ".worktrees", "main")
    worktree, branch = await wm.create("cache-pollution")

    _simulate_pytest_caches(worktree)
    (worktree / "src").mkdir()
    (worktree / "src" / "feature.py").write_text("FEATURE = 1\n", encoding="utf-8")
    (worktree / "app.py").write_text("print('changed')\n", encoding="utf-8")

    await wm.commit_all(worktree, "task: add feature")

    committed = _committed_files(worktree)
    assert "src/feature.py" in committed
    assert "app.py" in committed
    assert not any("__pycache__" in p for p in committed)
    assert not any(p.endswith((".pyc", ".pyo", ".pyd")) for p in committed)
    assert not any(".pytest_cache" in p for p in committed)
    assert not any(".mypy_cache" in p for p in committed)
    assert not any(".ruff_cache" in p for p in committed)

    changed = await wm.changed_files(branch)
    assert "src/feature.py" in changed
    assert "app.py" in changed
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in changed)
    assert not any(
        part in p
        for p in changed
        for part in (".pytest_cache", ".mypy_cache", ".ruff_cache")
    )

    # Caches remain on disk (not deleted); just untracked.
    assert (worktree / "pkg" / "__pycache__" / "mod.cpython-312.pyc").is_file()
    assert (worktree / ".pytest_cache" / "v" / "cache" / "nodeids").is_file()
    assert not (worktree / ".gitignore").exists()


@pytest.mark.asyncio
async def test_commit_all_preserves_tracked_source_deletion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_without_gitignore(repo)
    wm = WorktreeManager(repo, repo / ".worktrees", "main")
    worktree, branch = await wm.create("delete-source")

    _simulate_pytest_caches(worktree)
    (worktree / "keep_me.py").unlink()

    await wm.commit_all(worktree, "task: remove keep_me")

    committed = _committed_files(worktree)
    assert "keep_me.py" not in committed
    changed = await wm.changed_files(branch)
    assert "keep_me.py" in changed
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in changed)


@pytest.mark.asyncio
async def test_commit_all_includes_untracked_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_without_gitignore(repo)
    wm = WorktreeManager(repo, repo / ".worktrees", "main")
    worktree, branch = await wm.create("untracked-source")

    (worktree / "new_module.py").write_text("X = 1\n", encoding="utf-8")
    await wm.commit_all(worktree, "task: add module")

    assert "new_module.py" in _committed_files(worktree)
    assert "new_module.py" in await wm.changed_files(branch)


@pytest.mark.asyncio
async def test_commit_all_skips_large_generated_directory(tmp_path: Path) -> None:
    """Large .venv / node_modules must not be staged and must not trip size guards."""
    repo = tmp_path / "repo"
    _init_repo_without_gitignore(repo)
    wm = WorktreeManager(repo, repo / ".worktrees", "main")
    worktree, branch = await wm.create("fat-venv")

    venv_pkg = worktree / ".venv" / "lib" / "python3.12" / "site-packages" / "fatpkg"
    venv_pkg.mkdir(parents=True, exist_ok=True)
    # ~2 MiB of junk — would exceed default HARNESS_MAX_DIFF if counted, and
    # would pollute the commit if staged.
    blob = b"x" * (256 * 1024)
    for i in range(8):
        (venv_pkg / f"mod_{i}.py").write_bytes(blob)

    (worktree / "ok.py").write_text("OK = True\n", encoding="utf-8")
    await wm.commit_all(worktree, "task: tiny change beside fat venv")

    committed = _committed_files(worktree)
    assert "ok.py" in committed
    assert not any(".venv" in p for p in committed)
    changed = await wm.changed_files(branch)
    assert changed == ["ok.py"]
    # Working tree untouched — harness must not delete user/generated files.
    assert (venv_pkg / "mod_0.py").is_file()


@pytest.mark.asyncio
async def test_commit_all_excludes_nested_venv_node_modules_cache(
    tmp_path: Path,
) -> None:
    """Nested venv/node_modules/cache/build/dist/egg-info must use **/ pathspecs."""
    repo = tmp_path / "repo"
    _init_repo_without_gitignore(repo)
    wm = WorktreeManager(repo, repo / ".worktrees", "main")
    worktree, branch = await wm.create("nested-generated")

    nested_venv = worktree / "nested" / "venv" / "lib"
    nested_venv.mkdir(parents=True, exist_ok=True)
    (nested_venv / "junk.py").write_text("JUNK = 1\n", encoding="utf-8")

    nested_nm = worktree / "apps" / "web" / "node_modules" / "leftpad"
    nested_nm.mkdir(parents=True, exist_ok=True)
    (nested_nm / "index.js").write_text("module.exports = 1\n", encoding="utf-8")

    nested_cache = worktree / "packages" / "cache" / "pip"
    nested_cache.mkdir(parents=True, exist_ok=True)
    (nested_cache / "wheels.db").write_bytes(b"\0" * 32)

    nested_build = worktree / "services" / "api" / "build"
    nested_build.mkdir(parents=True, exist_ok=True)
    (nested_build / "out.js").write_text("built\n", encoding="utf-8")

    nested_dist = worktree / "packages" / "ui" / "dist"
    nested_dist.mkdir(parents=True, exist_ok=True)
    (nested_dist / "index.js").write_text("dist\n", encoding="utf-8")

    egg = worktree / "nested" / "pkg.egg-info"
    egg.mkdir(parents=True, exist_ok=True)
    (egg / "PKG-INFO").write_text("Name: pkg\n", encoding="utf-8")

    (worktree / "src").mkdir()
    (worktree / "src" / "feature.py").write_text("FEATURE = 1\n", encoding="utf-8")
    (worktree / "keep_me.py").unlink()

    await wm.commit_all(worktree, "task: nested excludes + source deletion")

    committed = _committed_files(worktree)
    assert "src/feature.py" in committed
    assert "keep_me.py" not in committed
    assert not any("venv" in p for p in committed)
    assert not any("node_modules" in p for p in committed)
    assert not any("/cache/" in f"/{p}" or p.startswith("cache/") for p in committed)
    assert not any("build" in p for p in committed)
    assert not any("dist" in p for p in committed)
    assert not any(p.endswith(".egg-info/PKG-INFO") or ".egg-info/" in p for p in committed)

    changed = await wm.changed_files(branch)
    assert "src/feature.py" in changed
    assert "keep_me.py" in changed
    assert not any(
        part in p
        for p in changed
        for part in ("venv", "node_modules", "cache", "build", "dist", ".egg-info")
    )

    # Nested generated trees remain on disk (not deleted).
    assert (nested_venv / "junk.py").is_file()
    assert (nested_nm / "index.js").is_file()
    assert (nested_cache / "wheels.db").is_file()


@pytest.mark.asyncio
async def test_uncommitted_diff_excludes_generated(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_without_gitignore(repo)
    wm = WorktreeManager(repo, repo / ".worktrees", "main")
    worktree, _branch = await wm.create("diff-caches")

    _simulate_pytest_caches(worktree)
    (worktree / "src.py").write_text("S = 1\n", encoding="utf-8")

    diff = await wm.uncommitted_diff(worktree)
    assert "src.py" in diff
    assert "__pycache__" not in diff
    assert ".pytest_cache" not in diff
    assert ".pyc" not in diff
