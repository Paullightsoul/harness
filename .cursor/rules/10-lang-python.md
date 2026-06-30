---
description: Language overlay для Python-проектов. Применяется, если профиль проекта language=python.
alwaysApply: false
---

# Language overlay: Python

Применяется поверх конституции (`00-constitution.md`), когда `.harness/project.toml`
содержит `language = "python"`. Каноничный источник правил — `/home/shared-context/`
и `/home/.cursor/rules/python-backend.mdc`.

## Стек
- Python 3.12+. Менеджер: `uv` (или `pip`, если `uv` нет).
- Формат/линт: **ruff** (`ruff format`, `ruff check`).
- Типы: **mypy** strict. Весь новый код типизирован.
- Тесты: **pytest** / `pytest-asyncio`.

## Hard rules (из канона /home)
- FastAPI: все endpoints `async def`.
- SQLAlchemy 2.x **async only** (`AsyncSession`), Alembic для миграций.
- Pydantic v2 для DTO, `pydantic-settings` для конфигов.
- `structlog` (не `print`), `httpx.AsyncClient` (не `requests`), `aiokafka` для Kafka.
- Repository / Service / thin-router. Datetimes timezone-aware. IDs — UUIDv7/ULID.
- Bare `except:` запрещён.

## Типовой профиль `.harness/project.toml`
```toml
language = "python"
canon = "python-backend"

[[gates]]
id = "lint"
cmd = "ruff check ."

[[gates]]
id = "types"
cmd = "mypy ."

[[gates]]
id = "test"
cmd = "pytest -q"

# опционально: гонять гейты в изолированном контейнере (HARNESS_SANDBOX=docker)
[sandbox]
image = "python:3.12"
network = "none"
```
