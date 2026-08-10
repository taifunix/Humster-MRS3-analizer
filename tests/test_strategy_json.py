from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.lots import LotMethod, equal_lots
from mrs3.models import Side
from mrs3.strategy_json import (
    StrategyValidationError,
    generate_strategy,
    validate_strategy,
    validate_unique_names,
)


def _template() -> dict[str, object]:
    entry_long = {
        "id": 1,
        "side": "buy",
        "type": "SMA",
        "source": "ohlc4",
        "len": 3,
        "multiplier": 0.997,
        "lot_x": 1.0,
        "order_type": "limit",
        "post_only": True,
        "hidden": False,
        "value": None,
    }
    entry_short = {**entry_long, "side": "sell", "multiplier": 1.003}
    return {
        "name": "TEMPLATE",
        "is_runing": True,
        "unrelated": {"preserve": "exactly"},
        "basic": {
            "strategy": "mrs3",
            "symbol": "OLD",
            "time_frame": "1h",
            "use_long": True,
            "use_short": True,
        },
        "mrs3": {
            "ma_long": [entry_long],
            "ma_short": [entry_short],
            "ma_close_long": {
                "len": 4,
                "multiplier": 1.003,
                "side": "sell",
            },
            "ma_close_short": {
                "len": 4,
                "multiplier": 0.997,
                "side": "buy",
            },
        },
    }


def _structure(side: Side = Side.LONG, shifts: tuple[int, ...] = (270,)) -> dict[str, object]:
    orders = tuple(
        {
            "id": index,
            "plateau_id": f"P{index}",
            "point_id": f"POINT{index}",
            "open_ma": index + 2,
            "shift_bp": shift,
            "shift_pct": shift / 100,
            "source_pnl_pct": 10.0 * index,
            "source_dd_pct": 5.0,
            "source_efficiency": 2.0 * index,
            "trades": 20,
            "close_support": 1.0,
            "standalone_eligible": True,
            "depth_eligible": True,
        }
        for index, shift in enumerate(shifts, start=1)
    )
    return {
        "structure_id": "STR_0123456789abcdef",
        "symbol": "ADMSTOCK_USDT",
        "side": side.value,
        "timeframe": "2h",
        "common_close_ma": 4,
        "order_count": len(orders),
        "orders": orders,
        "status": "READY_MRS3_STRUCTURE",
    }


def _source_points(structure: dict[str, object]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "point_id": order["point_id"],
                "plateau_id": order["plateau_id"],
                "symbol": structure["symbol"],
                "side": structure["side"],
                "timeframe": structure["timeframe"],
                "shift_bp": order["shift_bp"],
                "shift_pct": order["shift_pct"],
                "open_ma": order["open_ma"],
                "close_ma": structure["common_close_ma"],
                "pnl_pct": order["source_pnl_pct"],
                "dd_pct": order["source_dd_pct"],
                "efficiency": order["source_efficiency"],
                "trades": order["trades"],
                "economic_pass": True,
                "standalone_eligible": order["standalone_eligible"],
                "depth_eligible": order["depth_eligible"],
                "refine_required": False,
            }
            for order in structure["orders"]
        ]
    )


def test_long_json_multiplier_flags_and_template_preservation() -> None:
    structure = _structure(Side.LONG)
    generated = generate_strategy(
        _template(),
        structure,
        (Decimal("1"),),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )

    assert generated["is_runing"] is False
    assert generated["basic"]["use_long"] is True
    assert generated["basic"]["use_short"] is False
    assert generated["mrs3"]["ma_long"][0]["multiplier"] == 0.973
    assert generated["mrs3"]["ma_close_long"]["multiplier"] == 1.003
    assert generated["unrelated"] == {"preserve": "exactly"}


def test_short_json_multiplier_and_close_multiplier() -> None:
    structure = _structure(Side.SHORT)
    generated = generate_strategy(
        _template(),
        structure,
        (Decimal("1"),),
        LotMethod.INCOME,
        AlgorithmConfig.defaults(),
    )

    assert generated["basic"]["use_long"] is False
    assert generated["basic"]["use_short"] is True
    assert generated["mrs3"]["ma_short"][0]["multiplier"] == 1.027
    assert generated["mrs3"]["ma_close_short"]["multiplier"] == 0.997


def test_four_orders_are_created_from_one_template_prototype() -> None:
    structure = _structure(Side.LONG, shifts=(90, 150, 230, 310))
    generated = generate_strategy(
        _template(),
        structure,
        equal_lots(4, AlgorithmConfig.defaults()),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )

    entries = generated["mrs3"]["ma_long"]
    assert [entry["id"] for entry in entries] == [1, 2, 3, 4]
    assert [entry["len"] for entry in entries] == [3, 4, 5, 6]


