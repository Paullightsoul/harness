#!/usr/bin/env bash
# Hook beforeShellExecution: блокирует деструктивные команды агентов (defense-in-depth).
# Получает на stdin JSON с полем .command, отвечает JSON-решением на stdout.
#
# ВНИМАНИЕ: точная схема hooks может меняться — сверься с https://cursor.com/docs.
# Реальное принуждение качества — это `make check` в цикле оркестрации, а не этот хук.
set -euo pipefail

payload="$(cat)"

# Вытаскиваем команду (jq если есть, иначе грубый grep).
if command -v jq >/dev/null 2>&1; then
  cmd="$(printf '%s' "$payload" | jq -r '.command // .args.command // empty' 2>/dev/null || true)"
else
  cmd="$payload"
fi

# Чёрный список опасных паттернов.
deny_patterns=(
  'rm[[:space:]]+-rf[[:space:]]+/'
  'git[[:space:]]+push[[:space:]].*--force'
  'git[[:space:]]+push[[:space:]].*-f([[:space:]]|$)'
  'git[[:space:]]+reset[[:space:]]+--hard[[:space:]]+origin'
  ':\(\)\{.*\};:'          # fork bomb
  'mkfs'
  'dd[[:space:]]+if='
  '>[[:space:]]*/dev/sd'
  'chmod[[:space:]]+-R[[:space:]]+777[[:space:]]+/'
  # anti-gaming: запись в защищённую зону спеков/acceptance (best-effort на уровне shell;
  # надёжная проверка — git-diff guard в движке, harness/policy/guard.py)
  '>[[:space:]]*tests/spec/'
  '(tee|sed[[:space:]]+-i|cp|mv)[[:space:]].*tests/spec/'
)

for p in "${deny_patterns[@]}"; do
  if printf '%s' "$cmd" | grep -Eq "$p"; then
    printf '{"permission":"deny","userMessage":"guard.sh заблокировал деструктивную команду: %s"}\n' "$p"
    exit 0
  fi
done

printf '{"permission":"allow"}\n'
exit 0
