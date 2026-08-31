from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from mrs3.performance_v2_selection import (
    PerformanceV2SelectionError,
    SelectionConfig,
    load_selection_config,
    parse_selection_request,
)


def _config(path: Path, **selection: object) -> Path:
    path.write_text(
        json.dumps({
            "unified_performance_v2": {
                "database_root": "data/performance-v2",
                "finalist_selection": selection,
            },
        }),
        encoding="utf-8",
    )
    return path


def test_parse_selection_request_accepts_all_known_stages_in_order() -> None:
    stages = [
        "filter_holding_outlier", "filter_low_trades", "ab_deterioration", "pareto_dd5_balanced",
        "pareto_plateau_points_per_order", "pareto_plateau_points_total", "pareto_efficiency_shift",
        "pareto_dd5_holding", "pareto_dd5_close_ma", "pareto_dd5_first_shift",
        "pareto_conditional_close_ma", "pareto_primary", "pareto_dd5_capital",
    ]

    request = parse_selection_request({
        "symbol": "BTCUSDT",
        "side": "LONG",
        "stages": [
            {"id": stage_id, "enabled": index < 4,
             "scope": "pair_side" if index < 4 else "pair_side_timeframe"}
            for index, stage_id in enumerate(stages)
        ],
    })

    assert request.symbol == "BTCUSDT"
    assert request.side == "LONG"
    assert [stage.id for stage in request.stages] == stages


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"symbol": "BTCUSDT", "side": "LONG", "stages": [{"id": "unknown", "enabled": True, "scope": "pair_side"}]}, "UNKNOWN_STAGE"),
        ({"symbol": "BTCUSDT", "side": "LONG", "stages": [{"id": "ab_deterioration", "enabled": True, "scope": "pair_side"}, {"id": "ab_deterioration", "enabled": False, "scope": "pair_side"}]}, "DUPLICATE_STAGE"),
        ({"symbol": "BTCUSDT", "side": "LONG", "stages": [{"id": "ab_deterioration", "enabled": True, "scope": "global"}]}, "INVALID_SCOPE"),
    ],
)
def test_parse_selection_request_rejects_unknown_duplicate_and_invalid_scope(payload: dict[str, object], code: str) -> None:
    with pytest.raises(PerformanceV2SelectionError, match=code):
        parse_selection_request(payload)


def test_selection_config_reads_agreed_defaults(tmp_path: Path) -> None:
    config = load_selection_config(_config(tmp_path / "config.performance.json"))

    assert config.ab_final_days == 14
    assert config.ab_return_floor_pct == 5
    assert config.ab_return_divisor == 10
    assert config.ab_win_rate_floor_pct == 58
    assert config.ab_trade_rate_divisor == 7
    assert config.plateau_points_pareto_pnl_multiplier == 2


def test_selection_config_reads_explicit_overrides(tmp_path: Path) -> None:
    config = load_selection_config(_config(
        tmp_path / "config.performance.json",
        ab_final_days=21,
        ab_return_floor_pct=4.5,
        ab_return_divisor=8,
        ab_win_rate_floor_pct=60,
        ab_trade_rate_divisor=6,
        plateau_points_pareto_pnl_multiplier=1.5,
    ))

    assert config.ab_final_days == 21
    assert config.ab_return_floor_pct == Decimal("4.5")
    assert config.plateau_points_pareto_pnl_multiplier == Decimal("1.5")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ab_final_days", 0),
        ("ab_final_days", True),
        ("ab_return_floor_pct", 0),
        ("ab_return_divisor", "wrong"),
        ("ab_win_rate_floor_pct", float("nan")),
        ("ab_trade_rate_divisor", float("inf")),
        ("plateau_points_pareto_pnl_multiplier", False),
    ],
)
def test_selection_config_rejects_invalid_values(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(PerformanceV2SelectionError, match=f"INVALID_CONFIG_{field}"):
        load_selection_config(_config(tmp_path / "config.performance.json", **{field: value}))


def test_selection_config_rejects_malformed_v2_namespace(tmp_path: Path) -> None:
    path = tmp_path / "config.performance.json"
    path.write_text(json.dumps({"unified_performance_v2": []}), encoding="utf-8")

    with pytest.raises(PerformanceV2SelectionError, match="INVALID_CONFIG"):
        load_selection_config(path)


def test_real_selection_config_has_agreed_defaults() -> None:
    config = load_selection_config(Path(__file__).resolve().parents[1] / "config.performance.json")

    assert config == SelectionConfig()
