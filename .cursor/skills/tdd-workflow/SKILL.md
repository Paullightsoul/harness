---
name: tdd-workflow
description: >
  TDD-цикл для воркера harness: Red → Green → Refactor. Каждый acceptance
  criterion сначала покрывается тестом, потом реализуется, потом чистится.
  Гейты проекта — оракул истины.
metadata:
  origin: ECC (adapted)
  harness_role: worker
---

# TDD Workflow — Red-Green-Refactor

Воркер реализует задачу через TDD-цикл. Гейты из `.harness/project.toml` —
машинная проверка, которую нельзя обмануть.

## Когда активировать

- Любая задача с acceptance criteria (= почти все задачи harness).
- Особенно: новая логика, API-эндпоинты, сервисы, утилиты.

## Цикл (по каждому acceptance criterion)

```
┌────────────────────────────────────────┐
│  1. RED — напиши падающий тест         │
│     Тест проверяет ОДИН criterion.     │
│     Прогони — должен быть красный.     │
├────────────────────────────────────────┤
│  2. GREEN — минимальная реализация     │
│     Только то, что нужно для зелёного  │
│     теста. Без оверинжиниринга.        │
├────────────────────────────────────────┤
│  3. REFACTOR — чистка                  │
│     Убери дублирование, выровняй       │
│     именование. Тесты остаются зелёными│
├────────────────────────────────────────┤
│  4. GATE CHECK — прогони гейты         │
│     Команды из `.harness/project.toml` │
│     Все зелёные → следующий criterion. │
└────────────────────────────────────────┘
```

## Правила

### Вертикальные срезы
Каждый цикл — один acceptance criterion. Не пиши все тесты разом, потом весь
код разом. Один тест → одна реализация → чистка → следующий.

### Минимальная реализация
На шаге GREEN — только код для зелёного теста. Без «а давай сразу добавим
обработку ошибок, которая понадобится потом». Потом — это следующий criterion.

### Не подгоняй тесты
Anti-gaming guard harness запрещает воркеру править зону `tests/spec/**`.
Тесты пишутся по acceptance criteria из спеки, а не по результатам реализации.

## Примеры

### Python (FastAPI + pytest)

```python
# 1. RED — тест
async def test_create_user_returns_201(client):
    resp = await client.post("/api/users", json={"email": "a@b.com", "name": "A"})
    assert resp.status_code == 201
    assert resp.json()["email"] == "a@b.com"

# 2. GREEN — минимальная реализация
@router.post("/api/users", status_code=201)
async def create_user(body: CreateUserRequest, svc: UserService = Depends()):
    return await svc.create(body)

# 3. REFACTOR — если нужно
# 4. GATE CHECK: pytest -q && ruff check && mypy
```

### TypeScript (Next.js + vitest)

```typescript
// 1. RED
test("GET /api/users returns 200", async () => {
  const res = await fetch("/api/users");
  expect(res.status).toBe(200);
});

// 2. GREEN
export async function GET() {
  const users = await db.user.findMany();
  return NextResponse.json(users);
}

// 3. REFACTOR
// 4. GATE CHECK: pnpm test --run && pnpm lint && pnpm typecheck
```

## Интеграция с harness

- **Гейты проекта** = оракул истины. `pytest`/`pnpm test` из
  `.harness/project.toml` — финальная проверка каждого цикла.
- **Ревьюер** проверит, что тесты осмысленны (не подогнаны под зелёный).
- **Anti-gaming guard** не позволит подменить спеки/acceptance.

## Анти-паттерны

- Писать код без теста → «надеюсь, работает» → гейт ловит.
- Все тесты сразу → спекулятивные тесты, часть окажется ненужной.
- Тест, который всегда зелёный → бесполезен, ревьюер отклонит.
- Оверинжиниринг на GREEN → трата бюджета, задача дороже.
