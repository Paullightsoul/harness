"""Конфигурация durable-плейна: подключение к Postgres (DBOS system database)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Дефолт совпадает с compose.yaml (порт 5439, чтобы не конфликтовать с другими PG на хосте).
_DEFAULT_URL = "postgresql://harness:harness@localhost:5439/harness"


def system_db_url() -> str:
    return os.environ.get("DBOS_SYSTEM_DATABASE_URL", _DEFAULT_URL)


@dataclass
class DurableConfig:
    """Настройки durable-режима. enabled берётся из HARNESS_DURABLE=1."""

    app_name: str = field(default_factory=lambda: os.environ.get("DBOS_APP_NAME", "harness"))
    system_database_url: str = field(default_factory=system_db_url)
    queue_name: str = "harness-tasks"
    max_parallel: int = field(default_factory=lambda: int(os.environ.get("MAX_PARALLEL", "3")))

    @staticmethod
    def is_enabled() -> bool:
        return os.environ.get("HARNESS_DURABLE", "0") == "1"
