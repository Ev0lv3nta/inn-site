import json
from decimal import Decimal
from pathlib import Path

from filelock import FileLock


class BudgetError(RuntimeError):
    pass


class Budget:
    """Резервируем верхнюю оценку до запроса, учитываем фактическую цену после."""

    def __init__(self, path: Path, limit: Decimal):
        self.path, self.limit = path, limit

    def update(self, change: Decimal, actual: Decimal = Decimal(0)):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock", timeout=10):
            if self.path.exists():
                data = json.loads(self.path.read_text())
            else:
                data = {"allocated_usd": "0", "observed_usd": "0"}
            total = Decimal(data["allocated_usd"]) + change
            if total > self.limit:
                raise BudgetError("Достигнут лимит расходов LLM")
            data["allocated_usd"] = str(total)
            data["observed_usd"] = str(Decimal(data["observed_usd"]) + actual)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, indent=2))
            temp.replace(self.path)

    def reserve(self, amount: Decimal):
        self.update(amount)

    def settle(self, reserved: Decimal, actual: Decimal):
        self.update(actual - reserved, actual)
