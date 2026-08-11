"""Harness: детерминированный control plane вокруг стохастических агентов Cursor.

Слои:
    domain/        — модель предметной области + конечный автомат задачи
    store/         — SQLite-состояние и append-only журнал событий (resume)
    runner/        — абстракция над агентом (Cursor SDK / cursor-agent CLI)
    gates/         — проверки качества (make check и др., pluggable)
    worktree/      — изоляция задач через git worktree + merge queue
    tasks_io/      — парсинг PLAN.md и tasks/*.md в граф задач
    policy/        — бюджет и лестница эскалации моделей
    scheduler/     — DAG-планировщик + асинхронный движок-автомат
    interface/     — CLI, реестр проектов, нотификации
"""

from __future__ import annotations

__version__ = "4.0.0-dev"
