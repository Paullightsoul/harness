"""v2-035: skill-stocktake — аудит `prompts/` и `.cursor/skills/`.

ECC `skills/skill-stocktake`: набор скиллов/промптов растёт без контроля — никто
не проверяет, читаются ли они реально агентами или просто жрут контекст на
каждый вызов. Детерминированный (без LLM) отчёт:
  - размер файла (эвристический прокси токенов — `_BYTES_PER_TOKEN`),
  - когда последний раз менялся,
  - сколько раз имя файла/скилла встретилось в `agent_events` (транскрипте
    v2-009) — прокси «реально упоминался агентом», не «был прочитан» (мы не
    инструментируем чтение файлов агентом, только пост-фактум grep транскрипта).

Только отчёт с рекомендациями — авто-удаление запрещено спекой (человек решает).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness.store.repository import Store

# Грубая эвристика размера в "токенах" (для human-readable отчёта, не точный счёт).
_BYTES_PER_TOKEN = 4
_HEAVY_TOKEN_THRESHOLD = 1000  # ~4000 байт — файл, ощутимый в промпте


@dataclass
class SkillUsage:
    """Один просканированный файл (промпт-роль или SKILL.md)."""

    path: str
    kind: str            # "prompt" | "skill"
    size_bytes: int
    modified_at: str     # ISO8601
    mentions: int         # вхождений needle в agent_events.payload по всем run'ам

    @property
    def approx_tokens(self) -> int:
        return self.size_bytes // _BYTES_PER_TOKEN

    @property
    def is_heavy(self) -> bool:
        return self.approx_tokens >= _HEAVY_TOKEN_THRESHOLD

    @property
    def is_unmentioned(self) -> bool:
        return self.mentions == 0


@dataclass
class StocktakeReport:
    root: Path
    items: list[SkillUsage] = field(default_factory=list)

    @property
    def heavy_and_unmentioned(self) -> list[SkillUsage]:
        """Кандидаты «жрут контекст зря»: тяжёлые И ни разу не упомянутые.

        Не «мёртвый код» в строгом смысле — агент мог прочитать и просто не
        процитировать явно; поэтому это рекомендация на ревизию, не приговор.
        """
        return sorted(
            (i for i in self.items if i.is_heavy and i.is_unmentioned),
            key=lambda i: -i.size_bytes,
        )

    def markdown(self) -> str:
        lines = [
            "# Skill stocktake report",
            "",
            f"- **root:** `{self.root}`",
            f"- **files scanned:** {len(self.items)}",
            f"- **heavy + unmentioned (кандидаты на ревизию):** "
            f"{len(self.heavy_and_unmentioned)}",
            "",
        ]
        if not self.items:
            lines.append("Нечего сканировать — `prompts/` и `.cursor/skills/` пусты.")
            return "\n".join(lines)

        lines.append("## Все файлы")
        lines.append("")
        lines.append("| path | kind | ~tokens | mentions | modified |")
        lines.append("|---|---|---|---|---|")
        for item in sorted(self.items, key=lambda i: -i.size_bytes):
            lines.append(
                f"| `{item.path}` | {item.kind} | {item.approx_tokens} | "
                f"{item.mentions} | {item.modified_at[:10]} |"
            )
        lines.append("")

        heavy = self.heavy_and_unmentioned
        if heavy:
            lines.append("## Кандидаты на ревизию (тяжёлые + ни разу не упомянуты)")
            lines.append("")
            for item in heavy:
                lines.append(
                    f"- `{item.path}` — ~{item.approx_tokens} токенов, 0 упоминаний "
                    "в agent_events. Рассмотри: сократить, разбить на секции по "
                    "требованию, или убедиться, что скилл вообще подключён в промпте."
                )
            lines.append("")
        else:
            lines.append("✅ Нет тяжёлых неиспользуемых файлов.")
            lines.append("")
        return "\n".join(lines)


def _needle_for(path: Path, kind: str) -> str:
    """Строка для поиска упоминаний в agent_events.

    Скиллы все называются `SKILL.md` — уникальный идентификатор это каталог
    (`.cursor/skills/<name>/SKILL.md` → `<name>`). Промпты уже уникальны по имени
    файла (`prompts/worker.md` → `worker`).
    """
    return path.parent.name if kind == "skill" else path.stem


def _count_mentions(store: Store, needle: str) -> int:
    """Сколько раз `needle` встретился в payload агент-событий по всем run'ам.

    Прямой SQL по `store._conn` (служебный read-only доступ, тот же паттерн, что
    `dashboard/app.py._list_runs` — stocktake не часть публичного контракта Store).
    """
    row = store._conn.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS c FROM agent_events WHERE payload LIKE ?",
        (f"%{needle}%",),
    ).fetchone()
    return int(row["c"]) if row else 0


def _scan(directory: Path, kind: str, pattern: str, store: Store) -> list[SkillUsage]:
    if not directory.exists():
        return []
    out: list[SkillUsage] = []
    for path in sorted(directory.rglob(pattern)):
        if not path.is_file():
            continue
        stat = path.stat()
        needle = _needle_for(path, kind)
        out.append(SkillUsage(
            path=str(path),
            kind=kind,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            mentions=_count_mentions(store, needle),
        ))
    return out


def run_stocktake(root: Path, store: Store) -> StocktakeReport:
    """Просканировать `root/prompts/*.md` и `root/.cursor/skills/*/SKILL.md`.

    `root` — дом harness (свои промпты/скиллы), не целевой проект — тот же
    scope, что у `harness/security/scanner.py` (v2-031).
    """
    items = [
        *_scan(root / "prompts", "prompt", "*.md", store),
        *_scan(root / ".cursor" / "skills", "skill", "SKILL.md", store),
    ]
    return StocktakeReport(root=root, items=items)
