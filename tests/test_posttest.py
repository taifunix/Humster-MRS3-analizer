from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.posttest import (
    compare_posttest,
    normalize_dd5_row,
    one_order_baselines,
    pareto_front,
    pareto_variants,
    rank_near_ties,
    run_posttest,
    scale_strategy_json,
    selection_finalists,
    selection_summary,
    sequential_selection,
)


def test_dd5_projection_and_capital_proxy() -> None:
    row = normalize_dd5_row(
        {
            "strategy_name": "A",
            "pnl_pct": 20,
            "dd_pct": 10,
            "effective_days": 20,
            "lots": (Decimal("1"),),
        },
        AlgorithmConfig.defaults(),
    )
    assert row["dd5_scale"] == Decimal("0.5")
    assert row["projected_pnl_dd5"] == Decimal("10.0")
    assert row["projected_dd_pct"] == Decimal("5.0")
    assert row["capital_requirement_proxy"] == Decimal("0.55")
    assert row["pnl30_dd5"] == Decimal("15.0")


def test_pareto_dominance_requires_at_least_one_strict_dimension() -> None:
    frame = pd.DataFrame(
        [
            {"strategy_name": "A", "pnl30_dd5": 20.0, "capital_requirement_proxy": 1.0},
            {"strategy_name": "B", "pnl30_dd5": 20.0, "capital_requirement_proxy": 1.2},
            {"strategy_name": "C", "pnl30_dd5": 20.0, "capital_requirement_proxy": 1.0},
        ]
    )
    kept = pareto_front(frame)
    assert list(kept["strategy_name"]) == ["A", "C"]


def test_near_tie_five_percent_prefers_capital_efficiency() -> None:
    frame = pd.DataFrame(
        [
            {
                "strategy_name": "A",
                "pnl30_dd5": 100.0,
                "capital_efficiency_30": 100.0,
                "capital_requirement_proxy": 1.0,
                "trades": 20,
            },
            {
                "strategy_name": "B",
                "pnl30_dd5": 96.0,
                "capital_efficiency_30": 120.0,
                "capital_requirement_proxy": 0.8,
                "trades": 20,
            },
        ]
    )
    ordered = rank_near_ties(frame, AlgorithmConfig.defaults())
    assert ordered.iloc[0]["strategy_name"] == "B"


def test_compare_posttest_parses_audit_lots_and_marks_pareto() -> None:
    raw = pd.DataFrame(
        [
            {
                "strategy_name": "A",
                "total_pnl_percent": 20.0,
                "max_drawdown_percent": 10.0,
                "win_rate": 80.0,
                "profit_factor": 2.0,
                "total_trades": 20,
                "effective_days": 20,
            },
            {
                "strategy_name": "B",
                "total_pnl_percent": 18.0,
                "max_drawdown_percent": 10.0,
                "win_rate": 80.0,
                "profit_factor": 2.0,
                "total_trades": 20,
                "effective_days": 20,
            },
        ]
    )
    variants = pd.DataFrame(
        [
            {"strategy_name": "A", "lots": '["1.000000000000"]'},
            {"strategy_name": "B", "lots": '["1.000000000000"]'},
        ]
    )
    tables = compare_posttest(raw, variants, AlgorithmConfig.defaults())
    assert len(tables.normalized) == 2
    assert tables.comparison.loc[tables.comparison["strategy_name"].eq("A"), "pareto"].item()
    assert not tables.comparison.loc[tables.comparison["strategy_name"].eq("B"), "pareto"].item()


def test_scaled_strategy_json_keeps_lot_above_one() -> None:
    strategy = {"basic": {"use_long": True}, "mrs3": {"ma_long": [{"lot_x": 1.0}]}}
    scaled = scale_strategy_json(strategy, Decimal("2"), AlgorithmConfig.defaults())
    assert scaled["mrs3"]["ma_long"][0]["lot_x"] == 2.5
    assert scaled["name"].endswith("_DD5")


