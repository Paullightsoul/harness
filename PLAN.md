# PLAN — ZY-2nd-flight: починка монорепо + расплод на 4 standalone-репозитория

> Источник правды по декомпозиции. Монорепо: `/home/ZY-2nd-flight`, ветка `agents/zy-r2-initial`
> (base для будущего merge — `main`, GitHub `funtech-group/ZY-2nd-flight`).
> Все факты ниже проверены прогоном `make lint` / `make test` / `mypy` по фактическому репозиторию.

## Цель

1. **Phase A** — устранить CI-блокеры монорепо так, чтобы `cd /home/ZY-2nd-flight && make lint && make test` были зелёными. БЕЗ `git push` / `gh` / merge.
2. **Phase B** — выделить 4 самостоятельных репозитория (`zy-sdk`, `zy-api-service`, `zy-task-service`, `zy-admin-service`), у каждого свой `pyproject.toml` (standalone, без `[tool.uv.workspace]`), `Dockerfile`, `.github/workflows/ci.yml`, `README.md`, `.harness/project.toml`, `git init` + initial commit.
3. **Phase C** — локально прогнать CI-эквивалент (`make lint`, `make test`, `docker build`) для монорепо и каждого из 4 репо.
4. **Human-gate** — `gh repo create` / `git push` / создание PR и merge выполняет ЧЕЛОВЕК. Воркер только готовит чеклист в `reviews/`.

## Фактическое состояние репозитория (проверено)

| Что | Факт |
|-----|------|
| Структура | `services/{api-service,task-service,admin-service}` + `libs/sdk` (пакет `zy-sdk`). uv-workspace, `members = ["services/*", "libs/*"]`. |
| Gates (`.harness/project.toml`) | `lint = make lint`, `test = make test`. |
| `make lint` | `uv run ruff check services libs` (✅ проходит) + `uv run mypy services libs` (❌ падает). |
| `make test` | per-member `pytest --import-mode=importlib` по каждому члену + корневой `tests/` (❌ падает на `test_scaffold`). |
| Версия SDK | `libs/sdk/pyproject.toml` и `__init__.py` → `0.1.0`. Канон = **0.1.0**. |
| JWT | api-service → `JWT_SECRET_KEY` (канон), task-service → `JWT_SECRET` (чинить task-service). |

### Подтверждённые блокеры

1. **test_scaffold.** `tests/test_scaffold.py::test_zy_sdk_importable` ждёт `zy_sdk.__version__ == "0.0.0"`, фактически `0.1.0`. Канон 0.1.0 → чиним ТЕСТ.
2. **mypy не в CI.** `.github/workflows/ci.yml` job `lint` гоняет только `ruff check` + `ruff format --check` (без mypy). `make lint` и README заявляют ruff + mypy. → добавить шаг mypy в CI.
3. **mypy падает (duplicate module + реальные ошибки типов).** Это глубже, чем «перенести admin conftest». Проверено:
   - `uv run mypy services libs` падает на `Duplicate module named "conftest"` (`services/admin-service/conftest.py` ↔ `services/api-service/tests/conftest.py`). За ним — `Duplicate module named "tests"` (несколько `tests/__init__.py`). Все коллизии — в тестовых каталогах.
   - mypy сейчас умирает на первой же коллизии и **фактически не проверяет код вообще**. После снятия коллизии всплывают реальные ошибки типов в `src` (см. ниже). Тестовые файлы дают ~120 ошибок `no-untyped-def` (тесты исторически вообще не проверялись).
   - **Решение (см. Архитектурные решения):** mypy проверяет ТОЛЬКО `src` (исключаем `tests/` из mypy), admin `conftest.py` переносим в `tests/` (единый layout). После этого `mypy services libs` чек-листит 152 src-файла и оставляет ровно **7 реальных ошибок в `src`** (5 в `libs/sdk`, 2 в `services/admin-service/manage.py`) — их и чиним.
4. **JWT env mismatch.** task-service использует `JWT_SECRET` (`core/config.py:34`, `core/auth.py:84`, `tests/test_tasks.py` ×4, `tests/test_bonus_api.py` ×1). Канон — `JWT_SECRET_KEY`. api-service уже на `JWT_SECRET_KEY` — не трогать.

### Реальные mypy-ошибки в `src` (после исключения tests/) — проверено `mypy services libs --exclude '(^|/)tests/'`

