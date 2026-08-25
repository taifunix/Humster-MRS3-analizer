from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.selection import (
    OPERATIONAL_FACTS_VERSION,
    build_close_profiles,
    build_structures,
    choose_cma_representative,
    choose_equivalent_default,
    choose_primary_representative,
    close_status,
    compute_close_support,
    equivalent,
    has_frozen_operational_facts,
    recompute_continuity,
    required_gap_bp,
    require_complete_operational_facts,
    select_base_one_order,
    validate_frozen_operational_facts,
    _order_from_point,
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
        "event_mode": "legacy_trades_proxy",
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


def _profile_row(
    plateau_id: str,
    point_id: str,
    *,
    close_ma: int = 4,
    support: float = 1.0,
    status: str = "PRIMARY_CLOSE",
    continuity_status: str = "USABLE",
    usable: bool = True,
) -> dict[str, object]:
    return {
        "plateau_id": plateau_id,
        "symbol": "AAAUSDT",
        "side": "LONG",
        "timeframe": "2h",
        "close_ma": close_ma,
        "support": support,
        "status": status,
        "point_id": point_id,
        "continuity_status": continuity_status,
        "usable": usable,
    }


def _event_point(
    point_id: str,
    *,
    close_ma: int,
    shift_bp: int,
    pnl: float,
    efficiency: float,
    events: int,
    event_eligible: bool = True,
    open_ma: int = 3,
    trades: int = 20,
) -> dict[str, object]:
    row = _point(
        point_id,
        "P1",
        shift_bp=shift_bp,
        open_ma=open_ma,
        close_ma=close_ma,
        pnl=pnl,
        efficiency=efficiency,
        trades=trades,
    )
    row["point_event_count"] = events
    row["event_eligible"] = event_eligible
    return row


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


def test_equivalent_group_prefers_more_independent_events_before_shift() -> None:
    rows = pd.DataFrame([
        {**_point("MORE_EVENTS", "P1", shift_bp=190, pnl=100, efficiency=10), "point_event_count": 5},
        {**_point("MORE_SHIFT", "P1", shift_bp=230, pnl=97, efficiency=9.7), "point_event_count": 3},
    ])

    chosen = choose_equivalent_default(rows, AlgorithmConfig.defaults())

    assert chosen["point_id"] == "MORE_EVENTS"


def test_real_event_representative_rejects_missing_point_event_count() -> None:
    rows = pd.DataFrame([
        {**_point("REAL", "P1", shift_bp=190), "event_mode": "real_independent_events"}
    ])

    with pytest.raises(ValueError, match="point_event_count"):
        choose_equivalent_default(rows, AlgorithmConfig.defaults())


@pytest.mark.parametrize("value", [None, float("nan")])
def test_real_order_rejects_missing_plateau_diagnostics_descriptively(value: object) -> None:
    point = pd.Series({
        **_point("REAL", "P1", shift_bp=190),
        "event_mode": "real_independent_events",
        "plateau_point_count": value,
        "base_point_trades": 20,
        "plateau_total_trades": 20,
        "point_event_count": 1,
    })

    with pytest.raises(ValueError, match="REAL.*plateau diagnostics"):
        _order_from_point(point, 1.0)


@pytest.mark.parametrize(
    ("support", "status"),
    [
        (Decimal("0.90"), "CORE_CLOSE"),
        (Decimal("0.60"), "SUPPORTED_CLOSE"),
        (Decimal("0.5999"), "UNSUPPORTED_CLOSE"),
    ],
)
def test_close_support_boundaries(support: Decimal, status: str) -> None:
    assert close_status(support, AlgorithmConfig.defaults()) == status


def test_close_profile_has_one_representative_per_present_close_ma() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8),
        _point("C6", "P1", shift_bp=230, close_ma=6, pnl=55, efficiency=5.5),
        _point("C7", "P1", shift_bp=230, close_ma=7, pnl=95, efficiency=9.5),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])

    _, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert list(profile["close_ma"]) == [4, 5, 6, 7]
    assert list(profile["status"]) == [
        "PRIMARY_CLOSE",
        "SUPPORTED_CLOSE",
        "UNSUPPORTED_CLOSE",
        "CORE_CLOSE",
    ]


def test_close_ma_missing_from_plateau_has_no_representative() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4),
        _point("C6", "P1", shift_bp=230, close_ma=6),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C6"))])

    updated, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert profile.query("close_ma == 5").empty
    assert list(profile["close_ma"]) == [4, 6]
    assert not updated.iloc[0]["close_refine_required"]


def test_outside_plateau_close_ma_point_is_not_this_plateau_representative() -> None:
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

    assert profile.query("close_ma == 5").empty
    assert list(profile["close_ma"]) == [4]
    assert not updated.iloc[0]["close_refine_required"]


def test_close_representative_ignores_open_ma_and_shift_distance() -> None:
    points = pd.DataFrame(
        [
            _point("C4", "P1", shift_bp=230, open_ma=3, close_ma=4),
            _point(
                "C5",
                "P1",
                shift_bp=230,
                open_ma=9,
                close_ma=5,
                pnl=80,
                efficiency=8,
            ),
        ]
    )
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C5"))])

    _, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    alternative = profile.loc[profile["close_ma"].eq(5)].iloc[0]
    assert alternative["status"] == "SUPPORTED_CLOSE"
    assert alternative["point_id"] == "C5"


def test_event_ineligible_close_ma_has_no_representative_row() -> None:
    points = pd.DataFrame([
        {**_point("C4", "P1", shift_bp=230, close_ma=4), "event_eligible": True},
        {**_point("C5", "P1", shift_bp=230, close_ma=5, pnl=100, efficiency=10), "event_eligible": False},
    ])
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C5"))])

    _, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert list(profile["close_ma"]) == [4]
    assert profile.query("close_ma == 5").empty


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

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())
    selected = select_base_one_order(points, updated, AlgorithmConfig.defaults())

    assert selected.iloc[0]["point_id"] == "P2"
    assert selected.iloc[0]["selection_type"] == "BASE_1ORD"


def test_base_one_order_skips_ready_plateau_without_frozen_base() -> None:
    points = pd.DataFrame([
        _point("NO_BASE", "P1", shift_bp=190, standalone=False),
        _point("BASE", "P2", shift_bp=230),
    ])
    plateaus = pd.DataFrame([
        {**_plateau("P1", ("NO_BASE",)), "base_1ord_point_id": None},
        {**_plateau("P2", ("BASE",)), "base_1ord_point_id": "BASE"},
    ])

    selected = select_base_one_order(points, plateaus, AlgorithmConfig.defaults())

    assert list(selected["point_id"]) == ["BASE"]


def test_base_one_order_fallback_uses_configured_target_drawdown() -> None:
    points = pd.DataFrame(
        [_point("P1", "P1", shift_bp=230, pnl=100, efficiency=10)]
    ).drop(columns=["pnl_dd5_theoretical"])
    plateaus = pd.DataFrame([_plateau("P1", ("P1",))])
    config = replace(AlgorithmConfig.defaults(), target_dd_pct=Decimal("7"))

    updated, _ = build_close_profiles(points, plateaus, config)
    selected = select_base_one_order(points, updated, config)

    assert selected.iloc[0]["pnl_dd5_theoretical"] == pytest.approx(70.0)


