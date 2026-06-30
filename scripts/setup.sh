#!/usr/bin/env bash
# Bootstrap harness: venv + зависимости + .env + проверка готовности.
# Использование:  bash scripts/setup.sh [--durable] [--telegram]
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "==> harness setup в $ROOT"

EXTRAS="dev"
for arg in "$@"; do
  case "$arg" in
    --durable) EXTRAS="$EXTRAS,durable" ;;
    --telegram) EXTRAS="$EXTRAS,telegram" ;;
    --all) EXTRAS="dev,durable,telegram,sdk" ;;
  esac
done

# 1) venv
if [ ! -d .venv ]; then
  python3 -m venv .venv
  echo "==> создан .venv"
fi
# shellcheck disable=SC1091
. .venv/bin/activate

# 2) зависимости
python -m pip install -q -U pip
python -m pip install -q -e ".[$EXTRAS]"
echo "==> установлены зависимости: [$EXTRAS]"

# 3) .env
if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> создан .env из .env.example — ЗАПОЛНИ ключи (CURSOR_API_KEY, Telegram)"
fi

chmod +x scripts/*.sh scripts/hooks/*.sh 2>/dev/null || true

# 4) проверка
echo "==> harness doctor:"
python -m harness.interface.cli doctor || true

cat <<'EOF'

==> Готово. Дальше:
  . .venv/bin/activate
  # отредактируй .env (ключи), при durable:  make pg-up
  harness init --repo /path/to/target-repo     # подготовить целевой проект
  harness doctor --project <name>               # проверить
  harness plan "<цель>"                          # спланировать (проверь PLAN.md/tasks)
  harness ingest "<цель>" && harness run        # исполнить
EOF
