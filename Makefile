# Гейты качества. `make check` — единая команда, которую гоняют воркер, ревьюер и цикл.
# Если в репозитории нет src/ или тестов — соответствующий шаг просто не упадёт.

PY_SRC ?= src tests

.PHONY: check lint type test format install setup doctor pg-up pg-down pg-logs pg-psql

check: lint type test ## Полный гейт: линт + типы + тесты

lint: ## ruff: формат-чек + линт
	ruff format --check $(PY_SRC)
	ruff check $(PY_SRC)

type: ## mypy strict
	mypy $(PY_SRC)

test: ## pytest (тихо, останавливаемся на первом фейле)
	pytest -q

format: ## автоформат + автофиксы
	ruff format $(PY_SRC)
	ruff check --fix $(PY_SRC)

install: ## установить инструменты гейтов
	python -m pip install -U ruff mypy pytest

setup: ## bootstrap: venv + зависимости + .env + doctor
	bash scripts/setup.sh

doctor: ## проверка готовности окружения
	python -m harness.interface.cli doctor

# ── durable-плейн: Postgres для DBOS (ADR-0003 Фаза 2) ────────────────────────
DBOS_DB_URL ?= postgresql://harness:harness@localhost:5439/harness

pg-up: ## поднять Postgres (docker compose)
	docker compose up -d

pg-down: ## остановить Postgres (данные сохраняются в volume)
	docker compose down

pg-logs: ## логи Postgres
	docker compose logs -f postgres

pg-psql: ## psql в базу harness
	PGPASSWORD=harness psql -h localhost -p 5439 -U harness -d harness
