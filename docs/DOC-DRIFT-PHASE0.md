# Doc drift notes — harness-v4 Phase 0 (P0.4)

**Дата:** 2026-08-05  
**Scope:** только `/home/1.harness-v4` (prod `1.harness` не правился)

| Место | Было | Стало / статус |
|-------|------|----------------|
| `GUIDE.md` § next --limit | «по умолчанию **2**» | Исправлено → **12** (CLI `default=12`, ADR-0014) |
| `SKILLS.md` role→model | Opus / kimi→glm / glm-5.2 | Указан SoT `model_map.py` + Cursor TaskTool slugs |
| `README.md` | Harness v3, worktrees off | Banner V4 + таблица v4 vs prod |
| `ARCHITECTURE.md` | TaskTool-first v3 | Header **v4-dev** + clone path |
| `.env.example` | USE_WORKTREES=0, BRAIN=/home/brain | V4 flags: worktrees=1, soft gates=0, MULTI_TENANT, BRAIN_AGENTS |
| scaffold / ZY `phpunit-optional` | soft behaviour | Задокументировано в soft-gates inventory (код гейтов ZY не меняли) |
| orch skill YouTrack «Готовность: FSM%» | honesty hole | Отмечено в soft-gates notes; flip на evidence% — Phase 1.5 |

Остаточный drift (не блокирует Phase 0):

- Legacy `ORCH_MODEL` / escalation ladder still in `Settings` for push Engine.
- Some scaffold profiles still list `glm-5.2-high` as review model (compat).
- `QUICKSTART.md` may still say «v3» in places — follow-up PR.
