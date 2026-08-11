"""v2-002: harness plan --project запускает оркестратора в CWD целевого репо,
не в доме harness. Без --project fallback на settings.root (обратная совместимость).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from harness.config import Settings
from harness.interface import cli as cli_module
from harness.projects.registry import Project, ProjectRegistry
from harness.runner.base import AgentResult


class _RecordingRunner:
    """Мок runner: записывает (prompt, model, cwd) для assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    async def run(
        self, prompt: str, *, model: str, cwd: Path, log_path: Path | None = None,
        progress_callback: object = None,
    ) -> AgentResult:
        self.calls.append((prompt, model, cwd))
        return AgentResult(ok=True, text="PLAN stub", cost_credits=0.0)


def _stub_settings(tmp_path: Path) -> Settings:
    """Settings с минимальными stub-файлами в tmp_path (prompts/, tasks/_TEMPLATE.md)."""
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "orchestrator.md").write_text("# orchestrator stub\n", encoding="utf-8")
    tasks = tmp_path / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "_TEMPLATE.md").write_text("# template stub\n", encoding="utf-8")
    return Settings(root=tmp_path)


def test_plan_with_project_uses_repo_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _stub_settings(tmp_path)
    # Регистрируем проект api → /srv/api (tmp_path/api)
    repo = tmp_path / "api-repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir()  # имитация git-репо
    reg_path = settings.root / "projects.json"
    ProjectRegistry(reg_path).add(
        Project(name="api", repo_path=str(repo), base_branch="main"),
    )

    recording = _RecordingRunner()
    monkeypatch.setattr(cli_module, "build_runner", lambda kind, role=None: recording)

    args = argparse.Namespace(goal="добавить REST API", project="api")
    rc = cli_module.cmd_plan(settings, args)

    assert rc == 0
    assert len(recording.calls) == 1
    _, _, cwd = recording.calls[0]
    assert cwd == repo, f"orchestrator CWD должен быть {repo}, не {cwd}"
    # Промпт содержит REPO ROOT блок
    prompt, _, _ = recording.calls[0]
    assert "=== REPO ROOT" in prompt
    assert str(repo) in prompt


def test_plan_without_project_falls_back_to_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обратная совместимость: без --project CWD = settings.root."""
    settings = _stub_settings(tmp_path)
    recording = _RecordingRunner()
    monkeypatch.setattr(cli_module, "build_runner", lambda kind, role=None: recording)

    args = argparse.Namespace(goal="цель", project=None)
    rc = cli_module.cmd_plan(settings, args)

    assert rc == 0
    _, _, cwd = recording.calls[0]
    assert cwd == settings.root


def test_plan_subparser_accepts_project_flag(tmp_path: Path) -> None:
    """Регресс: --project присутствует в plan subparser."""
    _stub_settings(tmp_path)  # настроить каталоги, чтобы Settings не падал
    parser = cli_module.build_parser()
    args = parser.parse_args(["plan", "цель", "--project", "api"])
    assert args.command == "plan"
    assert args.project == "api"
    assert args.goal == "цель"