def test_base_one_order_selects_roles_per_exact_scope_with_frozen_pool() -> None:
    points = []
    plateaus = []
    for index, size in enumerate((2, 2, 3, 10, 11), start=1):
        point_id = f"P{index}"
        member_ids = tuple(f"P{index}_{member}" for member in range(size))
        for member, member_id in enumerate(member_ids):
            points.append(_point(member_id, point_id, shift_bp=70 + index * 100 + member, pnl=100 - index, efficiency=10))
        plateaus.append({
            **_plateau(point_id, member_ids),
            "base_1ord_point_id": member_ids[0],
            "plateau_point_count": size,
            "plateau_event_count": 20,
        })
    config = replace(
        AlgorithmConfig.defaults(), min_plateau_points=2, min_plateau_events_per_month=0,
    )
    for point in points:
        point["event_mode"] = "real_independent_events"

    selected = select_base_one_order(
        pd.DataFrame([{**point, "event_mode": "real_independent_events"} for point in points]),
        pd.DataFrame(plateaus),
        config,
    )

    assert list(selected["selection_role"]) == [
        "ECONOMY_1", "STABILITY_1", "STABILITY_2", "ECONOMY_2",
    ]
    assert list(selected["point_id"]) == ["P3_0", "P4_0", "P5_0", "P1_0"]


def test_base_one_order_primary_then_fallback_trace() -> None:
    sizes = (15, 23, 12, 7)
    points = []
    plateaus = []
    for index, size in enumerate(sizes, start=1):
        plateau_id = f"P{index}"
        member_ids = tuple(f"{plateau_id}_{member}" for member in range(size))
        for member_id in member_ids:
            points.append({
                **_point(member_id, plateau_id, shift_bp=100 + index, pnl=100 - index, efficiency=10),
                "pnl_dd5_theoretical": float(5 - index),
            })
        plateaus.append({
            **_plateau(plateau_id, member_ids),
            "base_1ord_point_id": member_ids[0],
            "plateau_point_count": size,
            "plateau_event_count": 20,
        })

    selected = select_base_one_order(
        pd.DataFrame([{**point, "event_mode": "real_independent_events"} for point in points]),
        pd.DataFrame(plateaus),
        replace(AlgorithmConfig.defaults(), min_plateau_points=2, min_plateau_events_per_month=0),
    )

    assert list(selected["selection_role"]) == [
        "ECONOMY_1", "STABILITY_1", "ECONOMY_2", "FALLBACK_1",
    ]
    assert list(selected["plateau_point_count"]) == list(sizes)


def test_base_one_order_frozen_median_is_once_and_equality_is_inclusive() -> None:
    sizes = (3, 5, 7, 9)
    points = []
    plateaus = []
    for index, size in enumerate(sizes, start=1):
        plateau_id = f"P{index}"
        member_ids = tuple(f"{plateau_id}_{member}" for member in range(size))
        for member_id in member_ids:
            points.append({
                **_point(member_id, plateau_id, shift_bp=100 + index, pnl=100 - index, efficiency=10),
                "pnl_dd5_theoretical": float(5 - index),
            })
        plateaus.append({
            **_plateau(plateau_id, member_ids),
            "base_1ord_point_id": member_ids[0],
            "plateau_point_count": size,
            "plateau_event_count": 20,
        })

    selected = select_base_one_order(
        pd.DataFrame([{**point, "event_mode": "real_independent_events"} for point in points]),
        pd.DataFrame(plateaus),
        replace(AlgorithmConfig.defaults(), min_plateau_points=2, min_plateau_events_per_month=0),
    )

    # median is 6; stability roles may only use the 7/9 pools, and 3/5 are still E pools.
    assert list(selected["plateau_point_count"]) == [3, 7, 9, 5]


@pytest.mark.parametrize("slots, expected", [(1, 1), (2, 2), (3, 3), (4, 4)])
def test_base_one_order_underfills_without_repeating_candidates(slots: int, expected: int) -> None:
    points = []
    plateaus = []
    for index, size in enumerate((3, 4), start=1):
        plateau_id = f"P{index}"
        member_ids = tuple(f"{plateau_id}_{member}" for member in range(size))
        points.extend(
            _point(member_id, plateau_id, shift_bp=100 + index, pnl=100 - index, efficiency=10)
            for member_id in member_ids
        )
        plateaus.append({
            **_plateau(plateau_id, member_ids),
            "base_1ord_point_id": member_ids[0],
            "plateau_point_count": size,
            "plateau_event_count": 20,
        })
    config = replace(
        AlgorithmConfig.defaults(),
        min_plateau_points=2,
        min_plateau_events_per_month=0,
        base_one_order_slots=slots,
    )

    selected = select_base_one_order(
        pd.DataFrame([{**point, "event_mode": "real_independent_events"} for point in points]),
        pd.DataFrame(plateaus),
        config,
    )

    assert len(selected) == min(expected, 2)
    assert selected["point_id"].is_unique


def test_base_one_order_is_deterministic_under_shuffle_and_ties() -> None:
    points = []
    plateaus = []
    for index, size in enumerate((3, 3, 7), start=1):
        plateau_id = f"P{index}"
        member_ids = tuple(f"{plateau_id}_{member}" for member in range(size))
        points.extend(
            _point(member_id, plateau_id, shift_bp=100, pnl=100, efficiency=10)
            for member_id in member_ids
        )
        plateaus.append({
            **_plateau(plateau_id, member_ids),
            "base_1ord_point_id": member_ids[0],
            "plateau_point_count": size,
            "plateau_event_count": 20,
        })
    config = replace(AlgorithmConfig.defaults(), min_plateau_points=2, min_plateau_events_per_month=0)
    real_points = pd.DataFrame([
        {**point, "event_mode": "real_independent_events"} for point in points
    ])
    expected = select_base_one_order(real_points, pd.DataFrame(plateaus), config)
    shuffled = select_base_one_order(
        real_points.sample(frac=1, random_state=7).reset_index(drop=True),
        pd.DataFrame(plateaus).sample(frac=1, random_state=11).reset_index(drop=True),
        config,
    )

    assert list(shuffled["point_id"]) == list(expected["point_id"])
    assert list(shuffled["selection_role"]) == list(expected["selection_role"])


def test_base_one_order_rejects_duplicate_exact_identity_globally() -> None:
    duplicate = _point("DUP", "P1", shift_bp=100)
    points = pd.DataFrame([duplicate, {**duplicate, "plateau_id": "P2"}])
    plateaus = pd.DataFrame([
        {**_plateau("P1", ("DUP",)), "base_1ord_point_id": "DUP"},
    ])

    with pytest.raises(ValueError, match="duplicate exact identity"):
        select_base_one_order(points, plateaus, AlgorithmConfig.defaults())


def test_base_one_order_rejects_missing_frozen_member_in_exact_scope() -> None:
    points = pd.DataFrame([{
        **_point("BASE", "P1", shift_bp=190),
        "event_mode": "real_independent_events",
    }])
    plateaus = pd.DataFrame([{
        **_plateau("P1", ("BASE", "MISSING")),
        "base_1ord_point_id": "BASE",
        "plateau_point_count": 2,
        "plateau_event_count": 20,
    }])

    with pytest.raises(ValueError, match=r"missing or duplicated plateau member MISSING.*AAAUSDT\|LONG\|2h"):
        select_base_one_order(points, plateaus, AlgorithmConfig.defaults())


