from __future__ import annotations

from decimal import Decimal

import pytest

from mrs3.config import AlgorithmConfig
from mrs3.lots import equal_lots, income_lots, scale_lots_dd5


@pytest.mark.parametrize("order_count", [2, 3, 4])
def test_equal_lots_sum_exactly_one(order_count: int) -> None:
    lots = equal_lots(order_count, AlgorithmConfig.defaults())
    assert len(lots) == order_count
    assert sum(lots) == Decimal("1")


def test_income_lots_are_proportional_and_sum_exactly_one() -> None:
    lots = income_lots(
        [Decimal("10"), Decimal("20"), Decimal("30")],
        AlgorithmConfig.defaults(),
    )
    assert lots == (
        Decimal("0.166666666667"),
        Decimal("0.333333333333"),
        Decimal("0.500000000000"),
    )
    assert sum(lots) == Decimal("1")


def test_income_lots_reject_nonpositive_source_pnl() -> None:
    with pytest.raises(ValueError, match="positive"):
        income_lots([Decimal("10"), Decimal("0")], AlgorithmConfig.defaults())


def test_dd5_scaling_does_not_cap_individual_lot() -> None:
    lots, scale = scale_lots_dd5(
        (Decimal("1"),), Decimal("2"), AlgorithmConfig.defaults()
    )
    assert scale == Decimal("2.5")
    assert lots == (Decimal("2.500000000000"),)

