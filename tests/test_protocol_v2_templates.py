"""The repository templates meet protocol v2 with one base (protocol v2 §4.2, §4.3)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from mrs3.evaluation_protocol import EvaluationTarget, require_protocol_v2_tester_config


def test_repository_templates_meet_protocol_v2_with_one_base() -> None:
    from mrs3.evaluation_protocol import protocol_v2_unmet, tester_base
    from mrs3.panel_testing import mrs3_tester_config_template

    root = Path(__file__).resolve().parents[1]
    base = tester_base(mrs3_tester_config_template(root))
    tester = json.loads(mrs3_tester_config_template(root).read_text(encoding="utf-8"))
    strategy = json.loads((root / "templates/exchanges/Bybit/mrs3-strategy.json").read_text(encoding="utf-8"))

    # Protocol v2 §4.3: one base for the strategy and the tester.
    assert Decimal(str(strategy["basic"]["my_fix_balance"])) == base
    require_protocol_v2_tester_config(tester, EvaluationTarget(base, strategy["exchange"]["name"]))
    assert protocol_v2_unmet(strategy, EvaluationTarget(base, strategy["exchange"]["name"])) == ()
    for key in ("ma_long", "ma_short"):
        assert {(entry["type"], entry["source"]) for entry in strategy["mrs3"][key]} == {("SMA", "ohlc4")}


def test_mrs3_tester_templates_use_current_import_report_profile() -> None:
    root = Path(__file__).resolve().parents[1]
    expected_history = {
        "pnl_without_top5": False,
        "pnl_without_best_pair": False,
        "ulcer_index": False,
        "mc_max_dd_p95": False,
        "mc_loss_probability": False,
        "AnyEnabled": False,
    }

    for exchange in ("Bybit", "BingX"):
        config = json.loads((root / f"templates/exchanges/{exchange}/mrs3-tester.json").read_text(encoding="utf-8"))
        report = config["report"]
        assert report["enable_html_report"] is True
        assert report["include_settings"] is True
        assert report["include_chart_balance"] is True
        assert report["include_summary_table"] is True
        assert report["include_trades_table"] is True
        assert report["include_analytics"] is False
        assert report["include_monthly_returns_heatmap"] is False
        assert report["include_chart_ohlc"] is False
        assert report["include_chart_position"] is False
        assert report["include_position_stats"] is False
        assert report["analytics_monte_carlo"] == 0
        assert report["analytics_monte_carlo_seed"] == 42
        assert report["enable_timing_logs"] is False
        assert "include_strategy_settings" not in report
        assert config["order_trigger_fill_worst"] is False
        assert config["order_limit_fill_plus_tick"] is False
        assert config["order_market_delay"] is False
        assert config["target_drawdown_open"] == 0.0
        assert config["target_drawdown"] == 0.0
        assert config["reports_history"] == expected_history
        assert config["InitialBalance"] == 10000.0
        assert config["LimitOrderVolumeCheck"] is False
        assert "accounts" not in config
        assert "parameter_mining" in config and config["parameter_mining"] == []

        if exchange == "Bybit":
            assert config["maintenance_margin_rate"] == 0.005
        else:
            assert "maintenance_margin_rate" not in config
