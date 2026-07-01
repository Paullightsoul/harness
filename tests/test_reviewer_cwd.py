"""v2-001: reviewer запускается в worktree воркера, не в доме harness.

Проверяем два уровня:
  1. Prompt содержит блок `WORKTREE (твой CWD)` с путём worktree — ревьюер знает,
     что гейты можно гонять в CWD.
  2. Статический assertion: в source `engine.py` вызов `_reviewer_runner.run(...)`
     использует `cwd=worktree`, не `cwd=self._s.root`. End-to-end _process_task
     требует mock'а worktree/gates/runner — тяжелее; статическая проверка достаточна
     для регрессии (diff строки легко поймать, если кто-то вернёт старый CWD).
"""
from __future__ import annotations

import inspect
from pathlib import Path

from harness.config import Settings
from harness.domain.models import Task
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def _engine_with_stub_prompts(tmp_path: Path) -> Engine:
    """Engine с минимальными stub-промптами в tmp_path — изолирует от реального root."""
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "reviewer.md").write_text("# reviewer stub\n", encoding="utf-8")
    (prompts / "worker.md").write_text("# worker stub\n", encoding="utf-8")
    (prompts / "orchestrator.md").write_text("# orchestrator stub\n", encoding="utf-8")
    store = Store(tmp_path / "state.db")
    return Engine(Settings(root=tmp_path), store)


def test_reviewer_prompt_includes_worktree_block(tmp_path: Path) -> None:
    engine = _engine_with_stub_prompts(tmp_path)
    task = Task(
        id="001", run_id="r1", title="t",
        spec_path=str(tmp_path / "spec.md"), status="review",
    )
    (tmp_path / "spec.md").write_text("# spec\n", encoding="utf-8")

    prompt = engine._reviewer_prompt(
        task, gates_ok=True, gate_tail="ok", diff="diff --git a b",
        worktree=Path("/tmp/worktree-001"),
    )
    assert "=== WORKTREE (твой CWD) ===" in prompt
    assert "/tmp/worktree-001" in prompt
    assert "Гейты можно гонять прямо тут" in prompt


def test_reviewer_prompt_without_worktree_omits_block(tmp_path: Path) -> None:
    """Обратная совместимость: worktree=None — блок не добавляется."""
    engine = _engine_with_stub_prompts(tmp_path)
    task = Task(
        id="001", run_id="r1", title="t",
        spec_path=str(tmp_path / "spec.md"), status="review",
    )
    (tmp_path / "spec.md").write_text("# spec\n", encoding="utf-8")

    prompt = engine._reviewer_prompt(
        task, gates_ok=True, gate_tail="ok", diff="d", worktree=None
    )
    assert "WORKTREE (твой CWD)" not in prompt


def test_reviewer_runner_call_uses_worktree_cwd() -> None:
    """Регресс: в source engine.py вызов reviewer_runner.run использует cwd=worktree."""
    src = inspect.getsource(Engine._process_task)
    assert "cwd=worktree" in src, (
        "reviewer_runner.run должен использовать cwd=worktree, не cwd=self._s.root"
    )
    assert "cwd=self._s.root" not in src, (
        "старый cwd=self._s.root для reviewer должен быть убран из _process_task"
    )
