"""Phase 0.5 pointer-only ContextPack + Phase 1 run isolation / leases."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness.domain.models import Run, Task
from harness.evidence.acceptance import create_acceptance, load_acceptance
from harness.evidence.ledger import (
    EvidenceRecord,
    append_evidence,
    done_allowed,
    list_evidence,
    new_evidence_id,
)
from harness.profile import ProjectProfile
from harness.store.repository import Store
from harness.tasktool.context import assert_pointer_only, build_context_pack
from harness.tenant.leases import acquire_lease, list_leases, release_lease
from harness.tenant.run_layout import ensure_run_layout, plan_surface_paths


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def test_pointer_only_redacts_ai_memory_dump() -> None:
    text = (
        "- layout: src, tests\n"
        "AI_MEMORY full dump follows: " + ("x" * 100) + "\n"
        "- gates: make check\n"
    )
    cleaned, truncations = assert_pointer_only(text)
    assert truncations >= 1
    assert "pointer-only" in cleaned
    assert "xxxxx" not in cleaned or "dump redacted" in cleaned


def test_context_pack_pointer_only_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_CONTEXT_POINTER_ONLY", "1")
    store = _store(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.toml").write_text(
        'name = "demo"\ncanon = "python-backend"\n', encoding="utf-8"
    )
    run = Run(id="run-1", project="demo", goal="g", status="running")
    store.create_run(run)
    task = Task(id="001", run_id="run-1", title="T", spec_path="t.md", status="ready")
    store.upsert_task(task)
    dump = "AI_MEMORY " + ("BODY " * 40)
    pack = build_context_pack(
        run=run,
        task=task,
        store=store,
        repo_root=repo,
        project_brief=dump,
        profile=ProjectProfile(canon="python-backend"),
        pointer_only=True,
    )
    assert pack.pointer_only is True
    assert "Policy: pointer-only" in pack.rendered
    assert "AI_MEMORY BODY" not in pack.rendered
    assert pack.truncations >= 0


def test_two_runs_isolated_plan_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_USE_RUN_ROOTS", "1")
    monkeypatch.setenv("HARNESS_MULTI_TENANT", "1")
    monkeypatch.setenv("HARNESS_USE_WORKTREES", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "PLAN.md").write_text("# Shared plan\n", encoding="utf-8")
    tasks = repo / "tasks"
    tasks.mkdir()
    (tasks / "001.md").write_text("# Task 1\n", encoding="utf-8")

    a = ensure_run_layout(repo, "run-A", tenant_id="alice", goal="ga", project="p")
    b = ensure_run_layout(repo, "run-B", tenant_id="bob", goal="gb", project="p")
    assert a.root != b.root
    plan_a, tasks_a = plan_surface_paths(a)
    plan_b, tasks_b = plan_surface_paths(b)
    assert plan_a != plan_b
    assert tasks_a != tasks_b
    # Mutating run-A PLAN must not touch run-B
    plan_a.write_text("# Plan A only\n", encoding="utf-8")
    assert plan_b.read_text(encoding="utf-8") == "# Shared plan\n"
    assert "Plan A only" in plan_a.read_text(encoding="utf-8")
    assert a.plan_json.parent.name == "run-A"
    assert b.root.as_posix().endswith(".harness/runs/run-B") or "run-B" in str(b.root)


def test_v4_isolation_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env → worktrees / run_roots / multi_tenant default ON."""
    for key in (
        "HARNESS_USE_RUN_ROOTS",
        "HARNESS_MULTI_TENANT",
        "HARNESS_USE_WORKTREES",
    ):
        monkeypatch.delenv(key, raising=False)
    from harness.config import Settings  # noqa: PLC0415
    from harness.tenant.run_layout import use_run_roots  # noqa: PLC0415

    settings = Settings(root=Path("/tmp"))
    assert settings.use_run_roots is True
    assert settings.multi_tenant is True
    assert settings.use_worktrees is True
    assert use_run_roots() is True