def test_base_one_order_rejects_duplicate_frozen_member_in_exact_scope() -> None:
    points = pd.DataFrame([
        {**_point("BASE", "P1", shift_bp=190), "event_mode": "real_independent_events"},
        {**_point("M1", "P1", shift_bp=230), "event_mode": "real_independent_events"},
        {**_point("M1", "P1", shift_bp=270), "event_mode": "real_independent_events"},
    ])
    plateaus = pd.DataFrame([{
        **_plateau("P1", ("BASE", "M1")),
        "base_1ord_point_id": "BASE",
        "plateau_point_count": 2,
        "plateau_event_count": 20,
    }])

    with pytest.raises(ValueError, match=r"missing or duplicated plateau member M1.*AAAUSDT\|LONG\|2h"):
        select_base_one_order(points, plateaus, AlgorithmConfig.defaults())


def test_base_one_order_legacy_proxy_does_not_validate_monthly_integer() -> None:
    points = pd.DataFrame([{
        **_point("BASE", "P1", shift_bp=190),
        "event_mode": "legacy_trades_proxy",
    }])
    plateaus = pd.DataFrame([{
        **_plateau("P1", ("BASE",)),
        "base_1ord_point_id": "BASE",
        "plateau_point_count": 1,
        "plateau_event_count": "N/A_LEGACY_PROXY",
    }])

    selected = select_base_one_order(points, plateaus, AlgorithmConfig.defaults())

    assert list(selected["point_id"]) == ["BASE"]


def test_base_one_order_real_events_require_admission_diagnostics() -> None:
    points = pd.DataFrame([{
        **_point("BASE", "P1", shift_bp=190),
        "event_mode": "real_independent_events",
    }])
    plateaus = pd.DataFrame([{
        **_plateau("P1", ("BASE",)),
        "base_1ord_point_id": "BASE",
        "plateau_point_count": 1,
    }])

    with pytest.raises(ValueError, match="real_independent_events.*plateau_event_count"):
        select_base_one_order(points, plateaus, AlgorithmConfig.defaults())


def test_base_one_order_legacy_proxy_keeps_one_candidate_per_exact_scope() -> None:
    first = {**_point("BASE_2H", "P2H", shift_bp=190), "event_mode": "legacy_trades_proxy"}
    second = {
        **_point("BASE_1H", "P1H", shift_bp=230),
        "timeframe": "1h",
        "event_mode": "legacy_trades_proxy",
    }
    plateaus = pd.DataFrame([
        {**_plateau("P2H", ("BASE_2H",)), "base_1ord_point_id": "BASE_2H"},
        {
            **_plateau("P1H", ("BASE_1H",)),
            "timeframe": "1h",
            "base_1ord_point_id": "BASE_1H",
        },
    ])

    selected = select_base_one_order(
        pd.DataFrame([first, second]), plateaus, AlgorithmConfig.defaults()
    )

    assert list(selected["point_id"]) == ["BASE_1H", "BASE_2H"]


@pytest.mark.parametrize("mode", [None, "unsupported"])
def test_base_one_order_rejects_missing_or_unknown_event_mode(mode: object) -> None:
    point = _point("BASE", "P1", shift_bp=190)
    point["event_mode"] = mode
    plateaus = pd.DataFrame([{
        **_plateau("P1", ("BASE",)), "base_1ord_point_id": "BASE",
    }])

    with pytest.raises(ValueError, match="event_mode is required|unknown event mode"):
        select_base_one_order(pd.DataFrame([point]), plateaus, AlgorithmConfig.defaults())


def _structure_fixture(
    *, second_standalone: bool = True, first_standalone: bool = True
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    specs = [
        ("P1", "A", 70, first_standalone),
        ("P2", "B", 170, second_standalone),
        ("P3", "C", 310, True),
        ("P4", "D", 470, True),
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
            _profile_row(plateau_id, point_id)
            for plateau_id, point_id, _, _ in specs
        ]
    )
    return points, plateaus, profiles


def test_two_three_four_orders_are_generated_independently() -> None:
    structures, _ = build_structures(*_structure_fixture(), AlgorithmConfig.defaults())
    assert set(structures["order_count"]) == {2, 3, 4}


def test_multi_order_admission_excludes_below_threshold_real_event_plateau() -> None:
    points, plateaus, profiles = _structure_fixture()
    points["event_mode"] = "real_independent_events"
    points["plateau_point_count"] = 3
    points["base_point_trades"] = points["trades"]
    points["plateau_total_trades"] = points["trades"]
    points["point_event_count"] = 20
    plateaus["plateau_point_count"] = 3
    plateaus["plateau_event_count"] = [19, 20, 20, 20]

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    used_plateaus = {
        str(order["plateau_id"]) for orders in structures["orders"] for order in orders
    }
    assert "P1" not in used_plateaus
    assert {"P2", "P3"}.issubset(used_plateaus)


def test_multi_order_admission_requires_real_event_diagnostics() -> None:
    points, plateaus, profiles = _structure_fixture()
    points["event_mode"] = "real_independent_events"
    points["plateau_point_count"] = 3
    points["base_point_trades"] = points["trades"]
    points["plateau_total_trades"] = points["trades"]
    points["point_event_count"] = 20
    plateaus["plateau_point_count"] = 3

    with pytest.raises(ValueError, match="real_independent_events.*plateau_event_count"):
        build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())


def test_multi_order_admission_requires_diagnostics_without_usable_close_profiles() -> None:
    points, plateaus, profiles = _structure_fixture()
    points["event_mode"] = "real_independent_events"
    plateaus["plateau_point_count"] = 3

    with pytest.raises(ValueError, match="real_independent_events.*plateau_event_count"):
        build_structures(points, plateaus, profiles.iloc[0:0], AlgorithmConfig.defaults())


