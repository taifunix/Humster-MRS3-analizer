from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pandas as pd

from mrs3.config import AlgorithmConfig
from mrs3.posttest import (
    compare_posttest,
    normalize_dd5_row,
    pareto_front,
    rank_near_ties,
    run_posttest,
    scale_strategy_json,
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


def test_run_posttest_writes_tables_and_scaled_retest_json(tmp_path: Path) -> None:
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

    scaled = json.loads(
        (artifacts.scaled_strategies_dir / "A_DD5.json").read_text(encoding="utf-8")
    )
    assert scaled["mrs3"]["ma_long"][0]["lot_x"] == 0.5
    assert artifacts.workbook == output / "posttest.xlsx"
    assert artifacts.scaled_count == 1
    assert (output / "posttest_csv" / "18_Final_Comparison.csv").exists()