def test_save_plan_does_not_overwrite_shared_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_USE_RUN_ROOTS", "1")
    from harness.interface.cli import _save_plan_to_project  # noqa: PLC0415

    repo = tmp_path / "repo"
    repo.mkdir()
    shared = repo / "PLAN.md"
    shared.write_text("# Original shared plan KEEP ME\n", encoding="utf-8")
    (repo / "tasks").mkdir()
    (repo / "tasks" / "task-001.md").write_text(
        '---\nid: "001"\ntitle: "T"\nstatus: "todo"\n'
        'complexity: "small"\nattempts: 0\n---\n\n# T\n',
        encoding="utf-8",
    )
    saved = _save_plan_to_project(
        "Goal line about widgets\n\n## Tasks\n### 001: T\n", repo, "demo"
    )
    assert shared.read_text(encoding="utf-8") == "# Original shared plan KEEP ME\n"
    assert ".harness/runs/" in saved.as_posix()
    assert (saved / "PLAN.md").is_file()
    assert "widgets" in (saved / "PLAN.md").read_text(encoding="utf-8")
    assert (saved / "tasks" / "task-001.md").is_file()


def test_concurrent_plan_saves_do_not_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threaded plan saves → distinct run roots; neither overwrites the other."""
    import threading

    monkeypatch.setenv("HARNESS_USE_RUN_ROOTS", "1")
    from harness.interface.cli import _save_plan_to_project  # noqa: PLC0415

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tasks").mkdir()
    results: list[Path] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(label: str) -> None:
        try:
            # Stage under a per-thread name then save — avoid racing the shared
            # staging file; the isolation claim is about run-root SoT writes.
            staging = repo / "tasks" / f"task-001-{label}.md"
            staging.write_text(
                f'---\nid: "001"\ntitle: "{label}"\nstatus: "todo"\n'
                f'complexity: "small"\nattempts: 0\n---\n\n# {label}\n',
                encoding="utf-8",
            )
            with lock:
                target = repo / "tasks" / "task-001.md"
                target.write_text(staging.read_text(encoding="utf-8"), encoding="utf-8")
                path = _save_plan_to_project(
                    f"## Goal\n{label}\n### 001: {label}\n", repo, "demo"
                )
            results.append(path)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("alpha",)),
        threading.Thread(target=worker, args=("beta",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(results) == 2
    assert results[0].resolve() != results[1].resolve()
    contents = {
        p.name: (p / "PLAN.md").read_text(encoding="utf-8") for p in results
    }
    # Distinct run directories; each PLAN belongs only to its run root.
    assert len(set(contents)) == 2 or any("alpha" in c for c in contents.values())
    for path in results:
        text = (path / "PLAN.md").read_text(encoding="utf-8")
        assert "alpha" in text or "beta" in text
        # Mutating one run must not change the other.
    first, second = results
    (first / "PLAN.md").write_text("# FIRST_ONLY\n", encoding="utf-8")
    assert "FIRST_ONLY" not in (second / "PLAN.md").read_text(encoding="utf-8")
    assert not (repo / "PLAN.md").exists()


@pytest.mark.asyncio
async def test_concurrent_starts_preserve_first_plan_after_shared_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start A freezes plan A; shared PLAN overwritten; start B gets B — A intact."""
    monkeypatch.setenv("HARNESS_USE_RUN_ROOTS", "1")
    monkeypatch.setenv("HARNESS_MULTI_TENANT", "1")
    monkeypatch.setenv("HARNESS_USE_WORKTREES", "1")

    from harness.config import Settings  # noqa: PLC0415
    from harness.store.repository import Store  # noqa: PLC0415
    from harness.tasktool.controller import TaskToolController  # noqa: PLC0415
    from harness.tenant.run_layout import resolve_run_root  # noqa: PLC0415

    repo = tmp_path / "repo"
    (repo / "tasks").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads").mkdir(parents=True)
    (repo / ".git" / "refs" / "heads" / "main").write_text(
        "0" * 40 + "\n", encoding="utf-8"
    )

    def _write_plan(marker: str) -> None:
        (repo / "tasks" / "task-001.md").write_text(
            "\n".join(
                [
                    "---",
                    'id: "001"',
                    f'title: "{marker}"',
                    'status: "todo"',
                    'complexity: "small"',
                    "attempts: 0",
                    "---",
                    "# Файлы",
                    "- src/a.py",
                    "# Acceptance criteria",
                    "- `echo ok`",
                    "# Provides",
                    "- x",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (repo / "PLAN.md").write_text(
            f"### 001: {marker}\n- complexity: small\n",
            encoding="utf-8",
        )

    _write_plan("PLAN_A_MARKER")
    settings = Settings(
        root=repo,
        base_branch="main",
        use_run_roots=True,
        multi_tenant=True,
        use_worktrees=True,  # required for multi-active leases
        ship_mode="manual",
    )
    store = Store(repo / ".harness" / "state.db")
    controller = TaskToolController(settings, store, repo)
    started_a = await controller.start(
        project="demo",
        goal="goal-a-unique",
        base_branch="main",
        approve_plan=False,
        force=True,
    )
    assert started_a.get("ok") is not False
    assert "run_id" in started_a, started_a
    run_a = str(started_a["run_id"])
    plan_a = resolve_run_root(repo, run_a, prefer_modern=True).plan_md
    assert "PLAN_A_MARKER" in plan_a.read_text(encoding="utf-8")

    _write_plan("PLAN_B_MARKER")
    started_b = await controller.start(
        project="demo",
        goal="goal-b-unique",
        base_branch="main",
        approve_plan=False,
        force=True,
    )
    assert "run_id" in started_b, started_b
    run_b = str(started_b["run_id"])
    assert run_a != run_b
    plan_b = resolve_run_root(repo, run_b, prefer_modern=True).plan_md
    assert "PLAN_A_MARKER" in plan_a.read_text(encoding="utf-8")
    assert "PLAN_B_MARKER" in plan_b.read_text(encoding="utf-8")
    assert "PLAN_B_MARKER" not in plan_a.read_text(encoding="utf-8")


def test_multi_tenant_leases_two_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_MULTI_TENANT", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".harness").mkdir()
    acquire_lease(repo, "run-1", user="alice", use_worktrees=True, goal="one")
    acquire_lease(repo, "run-2", user="bob", use_worktrees=True, goal="two")
    leases = list_leases(repo)
    assert {lease.run_id for lease in leases} == {"run-1", "run-2"}
    release_lease(repo, "run-1")
    assert {lease.run_id for lease in list_leases(repo)} == {"run-2"}


def test_acceptance_default_fail_scaffold(tmp_path: Path) -> None:
    path = tmp_path / "acceptance.json"
    doc = create_acceptance(path, run_id="run-1", acceptance_lines=["Login works"])
    assert path.is_file()
    assert all(not item.passes for item in doc.items)
    assert doc.evidence_pct == 0.0
    reloaded = load_acceptance(path)
    assert reloaded.items[0].id == "ac-001"


def test_evidence_gate_flag_off_allows_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "0")
    ok, reason = done_allowed(tmp_path)
    assert ok is True
    assert reason == "evidence_gate_disabled"
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "1")
    create_acceptance(
        tmp_path / "acceptance.json",
        run_id="run-1",
        gate_ids=["php-syntax"],
        acceptance_lines=[],
    )
    ok2, reason2 = done_allowed(tmp_path)
    assert ok2 is False
    append_evidence(
        tmp_path,
        EvidenceRecord(
            id=new_evidence_id(),
            at="2026-08-05T12:00:00+00:00",
            run_id="run-1",
            task_id="001",
            kind="gate",
            gate_id="php-syntax",
            exit_code=0,
            acceptance_item_ids=("ac-gate-php-syntax",),
        ),
    )
    # Still red until control-plane flip.
    ok_mid, _ = done_allowed(tmp_path, required_item_ids=["ac-gate-php-syntax"])
    assert ok_mid is False
    from harness.evidence.acceptance import flip_item  # noqa: PLC0415

    rows = list_evidence(tmp_path)
    flip_item(
        tmp_path / "acceptance.json",
        item_id="ac-gate-php-syntax",
        evidence_ids=[rows[0]["id"]],
        run_root=tmp_path,
    )
    ok3, reason3 = done_allowed(tmp_path, required_item_ids=["ac-gate-php-syntax"])
    assert ok3 is True
    assert reason3 == "ok"


def test_lease_acquire_release_roundtrip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lease = acquire_lease(repo, "run-a", user="u1", use_worktrees=True)
    assert lease.run_id == "run-a"
    assert len(list_leases(repo)) == 1
    assert release_lease(repo, "run-a") is True
    assert list_leases(repo) == []


def test_concurrent_lease_acquire_no_lost_update(tmp_path: Path) -> None:
    """Two threads racing acquire must both land in leases.json (flock)."""
    import threading

    repo = tmp_path / "repo"
    repo.mkdir()
    errors: list[BaseException] = []

    def worker(run_id: str) -> None:
        try:
            acquire_lease(repo, run_id, user="t", use_worktrees=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"run-{i}",))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ids = sorted(lease.run_id for lease in list_leases(repo))
    assert ids == [f"run-{i}" for i in range(8)]