def test_structures_use_one_representative_per_plateau_and_close_ma() -> None:
    points = pd.DataFrame(
        [
            {**_point("P1_MORE_EVENTS", "P1", shift_bp=110), "point_event_count": 6, "event_eligible": True},
            {**_point("P1_MORE_SHIFT", "P1", shift_bp=120, pnl=98, efficiency=9.8), "point_event_count": 4, "event_eligible": True},
            {**_point("P2_ONLY", "P2", shift_bp=230), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau("P1", ("P1_MORE_EVENTS", "P1_MORE_SHIFT")), _plateau("P2", ("P2_ONLY",))]
    )
    profiles = pd.DataFrame(
        [
            _profile_row(plateau_id, point_id)
            for plateau_id, point_id in (("P1", "P1_MORE_EVENTS"), ("P2", "P2_ONLY"))
        ]
    )

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    used = {
        order["point_id"]
        for orders in structures["orders"]
        for order in orders
    }
    assert "P1_MORE_EVENTS" in used
    assert "P1_MORE_SHIFT" not in used


def test_structures_choose_one_representative_for_each_close_ma() -> None:
    points = pd.DataFrame(
        [
            {**_point("P1_C4_MORE_EVENTS", "P1", shift_bp=110, close_ma=4), "point_event_count": 6, "event_eligible": True},
            {**_point("P1_C4_MORE_SHIFT", "P1", shift_bp=120, close_ma=4, pnl=98, efficiency=9.8), "point_event_count": 4, "event_eligible": True},
            {**_point("P1_C5_ONLY", "P1", shift_bp=110, close_ma=5), "point_event_count": 5, "event_eligible": True},
            {**_point("P2_C4_ONLY", "P2", shift_bp=230, close_ma=4), "point_event_count": 5, "event_eligible": True},
            {**_point("P2_C5_ONLY", "P2", shift_bp=230, close_ma=5), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame([
        _plateau("P1", tuple(points.loc[points["plateau_id"].eq("P1"), "point_id"])),
        _plateau("P2", tuple(points.loc[points["plateau_id"].eq("P2"), "point_id"])),
    ])
    profiles = pd.DataFrame([
        _profile_row(plateau_id, point_id, close_ma=close_ma, status="SUPPORTED_CLOSE")
        for plateau_id, close_ma, point_id in (
            ("P1", 4, "P1_C4_MORE_EVENTS"), ("P1", 5, "P1_C5_ONLY"),
            ("P2", 4, "P2_C4_ONLY"), ("P2", 5, "P2_C5_ONLY"),
        )
    ])

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    assert set(structures["common_close_ma"]) == {4, 5}
    assert {
        (row.common_close_ma, tuple(order["point_id"] for order in row.orders))
        for row in structures.itertuples(index=False)
    } == {
        (4, ("P1_C4_MORE_EVENTS", "P2_C4_ONLY")),
        (5, ("P1_C5_ONLY", "P2_C5_ONLY")),
    }
    assert "source_pnl_sum" not in structures.columns


def test_structures_use_the_representative_frozen_in_close_profiles() -> None:
    points = pd.DataFrame([
        {**_point("P1_PROFILE", "P1", shift_bp=110), "point_event_count": 4, "event_eligible": True},
        {**_point("P1_OTHER", "P1", shift_bp=120), "point_event_count": 9, "event_eligible": True},
        {**_point("P2_PROFILE", "P2", shift_bp=230), "point_event_count": 5, "event_eligible": True},
    ])
    plateaus = pd.DataFrame([
        _plateau("P1", ("P1_PROFILE", "P1_OTHER")),
        _plateau("P2", ("P2_PROFILE",)),
    ])
    profiles = pd.DataFrame([
        _profile_row("P1", "P1_PROFILE"),
        _profile_row("P2", "P2_PROFILE"),
    ])

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    assert tuple(order["point_id"] for order in structures.iloc[0]["orders"]) == (
        "P1_PROFILE", "P2_PROFILE"
    )


@pytest.mark.parametrize(
    ("continuity_status", "usable"),
    [
        ("BLOCKED_BY_CONTINUITY", False),
        ("BREAK_UNSUPPORTED", False),
        ("USABLE", False),
    ],
)
def test_unusable_continuity_representative_cannot_form_structure(
    continuity_status: str, usable: bool
) -> None:
    points = pd.DataFrame(
        [
            {**_point("A", "P1", shift_bp=110), "point_event_count": 6, "event_eligible": True},
            {**_point("B", "P2", shift_bp=170), "point_event_count": 5, "event_eligible": True},
            {**_point("C", "P3", shift_bp=250), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau("P1", ("A",)), _plateau("P2", ("B",)), _plateau("P3", ("C",))]
    )
    profiles = pd.DataFrame(
        [
            _profile_row(pid, ptid, continuity_status=cont_status, usable=is_usable)
            for pid, ptid, cont_status, is_usable in (
                ("P1", "A", "USABLE", True),
                ("P2", "B", continuity_status, usable),
                ("P3", "C", "USABLE", True),
            )
        ]
    )

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    used_points = {
        str(order["point_id"]) for orders in structures["orders"] for order in orders
    }
    used_plateaus = {
        str(order["plateau_id"]) for orders in structures["orders"] for order in orders
    }
    assert "B" not in used_points
    assert "P2" not in used_plateaus
    assert any(
        len(orders) == 2
        and {str(order["plateau_id"]) for order in orders} == {"P1", "P3"}
        for orders in structures["orders"]
    )


def test_below_support_floor_representative_cannot_form_structure() -> None:
    points = pd.DataFrame(
        [
            {**_point("A", "P1", shift_bp=110), "point_event_count": 6, "event_eligible": True},
            {**_point("B", "P2", shift_bp=170), "point_event_count": 5, "event_eligible": True},
            {**_point("C", "P3", shift_bp=250), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau("P1", ("A",)), _plateau("P2", ("B",)), _plateau("P3", ("C",))]
    )
    profiles = pd.DataFrame(
        [
            _profile_row("P1", "A"),
            _profile_row("P2", "B", support=0.5, status="SUPPORTED_CLOSE"),
            _profile_row("P3", "C"),
        ]
    )

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    used_plateaus = {
        str(order["plateau_id"]) for orders in structures["orders"] for order in orders
    }
    assert "P2" not in used_plateaus
    assert "P1" in used_plateaus and "P3" in used_plateaus


def test_structure_uses_frozen_point_even_when_equivalent_alternate_exists() -> None:
    points = pd.DataFrame(
        [
            {**_point("P1_FROZEN", "P1", shift_bp=110), "point_event_count": 4, "event_eligible": True},
            {**_point("P1_ALT", "P1", shift_bp=120, pnl=98, efficiency=9.8), "point_event_count": 9, "event_eligible": True},
            {**_point("P2_ONLY", "P2", shift_bp=230), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau("P1", ("P1_FROZEN", "P1_ALT")), _plateau("P2", ("P2_ONLY",))]
    )
    profiles = pd.DataFrame(
        [
            _profile_row("P1", "P1_FROZEN"),
            _profile_row("P2", "P2_ONLY"),
        ]
    )

    fresh = choose_cma_representative(
        points.loc[points["plateau_id"].eq("P1")], 4, AlgorithmConfig.defaults()
    )
    assert fresh["point_id"] == "P1_ALT"

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    used_points = {
        str(order["point_id"]) for orders in structures["orders"] for order in orders
    }
    assert "P1_FROZEN" in used_points
    assert "P1_ALT" not in used_points


def test_one_plateau_never_supplies_two_orders_to_one_structure() -> None:
    points = pd.DataFrame(
        [
            {**_point("A1", "P1", shift_bp=110), "point_event_count": 6, "event_eligible": True},
            {**_point("A2", "P1", shift_bp=120, pnl=98, efficiency=9.8), "point_event_count": 5, "event_eligible": True},
            {**_point("B", "P2", shift_bp=180), "point_event_count": 5, "event_eligible": True},
            {**_point("C", "P3", shift_bp=250), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame(
        [
            _plateau("P1", ("A1", "A2")),
            _plateau("P2", ("B",)),
            _plateau("P3", ("C",)),
        ]
    )
    profiles = pd.DataFrame(
        [
            _profile_row("P1", "A1"),
            _profile_row("P1", "A2"),
            _profile_row("P2", "B"),
            _profile_row("P3", "C"),
        ]
    )

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    assert not structures.empty
    for orders in structures["orders"]:
        plateau_ids = [str(order["plateau_id"]) for order in orders]
        assert len(set(plateau_ids)) == len(plateau_ids)
    used_points = {
        str(order["point_id"]) for orders in structures["orders"] for order in orders
    }
    assert not {"A1", "A2"} <= used_points


def test_two_three_four_structures_are_independent_universes() -> None:
    specs = [
        ("P1", "A", 70),
        ("P2", "B", 170),
        ("P3", "C", 310),
        ("P4", "D", 470),
    ]
    points = pd.DataFrame(
        [
            {**_point(point_id, plateau_id, shift_bp=shift), "point_event_count": 6, "event_eligible": True}
            for plateau_id, point_id, shift in specs
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau(plateau_id, (point_id,)) for plateau_id, point_id, _ in specs]
    )
    profiles = pd.DataFrame(
        [_profile_row(plateau_id, point_id) for plateau_id, point_id, _ in specs]
    )

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    assert set(structures["order_count"]) == {2, 3, 4}
    assert (
        structures["order_count"].value_counts().sort_index().to_dict()
        == {2: 6, 3: 4, 4: 1}
    )
    structures_by_orders = {
        tuple(str(order["plateau_id"]) for order in orders): order_count
        for order_count, orders in zip(structures["order_count"], structures["orders"])
    }
    assert structures_by_orders[("P1", "P2")] == 2
    assert structures_by_orders[("P1", "P2", "P3")] == 3
    assert structures_by_orders[("P1", "P2", "P3", "P4")] == 4


def test_max_orders_caps_independent_universes() -> None:
    specs = [
        ("P1", "A", 70),
        ("P2", "B", 170),
        ("P3", "C", 310),
        ("P4", "D", 470),
    ]
    points = pd.DataFrame(
        [
            {**_point(point_id, plateau_id, shift_bp=shift), "point_event_count": 6, "event_eligible": True}
            for plateau_id, point_id, shift in specs
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau(plateau_id, (point_id,)) for plateau_id, point_id, _ in specs]
    )
    profiles = pd.DataFrame(
        [_profile_row(plateau_id, point_id) for plateau_id, point_id, _ in specs]
    )
    config = replace(AlgorithmConfig.defaults(), max_orders=3)

    structures, _ = build_structures(points, plateaus, profiles, config)

    assert set(structures["order_count"]) == {2, 3}


def test_structures_preserve_exact_point_event_counts() -> None:
    points = pd.DataFrame(
        [
            {**_point("A", "P1", shift_bp=70, trades=20), "point_event_count": 6, "event_eligible": True},
            {**_point("B", "P2", shift_bp=170, trades=15), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame([_plateau("P1", ("A",)), _plateau("P2", ("B",))])
    profiles = pd.DataFrame([_profile_row("P1", "A"), _profile_row("P2", "B")])

    structures, _ = build_structures(points, plateaus, profiles, AlgorithmConfig.defaults())

    row = structures.iloc[0]
    assert tuple(order["point_event_count"] for order in row["orders"]) == (6, 5)
    assert row["Order1EventCount"] == 6
    assert row["Order2EventCount"] == 5


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


def test_build_structures_rejects_gap_failures_without_deep_gap_status() -> None:
    points = pd.DataFrame(
        [
            {**_point("A", "P1", shift_bp=70), "point_event_count": 6, "event_eligible": True},
            {**_point("B", "P2", shift_bp=140), "point_event_count": 5, "event_eligible": True},
            {**_point("C", "P3", shift_bp=470), "point_event_count": 5, "event_eligible": True},
        ]
    )
    plateaus = pd.DataFrame(
        [_plateau("P1", ("A",)), _plateau("P2", ("B",)), _plateau("P3", ("C",))]
    )
    profiles = pd.DataFrame(
        [_profile_row("P1", "A"), _profile_row("P2", "B"), _profile_row("P3", "C")]
    )

    structures, diagnostics = build_structures(
        points, plateaus, profiles, AlgorithmConfig.defaults()
    )

    assert not structures.empty
    assert set(structures["status"]) == {"READY_MRS3_STRUCTURE"}
    assert not structures["orders"].map(
        lambda orders: tuple(order["point_id"] for order in orders) == ("A", "B")
    ).any()
    assert "GAP_TOO_SMALL" in set(diagnostics["reason"])
    assert not diagnostics["status"].eq("DEEP_GAP_RESEARCH").any()


@pytest.mark.parametrize(
    ("left", "right", "want_status"),
    [
        (70, 140, "GAP_TOO_SMALL"),
        (70, 170, "READY_MRS3_STRUCTURE"),
        (170, 270, "READY_MRS3_STRUCTURE"),
        (200, 310, "GAP_TOO_SMALL"),
        (200, 350, "READY_MRS3_STRUCTURE"),
        (310, 430, "GAP_TOO_SMALL"),
        (310, 470, "READY_MRS3_STRUCTURE"),
        (410, 500, "GAP_TOO_SMALL"),
    ],
)
def test_gap_boundaries(left: int, right: int, want_status: str) -> None:
    orders = [
        {"plateau_id": "P1", "shift_bp": left, "standalone_eligible": True, "depth_eligible": True},
        {"plateau_id": "P2", "shift_bp": right, "standalone_eligible": True, "depth_eligible": True},
    ]
    assert validate_order_tuple(orders, AlgorithmConfig.defaults()) == want_status


def test_gap_resolver_uses_configured_rules_not_hard_coded_thresholds() -> None:
    config = replace(
        AlgorithmConfig.defaults(),
        gap_rules=((30, 80, 10), (80, 200, 10), (200, 300, 10), (300, 551, 10)),
    )
    orders = [
        {"plateau_id": "P1", "shift_bp": 70, "standalone_eligible": True, "depth_eligible": True},
        {"plateau_id": "P2", "shift_bp": 90, "standalone_eligible": True, "depth_eligible": True},
    ]
    assert validate_order_tuple(orders, config) == "READY_MRS3_STRUCTURE"
    assert validate_order_tuple(orders, AlgorithmConfig.defaults()) == "GAP_TOO_SMALL"


def test_gap_validation_checks_every_adjacent_pair() -> None:
    orders = [
        {"plateau_id": "P1", "shift_bp": 70, "standalone_eligible": True, "depth_eligible": True},
        {"plateau_id": "P2", "shift_bp": 170, "standalone_eligible": True, "depth_eligible": True},
        {"plateau_id": "P3", "shift_bp": 230, "standalone_eligible": True, "depth_eligible": True},
    ]
    assert validate_order_tuple(orders, AlgorithmConfig.defaults()) == "GAP_TOO_SMALL"


def test_required_gap_bp_reads_configured_rules() -> None:
    config = AlgorithmConfig.defaults()
    assert required_gap_bp(30, config) == 80
    assert required_gap_bp(79, config) == 80
    assert required_gap_bp(80, config) == 100
    assert required_gap_bp(199, config) == 100
    assert required_gap_bp(200, config) == 130
    assert required_gap_bp(299, config) == 130
    assert required_gap_bp(300, config) == 150
    assert required_gap_bp(550, config) == 150


def test_required_gap_bp_rejects_uncovered_left_shift() -> None:
    config = AlgorithmConfig.defaults()
    with pytest.raises(ValueError, match="not covered"):
        required_gap_bp(551, config)
    with pytest.raises(ValueError, match="not covered"):
        required_gap_bp(29, config)


def test_same_plateau_cannot_produce_two_orders() -> None:
    orders = [
        {"plateau_id": "P1", "shift_bp": 110, "standalone_eligible": True, "depth_eligible": True},
        {"plateau_id": "P1", "shift_bp": 170, "standalone_eligible": True, "depth_eligible": True},
    ]
    assert validate_order_tuple(orders, AlgorithmConfig.defaults()) == "SAME_PLATEAU_USED_TWICE"


def test_cma_representative_considers_all_close_ma_members() -> None:
    rows = pd.DataFrame(
        [
            _event_point("NEAR", close_ma=4, shift_bp=110, open_ma=3, pnl=100, efficiency=10, events=4),
            _event_point("FAR_SHIFT", close_ma=4, shift_bp=230, open_ma=3, pnl=98, efficiency=9.8, events=6),
            _event_point("FAR_OPENMA", close_ma=4, shift_bp=110, open_ma=9, pnl=97, efficiency=9.7, events=5),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is not None
    assert chosen["point_id"] == "FAR_SHIFT"


def test_cma_economic_reference_does_not_use_event_count() -> None:
    rows = pd.DataFrame(
        [
            _event_point("HIGH_PNL", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=4),
            _event_point("MORE_EVENTS", close_ma=4, shift_bp=230, pnl=94, efficiency=9.4, events=9),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is not None
    assert chosen["point_id"] == "HIGH_PNL"


def test_cma_equivalence_uses_both_pnl_and_efficiency() -> None:
    rows = pd.DataFrame(
        [
            _event_point("REF", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=4),
            _event_point("PNL_OK_EFF_FAR", close_ma=4, shift_bp=230, pnl=96, efficiency=8, events=9),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is not None
    assert chosen["point_id"] == "REF"


def test_cma_event_filter_runs_after_equivalence() -> None:
    rows = pd.DataFrame(
        [
            _event_point("INELIGIBLE_REF", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=9, event_eligible=False),
            _event_point("ELIGIBLE_NEAR", close_ma=4, shift_bp=230, pnl=94, efficiency=9.4, events=9),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is None


def test_cma_event_count_beats_higher_shift() -> None:
    rows = pd.DataFrame(
        [
            _event_point("MORE_EVENTS", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=6),
            _event_point("HIGHER_SHIFT", close_ma=4, shift_bp=230, pnl=97, efficiency=9.7, events=4),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is not None
    assert chosen["point_id"] == "MORE_EVENTS"


def test_cma_shift_tie_breaker_prefers_higher_shift() -> None:
    rows = pd.DataFrame(
        [
            _event_point("LOWER_SHIFT", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=4),
            _event_point("HIGHER_SHIFT", close_ma=4, shift_bp=230, pnl=97, efficiency=9.7, events=4),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is not None
    assert chosen["point_id"] == "HIGHER_SHIFT"


def test_cma_event_ineligible_point_cannot_be_representative() -> None:
    rows = pd.DataFrame(
        [
            _event_point("ELIGIBLE_REF", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=4),
            _event_point("INELIGIBLE_MORE_EVENTS", close_ma=4, shift_bp=230, pnl=98, efficiency=9.8, events=9, event_eligible=False),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is not None
    assert chosen["point_id"] == "ELIGIBLE_REF"


def test_cma_post_filter_rows_keep_original_equivalent_group() -> None:
    rows = pd.DataFrame(
        [
            _event_point("REF", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=9, event_eligible=False),
            _event_point("NEAR1", close_ma=4, shift_bp=230, pnl=99, efficiency=10.5, events=4),
            _event_point("NEAR2", close_ma=4, shift_bp=230, pnl=98, efficiency=9.6, events=6),
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is not None
    assert chosen["point_id"] == "NEAR2"


def test_event_ineligible_global_best_does_not_suppress_other_cma() -> None:
    points = pd.DataFrame(
        [
            _event_point("BEST_INELIGIBLE", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=9, event_eligible=False),
            _event_point("C5_ELIGIBLE", close_ma=5, shift_bp=230, pnl=80, efficiency=8, events=5),
        ]
    )
    plateaus = pd.DataFrame([_plateau("P1", ("BEST_INELIGIBLE", "C5_ELIGIBLE"))])

    updated, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert list(profile["close_ma"]) == [5]
    assert profile.iloc[0]["point_id"] == "C5_ELIGIBLE"
    assert profile.iloc[0]["support"] == pytest.approx(1.0)
    assert profile.iloc[0]["status"] == "PRIMARY_CLOSE"
    assert updated.iloc[0]["primary_close_ma"] == 5


def test_cma_real_independent_events_retain_hard_floor() -> None:
    rows = pd.DataFrame(
        [
            {
                **_event_point("LOW_EVENTS", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=2),
                "event_mode": "real_independent_events",
            }
        ]
    )

    chosen = choose_cma_representative(rows, 4, AlgorithmConfig.defaults())

    assert chosen is None


def test_cma_one_representative_per_plateau_close_ma() -> None:
    points = pd.DataFrame(
        [
            _event_point("C4_A", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=6),
            _event_point("C4_B", close_ma=4, shift_bp=230, pnl=98, efficiency=9.8, events=4),
            _event_point("C5", close_ma=5, shift_bp=230, pnl=80, efficiency=8, events=5),
        ]
    )
    plateaus = pd.DataFrame([_plateau("P1", ("C4_A", "C4_B", "C5"))])

    _, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert len(profile.query("close_ma == 4")) == 1
    assert len(profile.query("close_ma == 5")) == 1
    assert profile.query("close_ma == 4").iloc[0]["point_id"] == "C4_A"


def test_cma_representative_deterministic_under_row_permutation() -> None:
    rows = [
        _event_point("A", close_ma=4, shift_bp=110, pnl=100, efficiency=10, events=6),
        _event_point("B", close_ma=4, shift_bp=230, pnl=98, efficiency=9.8, events=4),
        _event_point("C", close_ma=4, shift_bp=350, pnl=97, efficiency=9.7, events=5),
    ]
    frame = pd.DataFrame(rows)
    shuffled = pd.concat([frame.iloc[[2]], frame.iloc[[0]], frame.iloc[[1]]], ignore_index=True)

    assert choose_cma_representative(frame, 4, AlgorithmConfig.defaults())["point_id"] == "A"
    assert choose_cma_representative(shuffled, 4, AlgorithmConfig.defaults())["point_id"] == "A"


def _cma_plateau_rows() -> list[dict[str, object]]:
    return [
        _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8),
        _point("C6", "P1", shift_bp=230, close_ma=6, pnl=55, efficiency=5.5),
        _point("C7", "P1", shift_bp=230, close_ma=7, pnl=95, efficiency=9.5),
    ]


def _frozen_facts_metrics() -> dict[str, object]:
    return {
        "symbol": "AAAUSDT",
        "side": "LONG",
        "timeframe": "2h",
        "operational_facts_version": OPERATIONAL_FACTS_VERSION,
        "primary_close_ma": 4,
        "cma_representatives": [
            {
                "close_ma": 4,
                "point_id": "AAAUSDT|LONG|2h|230|3|4",
                "support": 1.0,
                "support_status": "PRIMARY_CLOSE",
                "continuity_status": "USABLE",
                "usable": True,
            },
            {
                "close_ma": 5,
                "point_id": "AAAUSDT|LONG|2h|230|3|5",
                "support": 0.8,
                "support_status": "SUPPORTED_CLOSE",
                "continuity_status": "USABLE",
                "usable": True,
            },
        ],
        "base_1ord_point_id": "AAAUSDT|LONG|2h|230|3|4",
        "standalone_eligible_point_ids": ("AAAUSDT|LONG|2h|230|3|4",),
    }


def _facts_context(metrics: dict[str, object]) -> dict[str, object]:
    members = {row["point_id"] for row in metrics["cma_representatives"]}
    return {
        "surface_point_ids": members | {"AAAUSDT|LONG|2h|230|3|6"},
        "plateau_all_point_ids": members,
        "standalone_eligible_point_ids": tuple(metrics.get("standalone_eligible_point_ids") or ()),
    }


def test_primary_close_is_existing_representative_with_support_one() -> None:
    points = pd.DataFrame(_cma_plateau_rows())
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in _cma_plateau_rows()))])

    updated, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert updated.iloc[0]["primary_close_ma"] == 4
    primary = profile.loc[profile["status"].eq("PRIMARY_CLOSE")].iloc[0]
    assert primary["close_ma"] == 4
    assert primary["support"] == pytest.approx(1.0)
    assert len(profile.query("status == 'PRIMARY_CLOSE'")) == 1


def test_primary_ordering_breaks_pnl_then_efficiency_then_trades_then_dd() -> None:
    config = AlgorithmConfig.defaults()

    def primary_for(rows: list[dict[str, object]]) -> int:
        points = pd.DataFrame(rows)
        plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])
        updated, _ = build_close_profiles(points, plateaus, config)
        return int(updated.iloc[0]["primary_close_ma"])

    assert primary_for([
        _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=95, efficiency=12),
    ]) == 4
    assert primary_for([
        _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=100, efficiency=10, trades=25),
    ]) == 5
    assert primary_for([
        _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10),
        {**_point("C5", "P1", shift_bp=230, close_ma=5, pnl=100, efficiency=10), "dd_pct": 5.0},
    ]) == 5
    assert primary_for([
        _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=100, efficiency=10),
    ]) == 4


@pytest.mark.parametrize("bad", [0, -1])
def test_primary_non_positive_pnl_fails_closed(bad: float) -> None:
    bad_row = _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10)
    bad_row["pnl_pct"] = bad
    bad_row["dd_pct"] = 10.0
    bad_row["pnl_dd5_theoretical"] = 50.0
    points = pd.DataFrame([
        bad_row,
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8),
    ])
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C5"))])

    with pytest.raises(ValueError, match="PnL"):
        build_close_profiles(points, plateaus, AlgorithmConfig.defaults())


@pytest.mark.parametrize("bad", [0, -1])
def test_primary_non_positive_efficiency_fails_closed(bad: float) -> None:
    bad_row = _point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10)
    bad_row["efficiency"] = bad
    bad_row["dd_pct"] = 10.0
    bad_row["pnl_dd5_theoretical"] = 50.0
    points = pd.DataFrame([
        bad_row,
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8),
    ])
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C5"))])

    with pytest.raises(ValueError, match="efficiency"):
        build_close_profiles(points, plateaus, AlgorithmConfig.defaults())


@pytest.mark.parametrize("field", ["pnl_pct", "efficiency"])
@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_close_support_operands_fail_closed(field: str, bad: float) -> None:
    primary = pd.Series({"pnl_pct": 100, "efficiency": 10})
    representative = pd.Series({"pnl_pct": 80, "efficiency": 8, field: bad})
    with pytest.raises(ValueError):
        compute_close_support(primary, representative)
    bad_primary = pd.Series({"pnl_pct": 100, "efficiency": 10, field: bad})
    with pytest.raises(ValueError):
        compute_close_support(bad_primary, pd.Series({"pnl_pct": 80, "efficiency": 8}))


def test_close_support_outside_one_fails_closed() -> None:
    with pytest.raises(ValueError, match="CloseSupport"):
        compute_close_support(
            pd.Series({"pnl_pct": 80, "efficiency": 8}),
            pd.Series({"pnl_pct": 100, "efficiency": 10}),
        )


def test_nan_primary_value_fails_the_whole_build_without_facts() -> None:
    points = pd.DataFrame([
        {**_point("C4", "P1", shift_bp=230, close_ma=4, pnl=100, efficiency=10), "pnl_pct": float("nan")},
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8),
    ])
    plateaus = pd.DataFrame([_plateau("P1", ("C4", "C5"))])

    with pytest.raises((ValueError, ArithmeticError)):
        build_close_profiles(points, plateaus, AlgorithmConfig.defaults())


def test_close_support_exact_90_60_classes_preserved() -> None:
    points = pd.DataFrame(_cma_plateau_rows())
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in _cma_plateau_rows()))])

    _, profile = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert list(profile["close_ma"]) == [4, 5, 6, 7]
    assert list(profile["status"]) == [
        "PRIMARY_CLOSE",
        "SUPPORTED_CLOSE",
        "UNSUPPORTED_CLOSE",
        "CORE_CLOSE",
    ]


def test_continuity_missing_intermediate_cma_blocks_outer() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4),
        _point("C6", "P1", shift_bp=230, close_ma=6, pnl=90, efficiency=9),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())
    by_cma = {row["close_ma"]: row for row in updated.iloc[0]["cma_representatives"]}

    assert by_cma[4]["continuity_status"] == "USABLE" and by_cma[4]["usable"] is True
    assert by_cma[6]["continuity_status"] == "BLOCKED_BY_CONTINUITY"
    assert by_cma[6]["usable"] is False


def test_continuity_unsupported_breaks_and_blocks_outer_high_support() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=55, efficiency=5.5),
        _point("C6", "P1", shift_bp=230, close_ma=6, pnl=99, efficiency=9.9),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())
    by_cma = {row["close_ma"]: row for row in updated.iloc[0]["cma_representatives"]}

    assert by_cma[5]["support_status"] == "UNSUPPORTED_CLOSE"
    assert by_cma[5]["continuity_status"] == "BREAK_UNSUPPORTED"
    assert by_cma[5]["usable"] is False
    assert by_cma[6]["support_status"] == "CORE_CLOSE"
    assert by_cma[6]["continuity_status"] == "BLOCKED_BY_CONTINUITY"
    assert by_cma[6]["usable"] is False


def test_continuity_adjacent_supported_rows_are_usable() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8),
        _point("C6", "P1", shift_bp=230, close_ma=6, pnl=95, efficiency=9.5),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())
    by_cma = {row["close_ma"]: row for row in updated.iloc[0]["cma_representatives"]}

    assert all(row["continuity_status"] == "USABLE" and row["usable"] is True for row in by_cma.values())


def test_frozen_facts_shape_is_ordered_unique_and_versioned() -> None:
    points = pd.DataFrame(_cma_plateau_rows())
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in _cma_plateau_rows()))])

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())
    row = updated.iloc[0]
    facts = row["cma_representatives"]

    assert row["operational_facts_version"] == OPERATIONAL_FACTS_VERSION
    assert row["primary_close_ma"] == 4
    assert [entry["close_ma"] for entry in facts] == [4, 5, 6, 7]
    assert len({entry["point_id"] for entry in facts}) == len(facts)
    assert {entry["support_status"] for entry in facts} == {
        "PRIMARY_CLOSE",
        "SUPPORTED_CLOSE",
        "UNSUPPORTED_CLOSE",
        "CORE_CLOSE",
    }
    assert all(0 < entry["support"] <= 1 for entry in facts)
    primary_entries = [entry for entry in facts if entry["support_status"] == "PRIMARY_CLOSE"]
    assert len(primary_entries) == 1
    assert primary_entries[0]["close_ma"] == row["primary_close_ma"]


