"""v2-018: детерминированная проверка плана (PLAN.md + tasks/*.md) перед ingest.

Без LLM — чисто статический анализ:
  - каждый файл задачи существует и парсится;
  - `id`/`title`/`complexity` во frontmatter валидны;
  - `depends_on` ссылаются на существующие id;
  - `Provides:` заполнен для задач, от которых зависят другие;
  - acceptance criteria — исполняемые (начинаются с кода/команды);
  - файлы из секции `# Файлы` существуют в репо (опционально, warn).

Красный вердикт → `harness ingest` отказывается ingest'нуть без --force.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from harness.tasks_io.parser import parse_plan_dependencies, parse_task_file, read_section


@dataclass
class VerificationFinding:
    """Одно замечание верификатора. severity: error (блокирует ingest) | warn."""
    severity: str       # "error" | "warn"
    task_id: str        # "plan" если на уровне PLAN.md
    issue: str
    detail: str = ""


@dataclass
class VerificationReport:
    ok: bool                       # True если ни одного error
    findings: list[VerificationFinding] = field(default_factory=list)

    @property
    def errors(self) -> list[VerificationFinding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[VerificationFinding]:
        return [f for f in self.findings if f.severity == "warn"]

    def markdown(self) -> str:
        if not self.findings:
            return "✅ Plan verification: no findings.\n"
        lines = ["# Plan verification report", ""]
        if self.errors:
            lines.append(f"## ❌ Errors ({len(self.errors)}) — блокируют ingest")
            lines.append("")
            for f in self.errors:
                lines.append(f"- **task-{f.task_id}**: {f.issue}"
                             + (f" — {f.detail}" if f.detail else ""))
            lines.append("")
        if self.warnings:
            lines.append(f"## ⚠️ Warnings ({len(self.warnings)})")
            lines.append("")
            for f in self.warnings:
                lines.append(f"- **task-{f.task_id}**: {f.issue}"
                             + (f" — {f.detail}" if f.detail else ""))
            lines.append("")
        lines.append(f"**Verdict:** {'PASS' if self.ok else 'FAIL'}")
        return "\n".join(lines)


# Команды/коды в acceptance criteria — считаем «исполняемыми».
# Формат: `- [ ] \`pytest ...\`` или `- [x] make ...` (markdown checkbox опционален).
_AC_EXEC_RE = re.compile(
    r"^\s*[-*]\s*(\[[ xX]\]\s*)?(`|pytest|make|rg|grep|curl|uv run|npm|pnpm|yarn|"
    r"docker|git|harness|python|node|go test|cargo)"
)


def verify_plan(settings_root: Path) -> VerificationReport:  # noqa: PLR0912
    """Проверить PLAN.md + tasks/*.md в `settings_root`.

    `settings_root` — дом harness (где лежат PLAN.md и tasks/).
    Возвращает отчёт; `ok=True` если ни одного error.
    """
    report = VerificationReport(ok=True)
    plan_path = settings_root / "PLAN.md"
    tasks_dir = settings_root / "tasks"

    if not plan_path.exists():
        report.findings.append(VerificationFinding(
            severity="error", task_id="plan", issue="PLAN.md не найден",
            detail=str(plan_path),
        ))
        report.ok = False
        return report
    if not tasks_dir.exists():
        report.findings.append(VerificationFinding(
            severity="error", task_id="plan", issue="tasks/ каталог не найден",
            detail=str(tasks_dir),
        ))
        report.ok = False
        return report

    # Парсим зависимости из PLAN.md (граф).
    deps = parse_plan_dependencies(plan_path)

    # Список задач.
    task_files = sorted(tasks_dir.glob("task-*.md"))
    if not task_files:
        report.findings.append(VerificationFinding(
            severity="error", task_id="plan", issue="нет файлов tasks/task-*.md",
        ))
        report.ok = False
        return report

    parsed = [parse_task_file(p) for p in task_files]
    ids = {p.id for p in parsed}

    # Множество id, у которых есть dependents (для проверки Provides).
    has_dependents: set[str] = set()
    for dep_ids in deps.values():
        for d in dep_ids:
            has_dependents.add(d)

    for p in parsed:
        # 1. frontmatter: id, title, complexity
        if not p.id or not p.id.strip():
            report.findings.append(VerificationFinding(
                severity="error", task_id=p.path.stem, issue="нет id в frontmatter",
            ))
            report.ok = False
        if not p.title or not p.title.strip():
            report.findings.append(VerificationFinding(
                severity="error", task_id=p.id, issue="нет title в frontmatter",
            ))
            report.ok = False

        # 2. depends_on ссылаются на существующие id
        for dep in deps.get(p.id, []):
            if dep not in ids:
                report.findings.append(VerificationFinding(
                    severity="error", task_id=p.id,
                    issue=f"depends_on ссылается на несуществующий id '{dep}'",
                ))
                report.ok = False

        # 3. Provides заполнен для задач с dependents
        text = p.path.read_text(encoding="utf-8")
        provides = read_section(text, "Provides") or p.provides
        if p.id in has_dependents and not provides.strip():
            report.findings.append(VerificationFinding(
                severity="warn", task_id=p.id,
                issue="задача имеет dependents, но секция `Provides` пуста — "
                      "зависимые воркеры получат пустой «ИНТЕРФЕЙСЫ ЗАВИСИМОСТЕЙ»",
            ))

        # 4. Acceptance criteria — исполняемые команды
        ac_body = read_section(text, "Acceptance criteria")
        if not ac_body.strip():
            report.findings.append(VerificationFinding(
                severity="warn", task_id=p.id,
                issue="секция `Acceptance criteria` пуста",
            ))
        else:
            non_exec = []
            for line in ac_body.splitlines():
                s = line.strip()
                if not s or s.startswith("<!--") or s.startswith("#"):
                    continue
                if not _AC_EXEC_RE.match(s):
                    non_exec.append(s[:80])
            if non_exec:
                report.findings.append(VerificationFinding(
                    severity="warn", task_id=p.id,
                    issue="acceptance criteria не выглядят исполняемыми командами",
                    detail=f"первые: {non_exec[0]}",
                ))

    # 5. Зависимости в PLAN.md ссылаются на id, которых нет в tasks/
    plan_ids_with_deps = set(deps.keys())
    missing_in_tasks = plan_ids_with_deps - ids
    for mid in missing_in_tasks:
        report.findings.append(VerificationFinding(
            severity="warn", task_id="plan",
            issue=f"PLAN.md упоминает task '{mid}' в графе зависимостей, "
                  "но файла tasks/task-*.md для него нет",
        ))

    report.ok = len(report.errors) == 0
    return report
