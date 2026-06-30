"""Cost governor: жёсткий потолок расходов на Run.

Перед дорогим вызовом агента движок спрашивает governor.check(). При превышении
бросается BudgetExceeded — движок ставит Run на PAUSED (а не молча жжёт деньги).
"""

from __future__ import annotations


class BudgetExceeded(RuntimeError):
    def __init__(self, spent: float, budget: float) -> None:
        super().__init__(f"бюджет исчерпан: потрачено {spent:.2f} из {budget:.2f}")
        self.spent = spent
        self.budget = budget


class CostGovernor:
    def __init__(self, budget: float | None) -> None:
        self._budget = budget

    def check(self, spent: float) -> None:
        """Бросает BudgetExceeded, если расход достиг потолка. None = без потолка."""
        if self._budget is not None and spent >= self._budget:
            raise BudgetExceeded(spent, self._budget)

    def remaining(self, spent: float) -> float | None:
        return None if self._budget is None else max(0.0, self._budget - spent)