def test_frozen_base_uses_usable_standalone_representatives_only() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4),
        _point("C5", "P1", shift_bp=230, close_ma=5, pnl=80, efficiency=8, standalone=False),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert updated.iloc[0]["base_1ord_point_id"] == "C4"


def test_frozen_base_is_null_when_no_standalone_usable_representative() -> None:
    points = pd.DataFrame([
        _point("C4", "P1", shift_bp=230, close_ma=4, standalone=False),
    ])
    plateaus = pd.DataFrame([_plateau("P1", ("C4",))])

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert updated.iloc[0]["base_1ord_point_id"] is None


def test_frozen_base_excludes_continuity_blocked_representatives() -> None:
    rows = [
        _point("C4", "P1", shift_bp=230, close_ma=4),
        _point("C6", "P1", shift_bp=230, close_ma=6, pnl=90, efficiency=9),
    ]
    points = pd.DataFrame(rows)
    plateaus = pd.DataFrame([_plateau("P1", tuple(row["point_id"] for row in rows))])

    updated, _ = build_close_profiles(points, plateaus, AlgorithmConfig.defaults())

    assert updated.iloc[0]["base_1ord_point_id"] == "C4"


def test_select_base_one_order_requires_frozen_facts() -> None:
    points = pd.DataFrame([_point("C4", "P1", shift_bp=230, close_ma=4)])
    plateaus = pd.DataFrame([_plateau("P1", ("C4",))]).drop(columns=["ready"])

    with pytest.raises(ValueError, match="frozen operational facts"):
        select_base_one_order(points, plateaus, AlgorithmConfig.defaults())


