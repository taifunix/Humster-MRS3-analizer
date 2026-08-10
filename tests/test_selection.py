from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.selection import (
    build_close_profiles,
    build_structures,
    choose_equivalent_default,
    close_status,
    equivalent,
    select_base_one_order,
    validate_order_tuple,
)


def _point(
    point_id: str,
    plateau_id: str,
    *,
    shift_bp: int,
    open_ma: int = 3,
    close_ma: int = 4,
    pnl: float = 100,
    efficiency: float = 10,
    trades: int = 20,
    standalone: bool = True,
    depth: bool = True,
) -> dict[str, object]:
    return {
        "point_id": point_id,
        "plateau_id": plateau_id,
        "symbol": "AAAUSDT",
        "side": "LONG",
        "timeframe": "2h",
        "shift_bp": shift_bp,
        "shift_pct": shift_bp / 100,
        "open_ma": open_ma,
        "close_ma": close_ma,
        "pnl_pct": pnl,
        "dd_pct": pnl / efficiency,
        "efficiency": efficiency,
        "pnl_dd5_theoretical": pnl * 5 / (pnl / efficiency),
        "win_rate_pct": 90.0,
        "profit_factor": 3.0,
        "trades": trades,
        "economic_pass": True,
        "standalone_eligible": standalone,
        "depth_eligible": depth,
    }


def _plateau(plateau_id: str, point_ids: tuple[str, ...]) -> dict[str, object]:
    return {
        "plateau_id": plateau_id,
        "symbol": "AAAUSDT",
        "side": "LONG",
        "timeframe": "2h",
        "all_point_ids": point_ids,
        "ready": True,
    }


def test_equivalence_includes_exact_five_percent() -> None:
    left = {"pnl_pct": 95, "efficiency": 9.5}
    right = {"pnl_pct": 100, "efficiency": 10}
    assert equivalent(left, right, Decimal("0.05"))


def test_equivalent_group_prefers_larger_shift_then_stable_ties() -> None:
    rows = pd.DataFrame(
        [
            _point("LOW", "P1", shift_bp=190, pnl=100, efficiency=10),
            _point("HIGH", "P1", shift_bp=230, pnl=97, efficiency=9.7),
        ]
    )
    chosen = choose_equivalent_default(rows, AlgorithmConfig.defaults())
    assert chosen["point_id"] == "HIGH"


@pytest.mark.parametrize(
    ("support", "status"),
    [
        (Decimal("0.90"), "CORE_CLOSE"),
        (Decimal("0.75"), "SUPPORTED_CLOSE"),
        (Decimal("0.7499"), "UNSUPPORTED_CLOSE"),
    ],
)
def test_close_support_boundaries(support: Decimal, status: str) -> None:
    assert close_status(support, AlgorithmConfig.defaults()) == status


def test_close_expansion_stops_after_failed_period() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8),
        _point("C6", "P1", shift_bp=230, close_ma=6, pnl=70, efficiency=7),
        _point("C7", "P1", shift_bp=230, close_ma=7, pnl=95, efficiency=9.5),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])

    _, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert list(profile["close_ma"]) == [4, 5, 6]
    assert list(profile["status"]) == [
        "PRIMARY_CLOSE",
        "SUPPORTED_CLOSE",
        "UNSUPPORTED_CLOSE",
    ]


def test_close_missing_required_cell_is_refine_diagnostic() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4),
        _point("C6", "P1", shift_bp=230, close_ma=6),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C6"))])

    updated, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert profile.query("close_ma == 5").iloc[0]["status"] == "REFINE_REQUIRED_CLOSE"
    assert updated.iloc[0]["close_refine_required"]


def test_close_existing_outside_plateau_is_unsupported_not_refine() -> None:
    points = pd.DataFrame(
        [
            _point("C4", "P1", shift_bp=230, close_ma=4),
            _point("C5_OTHER", "P2", shift_bp=230, close_ma=5),
        ]
    )
    plateaus = pd.DataFrame([_plateau("P1", ("C4",))])

    updated, profile = build_close_profiles(
        points, plateaus, AlgorithmConfig.defaults()
    )

    neighbor = profile.query("close_ma == 5").iloc[0]
    assert neighbor["status"] == "UNSUPPORTED_CLOSE"
    assert not neighbor["refine_required"]
    assert not updated.iloc[0]["close_refine_required"]


def test_close_alternative_uses_configured_open_ma_radius() -> None:
    points = pd.DataFrame(
        [
            _point("C4", "P1", shift_bp=230, open_ma=3, close_ma=4),
            _point(
                "C5",
                "P1",
                shift_bp=230,
                open_ma=5,
                close_ma=5,
                pnl=80,
                efficiency=8,
            ),
        ]
    )
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C5"))])
    config = replace(AlgorithmConfig.defaults(), ma_neighbor_radius=2)

    _, profile = build_close_profiles(points, plateaus, config)

    alternative = profile.loc[profile["close_ma"].eq(5)].iloc[0]
    assert alternative["status"] == "SUPPORTED_CLOSE"
    assert alternative["point_id"] == "C5"


