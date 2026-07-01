#!/usr/bin/env bash
# Общая библиотека: конфигурация моделей и обёртка над cursor-agent.
# Подключается через `source` в plan.sh и orchestrate.sh.

set -euo pipefail

# ── Корень проекта ──────────────────────────────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── Конфигурация моделей (переопределяется переменными окружения) ────────────
# Дорогой планировщик — только он жжёт пул Third-Party API.
ORCH_MODEL="${ORCH_MODEL:-claude-opus-4-8-thinking-high}"
# Воркеры на auto — фактически в рамках подписки, "почти безлимит".
WORKER_MODEL="${WORKER_MODEL:-auto}"
# Ревьюер — китайская модель glm-5.2; смени на auto, если хочешь экономить.
REVIEWER_MODEL="${REVIEWER_MODEL:-glm-5.2-high}"

# Лестница эскалации воркера при провалах: auto -> kimi-k2.5 -> glm-5.2.
ESCALATION_MODELS="${ESCALATION_MODELS:-kimi-k2.5,glm-5.2-high}"

# Сколько раз воркер пытается доделать задачу после CHANGES от ревьюера.
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"

# Базовая ветка, в которую мержатся принятые задачи.
BASE_BRANCH="${BASE_BRANCH:-main}"

# Доп. флаги cursor-agent (например свой API-ключ через --api-key).
CURSOR_FLAGS="${CURSOR_FLAGS:-}"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR" "$ROOT/reviews"

# ── Проверка окружения ───────────────────────────────────────────────────────
require_cli() {
  if ! command -v cursor-agent >/dev/null 2>&1; then
    echo "ОШИБКА: не найден cursor-agent. Установи Cursor CLI: https://cursor.com/docs/cli/overview" >&2
    exit 1
  fi
}

# ── Запуск агента ────────────────────────────────────────────────────────────
# run_agent <model> <prompt-string> <log-name>
# Печатает финальный ответ агента в stdout и дублирует в logs/<log-name>.log.
run_agent() {
  local model="$1" prompt="$2" logname="$3"
  local logfile="$LOG_DIR/${logname}.log"
  echo "▶ agent[$model] → $logname" >&2
  # -p: headless; --force: применять изменения без подтверждений; text: финальный ответ.
  cursor-agent -p "$prompt" \
    --model "$model" \
    --output-format text \
    --force \
    $CURSOR_FLAGS 2>&1 | tee "$logfile"
}

# ── Работа с frontmatter задач ───────────────────────────────────────────────
task_field() {  # task_field <file> <field>
  # снимаем префикс "field:", затем инлайн-комментарий, затем кавычки и пробелы
  grep -E "^$2:" "$1" | head -1 \
    | sed -E "s/^$2:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^\"//; s/\"[[:space:]]*$//; s/[[:space:]]*$//"
}

set_task_field() {  # set_task_field <file> <field> <value>
  local file="$1" field="$2" value="$3"
  # macOS/BSD и GNU sed совместимо: переписываем во временный файл.
  awk -v f="$field" -v v="$value" '
    BEGIN{done=0}
    /^---$/{c++; print; next}
    (c==1 && $0 ~ "^"f":" && !done){print f": \""v"\""; done=1; next}
    {print}
  ' "$file" > "$file.tmp" && mv "$file.tmp" "$file"
}

run_gates() {  # возвращает 0 если make check зелёный
  echo "▶ make check" >&2
  make check 2>&1 | tee "$LOG_DIR/gates.log"
  return "${PIPESTATUS[0]}"
}