def test_validator_accepts_canonical_frozen_facts() -> None:
    metrics = _frozen_facts_metrics()
    validate_frozen_operational_facts(metrics, **_facts_context(metrics))
    assert has_frozen_operational_facts(metrics)


def test_validator_rejects_unknown_version() -> None:
    metrics = _frozen_facts_metrics()
    metrics["operational_facts_version"] = "bogus_v2"
    with pytest.raises(ValueError, match="version"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_unknown_statuses() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][1]["support_status"] = "MYSTERY_CLOSE"
    with pytest.raises(ValueError, match="support_status"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][1]["continuity_status"] = "MYSTERY"
    with pytest.raises(ValueError, match="continuity_status"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_support_status_contradiction() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][1]["support_status"] = "CORE_CLOSE"
    with pytest.raises(ValueError, match="support_status"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_recomputes_continuity_and_rejects_trusted_flags() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][1]["continuity_status"] = "BLOCKED_BY_CONTINUITY"
    metrics["cma_representatives"][1]["usable"] = False
    with pytest.raises(ValueError, match="continuity"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_duplicate_and_unordered_close_ma() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"].append(dict(metrics["cma_representatives"][1]))
    with pytest.raises(ValueError, match="duplicate"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"].reverse()
    with pytest.raises(ValueError, match="ordered"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_duplicate_point_id() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][1]["point_id"] = metrics["cma_representatives"][0]["point_id"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_representative_outside_surface() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][0]["point_id"] = "AAAUSDT|LONG|2h|310|3|4"
    with pytest.raises(ValueError, match="outside the surface"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_representative_outside_plateau() -> None:
    metrics = _frozen_facts_metrics()
    context = _facts_context(metrics)
    context["plateau_all_point_ids"] = {"AAAUSDT|LONG|2h|230|3|4"}
    with pytest.raises(ValueError, match="outside the Plateau"):
        validate_frozen_operational_facts(metrics, **context)


def test_validator_rejects_wrong_scope_and_close_ma() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][0]["point_id"] = "OTHER|LONG|2h|230|3|4"
    with pytest.raises(ValueError, match="scope"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][0]["point_id"] = "AAAUSDT|LONG|2h|230|3|7"
    with pytest.raises(ValueError, match="CloseMA"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_multiple_and_missing_primary() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][1]["support_status"] = "PRIMARY_CLOSE"
    metrics["cma_representatives"][1]["support"] = 1.0
    with pytest.raises(ValueError, match="PRIMARY_CLOSE"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][0]["support_status"] = "CORE_CLOSE"
    with pytest.raises(ValueError, match="PRIMARY_CLOSE"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_invalid_base_point() -> None:
    metrics = _frozen_facts_metrics()
    metrics["base_1ord_point_id"] = "AAAUSDT|LONG|2h|230|3|6"
    with pytest.raises(ValueError, match="base"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_base_not_usable_or_not_standalone() -> None:
    metrics = _frozen_facts_metrics()
    metrics["base_1ord_point_id"] = "AAAUSDT|LONG|2h|230|3|5"
    with pytest.raises(ValueError, match="standalone-eligible"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_base_not_continuity_usable() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"] = [
        metrics["cma_representatives"][0],
        {
            "close_ma": 6,
            "point_id": "AAAUSDT|LONG|2h|230|3|6",
            "support": 0.95,
            "support_status": "CORE_CLOSE",
            "continuity_status": "BLOCKED_BY_CONTINUITY",
            "usable": False,
        },
    ]
    metrics["base_1ord_point_id"] = "AAAUSDT|LONG|2h|230|3|6"
    metrics["standalone_eligible_point_ids"] = (
        "AAAUSDT|LONG|2h|230|3|4",
        "AAAUSDT|LONG|2h|230|3|6",
    )
    with pytest.raises(ValueError, match="continuity-usable"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_require_complete_operational_facts_accepts_null_base_key() -> None:
    metrics = _frozen_facts_metrics()
    metrics["base_1ord_point_id"] = None

    require_complete_operational_facts(metrics)


def test_require_complete_operational_facts_rejects_missing_fact_fields() -> None:
    with pytest.raises(ValueError, match="frozen operational facts"):
        require_complete_operational_facts({})
    metrics = _frozen_facts_metrics()
    del metrics["base_1ord_point_id"]
    with pytest.raises(ValueError, match="base_1ord_point_id"):
        require_complete_operational_facts(metrics)


def test_validator_requires_base_key_present_even_when_null() -> None:
    metrics = _frozen_facts_metrics()
    del metrics["base_1ord_point_id"]

    with pytest.raises(ValueError, match="base_1ord_point_id"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_extra_representative_field() -> None:
    metrics = _frozen_facts_metrics()
    metrics["cma_representatives"][0]["extra_persisted_field"] = "nope"

    with pytest.raises(ValueError, match="unexpected fields"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))


def test_validator_rejects_missing_representative_field() -> None:
    metrics = _frozen_facts_metrics()
    del metrics["cma_representatives"][0]["usable"]

    with pytest.raises(ValueError, match="missing fields"):
        validate_frozen_operational_facts(metrics, **_facts_context(metrics))
