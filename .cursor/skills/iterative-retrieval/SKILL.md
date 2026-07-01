---
name: iterative-retrieval
description: >-
  Progressive context refinement — 4-phase loop (DISPATCH → EVALUATE → REFINE →
  LOOP, max 3 cycles) для самостоятельного добора контекста воркером ДО написания
  кода. Решает «subagent context problem»: воркер не знает, какие файлы/паттерны
  ему нужны, пока не начнёт.
metadata:
  harness_role: worker
  origin: ECC (skills/iterative-retrieval)
---

# Iterative Retrieval

Решает «context problem» в агентских workflows: воркер получает спеку, но не знает
наперёд, какие файлы/паттерны/терминология в репо ему нужны. Стандартные подходы
падают: «послать всё» — переполнение контекста; «послать ничего» — не хватает
критичного; «угадать» — часто ошибаются.

## Когда активировать

- Перед написанием нового кода (воркер harness).
- При ответе «куда положить X?» / «есть ли уже Y?» / «как зовут Z?».
- Когда спека упоминает имена/пути, которые надо верифицировать в репо.

## 4-фазный цикл (макс. 3 цикла)

```
DISPATCH ──► EVALUATE ──► REFINE ──► LOOP (≤3) ──► достаточно → код
```

### Phase 1: DISPATCH
Широкий запрос — `rg`/`Grep`/`Glob` по высокоуровневым паттернам и ключевым словам
из спеки. Не угадывай точные пути — начни с `src/**/*.py`, `**/<имя из спеки>*`.

### Phase 2: EVALUATE
Оцени релевантность каждого найденного файла (0–1):
- **0.8–1.0** — напрямую реализует нужную функциональность.
- **0.5–0.7** — содержит связанные паттерны/типы.
- **0.2–0.4** — тангенциально.
- **0–0.2** — не относится, исключить.

Заодно: выяви **missingContext** — каких файлов/данных всё ещё не хватает.

### Phase 3: REFINE
Уточни запрос:
- добавь паттерны из high-relevance файлов;
- добавь terminology, найденную в коде (часто отличается от спеки);
- исключи подтверждённо нерелевантные пути;
- таргетируй конкретные gap'ы из EVALUATE.

### Phase 4: LOOP
Повтори с уточнённым запросом. Критерий останова — ≥3 high-relevance файлов
(≥0.7) и нет критических gap'ов. Max 3 цикла, потом — к коду.

## Практические примеры

### Bug fix: «Fix the authentication token expiry bug»
- Cycle 1: DISPATCH "token, auth, expiry" в `src/**` → `auth.ts` (0.9),
  `tokens.ts` (0.8), `user.ts` (0.3). REFINE: добавить "refresh", "jwt";
  исключить `user.ts`.
- Cycle 2: DISPATCH уточнённое → `session-manager.ts` (0.95),
  `jwt-utils.ts` (0.85). Достаточно.

### Feature: «Add rate limiting»
- Cycle 1: DISPATCH "rate, limit, api" в `routes/**` → 0 совпадений.
  REFINE: codebase использует "throttle", не "rate".
- Cycle 2: DISPATCH "throttle, middleware" → `throttle.ts` (0.9),
  `middleware/index.ts` (0.7). Достаточно.

## Интеграция в harness

В `prompts/worker.md` — обязательный шаг перед TDD-циклом:

```
1. Iterative retrieval (≤3 цикла): DISPATCH → EVALUATE → REFINE → LOOP.
   Верни `retrieval_result:` в output (файлы + relevance).
2. TDD по каждому acceptance criterion.
```

## Best practices

- Начинай широко, сужай постепенно — не переопределяй начальный запрос.
- Учи terminology кодбейза — первый цикл часто выявляет naming conventions.
- Трекай missingContext явно — это драйвит refinement.
- Останавливайся на «достаточно» — 3 high-relevance файла лучше 10 посредственных.
- Исключай уверенно — low-relevance файлы не станут релевантными.

## Связанное

- `search-first` скилл — родственный (reuse over reinvent).
- `graphify` — для архитектурных вопросов вместо `rg` (v2-017).
- ECC `skills/iterative-retrieval/SKILL.md` — источник.