def test_base_one_order_uses_dd5_after_plateau_local_equivalence() -> None:
    points = pd.DataFrame(
        [
            _point("P1_LOW", "P1", shift_bp=190, pnl=100, efficiency=10),
            _point("P1_HIGH", "P1", shift_bp=230, pnl=97, efficiency=9.7),
            _point("P2", "P2", shift_bp=270, pnl=80, efficiency=12),
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau("P1", ("P1_LOW", "P1_HIGH")), _plateau("P2", ("P2",))]
    )

    selected = select_base_one_order(points, plateaus, AlgorithmConfig.defaults())

    assert selected.iloc[0]["point_id"] == "P2"
    assert selected.iloc[0]["selection_type"] == "BASE_1ORD"


def test_base_one_order_fallback_uses_configured_target_drawdown() -> None:
    points = pd.DataFrame(
        [_point("P1", "P1", shift_bp=230, pnl=100, efficiency=10)]
    ).drop(columns=["pnl_dd5_theoretical"])
    plateaus = pd.DataFrame([_plateau("P1", ("P1",))])
    config = replace(AlgorithmConfig.defaults(), target_dd_pct=Decimal("7"))

    selected = select_base_one_order(points, plateaus, config)

    assert selected.iloc[0]["pnl_dd5_theoretical"] == pytest.approx(70.0)


def _structure_fixture(
    *, second_standalone: bool = True, first_standalone: bool = True
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    specs = [
        ("P1", "A", 110, first_standalone),
        ("P2", "B", 170, second_standalone),
        ("P3", "C", 250, True),
        ("P4", "D", 330, True),
    ]
    points = pd.DataFrame(
        [
            _point(point_id, plateau_id, shift_bp=shift, standalone=standalone)
            for plateau_id, point_id, shift, standalone in specs
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau(plateau_id, (point_id,)) for plateau_id, point_id, _, _ in specs]
    )
    profiles = pd.DataFrame(
        [
            {
                "plateau_id": plateau_id,
                "symbol": "AAAUSDT",
                "side": "LONG",
                "timeframe": "2h",
                "close_ma": 4,
                "support": 1.0,
                "status": "PRIMARY_CLOSE",
                "point_id": point_id,
            }
            for plateau_id, point_id, _, _ in specs
        ]
    )
    return points, plateaus, profiles


def test_two_three_four_orders_are_generated_independently() -> None:
    structures, _ = build_structures(*_structure_fixture(), AlgorithmConfig.defaults())
    assert set(structures["order_count"]) == {2, 3, 4}


def test_deep_order_does_not_require_standalone_sample() -> None:
    structures, _ = build_structures(
        *_structure_fixture(second_standalone=False), AlgorithmConfig.defaults()
    )
    matching = structures.loc[
        structures["orders"].map(
            lambda orders: tuple(order["point_id"] for order in orders) == ("A", "B")
        )
    ]
    assert len(matching) == 1
    assert matching.iloc[0]["low_sample_depth_count"] == 1


def test_first_order_requires_standalone_eligibility() -> None:
    structures, rejected = build_structures(
        *_structure_fixture(first_standalone=False), AlgorithmConfig.defaults()
    )
    assert not structures["orders"].map(
        lambda orders: tuple(order["point_id"] for order in orders) == ("A", "B")
    ).any()
    assert "NO_STANDALONE_ELIGIBLE_FIRST_ORDER" in set(rejected["reason"])


@pytest.mark.parametrize(
    ("left", "right", "want_status"),
    [
        (90, 150, "READY_MRS3_STRUCTURE"),
        (150, 230, "READY_MRS3_STRUCTURE"),
        (150, 220, "GAP_TOO_SMALL"),
        (410, 500, "DEEP_GAP_RESEARCH"),
    ],
)
def test_gap_boundaries(left: int, right: int, want_status: str) -> None:
    orders = [
        {"plateau_id": "P1", "shift_bp": left, "standalone_eligible": True, "depth_eligible": True},
        {"plateau_id": "P2", "shift_bp": right, "standalone_eligible": True, "depth_eligible": True},
    ]
    assert validate_order_tuple(orders, AlgorithmConfig.defaults()) == want_status


def test_gap_zone_boundary_comes_from_configuration() -> None:
    config = replace(AlgorithmConfig.defaults(), gap_mid_start_bp=200)
    orders = [
        {
            "plateau_id": "P1",
            "shift_bp": 190,
            "standalone_eligible": True,
            "depth_eligible": True,
        },
        {
            "plateau_id": "P2",
            "shift_bp": 250,
            "standalone_eligible": True,
            "depth_eligible": True,
        },
    ]

    assert validate_order_tuple(orders, config) == "READY_MRS3_STRUCTURE"


def test_same_plateau_cannot_produce_two_orders() -> None:
    orders = [
        {"plateau_id": "P1", "shift_bp": 110, "standalone_eligible": True, "depth_eligible": True},
        {"plateau_id": "P1", "shift_bp": 170, "standalone_eligible": True, "depth_eligible": True},
    ]
    assert validate_order_tuple(orders, AlgorithmConfig.defaults()) == "SAME_PLATEAU_USED_TWICE"
