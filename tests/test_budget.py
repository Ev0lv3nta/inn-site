import json
from decimal import Decimal

import pytest

from inn_site.budget import Budget, BudgetError


def test_estimated_reservation_prevents_overspend(tmp_path):
    budget = Budget(tmp_path / "spend.json", Decimal("0.01"))
    budget.reserve(Decimal("0.008"))
    with pytest.raises(BudgetError):
        budget.reserve(Decimal("0.003"))
    budget.settle(Decimal("0.008"), Decimal("0.001"))
    budget.reserve(Decimal("0.008"))
    assert Decimal(json.loads(budget.path.read_text())["allocated_usd"]) == Decimal("0.009")
    assert Decimal(json.loads(budget.path.read_text())["observed_usd"]) == Decimal("0.001")
