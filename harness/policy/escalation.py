"""Многоступенчатая лестница эскалации моделей.

Политика: китайские модели подключаются НЕ сразу. Дешёвый `auto` получает несколько
попыток с правками/уточнениями ревьюера; только если он так и не справился — задача
поднимается на `kimi-k2.5`, затем на `glm-5.2`. Если задача изначально повышенной
сложности — стартовая ступень сдвигается на `kimi` (auto пропускается), дальше `glm`.

Лестница: [base(auto), rung1(kimi), rung2(glm), ...] задаётся в конфиге (ESCALATION_MODELS).
`escalate_after` — сколько попыток даётся СТАРТОВОЙ ступени, прежде чем подниматься
выше (по одной ступени за последующую попытку).
"""

from __future__ import annotations

from collections.abc import Sequence


class EscalationLadder:
    def __init__(self, base_model: str, rungs: Sequence[str], escalate_after: int = 2) -> None:
        self._base = base_model
        self._rungs = [m for m in rungs if m]
        self._after = max(1, escalate_after)

    def _index(self, attempt_number: int, start_index: int) -> int:
        """Индекс ступени для попытки (1-based) с учётом стартовой ступени задачи."""
        ladder_len = len(self._rungs) + 1
        start = max(0, min(start_index, ladder_len - 1))
        climb = max(0, attempt_number - self._after)  # подъём начинается после escalate_after
        return min(start + climb, ladder_len - 1)

    def model_for_attempt(self, attempt_number: int, start_index: int = 0) -> str:
        idx = self._index(attempt_number, start_index)
        return self.ladder[idx]

    def is_escalated(self, attempt_number: int, start_index: int = 0) -> bool:
        """True, если используется не базовая (auto) модель — т.е. подключены платные."""
        return self._index(attempt_number, start_index) > 0

    @property
    def ladder(self) -> list[str]:
        """Полная лестница, включая базовую: [base, rung1, rung2, ...]."""
        return [self._base, *self._rungs]
