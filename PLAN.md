# PLAN — Упрощение auth ZY-2nd-flight: удаление auth-service, единый opaque-token flow

> Источник правды по декомпозиции цели. Целевой репозиторий воркеров: **`/home/ZY-2nd-flight`**
> (harness создаёт per-task git-worktree в `ZY-2nd-flight/.worktrees/task-<id>` от base-ветки;
> зависимые задачи стартуют от base уже с влитыми зависимостями).
> Все факты ниже проверены чтением фактических файлов и запуском реальных команд в `/home/ZY-2nd-flight`.

## Цель

Упростить авторизацию R2:

1. **Удалить избыточный `auth-service`** целиком (сервис-дубликат). Выпуск opaque access token
   остаётся **только** в `api-service` — `POST /api/v1/user/access-token` (уже реализован через
   `zy_sdk.auth.AuthTokenStore`).
2. **Единый shared Redis `AuthTokenStore`** для валидации в `api-service` и `task-service`
   (уже так — задача лишь фиксирует и чистит хвосты).
3. **`admin-service` — только Django sessions, без Redis** (сейчас Redis объявлен «для кэша»;
   убрать из env/compose/докстрингов, чтобы админка вообще не зависела от Redis).
4. **Удалить JWT-остатки** (неиспользуемые DTO `RefreshTokenRequest` / `AccessTokenResponse`
   в api-service; упоминания JWT в docs/README/OpenAPI).
5. **Обновить** compose / Dockerfile / Makefile / CI / ADR / docs под новую (упрощённую) картину.

### Что УЖЕ есть в репозитории (проверено чтением)

| Факт | Где |
|------|-----|
| `AuthTokenStore` (opaque, `secrets.token_urlsafe(32)`, Redis, namespace `gold`) | `libs/sdk/src/zy_sdk/auth/token_store.py` |
| `build_current_user_dependency`, `UserAuthContext`, `AuthSettings` | `libs/sdk/src/zy_sdk/auth/{dependency,context,settings}.py` |
| api-service выпускает токен `POST /api/v1/user/access-token` | `services/api-service/src/api_service/api/auth.py` + `services/.../services/auth.py` |
| api-service валидирует bearer через shared store | `services/api-service/src/api_service/core/auth.py`, `core/container.py` |
| task-service валидирует bearer через shared store | `services/task-service/src/task_service/core/auth.py`, `core/config.py` |
| admin-service на Django DB sessions (`SESSION_ENGINE=...backends.db`) | `services/admin-service/src/admin_service/config/settings.py` |
| **auth-service (дубликат) — подлежит удалению** | `services/auth-service/**`, `deploy/docker/auth-service/**` |

### Что дублирует auth-service (почему удаляем)

`services/auth-service/` поднимает FastAPI на порту `8003` и повторяет выпуск токена
(`POST /auth/v1/access-token`) плюс `introspect` / `revoke` — всё поверх того же
`zy_sdk.auth.AuthTokenStore`. Выпуск уже есть в api-service; `introspect`/`revoke` в R2
не используются на hot-path (валидация — прямой Redis GET в каждом сервисе). Сервис —
чистое дублирование, увеличивающее операционную поверхность.

## Фактическое состояние гейтов (ПРОВЕРЕНО командами в `/home/ZY-2nd-flight`)

`.harness/project.toml` целевого репо задаёт гейты:

- `lint` → `make lint`  = `ruff check services libs deploy` **+** `ruff format --check services libs deploy` **+** `mypy services libs`
- `test` → `make test`  = `pytest` по каждому воркспейс-мемберу + `tests/`

### ⚠️ Предсуществующее КРАСНОЕ состояние baseline (ВНЕ scope цели)

Проверено на чистом дереве **до** любых изменений:

| Команда | Результат | Детали |
|---|---|---|
| `ruff check services libs deploy` | ✅ GREEN (exit 0) | — |
| `ruff format --check services libs deploy` | ❌ RED (exit 1) | **5 файлов** «would be reformatted»: `libs/sdk/src/zy_sdk/auth/token_store.py`, `libs/sdk/tests/test_auth_dependency.py`, `libs/sdk/tests/test_token_store.py`, `services/admin-service/src/admin_service/apps/game/models.py`, `services/admin-service/src/admin_service/apps/onboarding/models.py` |
| `mypy services libs` | ❌ RED (exit 1) | **51 ошибка в 9 файлах**, все в `services/admin-service/**` (django `ModelAdmin`/`TabularInline` `type-arg`, `no-untyped-def`, `forms.py`, `conditions.py`, `settings.py:1`) |
| `make test` | ❌ RED (exit 2) | **ровно 1 падение**: `tests/test_scaffold.py::test_admin_service_package_importable` (`admin_service.__doc__ is None`). Все 102 сервис-теста + libs проходят. |

**Следствие для acceptance-критериев.** Полный `make lint` / `make test` НЕ могут быть
зелёными в рамках этой цели без починки чужого кода (django-admin аннотации, реформат
моделей, scaffold-тест) — это расширение scope и **запрещено** ролью оркестратора
(«не чинить лишнее»). Поэтому критерии сформулированы **таргетированно** по фактически
изменяемым файлам + **guard от регрессий**:

**Зелёный подмножество-оракул (ПРОВЕРЕНО):**

- `uv run ruff check services libs deploy` → GREEN (держим зелёным).
- `uv run mypy services/api-service/src services/task-service/src libs/sdk/src` → **GREEN** (`Success: no issues found in 123 source files`).
- `uv run ruff format --check <изменённые файлы>` → GREEN (изменённых файлов нет в baseline-списке из 5; держать отформатированными).
- `uv run pytest --import-mode=importlib services/<svc>/tests` → GREEN для api/task/admin/libs.

**Guard от регрессий (для всех задач):**

- Не увеличивать число ошибок `mypy services libs` сверх baseline (51).
- Не увеличивать число падений `make test` сверх baseline (1 — только `test_scaffold.py`).
- Не добавлять новых файлов в список `ruff format --check` (baseline: 5).

## Архитектурные решения (общие контракты между задачами)

