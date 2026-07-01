"""v2-034: `EngineTaskExecutor` — реальный `TaskExecutor` (worktree+runner+ProfileGate)
вместо fake.

Unit-тесты самого executor'а на реальном git (мок только runner/gate — дорогие
и недетерминированные части). Гоняются всегда, без внешних зависимостей.
Live end-to-end через `DurableOrchestrator` на настоящем DBOS+Postgres —
отдельный файл `test_durable_executor_live.py` (см. его докстринг почему).
"""
from __future__ import annotations

from pathlib import Path

from durable_executor_helpers import (
    StubGate,
    StubReviewerRunner,
    StubWorkerRunner,
    git_log,
    init_repo,
    make_executor,
    run_git,
    seed_task,
)

from harness.config import Settings
from harness.domain.models import Task
from harness.durable.executor import EngineTaskExecutor, TaskExecutor
from harness.store.repository import Store

# ── setup ────────────────────────────────────────────────────────────────────

def test_setup_creates_worktree_and_updates_task(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo)
    seed_task(executor, "r1", "001", tmp_path)

    executor.setup("r1", "001")

    task = executor._store.get_task("r1", "001")  # noqa: SLF001
    assert task is not None
    assert task.status == "ready"
    assert task.branch == "task/001"
    assert Path(task.worktree_path).exists()


def test_setup_is_idempotent_preserves_prior_commits(tmp_path: Path) -> None:
    """Повторный setup (напр. переигрывание шага после краха) не должен стирать
    уже сделанные коммиты попыток — регресс на `-B` из WorktreeManager.create."""
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo)
    seed_task(executor, "r1", "001", tmp_path)

    executor.setup("r1", "001")
    task = executor._store.get_task("r1", "001")  # noqa: SLF001
    assert task is not None
    worktree = Path(task.worktree_path)
    (worktree / "extra.txt").write_text("attempt 1 work\n", encoding="utf-8")
    run_git(worktree, "add", "-A")
    run_git(worktree, "commit", "-q", "-m", "attempt 1")
    log_before = git_log(worktree)

    executor.setup("r1", "001")  # повторный вызов — идемпотентный no-op

    assert git_log(worktree) == log_before
    assert (worktree / "extra.txt").exists()


# ── worker ───────────────────────────────────────────────────────────────────

def test_worker_calls_runner_and_commits_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    worker = StubWorkerRunner(edit="changed by worker\n")
    executor = make_executor(tmp_path, repo, worker=worker)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    result = executor.worker("r1", "001", 1, feedback="")

    assert result.ok is True
    assert result.cost_credits == 0.5
    assert worker.calls
    assert "тестовая задача" in worker.calls[0]
    task = executor._store.get_task("r1", "001")  # noqa: SLF001
    assert task is not None
    log = git_log(Path(task.worktree_path))
    assert "durable attempt 1" in log


def test_worker_includes_feedback_in_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    worker = StubWorkerRunner()
    executor = make_executor(tmp_path, repo, worker=worker)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    executor.worker("r1", "001", 2, feedback="исправь тест X")

    assert "исправь тест X" in worker.calls[0]


def test_worker_commit_is_idempotent_when_nothing_changed(tmp_path: Path) -> None:
    """Переигрывание worker-шага без новых правок (edit=None) — commit no-op'ится
    (git commit с пустым diff падает тихо, returncode игнорируется)."""
    repo = tmp_path / "repo"
    init_repo(repo)
    worker = StubWorkerRunner(edit=None)
    executor = make_executor(tmp_path, repo, worker=worker)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    executor.worker("r1", "001", 1, feedback="")  # не должно упасть
    executor.worker("r1", "001", 1, feedback="")  # повторный прогон — тоже не должен


# ── gate ─────────────────────────────────────────────────────────────────────

def test_gate_reflects_stub_result(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo, gate=StubGate(passed=False))
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    assert executor.gate("r1", "001") is False


# ── review ───────────────────────────────────────────────────────────────────

def test_review_approve_has_no_feedback(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo, reviewer=StubReviewerRunner("VERDICT: APPROVE"))
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    result = executor.review("r1", "001", 1, gates_passed=True)

    assert result.verdict == "approve"
    assert result.feedback == ""


def test_review_changes_carries_feedback(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    reviewer = StubReviewerRunner("отчёт...\nVERDICT: CHANGES\nисправь X")
    executor = make_executor(tmp_path, repo, reviewer=reviewer)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    result = executor.review("r1", "001", 1, gates_passed=False)

    assert result.verdict == "changes"
    assert "исправь X" in result.feedback


def test_review_malformed_verdict_defaults_to_changes(tmp_path: Path) -> None:
    """Базовый ADR-0003 контракт (без v2-006 unparsed-эскалации durable-плейна):
    отсутствие VERDICT — безопасный дефолт CHANGES, не APPROVE."""
    repo = tmp_path / "repo"
    init_repo(repo)
    reviewer = StubReviewerRunner("ревьюер ничего не написал по делу")
    executor = make_executor(tmp_path, repo, reviewer=reviewer)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    result = executor.review("r1", "001", 1, gates_passed=True)

    assert result.verdict == "changes"


# ── merge ────────────────────────────────────────────────────────────────────

def test_merge_succeeds_and_updates_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    worker = StubWorkerRunner(edit="merged content\n")
    executor = make_executor(tmp_path, repo, worker=worker)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")
    executor.worker("r1", "001", 1, feedback="")

    merged = executor.merge("r1", "001")

    assert merged is True
    assert (repo / "f.txt").read_text(encoding="utf-8") == "merged content\n"


def test_merge_is_idempotent_on_already_merged_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo, worker=StubWorkerRunner(edit="x\n"))
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")
    executor.worker("r1", "001", 1, feedback="")

    assert executor.merge("r1", "001") is True
    assert executor.merge("r1", "001") is True  # git: "Already up to date" — не ошибка


