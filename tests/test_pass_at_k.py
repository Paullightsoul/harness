"""v2-030: pass@k / pass^k метрики — количественная оценка "агент справляется".

pass@k — доля задач, где хотя бы одна из первых k попыток получила
VERDICT: APPROVE (мягкая метрика — "справился в пределах бюджета попыток").
pass^k — доля задач, где ВСЕ первые k попыток (или все имеющиеся, если их
меньше k) прошли гейты (строгая метрика на объективном сигнале — "стабильно
рабочий код", а не "в итоге дожали"; verdict=approve возможен максимум на
одной попытке в escalation-цикле, поэтому строить pass^k на verdict было бы
вырожденно).

Обе метрики исключают trivial-tier задачи (auto-approve без ревьюера — не
несёт сигнала "справился с фидбэком") и задачи без единой попытки.
"""
from __future__ import annotations

from pathlib import Path

from harness.domain.models import Run, Task
from harness.interface.cli import _format_metrics_line, _metrics_summary
from harness.store.repository import Store


def _seed_run(store: Store, run_id: str = "r1") -> None:
    store.create_run(Run(id=run_id, project="p", goal="g", status="running"))


def _add_task(store: Store, run_id: str, task_id: str, complexity: str = "small") -> None:
    store.upsert_task(Task(
        id=task_id, run_id=run_id, title=f"t{task_id}", spec_path="x",
        status="pending", complexity=complexity,
    ))


def _add_attempt(
    store: Store, run_id: str, task_id: str, number: int, *, gates_passed: bool, verdict: str,
) -> None:
    attempt_id = store.start_attempt(run_id, task_id, number, model="auto")
    store.finish_attempt(
        attempt_id, run_id=run_id, task_id=task_id, worker_output="",
        gates_passed=gates_passed, verdict=verdict, cost_credits=0.1,
    )


def test_pass_at_1_true_when_first_attempt_approved(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")

    assert store.pass_at_k("r1", 1) == 1.0


def test_pass_at_k_any_of_first_k_counts(tmp_path: Path) -> None:
    """changes, changes, approve → pass@1=0.0, pass@3=1.0 (нашёл на 3-й)."""
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=False, verdict="changes")
    _add_attempt(store, "r1", "001", 2, gates_passed=False, verdict="changes")
    _add_attempt(store, "r1", "001", 3, gates_passed=True, verdict="approve")

    assert store.pass_at_k("r1", 1) == 0.0
    assert store.pass_at_k("r1", 3) == 1.0


def test_pass_at_k_only_counts_window(tmp_path: Path) -> None:
    """Approve на 3-й попытке не засчитывается в pass@2 (окно первых 2)."""
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=False, verdict="changes")
    _add_attempt(store, "r1", "001", 2, gates_passed=False, verdict="changes")
    _add_attempt(store, "r1", "001", 3, gates_passed=True, verdict="approve")

    assert store.pass_at_k("r1", 2) == 0.0


def test_pass_at_k_excludes_trivial_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001", complexity="trivial")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")
    _add_task(store, "r1", "002", complexity="small")
    _add_attempt(store, "r1", "002", 1, gates_passed=False, verdict="changes")

    # Только задача "002" (small) считается; она провалилась → pass@1 = 0.0
    assert store.pass_at_k("r1", 1) == 0.0


def test_pass_at_k_excludes_tasks_without_attempts(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")
    _add_task(store, "r1", "002")  # pending, ни одной попытки

    assert store.pass_at_k("r1", 1) == 1.0  # знаменатель = 1, не 2


def test_pass_at_k_none_when_nothing_to_measure(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001", complexity="trivial")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")

    assert store.pass_at_k("r1", 1) is None
    assert store.pass_all_k("r1", 3) is None


def test_pass_all_k_true_when_all_window_attempts_green(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="changes")
    _add_attempt(store, "r1", "001", 2, gates_passed=True, verdict="changes")
    _add_attempt(store, "r1", "001", 3, gates_passed=True, verdict="approve")

    assert store.pass_all_k("r1", 3) == 1.0


def test_pass_all_k_false_when_one_gate_red(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="changes")
    _add_attempt(store, "r1", "001", 2, gates_passed=False, verdict="changes")
    _add_attempt(store, "r1", "001", 3, gates_passed=True, verdict="approve")

    assert store.pass_all_k("r1", 3) == 0.0


def test_pass_all_k_uses_available_attempts_when_fewer_than_k(tmp_path: Path) -> None:
    """Задача с 1 попыткой (approve сразу) и k=3 — окно = имеющаяся попытка."""
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")

    assert store.pass_all_k("r1", 3) == 1.0


def test_pass_at_k_averages_across_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")
    _add_task(store, "r1", "002")
    _add_attempt(store, "r1", "002", 1, gates_passed=False, verdict="changes")

    assert store.pass_at_k("r1", 1) == 0.5


def test_metrics_summary_has_three_keys(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")

    summary = _metrics_summary(store, "r1")
    assert set(summary) == {"pass@1", "pass@3", "pass^3"}
    assert summary["pass@1"] == 1.0


def test_format_metrics_line_renders_values(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")
    _add_attempt(store, "r1", "001", 1, gates_passed=True, verdict="approve")

    line = _format_metrics_line(store, "r1")
    assert line.startswith("Metrics: ")
    assert "pass@1=1.00" in line


def test_format_metrics_line_empty_when_no_data(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    _seed_run(store)
    _add_task(store, "r1", "001")  # без попыток

    assert _format_metrics_line(store, "r1") == ""
