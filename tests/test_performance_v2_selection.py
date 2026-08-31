from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from mrs3.performance_v2_selection import (
    PerformanceV2SelectionError,
    SelectionConfig,
    load_selection_candidates,
    load_selection_config,
    parse_selection_request,
)
from mrs3.performance_v2_store import initialize_performance_v2


UTC = timezone.utc


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


def _candidate_db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(str(tmp_path / "strategy_performance.duckdb"))
    initialize_performance_v2(connection)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
           order_count, analysis_run_id, candidate_identity, lifecycle_status,
           created_at_utc, updated_at_utc) values ('alpha', 'BTCUSDT', 'LONG', '1h',
           3, 1, 'run', 'candidate', 'ACTIVE', ?, ?) returning strategy_id""",
        [start, start],
    ).fetchone()[0]
    result_id = connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
           commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
           max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
           values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 5, 5, 2, 2, ?) returning result_id""",
        [strategy_id, start, datetime(2026, 1, 31, tzinfo=UTC), start],
    ).fetchone()[0]
    connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
    connection.execute("insert into analysis_plateaus values ('run', 'P1', 12, 34)")
    connection.execute(
        """insert into strategy_orders values (?, 1, 7, .995, 125, 1, 'run', 'P1', 8)""",
        [strategy_id],
    )
    connection.executemany(
        "insert into strategy_actions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (result_id, 0, start, "BTCUSDT", 1, "closed", 1, 0, "", 0, 0, 100, None),
            (result_id, 1, datetime(2026, 1, 2, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 100, None),
            (result_id, 2, datetime(2026, 1, 3, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 10, 2, 110, None),
        ],
    )
    connection.executemany(
        "insert into strategy_equity values (?, ?, ?, ?, ?)",
        [
            (result_id, 0, start, 100, 100),
            (result_id, 1, datetime(2026, 1, 2, tzinfo=UTC), 100, 100),
            (result_id, 2, datetime(2026, 1, 3, tzinfo=UTC), 110, 110),
            (result_id, 3, datetime(2026, 1, 31, tzinfo=UTC), 110, 110),
        ],
    )
    return connection


def test_loader_derives_proxy_holding_and_order_plateau_counts(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    try:
        row = load_selection_candidates(connection, request).iloc[0]
    finally:
        connection.close()

    assert row["order_1_plateau_point_count"] == 12
    assert row["total_plateau_point_count"] == 12
    assert row["dd5_proxy"] is not None
    assert row["dd5_proxy"] > 0
    assert row["holding_p95_minutes"] == 1440
    assert row["ab_pnl_change_30d_pct"] is None


def test_loader_leaves_incomplete_order_and_empty_candidate_facts_blank(tmp_path: Path) -> None:
    connection = _candidate_db(tmp_path)
    request = parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []})
    try:
        connection.execute("update strategies set order_count = 2")
        connection.execute("update strategy_results set max_drawdown_pct = 10")
        row = load_selection_candidates(connection, request).iloc[0]
        now = datetime(2026, 2, 1, tzinfo=UTC)
        no_orders_id = connection.execute(
            """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc) values ('without-orders', 'BTCUSDT', 'LONG', '1h',
               3, 1, 'run', 'candidate-2', 'ACTIVE', ?, ?) returning strategy_id""",
            [now, now],
        ).fetchone()[0]
        no_orders_result = connection.execute(
            """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
               commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
               max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
               values (?, ?, ?, 'Bybit', .0004, 100, 100, 0, 0, 0, 0, 0, 0, ?) returning result_id""",
            [no_orders_id, now, now, now],
        ).fetchone()[0]
        connection.execute(
            "update strategies set current_result_id = ? where strategy_id = ?", [no_orders_result, no_orders_id]
        )
        all_rows = load_selection_candidates(connection, request).set_index("strategy_name")
        connection.execute("update strategy_results set max_drawdown_pct = 0 where strategy_id = 1")
        zero_dd = load_selection_candidates(connection, request).set_index("strategy_name").loc["alpha"]
        empty = load_selection_candidates(
            connection, parse_selection_request({"symbol": "ETHUSDT", "side": "LONG", "stages": []})
        )
    finally:
        connection.close()

    assert row["risk_scale"] == Decimal("0.5")
    assert row["dd5_proxy"] is not None
    assert row["scaled_lot_sum"] is None
    assert row["capital_proxy"] is None
    assert row["capital_efficiency"] is None
    assert set(all_rows.index) == {"alpha", "without-orders"}
    assert pd.isna(all_rows.loc["without-orders", "order_1_plateau_point_count"])
    assert pd.isna(all_rows.loc["without-orders", "capital_proxy"])
    assert zero_dd["dd5_proxy"] is None
    assert zero_dd["scaled_lot_sum"] is None
    assert "strategy_id" in empty.columns
    assert empty.empty
