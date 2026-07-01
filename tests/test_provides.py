"""v2-004: воркер обязан заявить `Provides:` для задач с dependents.
Парсер поддерживает свёрнутый (`Provides:`) и markdown (`# Provides`) форматы.
Движок извлекает блок из вывода воркера, сохраняет в `task.provides`, и если
dependents есть, а блок пустой → `PROVIDES_MISSING` + CHANGES без ревьюера.
"""
from __future__ import annotations

from pathlib import Path

from harness.config import Settings
from harness.domain.enums import EventType
from harness.domain.models import Run, Task
from harness.scheduler.engine import Engine
from harness.store.repository import Store


def _engine_with_stubs(tmp_path: Path) -> Engine:
    prompts = tmp_path / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "reviewer.md").write_text("# reviewer stub\n", encoding="utf-8")
    (prompts / "worker.md").write_text("# worker stub\n", encoding="utf-8")
    (prompts / "orchestrator.md").write_text("# orchestrator stub\n", encoding="utf-8")
    return Engine(Settings(root=tmp_path), Store(tmp_path / "state.db"))


def test_extract_provides_collapsed_format() -> None:
    """Свёрнутый формат: `Provides:` в начале строки, тело до пустой строки."""
    engine = _engine_with_stubs(Path("/tmp"))
    out = (
        "Изменённые файлы:\n- src/auth.py\n\n"
        "Provides:\n"
        "- src/auth.py::issue_token(user_id: UUID) -> str\n"
        "- src/auth.py::verify_token(token: str) -> TokenClaims\n"
        "\nГейты: PASS\n"
    )
    extracted = engine._extract_provides(out)
    assert "issue_token" in extracted
    assert "verify_token" in extracted
    assert "Гейты" not in extracted  # тело обрезано по пустой строке


def test_extract_provides_markdown_section() -> None:
    """Markdown-формат: `# Provides` секция до следующего заголовка."""
    engine = _engine_with_stubs(Path("/tmp"))
    out = (
        "## Изменённые файлы\n- x.py\n\n"
        "## Provides\n"
        "- x.py::foo() -> int\n"
        "- DTO: Bar в schemas.py\n"
        "\n## Запрещено\nничего\n"
    )
    extracted = engine._extract_provides(out)
    assert "foo()" in extracted
    assert "Bar" in extracted
    assert "Запрещено" not in extracted


def test_extract_provides_absent_returns_empty() -> None:
    engine = _engine_with_stubs(Path("/tmp"))
    assert engine._extract_provides("no provides here\nГейты: PASS\n") == ""


def test_extract_provides_empty_block_returns_empty() -> None:
    """`Provides:` без тела — пусто."""
    engine = _engine_with_stubs(Path("/tmp"))
    out = "Изменённые файлы:\n- x.py\n\nProvides:\n\nГейты: PASS\n"
    assert engine._extract_provides(out) == ""


def test_has_dependents_detects_dependent_task(tmp_path: Path) -> None:
    engine = _engine_with_stubs(tmp_path)
    store = engine._store
    store.create_run(Run(id="r1", project="p", goal="g", status="planning"))
    store.upsert_task(Task(id="001", run_id="r1", title="a", spec_path="x", status="pending"))
    store.upsert_task(
        Task(id="002", run_id="r1", title="b", spec_path="y", status="pending",
             depends_on=["001"]),
    )
    assert engine._has_dependents("r1", "001") is True
    assert engine._has_dependents("r1", "002") is False


def test_provides_missing_event_recorded(tmp_path: Path) -> None:
    """Когда dependents есть, а provides пустой — должно стать PROVIDES_MISSING.

    Здесь проверяем только факт наличия event-типа в enums и helper-логику;
    полный _process_task требует mock'а worktree/runner — separate integration test.
    """
    # Регресс: EventType.PROVIDES_MISSING существует
    assert EventType.PROVIDES_MISSING.value == "provides_missing"