def test_compare_posttest_accepts_tester_runner_column_names() -> None:
    raw = pd.DataFrame(
        [
            {
                "strategy_name": "A",
                "total_pnl_pct": 20.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 80.0,
                "profit_factor": 2.0,
                "total_trades": 20,
                "days_in_test": 20,
            }
        ]
    )
    variants = pd.DataFrame(
        [{"strategy_name": "A", "lots": '["1.000000000000"]'}]
    )

    tables = compare_posttest(raw, variants, AlgorithmConfig.defaults())

    assert tables.normalized.iloc[0]["pnl30_dd5"] == Decimal("15.00")


def test_compare_posttest_measures_holding_until_full_close_only() -> None:
    raw = pd.DataFrame(
        [
            {
                "strategy_name": "A",
                "total_pnl_pct": 20.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 80.0,
                "profit_factor": 2.0,
                "total_trades": 20,
                "days_in_test": 1,
                "trades_json": json.dumps(
                    [
                        {"Timestamp": "2026-08-01 00:00:00", "Symbol": "ONUSDT", "Action": "opened", "Post Side": "long", "Post Size": "1", "Side": "buy"},
                        {"Timestamp": "2026-08-01 00:10:00", "Symbol": "ONUSDT", "Action": "increased", "Post Side": "long", "Post Size": "2", "Side": "buy"},
                        {"Timestamp": "2026-08-01 00:20:00", "Symbol": "ONUSDT", "Action": "decreased", "Post Side": "long", "Post Size": "1", "Side": "sell"},
                        {"Timestamp": "2026-08-01 01:00:00", "Symbol": "ONUSDT", "Action": "closed", "Post Side": "", "Post Size": "0", "Side": "sell"},
                    ]
                ),
            }
        ]
    )
    variants = pd.DataFrame([{"strategy_name": "A", "lots": '["1"]'}])

    tables = compare_posttest(raw, variants, AlgorithmConfig.defaults())

    assert tables.holding_cycles.to_dict(orient="records") == [
        {
            "strategy_name": "A",
            "symbol": "ONUSDT",
            "position_side": "long",
            "opened_at": pd.Timestamp("2026-08-01T00:00:00Z"),
            "closed_at": pd.Timestamp("2026-08-01T01:00:00Z"),
            "holding_minutes": 60.0,
        }
    ]
    comparison = tables.comparison.iloc[0]
    assert comparison["full_position_cycle_count"] == 1
    assert comparison["holding_median_minutes"] == 60.0
    assert comparison["time_in_market_pct"] == pytest.approx(4.166666666666667)
    assert tables.holding_exclusions.empty
    assert tables.comparison.columns[:8].tolist() == [
        "strategy_name",
        "projected_pnl_dd5",
        "pnl30_dd5",
        "projected_dd_pct",
        "dd5_scale",
        "lots",
        "scaled_lots",
        "capital_requirement_proxy",
    ]


def test_compare_posttest_rejects_partial_action_that_claims_zero_post_size() -> None:
    raw = pd.DataFrame(
        [
            {
                "strategy_name": "A",
                "total_pnl_pct": 20.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 80.0,
                "profit_factor": 2.0,
                "total_trades": 20,
                "days_in_test": 1,
                "trades_json": json.dumps(
                    [
                        {"Timestamp": "2026-08-01 00:00:00", "Symbol": "ONUSDT", "Action": "opened", "Post Side": "long", "Post Size": "1", "Side": "buy"},
                        {"Timestamp": "2026-08-01 00:20:00", "Symbol": "ONUSDT", "Action": "decreased", "Post Side": "long", "Post Size": "0", "Side": "sell"},
                    ]
                ),
            }
        ]
    )
    variants = pd.DataFrame([{"strategy_name": "A", "lots": '["1"]'}])

    tables = compare_posttest(raw, variants, AlgorithmConfig.defaults())

    assert tables.holding_cycles.empty
    assert tables.holding_exclusions["reason"].tolist() == [
        "NON_CLOSE_ZERO_POST_SIZE",
        "NO_FULL_CLOSE:long",
    ]