def test_merge_returns_false_on_real_conflict(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo)
    seed_task(executor, "r1", "001", tmp_path)
    executor._store.upsert_task(Task(  # noqa: SLF001
        id="002", run_id="r1", title="t", spec_path=str(tmp_path / "task-002.md"),
        status="pending",
    ))
    (tmp_path / "task-002.md").write_text("# spec\n", encoding="utf-8")
    for tid in ("001", "002"):
        executor.setup("r1", tid)

    t1 = executor._store.get_task("r1", "001")  # noqa: SLF001
    t2 = executor._store.get_task("r1", "002")  # noqa: SLF001
    assert t1 is not None and t2 is not None
    Path(t1.worktree_path, "f.txt").write_text("from 001\n", encoding="utf-8")
    run_git(Path(t1.worktree_path), "commit", "-aqm", "001 change")
    Path(t2.worktree_path, "f.txt").write_text("from 002\n", encoding="utf-8")
    run_git(Path(t2.worktree_path), "commit", "-aqm", "002 change")

    assert executor.merge("r1", "001") is True
    assert executor.merge("r1", "002") is False  # конфликтует с уже влитым 001


def test_merge_post_merge_gate_gates_the_result(tmp_path: Path) -> None:
    """Мерж прошёл без git-конфликта, но post-merge гейт (на base) красный →
    merge() возвращает False, как «зелёные по отдельности» != «зелёный base»."""
    repo = tmp_path / "repo"
    init_repo(repo)
    gate = StubGate(passed=True)
    executor = make_executor(tmp_path, repo, worker=StubWorkerRunner(edit="x\n"), gate=gate)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")
    executor.worker("r1", "001", 1, feedback="")
    gate.passed = False  # гейт «портится» перед пост-мерж проверкой

    assert executor.merge("r1", "001") is False


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_removes_worktree_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")
    task = executor._store.get_task("r1", "001")  # noqa: SLF001
    assert task is not None
    worktree = Path(task.worktree_path)
    assert worktree.exists()

    executor.teardown("r1", "001", merged=True)

    assert not worktree.exists()


def test_teardown_is_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo)
    seed_task(executor, "r1", "001", tmp_path)
    executor.setup("r1", "001")

    executor.teardown("r1", "001", merged=True)
    executor.teardown("r1", "001", merged=True)  # повторный вызов — не должен упасть


# ── max_attempts / контракт ──────────────────────────────────────────────────

def test_max_attempts_reflects_settings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    settings = Settings(root=tmp_path, max_attempts=7)
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "worker.md").write_text("# w\n", encoding="utf-8")
    (prompts / "reviewer.md").write_text("# r\n", encoding="utf-8")
    executor = EngineTaskExecutor(
        settings, Store(tmp_path / "state.db"), repo_root=repo,
        worker_runner=StubWorkerRunner(),  # type: ignore[arg-type]
        reviewer_runner=StubReviewerRunner("x"),  # type: ignore[arg-type]
        gate=StubGate(),  # type: ignore[arg-type]
    )
    assert executor.max_attempts == 7


def test_executor_satisfies_task_executor_protocol(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    executor = make_executor(tmp_path, repo)
    assert isinstance(executor, TaskExecutor)


# ── mini end-to-end (без DBOS): setup→worker→gate→review→merge→teardown ────

def test_full_cycle_without_dbos(tmp_path: Path) -> None:
    """Прогон одной задачи через все шаги вручную (не через DurableOrchestrator) —
    доказывает, что реальная реализация (не fake) работает целиком."""
    repo = tmp_path / "repo"
    init_repo(repo)
    worker = StubWorkerRunner(edit="реальная фича\n")
    reviewer = StubReviewerRunner("VERDICT: APPROVE")
    executor = make_executor(tmp_path, repo, worker=worker, reviewer=reviewer)
    seed_task(executor, "r1", "001", tmp_path)

    executor.setup("r1", "001")
    wres = executor.worker("r1", "001", 1, feedback="")
    assert wres.ok
    gates_passed = executor.gate("r1", "001")
    assert gates_passed
    review = executor.review("r1", "001", 1, gates_passed)
    assert review.verdict == "approve"
    merged = executor.merge("r1", "001")
    assert merged
    executor.teardown("r1", "001", merged=True)

    assert (repo / "f.txt").read_text(encoding="utf-8") == "реальная фича\n"
    assert not (repo / ".worktrees" / "task-001").exists()
