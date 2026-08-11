# Skills для harness-оркестрации

Адаптированные skills из [ECC](https://github.com/affaan-m/ECC) (224k+ stars),
[Superpowers](https://github.com/obra/superpowers) и Karpathy Guidelines —
под нашу систему оркестрации (Cursor + harness).

> **SKILL.md** — открытый стандарт. Скилл = файл `SKILL.md` в папке
> `.cursor/skills/<name>/`. Cursor подхватывает их автоматически.
> Rules (`.cursor/rules/`) — всегда-включённые конвенции.
> Skills — workflow'ы, подгружаемые по запросу.

---

## Маппинг скиллов на роли harness

> **SoT моделей (Cursor Task Tool):** `harness/tasktool/model_map.py` —
> orch / worker / reviewer / research → `cursor-grok-4.5-high` (Grok-first;
> см. `docs/GROK-DEFAULTS.md`). Env: `TASKTOOL_*_MODEL`.
> Legacy Opus/kimi/glm в таблице ниже — **устарело**.
> Curated inject 1–3: `skills/catalog.json` + `harness skills select`.

| Роль в harness | Модель (TaskTool) | Скиллы | Что дают |
|---|---|---|---|
| **Оркестратор** / planner | `cursor-grok-4.5-high` | `harness-orchestrate` (+ catalog orch_core) | Plan / dispatch / YouTrack honesty |
| **Воркер** | `cursor-grok-4.5-high` | `anti-hallucination` + `search-first` + `tdd-workflow` + `token-economy` + `systematic-debugging` (inject ≤3) | Заземление, reuse, TDD, экономия, дебаг |
| **Ревьюер** / goal-judge | `cursor-grok-4.5-high` | `security-review` + `code-quality` | Безопасность, качество поверх AC |
| **Re-plan** / sub-orch / research | worker model (Grok) | `systematic-debugging` | Root-cause для refine/split/block |
| **Human-only** (не inject воркеру) | — | `grill-me`, `research`, `handoff`, firecrawl/… | Каталог `packs.human_only` |

---

## Установленные скиллы

Расположение: `.cursor/skills/` в каталоге harness.

### Для воркера

#### `anti-hallucination`
4 жёстких правила заземления из Karpathy Guidelines:
1. Прочитай, прежде чем менять.
2. Не выдумывай API — проверяй через grep/rg.
3. Минимальные изменения — только acceptance criteria.
4. Признай незнание — лучше BLOCKED, чем галлюцинация.

#### `search-first`
Исследование перед кодированием. Воркер ищет существующую реализацию в репо,
зависимостях и workspace (`/home/projects/`) **до** написания нового кода.
Правило «reuse over reinvent» из конституции workspace.

#### `tdd-workflow`
Red-Green-Refactor цикл по каждому acceptance criterion:
- RED: падающий тест на criterion.
- GREEN: минимальная реализация.
- REFACTOR: чистка (без ломания тестов).
- GATE CHECK: прогон гейтов из `.harness/project.toml`.

#### `token-economy`
Экономия контекста и выходных токенов (стиль Caveman):
- Сжатый вывод без нарратива.
- Прицельное чтение файлов (grep → read фрагмент).
- Не дублируй спеку в ответе.
Критично для auto-воркеров с ограниченным контекстом и бюджетом.

#### `systematic-debugging`
4-фазный root-cause анализ (из Superpowers) при провале гейтов:
1. Root-Cause Tracing — найти корень, не симптом.
2. Defense-in-Depth — минимальный фикс + регрессионный тест.
3. Condition-Based Waiting — прогон всех гейтов.
4. Verification — подтверждение.

### Для ревьюера

#### `security-review`
Чеклист безопасности из ECC, адаптированный под Python/FastAPI и TS/Next.js:
- Секреты, валидация ввода, SQL-инъекции, auth/authz, утечка данных.
- Критическая проблема безопасности = CHANGES даже при OK функциональности.

#### `code-quality`
Чеклист качества кода (из sentry-code-simplifier):
- Дублирование, мёртвый код, сложность, именование, обработка ошибок.
- Рефакторинг-паттерны: early return, extract method.

---

## Источники и внешние скиллы (для справки)

### Разработка
- **[Superpowers (obra)](https://github.com/obra/superpowers)** — SDLC из
  композируемых скиллов: декомпозиция, subagent-driven-development, systematic-debugging.
- **Karpathy Guidelines** — 4 правила против LLM-ошибок (анти-оверинжиниринг).
- **python-tdd-with-uv** — TDD на Python с быстрым `uv`.
- **reviewing-code** — структурное ревью: корректность, поддерживаемость, перформанс.

### Память / контекст
- **Caveman** — срезает выходные токены на ~65%, сохраняя код.
- **subagent-driven-development** — сабагенты держат чтения в своём контексте,
  возвращают только summary (= архитектура нашего harness).
- **continuous-learning-v2** — instinct-based обучение с confidence scoring.

### Безопасность
- **Claude Security / security-review** — reasoning по контексту кода.
- **security-reviewer** (jeffallan) — SAST + dependency audit + secrets.
- **Semgrep MCP** — детерминированный сканер как baseline.

### Где брать новые
- [anthropics/skills](https://github.com/anthropics/skills) — эталонные примеры.
- [obra/superpowers](https://github.com/obra/superpowers) — крупнейшая коллекция.
- [awesome-cursor-skills](https://github.com/spencerpauly/awesome-cursor-skills) — под Cursor.
- [affaan-m/ECC](https://github.com/affaan-m/ECC) — 277+ скиллов, cross-harness.

---

## Как подключить новый скилл

```bash
# 1. Создать папку в .cursor/skills/
mkdir -p .cursor/skills/my-skill

# 2. Создать SKILL.md с frontmatter
cat > .cursor/skills/my-skill/SKILL.md << 'EOF'
---
name: my-skill
description: Что делает скилл
metadata:
  harness_role: worker|reviewer|replan
---

# Заголовок

## Когда активировать
...

## Workflow
...
EOF

# 3. Cursor подхватит при следующем запуске
```

## Гигиена

- **8–12 скиллов** закрывают 90% потребностей. Не складируй всё подряд.
- Раз в месяц: удали неиспользуемые скиллы (они едят контекст).
- Скиллы для harness должны указывать `harness_role` в metadata —
  чтобы было ясно, кто их использует.