def test_run_posttest_writes_calculation_only_dd5_outputs(tmp_path: Path) -> None:
    results = tmp_path / "results.csv"
    pd.DataFrame(
        [
            {
                "strategy_name": "A",
                "total_pnl_pct": 20.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 80.0,
                "profit_factor": 2.0,
                "total_trades": 20,
                "days_in_test": 20,
            }
        ]
    ).to_csv(results, index=False)
    audit = tmp_path / "audit.xlsx"
    with pd.ExcelWriter(audit, engine="openpyxl") as writer:
        pd.DataFrame(
            [
                {
                    "strategy_name": "A",
                    "lots": '["1.000000000000"]',
                    "json_filename": "A.json",
                }
            ]
        ).to_excel(writer, sheet_name="11_Lot_Variants", index=False)
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "A.json").write_text(
        json.dumps(
            {
                "name": "A",
                "basic": {"use_long": True},
                "mrs3": {"ma_long": [{"lot_x": 1.0}]},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "posttest"

    artifacts = run_posttest(
        results,
        audit,
        strategies,
        output,
        AlgorithmConfig.defaults(),
    )

    assert artifacts.workbook == output / "posttest.xlsx"
    assert (output / "posttest_csv" / "18_Final_Comparison.csv").exists()
    manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))
    assert manifest["dd5_mode"] == "CALCULATION_ONLY"
    assert manifest["ranking_basis"] == ["projected_pnl_dd5", "projected_dd_pct"]
    assert not (output / "scaled_strategies").exists()
    assert (output / "posttest_csv" / "19_Position_Holding_Cycles.csv").exists()
    assert (output / "posttest_csv" / "20_Position_Holding_Exclusions.csv").exists()
    assert pd.ExcelFile(artifacts.workbook).sheet_names[:2] == [
        "00_Selection_Summary",
        "01_Finalists",
    ]
    assert pd.ExcelFile(artifacts.workbook).sheet_names == [
        "00_Selection_Summary",
        "01_Finalists",
        "16_Raw_MRS3_Results",
        "17_DD5_Normalized",
        "18_Final_Comparison",
        "19_Position_Holding_Cycles",
        "20_Position_Holding_Exclusions",
    ]
    assert not (output / "posttest_csv" / "00_Selection_Summary.csv").exists()
    assert not (output / "posttest_csv" / "01_Finalists.csv").exists()


def test_run_posttest_derives_lots_from_v07_strategy_json_when_audit_is_missing(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.csv"
    pd.DataFrame([{"strategy_name": "A", "total_pnl_pct": 20.0, "max_drawdown_pct": 10.0, "win_rate_pct": 80.0, "profit_factor": 2.0, "total_trades": 20, "days_in_test": 20}]).to_csv(results, index=False)
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "A.json").write_text(json.dumps({"name": "A", "basic": {"use_long": True}, "mrs3": {"ma_long": [{"lot_x": 1.0}]}}), encoding="utf-8")

    artifacts = run_posttest(results, tmp_path / "missing.xlsx", strategies, tmp_path / "posttest", AlgorithmConfig.defaults())

    manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))
    assert manifest["audit_xlsx"] == "derived_from_strategy_json"
    assert manifest["dd5_mode"] == "CALCULATION_ONLY"


def test_run_posttest_ignores_strategy_manifest_when_deriving_lots(tmp_path: Path) -> None:
    results = tmp_path / "results.csv"
    pd.DataFrame([{"strategy_name": "A", "total_pnl_pct": 20.0, "max_drawdown_pct": 10.0, "win_rate_pct": 80.0, "profit_factor": 2.0, "total_trades": 20, "days_in_test": 20}]).to_csv(results, index=False)
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "A.json").write_text(json.dumps({"name": "A", "basic": {"use_long": True}, "mrs3": {"ma_long": [{"lot_x": 1.0}]}}), encoding="utf-8")
    (strategies / "strategy_manifest.json").write_text("{}", encoding="utf-8")

    artifacts = run_posttest(results, tmp_path / "missing.xlsx", strategies, tmp_path / "posttest", AlgorithmConfig.defaults())

    assert artifacts.workbook.exists()


