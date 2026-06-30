# Quickstart — запустить harness за 5 минут

Полный справочник — `GUIDE.md`. Здесь — кратчайший путь и как начать в новом чате.

---

## A. Один раз (установка)

```bash
cd 1.harness/orchestration
make setup            # venv + зависимости + .env + проверка (или: make setup --all)
# поставь Cursor CLI (воркеры на подписке):
curl https://cursor.com/install -fsS | bash
```

Открой `.env` и заполни как минимум:

```ini
CURSOR_API_KEY=cursor_...          # для оркестратора/ревьюера (SDK-роли)
TELEGRAM_BOT_TOKEN=123:ABC...      # опционально — уведомления/бот
TELEGRAM_CHAT_ID=123456789
```

Активируй окружение и проверь готовность:

```bash
. .venv/bin/activate
harness doctor                      # всё ли на месте (python/git/cursor-agent/ключ/профиль)
```

> Telegram chat_id: напиши своему боту любое сообщение, открой
> `https://api.telegram.org/bot<TOKEN>/getUpdates` → поле `chat.id`.

---

## B. Подготовить проект (любой язык)

```bash
# целевой репозиторий (Python/TS/Go/Rust — определится автоматически)
harness init --repo /path/to/your-repo
# зарегистрировать как проект (для мульти-репо)
harness projects add --name myapp --repo /path/to/your-repo --base main
harness doctor --project myapp
```

`harness init` создаёт `.harness/project.toml` с гейтами под стек —
**проверь команды гейтов** (lint/types/test/build) под свой проект.

---

## C. Прогнать цель

```bash
harness plan   "Добавить REST API управления пользователями" --project myapp 2>/dev/null || \
harness plan   "Добавить REST API управления пользователями"
#   ⤷ ОТКРОЙ PLAN.md и tasks/*.md, проверь декомпозицию, поправь при желании
harness ingest "Добавить REST API управления пользователями" --project myapp
harness run    --project myapp
harness status
harness events
```

Параллельно — уведомления в Telegram (BLOCKED / бюджет / нужен аппрув / готово).
Для команд из чата запусти бота в отдельном терминале:

```bash
harness bot     # /runs  /status  /approve <run> <task>
```

Если включён human-gate (`HUMAN_GATE_MERGE=1`) — подтверждай мерж:
`harness approve <run_id> <task_id>` (или `/approve` в боте).

---

## D. Как начать в НОВОМ чате Cursor

Открой новый агентский чат в каталоге `1.harness/orchestration` и вставь:

```
Я работаю с harness (обвязка оркестрации агентов) в этом каталоге.
Прочитай GUIDE.md и ARCHITECTURE.md. Затем:
1) проверь готовность: `harness doctor` (и поправь, что красное);
2) подготовь мой проект: `harness init --repo <ПУТЬ_К_РЕПО>` и зарегистрируй его
   (`harness projects add --name <ИМЯ> --repo <ПУТЬ> --base main`);
3) спланируй мою цель: `harness plan "<МОЯ ЦЕЛЬ>"`, покажи мне PLAN.md и tasks/
   на проверку перед запуском;
4) после моего ОК — `harness ingest "<МОЯ ЦЕЛЬ>" --project <ИМЯ>` и `harness run --project <ИМЯ>`,
   следи за `harness status`/`harness events`, BLOCKED-задачи показывай мне.
Модели: воркер auto → kimi-k2.5 → glm-5.2 (эскалация), ревьюер glm-5.2, планировщик opus.
Не запускай run, пока я не подтвердил PLAN.
```

Замени `<ПУТЬ_К_РЕПО>`, `<ИМЯ>`, `<МОЯ ЦЕЛЬ>`. Готово — агент проведёт тебя по циклу.

---

## E. Durable-режим (переживает падения, для долгих/параллельных прогонов)

```bash
make pg-up                 # Postgres в docker (порт 5439)
# в .env:  HARNESS_DURABLE=1
harness run --project myapp
```

Демо восстановления: `python scripts/durable_demo.py crash` → `... resume`.