| Файл | Строка | Код | Суть |
|------|--------|-----|------|
| `libs/sdk/src/zy_sdk/broker/events.py` | 63 | `call-arg` | `cls(...)` без `event_id` (поле с `default_factory`, mypy без pydantic-плагина видит как обязательное). |
| `libs/sdk/src/zy_sdk/redis/lock.py` | 59 | `assignment` | `acquired: bool = await self._redis.set(...)` (правый тип `bool|str|bytes|None`). |
| `libs/sdk/src/zy_sdk/settings.py` | 51 | `no-any-return` | `return logging.getLevelName(...)` возвращает `Any`. |
| `libs/sdk/src/zy_sdk/settings.py` | 69 | `prop-decorator` | `@computed_field` поверх `@property`. |
| `libs/sdk/src/zy_sdk/broker/init.py` | 27 | `import-untyped` | `import aiokafka` без stub/`py.typed`. |
| `services/admin-service/manage.py` | 8 | `no-untyped-def` | `def main():` без `-> None`. |
| `services/admin-service/manage.py` | 23 | `no-untyped-call` | вызов нетипизированного `main()` (уйдёт после фикса строки 8). |

> api-service (`src`+tests) и task-service `src` под mypy **чистые** — отдельных задач не требуют.

## Архитектурные решения

### Phase A

| Область | Решение |
|---------|---------|
| Версия SDK | Канон `0.1.0`. Правим ТЕСТ `tests/test_scaffold.py` (`== "0.1.0"`), не SDK. |
| JWT | Единое `JWT_SECRET_KEY` во всех FastAPI-сервисах. В task-service переименовать `JWT_SECRET`→`JWT_SECRET_KEY` (config, auth, оба теста) и добавить `JWT_SECRET_KEY=` в `.env.example`. api-service не трогаем. |
| mypy scope | **mypy проверяет только `src` (production-код), `tests/` исключаются** из mypy. Обоснование: (а) duplicate-module коллизии все в тестах; (б) тесты исторически не типизировались (mypy умирал раньше) — приводить ~120 тест-функций к strict сейчас вне scope разблокировки CI; (в) `src` остаётся под strict mypy. Реализация: добавить `"(^\|/)tests/"` в `[tool.mypy] exclude` корневого `pyproject.toml`. |
| admin conftest | Перенести `services/admin-service/conftest.py` → `services/admin-service/tests/conftest.py` (как у api/task/sdk). Снимает root-level коллизию «conftest» и попадает под исключение `tests/`. pytest по-прежнему его подхватывает (conftest в каталоге тестов). |
| Фиксы типов в src | 7 ошибок (таблица выше) чиним точечно и file-local (без pydantic-плагина, чтобы не плодить новые ошибки): `# type: ignore[<code>]` с комментарием-объяснением там, где это про сторонние/декораторные ограничения; `int(...)` для `no-any-return`; снять явную аннотацию `: bool` в `lock.py`; добавить `-> None` в `manage.py`. |
| CI lint | В job `lint` добавить шаг `uv run mypy services libs` (после ruff). `ruff format --check` сохраняем. Job переименовать в `lint (ruff + mypy)`. |
| Запрет | Никаких `git push` / `gh` / merge. Не менять бизнес-логику в `src` (кроме точечных type-фиксов). |

### Phase B — standalone-репозитории