def test_run_posttest_derives_lots_from_tester_strategy_settings(tmp_path: Path) -> None:
    strategy = {"name": "A", "basic": {"use_long": True}, "mrs3": {"ma_long": [{"lot_x": 1.0}, {"lot_x": 2.0}]}}
    results = tmp_path / "results.csv"
    pd.DataFrame([{"strategy_name": "A", "total_pnl_pct": 20.0, "max_drawdown_pct": 10.0, "win_rate_pct": 80.0, "profit_factor": 2.0, "total_trades": 20, "days_in_test": 20, "strategy_settings_json": json.dumps(strategy)}]).to_csv(results, index=False)

    artifacts = run_posttest(results, tmp_path / "missing.xlsx", tmp_path / "missing-strategies", tmp_path / "posttest", AlgorithmConfig.defaults())

    comparison = pd.read_excel(artifacts.workbook, sheet_name="18_Final_Comparison")
    manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))
    assert comparison.loc[0, "lots"] == '["1.00", "2.00"]'
    assert manifest["audit_xlsx"] == "derived_from_tester_strategy_settings"


def test_run_posttest_falls_back_to_strategy_json_when_name_only_rows_have_no_settings(
    tmp_path: Path,
) -> None:
    results = tmp_path / "results.csv"
    pd.DataFrame(
        [
            {
                "strategy_name": "A",
                "total_pnl_pct": 20.0,
                "max_drawdown_pct": 10.0,
                "win_rate_pct": 80.0,
                "total_trades": 20,
                "period": "2026-07-01 .. 2026-07-21",
                "verification_mode": "strategy_name_only",
                "strategy_settings_json": "",
            }
        ]
    ).to_csv(results, index=False)
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "A.json").write_text(
        json.dumps({"name": "A", "basic": {"use_long": True}, "mrs3": {"ma_long": [{"lot_x": 1.0}]}}),
        encoding="utf-8",
    )

    artifacts = run_posttest(
        results,
        tmp_path / "missing.xlsx",
        strategies,
        tmp_path / "posttest",
        AlgorithmConfig.defaults(),
    )

    manifest = json.loads(artifacts.manifest.read_text(encoding="utf-8"))
    assert manifest["audit_xlsx"] == "derived_from_strategy_json"


def test_final_comparison_keeps_typed_full_precision_values() -> None:
    strategy = {
        "name": "A",
        "basic": {"use_long": True},
        "mrs3": {
            "ma_long": [
                {"lot_x": 0.333333, "multiplier": 0.997},
                {"lot_x": 0.666667, "multiplier": 0.989},
            ]
        },
    }
    raw = pd.DataFrame(
        [
            {
                "test_run_id": "run-precision",
                "strategy_name": "A",
                "total_pnl_pct": Decimal("20.12345"),
                "max_drawdown_pct": Decimal("10.12345"),
                "win_rate_pct": Decimal("80.12345"),
                "profit_factor": Decimal("2.12345"),
                "total_trades": 20,
                "days_in_test": Decimal("20.12345"),
                "strategy_settings_json": json.dumps(strategy),
            }
        ]
    )

    tables = compare_posttest(
        raw,
        pd.DataFrame([{"strategy_name": "A", "lots": '["0.333333", "0.666667"]'}]),
        AlgorithmConfig.defaults(),
    )

    comparison = tables.comparison.iloc[0]
    assert comparison["shift_bp_vector"] == "30 / 110"
    assert comparison["test_run_id"] == "run-precision"
    assert isinstance(comparison["projected_pnl_dd5"], Decimal)
    assert comparison["projected_pnl_dd5"] == tables.normalized.iloc[0]["projected_pnl_dd5"]
    assert comparison["dd5_scale"] == tables.normalized.iloc[0]["dd5_scale"]
    assert comparison["win_rate_pct"] == Decimal("80.12345")
    assert comparison["lots"] == (Decimal("0.333333"), Decimal("0.666667"))
    assert tables.normalized.iloc[0]["win_rate_pct"] == Decimal("80.12345")


