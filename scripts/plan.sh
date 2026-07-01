#!/usr/bin/env bash
# Шаг 1: планирование. Оркестратор (Opus) читает репозиторий + цель и пишет
# PLAN.md и tasks/task-*.md.
#
# Использование:
#   ./scripts/plan.sh "Добавить REST API для управления пользователями на FastAPI"
#   ./scripts/plan.sh -f goal.txt        # цель из файла

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_cli

if [[ "${1:-}" == "-f" && -n "${2:-}" ]]; then
  GOAL="$(cat "$2")"
else
  GOAL="${*:-}"
fi

if [[ -z "$GOAL" ]]; then
  echo "Укажи цель: ./scripts/plan.sh \"...\"  (или -f goal.txt)" >&2
  exit 1
fi

PROMPT="$(cat "$ROOT/prompts/orchestrator.md")

=== GOAL ===
$GOAL

=== ШАБЛОН ЗАДАЧИ (соблюдай строго) ===
$(cat "$ROOT/tasks/_TEMPLATE.md")
"

run_agent "$ORCH_MODEL" "$PROMPT" "orchestrator"

echo
echo "✅ Планирование завершено. Проверь PLAN.md и tasks/, затем запусти:"
echo "   ./scripts/orchestrate.sh"