def test_generated_strategy_passes_hard_validation() -> None:
    structure = _structure(Side.LONG, shifts=(90, 150, 230))
    generated = generate_strategy(
        _template(),
        structure,
        equal_lots(3, AlgorithmConfig.defaults()),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )

    validate_strategy(
        generated, structure, _source_points(structure), AlgorithmConfig.defaults()
    )


def test_validation_rejects_structure_orders_not_sorted_by_shift() -> None:
    source_structure = _structure(Side.LONG, shifts=(190, 270))
    source_points = _source_points(source_structure)
    corrupted = deepcopy(source_structure)
    corrupted["orders"] = tuple(reversed(corrupted["orders"]))
    generated = generate_strategy(
        _template(),
        corrupted,
        equal_lots(2, AlgorithmConfig.defaults()),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )

    with pytest.raises(StrategyValidationError, match="sorted by shift"):
        validate_strategy(
            generated, corrupted, source_points, AlgorithmConfig.defaults()
        )


def test_validation_rejects_incorrect_structure_order_count() -> None:
    structure = _structure(Side.LONG, shifts=(190, 270))
    generated = generate_strategy(
        _template(),
        structure,
        equal_lots(2, AlgorithmConfig.defaults()),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )
    corrupted = deepcopy(structure)
    corrupted["order_count"] = 3

    with pytest.raises(StrategyValidationError, match="order_count"):
        validate_strategy(
            generated, corrupted, _source_points(structure), AlgorithmConfig.defaults()
        )


def test_validation_rejects_fabricated_source_point() -> None:
    structure = _structure(Side.LONG)
    generated = generate_strategy(
        _template(), structure, (Decimal("1"),), LotMethod.EQUAL, AlgorithmConfig.defaults()
    )

    with pytest.raises(StrategyValidationError, match="source audit"):
        validate_strategy(generated, structure, pd.DataFrame(columns=["point_id"]), AlgorithmConfig.defaults())


def test_validation_rejects_real_point_id_with_fabricated_parameters() -> None:
    source_structure = _structure(Side.LONG)
    source_points = _source_points(source_structure)
    corrupted = deepcopy(source_structure)
    corrupted["orders"][0]["shift_bp"] = 280
    corrupted["orders"][0]["shift_pct"] = 2.8
    generated = generate_strategy(
        _template(),
        corrupted,
        (Decimal("1"),),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )

    with pytest.raises(StrategyValidationError, match="source point fields"):
        validate_strategy(
            generated,
            corrupted,
            source_points,
            AlgorithmConfig.defaults(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strategy", "mrs2"),
        ("symbol", "WRONG_USDT"),
        ("time_frame", "1h"),
    ],
)
def test_validation_rejects_incorrect_generated_basic_metadata(
    field: str, value: str
) -> None:
    structure = _structure(Side.LONG)
    generated = generate_strategy(
        _template(),
        structure,
        (Decimal("1"),),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )
    generated["basic"][field] = value

    with pytest.raises(StrategyValidationError, match="basic metadata"):
        validate_strategy(
            generated,
            structure,
            _source_points(structure),
            AlgorithmConfig.defaults(),
        )


def test_validation_rejects_name_not_derived_from_structure() -> None:
    structure = _structure(Side.LONG)
    generated = generate_strategy(
        _template(),
        structure,
        (Decimal("1"),),
        LotMethod.EQUAL,
        AlgorithmConfig.defaults(),
    )
    generated["name"] = "UNRELATED"

    with pytest.raises(StrategyValidationError, match="strategy name"):
        validate_strategy(
            generated,
            structure,
            _source_points(structure),
            AlgorithmConfig.defaults(),
        )


def test_equal_and_income_names_are_unique() -> None:
    structure = _structure(Side.LONG)
    equal = generate_strategy(
        _template(), structure, (Decimal("1"),), LotMethod.EQUAL, AlgorithmConfig.defaults()
    )
    income = generate_strategy(
        _template(), structure, (Decimal("1"),), LotMethod.INCOME, AlgorithmConfig.defaults()
    )
    validate_unique_names([equal, income])
    assert equal["name"] != income["name"]


def test_duplicate_names_are_rejected() -> None:
    with pytest.raises(StrategyValidationError, match="duplicate strategy names"):
        validate_unique_names([{"name": "A"}, {"name": "A"}])