def test_final_comparison_exposes_scoped_pareto_variants() -> None:
    frame = pd.DataFrame(
        [
            {"strategy_name": "A", "symbol": "ONUSDT", "side": "LONG", "timeframe": "1h", "order_count": 2, "pnl30_dd5": 20, "capital_requirement_proxy": 1, "holding_p95_minutes": 100, "common_close_ma": 5, "first_shift_bp": 30},
            {"strategy_name": "B", "symbol": "ONUSDT", "side": "LONG", "timeframe": "1h", "order_count": 2, "pnl30_dd5": 20, "capital_requirement_proxy": 1, "holding_p95_minutes": 50, "common_close_ma": 3, "first_shift_bp": 50},
            {"strategy_name": "C", "symbol": "ONUSDT", "side": "LONG", "timeframe": "1h", "order_count": 2, "pnl30_dd5": 15, "capital_requirement_proxy": 1, "holding_p95_minutes": 25, "common_close_ma": 2, "first_shift_bp": 70},
        ]
    )

    comparison = pareto_variants(frame).set_index("strategy_name")

    assert comparison.loc["A", "pareto_dd5_capital"]
    assert comparison.loc["B", "pareto_dd5_holding"]
    assert not comparison.loc["A", "pareto_dd5_holding"]
    assert comparison.loc["B", "pareto_dd5_close_ma"]
    assert comparison.loc["B", "pareto_dd5_first_shift"]
    assert comparison.loc["B", "pareto_dd5_balanced"]


