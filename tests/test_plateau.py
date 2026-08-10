from __future__ import annotations

from decimal import Decimal

import pandas as pd

from mrs3.config import AlgorithmConfig
from mrs3.plateau import (
    build_plateaus,
    component_envelope,
    core_link,
    find_isolated_peaks,
)


def _metric_point(pnl: float, efficiency: float) -> dict[str, float]:
    return {"pnl_pct": pnl, "efficiency": efficiency}


def _point(
    point_id: str,
    *,
    shift_bp: int,
    open_ma: int,
    close_ma: int = 4,
    pnl: float = 100.0,
    efficiency: float = 10.0,
    trades: int = 20,
    sample: bool = True,
    history: bool = True,
) -> dict[str, object]:
    return {
        "point_id": point_id,
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
        "win_rate_pct": 90.0,
        "profit_factor": 3.0,
        "trades": trades,
        "economic_pass": True,
        "refine_required": False,
        "standalone_sample_pass": sample,
        "history_pass": history,
    }


def test_core_link_accepts_exactly_point_nine() -> None:
    assert core_link(
        _metric_point(pnl=90, efficiency=9),
        _metric_point(pnl=100, efficiency=10),
    ) == Decimal("0.9")


def test_component_envelope_accepts_exactly_point_seven_five() -> None:
    assert component_envelope(
        [
            _metric_point(pnl=75, efficiency=7.5),
            _metric_point(pnl=100, efficiency=10),
        ]
    ) == Decimal("0.75")


def test_supported_point_cannot_bridge_two_core_components() -> None:
    points = pd.DataFrame(
        [
            _point("A1", shift_bp=190, open_ma=2),
            _point("A2", shift_bp=190, open_ma=3),
            _point("BORDER", shift_bp=230, open_ma=3, pnl=80, efficiency=8),
            _point("B1", shift_bp=270, open_ma=4),
            _point("B2", shift_bp=270, open_ma=5),
        ]
    )

    annotated, plateaus = build_plateaus(points, AlgorithmConfig.defaults())

    assert len(plateaus) == 2
    assert sum(len(ids) for ids in plateaus["supported_point_ids"]) == 1
    border = annotated.loc[annotated["point_id"].eq("BORDER")].iloc[0]
    assert border["plateau_role"] == "SUPPORTED"


def test_singleton_without_core_link_is_not_ready_plateau() -> None:
    points = pd.DataFrame([_point("ONLY", shift_bp=230, open_ma=3)])

    annotated, plateaus = build_plateaus(points, AlgorithmConfig.defaults())

    assert plateaus.empty
    assert pd.isna(annotated.iloc[0]["plateau_id"])
    assert not annotated.iloc[0]["depth_eligible"]


def test_plateau_ids_and_members_are_stable_under_input_shuffle() -> None:
    points = pd.DataFrame(
        [
            _point("P1", shift_bp=190, open_ma=2, pnl=100, efficiency=10),
            _point("P2", shift_bp=190, open_ma=3, pnl=95, efficiency=9.5),
            _point("P3", shift_bp=230, open_ma=3, pnl=80, efficiency=8),
        ]
    )

    first = build_plateaus(points, AlgorithmConfig.defaults())[1]
    second = build_plateaus(points.sample(frac=1, random_state=7), AlgorithmConfig.defaults())[1]

    assert first.iloc[0]["plateau_id"] == second.iloc[0]["plateau_id"]
    assert first.iloc[0]["all_point_ids"] == second.iloc[0]["all_point_ids"]


def test_standalone_and_depth_eligibility_are_separate() -> None:
    points = pd.DataFrame(
        [
            _point("P1", shift_bp=230, open_ma=2, sample=True),
            _point("P2", shift_bp=230, open_ma=3, sample=False),
        ]
    )

    annotated, plateaus = build_plateaus(points, AlgorithmConfig.defaults())

    assert len(plateaus) == 1
    p1 = annotated.loc[annotated["point_id"].eq("P1")].iloc[0]
    p2 = annotated.loc[annotated["point_id"].eq("P2")].iloc[0]
    assert p1["standalone_eligible"] and p1["depth_eligible"]
    assert not p2["standalone_eligible"] and p2["depth_eligible"]


def test_isolated_peak_is_audited_but_not_used_as_plateau() -> None:
    points = pd.DataFrame(
        [
            _point("BEST", shift_bp=190, open_ma=2, pnl=100, efficiency=10),
            _point("LOW", shift_bp=270, open_ma=6, pnl=40, efficiency=4),
        ]
    )
    annotated, plateaus = build_plateaus(points, AlgorithmConfig.defaults())

    isolated = find_isolated_peaks(annotated, AlgorithmConfig.defaults())

    assert plateaus.empty
    assert list(isolated["point_id"]) == ["BEST"]
    assert isolated.iloc[0]["status"] == "ISOLATED_PEAK"


def test_isolated_peak_threshold_uses_best_pair_tf_point_including_plateaus() -> None:
    points = pd.DataFrame(
        [
            _point("CORE1", shift_bp=190, open_ma=2, pnl=100, efficiency=10),
            _point("CORE2", shift_bp=190, open_ma=3, pnl=95, efficiency=9.5),
            _point("WEAK_SINGLE", shift_bp=270, open_ma=6, pnl=50, efficiency=5),
        ]
    )
    annotated, _ = build_plateaus(points, AlgorithmConfig.defaults())

    isolated = find_isolated_peaks(annotated, AlgorithmConfig.defaults())

    assert isolated.empty
