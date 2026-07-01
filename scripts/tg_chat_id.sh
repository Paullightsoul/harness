#!/usr/bin/env bash
# Печатает chat_id из последних апдейтов бота. Сначала впиши TELEGRAM_BOT_TOKEN в .env
# и напиши своему боту любое сообщение в Telegram, затем запусти этот скрипт.
set -euo pipefail
cd "$(dirname "$0")/.."

# подхватим токен из .env, если не задан в окружении
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -f .env ]; then
  # снимаем возможные обрамляющие кавычки и пробелы вокруг значения
  TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | head -1 | cut -d= -f2- \
    | sed -E 's/^[[:space:]]*"?//; s/"?[[:space:]]*$//')"
fi
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "TELEGRAM_BOT_TOKEN не задан (впиши в .env или export)." >&2
  exit 1
fi

resp="$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates")"
echo "Найденные chat_id:"
if command -v jq >/dev/null 2>&1; then
  echo "$resp" | jq -r '.result[].message.chat | "\(.id)\t\(.type)\t\(.title // .username // .first_name // "")"' | sort -u
else
  echo "$resp" | grep -oE '"chat":\{"id":-?[0-9]+' | grep -oE -- '-?[0-9]+' | sort -u
fi
echo
echo "Впиши нужный id в .env:  TELEGRAM_CHAT_ID=<id>"