def test_pareto_variants_compete_across_order_counts() -> None:
    frame = pd.DataFrame(
        [
            {"strategy_name": "ONE", "symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "order_count": 1, "pnl30_dd5": 30, "capital_requirement_proxy": 1, "holding_p95_minutes": 100, "common_close_ma": 2, "first_shift_bp": 90},
            {"strategy_name": "TWO", "symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "order_count": 2, "pnl30_dd5": 25, "capital_requirement_proxy": 1.2, "holding_p95_minutes": 120, "common_close_ma": 3, "first_shift_bp": 80},
        ]
    )

    result = pareto_variants(frame).set_index("strategy_name")

    assert result.loc["ONE", "pareto_dd5_capital"]
    assert not result.loc["TWO", "pareto_dd5_capital"]


def test_sequential_selection_filters_only_adverse_iqr_outliers() -> None:
    frame = pd.DataFrame(
        [
            {"strategy_name": "A", "holding_p95_minutes": 10, "trades": 100},
            {"strategy_name": "B", "holding_p95_minutes": 11, "trades": 100},
            {"strategy_name": "C", "holding_p95_minutes": 12, "trades": 100},
            {"strategy_name": "GOOD_EXTREME", "holding_p95_minutes": 1, "trades": 500},
            {"strategy_name": "BAD_EXTREME", "holding_p95_minutes": 100, "trades": 1},
        ]
    )
    for column, value in {
        "symbol": "ONUSDT",
        "side": "LONG",
        "timeframe": "2h",
        "pnl30_dd5": 10,
        "capital_requirement_proxy": 1,
        "capital_efficiency_30": 10,
        "first_shift_bp": 30,
        "common_close_ma": 2,
    }.items():
        frame[column] = value

    result = sequential_selection(frame).set_index("strategy_name")

    assert result.loc["GOOD_EXTREME", "selection_filter_pass"]
    assert not result.loc["BAD_EXTREME", "selection_filter_pass"]
    assert result.loc["BAD_EXTREME", "selection_reason"] == "FILTER_HOLDING_OUTLIER"


def test_sequential_selection_applies_conditional_third_pareto_per_scope() -> None:
    rows = []
    for index in range(4):
        rows.append(
            {
                "strategy_name": f"FOUR_{index}",
                "symbol": "ONUSDT",
                "side": "LONG",
                "timeframe": "2h",
                "holding_p95_minutes": 100,
                "trades": 100,
                "pnl30_dd5": 10 + index,
                "capital_requirement_proxy": 1 + index,
                "capital_efficiency_30": 40 - index,
                "first_shift_bp": 30 + index,
                "common_close_ma": 2 + index,
            }
        )
    for index in range(3):
        rows.append(
            {
                "strategy_name": f"THREE_{index}",
                "symbol": "ONUSDT",
                "side": "LONG",
                "timeframe": "4h",
                "holding_p95_minutes": 200,
                "trades": 50,
                "pnl30_dd5": 10 + index,
                "capital_requirement_proxy": 1 + index,
                "capital_efficiency_30": 30 - index,
                "first_shift_bp": 50 + index,
                "common_close_ma": 2 + index,
            }
        )
    rows.append(
        {
            "strategy_name": "STAGE1_REJECT_WITH_STRONG_STAGE2",
            "symbol": "ONUSDT",
            "side": "LONG",
            "timeframe": "2h",
            "holding_p95_minutes": 100,
            "trades": 100,
            "pnl30_dd5": 9,
            "capital_requirement_proxy": 2,
            "capital_efficiency_30": 100,
            "first_shift_bp": 100,
            "common_close_ma": 1,
        }
    )
    for symbol, side in (("BTCUSDT", "LONG"), ("ONUSDT", "SHORT")):
        for index in range(3):
            rows.append(
                {
                    "strategy_name": f"{symbol}_{side}_{index}",
                    "symbol": symbol,
                    "side": side,
                    "timeframe": "2h",
                    "holding_p95_minutes": 100,
                    "trades": 100,
                    "pnl30_dd5": 10 + index,
                    "capital_requirement_proxy": 1 + index,
                    "capital_efficiency_30": 30 - index,
                    "first_shift_bp": 50 + index,
                    "common_close_ma": 2 + index,
                }
            )

    result = sequential_selection(pd.DataFrame(rows)).set_index("strategy_name")

    assert result.loc[[f"FOUR_{index}" for index in range(4)], "selection_stage3_applied"].all()
    assert result.loc["FOUR_0", "selection_final"]
    assert result.loc[[f"FOUR_{index}" for index in range(1, 4)], "selection_final"].sum() == 0
    assert not result.loc[[f"THREE_{index}" for index in range(3)], "selection_stage3_applied"].any()
    assert result.loc[[f"THREE_{index}" for index in range(3)], "selection_final"].all()
    assert not result.loc["STAGE1_REJECT_WITH_STRONG_STAGE2", "selection_stage1"]
    assert not result.loc["STAGE1_REJECT_WITH_STRONG_STAGE2", "selection_stage2"]
    for symbol, side in (("BTCUSDT", "LONG"), ("ONUSDT", "SHORT")):
        names = [f"{symbol}_{side}_{index}" for index in range(3)]
        assert not result.loc[names, "selection_stage3_applied"].any()
        assert result.loc[names, "selection_final"].all()


def test_selection_summary_reports_each_scope_pipeline_counts() -> None:
    frame = pd.DataFrame(
        [
            {"strategy_name": "A", "symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "selection_holding_limit": 438.4, "selection_trades_floor": 59, "selection_filter_pass": True, "selection_stage1": True, "selection_stage2": True, "selection_stage3_applied": False, "selection_final": True},
            {"strategy_name": "B", "symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "selection_holding_limit": 438.4, "selection_trades_floor": 59, "selection_filter_pass": False, "selection_stage1": False, "selection_stage2": False, "selection_stage3_applied": False, "selection_final": False},
            {"strategy_name": "C", "symbol": "ONUSDT", "side": "LONG", "timeframe": "4h", "selection_holding_limit": 900, "selection_trades_floor": 20, "selection_filter_pass": True, "selection_stage1": True, "selection_stage2": True, "selection_stage3_applied": True, "selection_final": True},
        ]
    )

    summary = selection_summary(frame).to_dict(orient="records")

    assert summary == [
        {"symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "all_candidates": 2, "filter_pass": 1, "filter_rejected": 1, "stage1": 1, "stage2": 1, "stage3_applied": False, "final": 1, "holding_p95_limit": 438.4, "trades_floor": 59},
        {"symbol": "ONUSDT", "side": "LONG", "timeframe": "4h", "all_candidates": 1, "filter_pass": 1, "filter_rejected": 0, "stage1": 1, "stage2": 1, "stage3_applied": True, "final": 1, "holding_p95_limit": 900.0, "trades_floor": 20},
    ]


def test_selection_finalists_keeps_final_rows_in_deterministic_order() -> None:
    frame = pd.DataFrame(
        [
            {"strategy_name": "LOW", "symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "pnl30_dd5": 10, "selection_final": True},
            {"strategy_name": "OUT", "symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "pnl30_dd5": 99, "selection_final": False},
            {"strategy_name": "HIGH", "symbol": "ONUSDT", "side": "LONG", "timeframe": "2h", "pnl30_dd5": 20, "selection_final": True},
        ]
    )

    assert selection_finalists(frame)["strategy_name"].tolist() == ["HIGH", "LOW"]


def test_selection_layout_is_empty_when_scope_metadata_is_unavailable() -> None:
    frame = pd.DataFrame(
        [{"strategy_name": "A", "selection_final": False, "pnl30_dd5": 10}]
    )

    assert selection_summary(frame).empty
    assert selection_finalists(frame).empty
    assert selection_finalists(pd.DataFrame([{"strategy_name": "LEGACY"}])).empty
    assert selection_summary(pd.DataFrame()).empty
    assert selection_finalists(pd.DataFrame()).empty


def test_one_order_baselines_selects_top_three_from_matching_analysis_run(tmp_path: Path) -> None:
    database = tmp_path / "analysis.duckdb"
    connection = duckdb.connect(str(database))
    connection.execute("create table analysis_runs(run_id varchar, surface_id varchar, created_at_utc timestamp)")
    connection.execute("create table candidates(run_id varchar, candidate_json varchar)")
    connection.execute("create table plateaus(run_id varchar, metrics_json varchar)")
    connection.execute("create table surface_points(surface_id varchar, canonical_point_key varchar, point_event_count integer, metrics_json varchar)")
    connection.execute("create table surfaces(surface_id varchar, period_start_utc timestamp, period_end_utc timestamp)")
    connection.execute("insert into analysis_runs values ('RUN', 'SURFACE', '2026-01-01')")
    connection.execute("insert into candidates values ('RUN', ?)", [json.dumps({"structure_id": "STR_A"})])
    point_ids = [f"ONUSDT|LONG|1h|{shift}|3|5" for shift in (30, 50, 70, 90)]
    connection.execute("insert into plateaus values ('RUN', ?)", [json.dumps({"ready": True, "standalone_eligible_point_ids": point_ids})])
    metrics = [
        {"TotalPnLPercent": 20, "MaxDrawdownPercent": 10, "TotalTrades": 20},
        {"TotalPnLPercent": 18, "MaxDrawdownPercent": 6, "TotalTrades": 30},
        {"TotalPnLPercent": 15, "MaxDrawdownPercent": 5, "TotalTrades": 40},
        {"TotalPnLPercent": 8, "MaxDrawdownPercent": 5, "TotalTrades": 50},
    ]
    connection.executemany("insert into surface_points values ('SURFACE', ?, ?, ?)", [(point_id, index + 3, json.dumps(metric)) for index, (point_id, metric) in enumerate(zip(point_ids, metrics, strict=True))])
    connection.execute("insert into surfaces values ('SURFACE', '2026-01-01', '2026-01-31')")
    connection.close()

    baselines = one_order_baselines(pd.DataFrame([{"strategy_name": "ONUSDT_1h_LONG_2ORD_CMA5_STR_A_EQUAL"}]), database, AlgorithmConfig.defaults())

    assert baselines["metric_source"].tolist() == ["MRS2_1ORD_BASELINE"] * 3
    assert baselines["source_point_id"].tolist() == [point_ids[2], point_ids[1], point_ids[0]]
    assert baselines["common_close_ma"].tolist() == [5, 5, 5]
