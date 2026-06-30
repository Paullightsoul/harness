"""Durable-плейн поверх DBOS+Postgres (ADR-0003, Фаза 2).

Опциональный модуль: импортируется только в durable-режиме. Базовый пакет
`harness` остаётся импортируемым без установленного `dbos` (как и с `cursor-sdk`).
Включается флагом окружения; иначе движок работает на SQLite-сторе как раньше.
"""

from __future__ import annotations

__all__ = ["DurableConfig", "TaskExecutor", "WorkerStepResult", "ReviewStepResult"]

from harness.durable.config import DurableConfig
from harness.durable.executor import ReviewStepResult, TaskExecutor, WorkerStepResult
