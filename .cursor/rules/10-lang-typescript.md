---
description: Language overlay для TypeScript/Frontend-проектов. Применяется, если профиль проекта language=typescript.
alwaysApply: false
---

# Language overlay: TypeScript

Применяется поверх конституции (`00-constitution.md`), когда `.harness/project.toml`
содержит `language = "typescript"`. Каноничный источник —
`/home/.cursor/rules/typescript-frontend.mdc`.

## Стек
- TypeScript **strict mode**. Менеджер пакетов из проекта (`pnpm`/`npm`/`yarn` — смотри lock-файл).
- Линт/формат: ESLint + Prettier.
- Типы: `tsc --noEmit` (typecheck).
- Тесты: тестраннер проекта (`vitest`/`jest`).

## Hard rules
- Никакого `any` и `// @ts-ignore` без комментария с причиной.
- Next.js: Server Components по умолчанию там, где можно.
- API-вызовы через типизированный клиент; `zod`-схемы зеркалят backend DTO.
- Не коммить секреты/URL — конфиг через env.

## Типовой профиль `.harness/project.toml`
```toml
language = "typescript"
canon = "typescript-frontend"

[[gates]]
id = "lint"
cmd = "pnpm lint"

[[gates]]
id = "types"
cmd = "pnpm typecheck"

[[gates]]
id = "test"
cmd = "pnpm test --run"

[[gates]]
id = "build"
cmd = "pnpm build"

[review]
model = "glm-5.2-high"

# опционально: гонять гейты в изолированном контейнере (HARNESS_SANDBOX=docker)
[sandbox]
image = "node:20"
network = "none"
```