| Область | Решение (контракт) |
|---|---|
| **Выпуск токена** | ТОЛЬКО `api-service` `POST /api/v1/user/access-token` → `TokenResponse{access_token, token_type="bearer"}`. Никакого второго эмитента. |
| **Валидация** | `api-service` и `task-service` — прямой Redis GET через `zy_sdk.auth.AuthTokenStore.validate()` (namespace `gold`, DB `redis://.../3`). Без HTTP introspect, без auth-service. |
| **admin-service** | Django DB sessions (`SESSION_ENGINE="django.contrib.sessions.backends.db"`, cookie `admin_sessionid`). **Без Redis вообще** и без `zy_sdk.auth`. |
| **Общий Redis auth** | `AUTH_REDIS_URL`/`REDIS_URL=redis://127.0.0.1:6379/3`, `AUTH_TOKEN_NAMESPACE=gold`, `AUTH_TOKEN_TTL_SECONDS=86400`. Ключ: `gold:auth:token:{token}`. |
| **auth_service vs auth-service** | Удаляется **пакет `auth_service`** (сервис `auth-service`). ВНИМАНИЕ: в `api-service` есть легитимный класс `AuthService` и DI-переменная `auth_service` (underscore) — их НЕ трогать. Негативные grep'ы ищут дефисный `auth-service`, `from auth_service`/`import auth_service`, `auth/v1`, `introspect`, порт `8003`. |
| **TD/** | `TD/*.md` и `TD/openapi-gold.yaml` — исходная (замороженная) техспека, исключена из ruff (`extend-exclude`). Историю в TD **не переписываем** (вне scope). Обновляем только `docs/` и `README.md`. |
| **uv workspace** | Мемберы: `services/*`, `libs/*`. Удаление `services/auth-service/` требует регенерации `uv.lock` (`uv lock`), иначе `uv run` (а значит гейты) сломается на отсутствующем мембере. |
| **Изоляция задач по файлам** | Задачи владеют непересекающимися наборами файлов (см. таблицу) — чтобы параллельные worktree-ветки мержились без конфликтов. |

### Ключевые интерфейсы (не менять сигнатуры)

- `zy_sdk.auth.AuthTokenStore.create(user_id: str, phone_number: int | None = None) -> str`
- `zy_sdk.auth.AuthTokenStore.validate(token: str) -> UserAuthContext | None`
- `zy_sdk.auth.AuthTokenStore.revoke(token: str) -> bool`
- `api_service.schemas.auth.AccessTokenRequest{phone_number:int}` — **оставить**
- `api_service.schemas.auth.TokenResponse{access_token:str, token_type:str="bearer"}` — **оставить**
- `api_service.services.auth.AuthService.issue_access_token(phone_number:int) -> TokenResponse` — **оставить**

## Таблица задач

| ID | Задача | complexity | Зависит от | Владеет файлами (не пересекается с другими) |
|----|--------|-----------|------------|---------------------------------------------|
| 001 | Удалить `auth-service` (код, Docker, Makefile, CI, root pyproject, uv.lock) | high | — | `services/auth-service/**` (del), `deploy/docker/auth-service/**` (del), `Makefile`, `.github/workflows/ci.yml`, `pyproject.toml`, `uv.lock` |
| 002 | api-service: удалить JWT-остатки (`RefreshTokenRequest`, `AccessTokenResponse`) | normal | — | `services/api-service/src/api_service/schemas/auth.py`, `services/api-service/src/api_service/schemas/__init__.py` |
| 003 | admin-service: зафиксировать «без Redis» в settings-докстринге и README | normal | — | `services/admin-service/src/admin_service/config/settings.py`, `services/admin-service/README.md` |
| 004 | compose + env + чистка комментариев про auth-service/Redis | normal | — | `docker-compose.app.yml`, `services/api-service/.env.example`, `services/task-service/.env.example`, `services/admin-service/.env.example`, `services/task-service/src/task_service/core/config.py` |
| 005 | docs/OpenAPI: JWT → opaque bearer | normal | — | `docs/openapi-gold.yaml` |
| 006 | brain: обновить ADR-0007 + gold-auth-keys + auth/_MOC (вне репо) | normal | — | `/home/brain/decisions/ADR-0007-auth-opaque-tokens-shared-redis.md`, `/home/brain/redis/gold-auth-keys.md`, `/home/brain/auth/_MOC.md` |
| 007 | root README + финальная интеграционная проверка (нет остатков auth-service/JWT) | normal | 001,002,003,004,005 | `README.md` |

**Порядок выполнения:** 001–006 независимы и могут идти параллельно (непересекающиеся файлы).
**007 — последняя**, стартует от base с влитыми 001–005: делает in-repo greps по интегрированному
дереву + правит корневой `README.md`.

### Обоснование зависимостей

- **007 зависит от 001,002,003,004,005**, т.к. её acceptance — репозиторно-широкие негативные
  grep'ы (нет `auth-service`, нет `auth/v1`, нет JWT в docs/README, нет Redis у admin) — проверяемы
  только когда все правки уже влиты в base. 007 создаётся от base после мержа зависимостей.
- **006 не блокирует 007**: brain-файлы лежат вне `/home/ZY-2nd-flight`, на in-repo гейты и greps не влияют.
- Остальные задачи независимы: наборы файлов не пересекаются → параллельный merge без конфликтов.

### Почему 001 — `high`

Единственная широкая задача: удаление сервиса затрагивает build-wiring (Makefile, CI-matrix,
root `pyproject` mypy_path/isort), регенерацию `uv.lock` и обязана сохранить гейты зелёными на
своём подмножестве. Ошибка ломает сборку/линт всего репо → стартуем сразу с сильной модели.
Остальные задачи — узкие правки (`normal`, сначала дешёвый auto).
