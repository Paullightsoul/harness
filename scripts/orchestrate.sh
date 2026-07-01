#!/usr/bin/env bash
# Шаг 2: оркестрация. Для каждой задачи из tasks/:
#   ветка → воркер (auto) → make check → ревьюер → APPROVE? мерж : доработка.
#
# Использование:
#   ./scripts/orchestrate.sh            # все незавершённые задачи
#   ./scripts/orchestrate.sh task-003   # только конкретная задача

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_cli

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ОШИБКА: это не git-репозиторий. Выполни 'git init' и сделай первый коммит." >&2
  exit 1
fi

# Собираем список задач.
mapfile -t TASKS < <(ls "$ROOT"/tasks/task-*.md 2>/dev/null | sort)
if [[ -n "${1:-}" ]]; then
  TASKS=("$ROOT/tasks/${1%.md}.md")
fi
[[ ${#TASKS[@]} -eq 0 ]] && { echo "Нет задач в tasks/. Сначала запусти plan.sh"; exit 0; }

process_task() {
  local file="$1"
  local id title status branch attempt verdict feedback review_file
  id="$(task_field "$file" id)"
  title="$(task_field "$file" title)"
  status="$(task_field "$file" status)"

  if [[ "$status" == "done" ]]; then
    echo "⏭  task-$id ($title) уже done — пропуск"; return 0
  fi

  branch="task/$id"
  review_file="$ROOT/reviews/task-$id.md"
  echo "════════════════════════════════════════════════"
  echo "▶ task-$id: $title"
  echo "════════════════════════════════════════════════"

  git checkout "$BASE_BRANCH" >/dev/null 2>&1 || true
  git checkout -B "$branch" >/dev/null 2>&1
  set_task_field "$file" status "in_progress"

  feedback=""
  for ((attempt=1; attempt<=MAX_ATTEMPTS; attempt++)); do
    echo "── попытка $attempt/$MAX_ATTEMPTS ──"
    set_task_field "$file" attempts "$attempt"

    # 1) ВОРКЕР
    local wprompt
    wprompt="$(cat "$ROOT/prompts/worker.md")

=== СПЕКА ЗАДАЧИ (tasks/task-$id.md) ===
$(cat "$file")
"
    [[ -n "$feedback" ]] && wprompt="$wprompt

=== ФИДБЭК РЕВЬЮЕРА С ПРОШЛОЙ ПОПЫТКИ (исправь это) ===
$feedback
"
    run_agent "$WORKER_MODEL" "$wprompt" "worker-$id-a$attempt"

    # Воркер мог объявить блокировку из-за нехватки контекста.
    if [[ -f "$ROOT/reviews/task-$id.blocked.md" ]]; then
      echo "⛔ task-$id: воркер сообщил о блокировке. См. reviews/task-$id.blocked.md"
      set_task_field "$file" status "blocked"; return 0
    fi

    git add -A

    # 2) ГЕЙТЫ
    local gates_ok=1
    run_gates && gates_ok=0 || gates_ok=1

    # 3) РЕВЬЮЕР
    local rprompt
    rprompt="$(cat "$ROOT/prompts/reviewer.md")

=== СПЕКА ЗАДАЧИ ===
$(cat "$file")

=== РЕЗУЛЬТАТ make check (0=ОК) → код: $gates_ok ===
$(tail -n 40 "$LOG_DIR/gates.log" 2>/dev/null)

=== GIT DIFF относительно $BASE_BRANCH ===
$(git diff "$BASE_BRANCH" 2>/dev/null)
"
    run_agent "$REVIEWER_MODEL" "$rprompt" "reviewer-$id-a$attempt"

    verdict="$(grep -Eo 'VERDICT:[[:space:]]*(APPROVE|CHANGES)' "$review_file" 2>/dev/null | tail -1 | grep -Eo 'APPROVE|CHANGES' || echo CHANGES)"
    echo "Вердикт ревьюера: $verdict | гейты: $([[ $gates_ok -eq 0 ]] && echo PASS || echo FAIL)"

    if [[ "$verdict" == "APPROVE" && $gates_ok -eq 0 ]]; then
      git commit -q -m "task($id): $title" || true
      set_task_field "$file" status "done"
      git add -A && git commit -q -m "task($id): отметка done" || true
      git checkout "$BASE_BRANCH" >/dev/null 2>&1
      git merge --no-ff -m "merge task($id): $title" "$branch" >/dev/null 2>&1
      echo "✅ task-$id принята и влита в $BASE_BRANCH"
      return 0
    fi

    # Не принято → собираем фидбэк для следующей попытки.
    feedback="Гейты: $([[ $gates_ok -eq 0 ]] && echo PASS || echo FAIL).
$(cat "$review_file" 2>/dev/null)
$(tail -n 20 "$LOG_DIR/gates.log" 2>/dev/null)"
    git commit -q -m "task($id): попытка $attempt (на доработку)" || true
  done

  echo "❌ task-$id не принята за $MAX_ATTEMPTS попыток — помечена blocked. Ветка: $branch"
  set_task_field "$file" status "blocked"
  git checkout "$BASE_BRANCH" >/dev/null 2>&1 || true
  return 0
}

for t in "${TASKS[@]}"; do
  [[ -f "$t" ]] || { echo "нет файла: $t"; continue; }
  process_task "$t"
done

echo
echo "Готово. Статусы задач:"
grep -H -E '^(id|status):' "$ROOT"/tasks/task-*.md | sed 's#.*/##'
