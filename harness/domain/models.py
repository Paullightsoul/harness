"""Dataclass-модели домена. Чистые данные без поведения — поведение в движке."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Run:
    """Один прогон цели: планирование -> исполнение задач -> интеграция."""

    id: str
    project: str
    goal: str
    status: str  # RunStatus
    base_branch: str = "main"
    budget_credits: float | None = None
    spent_credits: float = 0.0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class Task:
    """Узел DAG. depends_on — id задач этого же Run. provides — контракт для зависимых."""

    id: str             # короткий id внутри run, напр. "003"
    run_id: str
    title: str
    spec_path: str      # путь к tasks/task-<id>.md
    status: str         # TaskStatus
    depends_on: list[str] = field(default_factory=list)
    provides: str = ""  # интерфейс, который задача отдаёт зависимым (инжектится им в контекст)
    complexity: str = "normal"  # "normal" | "high" — high стартует лестницу сразу с kimi
    attempts: int = 0
    branch: str = ""
    worktree_path: str = ""
    note: str = ""      # причина blocked / эскалации
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class Attempt:
    """Одна попытка воркера: worker -> gates -> review."""

    id: int             # автоинкремент
    task_id: str
    run_id: str
    number: int
    model: str          # фактическая модель воркера (с учётом эскалации)
    worker_output: str = ""
    gates_passed: bool | None = None
    verdict: str | None = None  # Verdict
    cost_credits: float = 0.0
    started_at: str = field(default_factory=_now)
    finished_at: str = ""


@dataclass
class Review:
    """Структурный вердикт ревьюера по попытке."""

    attempt_id: int
    task_id: str
    verdict: str        # Verdict
    report: str         # markdown-отчёт
    feedback: str = ""  # что исправить (идёт воркеру следующей попытки)
    created_at: str = field(default_factory=_now)


@dataclass
class Event:
    """Запись append-only журнала. Источник правды для resume и наблюдаемости."""

    id: int
    run_id: str
    type: str           # EventType
    task_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=_now)

    def detail_json(self) -> str:
        return json.dumps(self.detail, ensure_ascii=False)