| Решение | Детали |
|---------|--------|
| Физическое расположение | Каталоги `/home/zy-sdk`, `/home/zy-api-service`, `/home/zy-task-service`, `/home/zy-admin-service`. Симлинки `/home/projects/zy-*` → `/home/zy-*` (как у остальных проектов в `/home/projects/`). |
| Зависимость на zy-sdk (локаль) | В `pyproject.toml` сервисов: `[tool.uv.sources] zy-sdk = { path = "../zy-sdk" }` (sibling-layout: репо лежат рядом под `/home`). git/tag-dep — отложено на human-gate (после публикации). |
| Dev-зависимости | В монорепо ruff/mypy/pytest приходили из корневого `[dependency-groups] dev`. В каждом standalone-репо объявить свою `[dependency-groups] dev` (ruff, mypy, pytest, pytest-asyncio; для admin — pytest-django, django-stubs; для task/sdk — aiosqlite где нужно). |
| ruff/mypy config | Перенести релевантную часть конфигов из корневого `pyproject.toml` в каждый standalone (strict mypy, exclude tests, ruff select; admin — django-stubs plugin + `django_settings_module`). |
| Docker build context | **Buildx named build-context**, без `COPY ../` и без гигантского контекста `/home`. Сервис: контекст = каталог сервиса, sdk подаётся как доп. контекст: `docker build --build-context zy_sdk=/home/zy-sdk -t <svc>:test .`; в Dockerfile `COPY --from=zy_sdk . ./zy-sdk/` (раскладка sibling внутри образа, чтобы `../zy-sdk` из path-dep резолвился). Адаптировано из `deploy/docker/*/Dockerfile`. |
| zy-sdk Docker | Лёгкий multi-stage (builder `uv sync` → runtime venv). CMD — no-op/`python -c "import zy_sdk"`; достаточно успешного `docker build`. |
| admin Docker | Editable install (BASE_DIR зависит от расположения `settings.py`), `gunicorn`, `collectstatic` — как в монорепо, адаптировано под sibling-layout + named context для `zy_sdk`. path-dep на zy-sdk объявлен (контракт), runtime-import не обязателен. |
| Источник кода | Копируется из монорепо ПОСЛЕ Phase A: `libs/sdk/` → `zy-sdk`, `services/<svc>/` → `zy-<svc>`. |
| Git | Каждый репо — отдельный `git init` + initial commit (branch `main`). Монорепо `/home/ZY-2nd-flight` НЕ удаляется. |

### Phase C — локальная верификация

```bash
# Монорепо (после Phase A):
cd /home/ZY-2nd-flight && make lint && make test && make docker-build

# Standalone (после 007–011):
for r in zy-sdk zy-api-service zy-task-service zy-admin-service; do
  ( cd /home/$r && make lint && make test ); done
cd /home/zy-sdk && docker build -t zy-sdk:test .
for s in api task admin; do
  cd /home/zy-$s-service && docker build --build-context zy_sdk=/home/zy-sdk -t zy-$s-service:test . ; done
```

## Задачи

| id  | фаза | название | depends_on | complexity | status |
|-----|------|----------|-----------|------------|--------|
| 001 | A | Исправить test_scaffold: версия zy-sdk 0.1.0 | — | normal | todo |
| 002 | A | Унифицировать JWT_SECRET_KEY в task-service | — | normal | todo |
| 003 | A | mypy: исключить tests/ + перенести admin conftest | — | normal | todo |
| 004 | A | Починить mypy-ошибки в libs/sdk src | — | normal | todo |
| 005 | A | Починить mypy-ошибки в admin-service/manage.py | — | normal | todo |
| 006 | A | Добавить mypy в CI lint + зелёные make lint/test | 001,002,003,004,005 | normal | todo |
| 007 | B | Scaffold standalone репо zy-sdk | 004 | normal | todo |
| 008 | B | Scaffold standalone репо zy-api-service (+Docker) | 002,007 | high | todo |
| 009 | B | Scaffold standalone репо zy-task-service (+Docker) | 002,007 | high | todo |
| 010 | B | Scaffold standalone репо zy-admin-service (+Docker) | 005,007 | high | todo |
| 011 | B | Симлинки /home/projects/zy-* + projects-index.json | 007,008,009,010 | normal | todo |
| 012 | C | Верификация CI-эквивалента монорепо | 006 | high | todo |
| 013 | C | Верификация CI-эквивалента 4 standalone-репо | 008,009,010,011 | high | todo |
| 014 | gate | [BLOCKED] gh repo create / push / PR / merge | 012,013 | normal | blocked |

### Граф зависимостей по волнам

```
Wave 1 (Phase A, параллельно):   001   002   003   004   005
                                    \    \    |    /    /
Wave 2 (Phase A gate + sdk):        006        007(←004)
                                                 |
Wave 3 (Phase B сервисы):        008(←002,007) 009(←002,007) 010(←005,007)
                                                 |
Wave 4 (Phase B линковка):                     011(←007,008,009,010)
                                                 |
Wave 5 (Phase C):                012(←006)     013(←008,009,010,011)
                                                 |
Wave 6 (human-gate):                           014(←012,013)  [blocked]
```

Single-writer-per-file: в каждой волне задачи трогают непересекающиеся наборы файлов.
