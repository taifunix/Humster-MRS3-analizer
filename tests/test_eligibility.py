from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.eligibility import (
    absolute_trade_floor,
    annotate_eligibility,
    base_rate,
    shift_factor,
)


def _point(**overrides: object) -> dict[str, object]:
    point: dict[str, object] = {
        "point_id": "AAAUSDT|LONG|2h|190|3|4",
        "symbol": "AAAUSDT",
        "side": "LONG",
        "timeframe": "2h",
        "shift_bp": 190,
        "shift_pct": 1.9,
        "open_ma": 3,
        "close_ma": 4,
        "pnl_pct": 30.0,
        "dd_pct": 10.0,
        "win_rate_pct": 70.0,
        "profit_factor": 3.0,
        "trades": 20,
        "report_start": pd.Timestamp("2026-07-15"),
        "report_end": pd.Timestamp("2026-08-06"),
        "listing_date": pd.Timestamp("2026-07-01"),
    }
    point.update(overrides)
    return point


@pytest.mark.parametrize(("shift_bp", "want"), [(200, 10), (201, 5)])
def test_absolute_trade_floor_boundary(shift_bp: int, want: int) -> None:
    assert absolute_trade_floor(shift_bp, AlgorithmConfig.defaults()) == want


def test_effective_history_starts_at_listing_date() -> None:
    point = _point(listing_date=pd.Timestamp("2026-07-28"))

    out = annotate_eligibility(pd.DataFrame([point]), AlgorithmConfig.defaults()).iloc[0]

    assert out["effective_start"] == pd.Timestamp("2026-07-28")
    assert out["effective_days"] == 9.0
    assert out["history_pass"]


def test_history_below_seven_days_is_audited_but_not_passed() -> None:
    point = _point(listing_date=pd.Timestamp("2026-08-01"))

    out = annotate_eligibility(pd.DataFrame([point]), AlgorithmConfig.defaults()).iloc[0]

    assert out["effective_days"] == 5.0
    assert not out["history_pass"]
    assert "OBSERVE_ONLY_HISTORY" in out["reject_reasons"]


@pytest.mark.parametrize(
    ("shift_bp", "want"),
    [
        (150, Decimal("1.00")),
        (151, Decimal("0.90")),
        (200, Decimal("0.90")),
        (201, Decimal("0.30")),
        (310, Decimal("0.30")),
        (311, Decimal("0.20")),
        (470, Decimal("0.20")),
        (471, None),
    ],
)
def test_shift_factor_boundaries(shift_bp: int, want: Decimal | None) -> None:
    assert shift_factor(shift_bp, AlgorithmConfig.defaults()) == want


def test_30m_base_rate_is_provisional_1_59() -> None:
    assert base_rate("30m", AlgorithmConfig.defaults()) == Decimal("1.59")


def test_relative_sample_floor_uses_ceiling_and_maximum() -> None:
    point = _point(
        report_end=pd.Timestamp("2026-07-25"),
        report_start=pd.Timestamp("2026-07-15"),
        listing_date=pd.Timestamp("2026-07-01"),
        trades=10,
    )

    out = annotate_eligibility(pd.DataFrame([point]), AlgorithmConfig.defaults()).iloc[0]

    assert out["relative_min_trades"] == 11
    assert out["absolute_min_trades"] == 10
    assert out["required_min_trades"] == 11
    assert not out["standalone_sample_pass"]


def test_economic_gate_accepts_exact_boundaries() -> None:
    out = annotate_eligibility(pd.DataFrame([_point()]), AlgorithmConfig.defaults()).iloc[0]

    assert out["efficiency"] == pytest.approx(3.0)
    assert out["pnl_dd5_theoretical"] == pytest.approx(15.0)
    assert out["economic_pass"]


def test_event_gate_marks_points_below_configured_minimum_ineligible() -> None:
    rows = pd.DataFrame([_point(point_event_count=2), _point(point_id="B", point_event_count=3)])

    out = annotate_eligibility(rows, AlgorithmConfig.defaults())

    assert out["event_eligible"].tolist() == [False, True]
    assert "INSUFFICIENT_POINT_EVENTS" in out.iloc[0]["reject_reasons"]


@pytest.mark.parametrize(
    "point_event_count",
    [1.5, "1.5", -1, "-1", float("inf"), "inf", float("nan"), "nan"],
)
def test_event_gate_rejects_lossy_point_event_counts(point_event_count: object) -> None:
    """A lossy cast must not turn invalid event counts into valid counts."""
    with pytest.raises(ValueError):
        annotate_eligibility(
            pd.DataFrame([_point(point_event_count=point_event_count)]),
            AlgorithmConfig.defaults(),
        )


def test_real_event_mode_requires_point_event_count() -> None:
    with pytest.raises(ValueError, match="point_event_count"):
        annotate_eligibility(
            pd.DataFrame([_point(event_mode="real_independent_events")]),
            AlgorithmConfig.defaults(),
        )


def test_economic_minimum_pnl_is_configurable_and_strict() -> None:
    config = replace(
        AlgorithmConfig.defaults(), economic_min_pnl_pct=Decimal("30")
    )
    boundary = annotate_eligibility(pd.DataFrame([_point(pnl_pct=30)]), config).iloc[0]
    above = annotate_eligibility(pd.DataFrame([_point(pnl_pct=30.01)]), config).iloc[0]

    assert not boundary["economic_pass"]
    assert "REJECT_PNL_MINIMUM" in boundary["reject_reasons"]
    assert above["economic_pass"]


def test_theoretical_projection_uses_configured_target_drawdown() -> None:
    config = replace(AlgorithmConfig.defaults(), target_dd_pct=Decimal("7"))

    out = annotate_eligibility(pd.DataFrame([_point()]), config).iloc[0]

    assert out["pnl_dd5_theoretical"] == pytest.approx(21.0)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"pnl_pct": 0.0}, "REJECT_PNL_NONPOSITIVE"),
        ({"dd_pct": 0.0}, "REJECT_DD_NONPOSITIVE"),
        ({"win_rate_pct": 69.99}, "REJECT_WIN_RATE"),
        ({"dd_pct": 11.01}, "REJECT_DRAWDOWN"),
        ({"pnl_pct": 29.99}, "REJECT_EFFICIENCY"),
    ],
)
def test_economic_rejections_are_machine_readable(
    overrides: dict[str, float], reason: str
) -> None:
    out = annotate_eligibility(
        pd.DataFrame([_point(**overrides)]), AlgorithmConfig.defaults()
    ).iloc[0]

    assert not out["economic_pass"]
    assert reason in out["reject_reasons"]


def test_shift_above_calibrated_domain_has_no_fabricated_relative_floor() -> None:
    out = annotate_eligibility(
        pd.DataFrame([_point(shift_bp=480, shift_pct=4.8, trades=999)]),
        AlgorithmConfig.defaults(),
    ).iloc[0]

    assert pd.isna(out["relative_min_trades"])
    assert pd.isna(out["required_min_trades"])
    assert not out["standalone_sample_pass"]
    assert "REJECT_SAMPLE_UNCALIBRATED" in out["reject_reasons"]
