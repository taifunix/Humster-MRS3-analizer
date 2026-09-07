from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from mrs3.portfolio.metrics import calculate_metrics, check_applied_leverage
from mrs3.portfolio.reports import normalize_report
from tests.test_portfolio_reports import fixture_report


def test_primary_result_uses_full_equity_and_keeps_realized_separate():
    actions = [
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "1"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "CLOSE", "side": "LONG", "size": "1", "pnl": "5"},
        {"timestamp": "2026-01-01T00:00:02Z", "symbol": "ETHUSDT", "action": "OPEN", "side": "LONG", "size": "1"},
    ]
    raw = fixture_report(actions=actions)
    raw["period"]["end"] = "2026-01-01T00:00:03Z"
    raw["series"]["equity"] = [
        {"timestamp": "2026-01-01T00:00:00Z", "value": "100"},
        {"timestamp": "2026-01-01T00:00:01Z", "value": "105"},
        {"timestamp": "2026-01-01T00:00:03Z", "value": "98"},
    ]
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.primary_result == Decimal("-2")
    assert metrics.realized_pnl == Decimal("5")
    assert abs(metrics.actual_drawdown_pct - Decimal("7") / Decimal("105") * Decimal("100")) < Decimal("0.0000000001")
    assert "OPEN_AT_END" in metrics.censoring
    assert metrics.actual_concurrency == 1


def test_missing_equity_sample_is_blocking_and_metrics_are_undefined():
    metrics = calculate_metrics(normalize_report(fixture_report(equity=False)))
    assert metrics.primary_result is None
    assert "EQUITY_PATH_MISSING" in metrics.blocking_diagnostics
    assert not metrics.complete


def test_gaps_and_margin_guard_are_retained_separately():
    raw = fixture_report()
    raw["series"]["equity"] = [
        {"timestamp": "2026-01-01T00:00:00Z", "value": "100"},
        {"timestamp": "2026-01-01T00:00:01Z", "value": "102"},
        {"timestamp": "2026-01-01T00:00:03Z", "value": "101"},
    ]
    raw["period"]["end"] = "2026-01-01T00:00:03Z"
    raw["series"]["notional"] = [["2026-01-01T00:00:00Z", "20"], ["2026-01-01T00:00:03Z", "20"]]
    raw["series"]["margin_balance"] = [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:03Z", "100"]]
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.gaps and metrics.margin_guard.status == "PASS"
    assert metrics.coverage == "COMPLETE"


def test_censored_cycle_keeps_realized_partial_reduction():
    raw = fixture_report(actions=[
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "5"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "REDUCE", "side": "LONG", "size": "2", "pnl": "3"},
    ])
    raw["portfolio"] = {"realized_pnl": "3"}
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.realized_pnl == Decimal("3")
    assert "FINANCIAL_RECONCILIATION_FAILED" not in metrics.blocking_diagnostics


def test_drawdown_percentage_uses_peak_at_each_trough():
    raw = fixture_report()
    raw["period"]["end"] = "2026-01-01T00:00:03Z"
    raw["series"]["equity"] = [
        ["2026-01-01T00:00:00Z", "100"],
        ["2026-01-01T00:00:01Z", "50"],
        ["2026-01-01T00:00:02Z", "1000"],
        ["2026-01-01T00:00:03Z", "950"],
    ]
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.actual_drawdown == Decimal("50")
    assert metrics.actual_drawdown_pct == Decimal("50")


def test_summary_reconciliation_accepts_case_and_space_variants():
    raw = fixture_report()
    raw["portfolio"] = {" Initial Equity ": "100", "FINAL EQUITY": "103", "Realized PNL": "4"}
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.primary_result == Decimal("3")
    assert "FINANCIAL_RECONCILIATION_FAILED" not in metrics.blocking_diagnostics


def test_partial_summary_boundaries_are_blocking_reconciliation_evidence():
    raw = fixture_report()
    raw["portfolio"] = {"Initial Equity": "100"}
    metrics = calculate_metrics(normalize_report(raw))
    assert "FINANCIAL_RECONCILIATION_FAILED" in metrics.blocking_diagnostics
    assert not metrics.complete


def test_leverage_missing_and_mismatch_are_typed():
    assert check_applied_leverage({"BTCUSDT": "3"}, {}).reason == "LEVERAGE_UNVERIFIED"
    assert check_applied_leverage({"BTCUSDT": "3"}, {"BTCUSDT": "5"}).reason == "LEVERAGE_MISMATCH"


def test_planned_leverage_without_report_readback_needs_retest():
    metrics = calculate_metrics(normalize_report(fixture_report()), planned_leverage={"BTCUSDT": "3"})
    assert metrics.status == "NEEDS_RETEST"
    assert metrics.leverage.reason == "LEVERAGE_UNVERIFIED"


def test_margin_bound_failure_is_a_blocking_metric():
    raw = fixture_report()
    raw["series"]["notional"] = [["2026-01-01T00:00:00Z", "200"], ["2026-01-01T00:00:02Z", "200"]]
    raw["series"]["margin_balance"] = [["2026-01-01T00:00:00Z", "100"], ["2026-01-01T00:00:02Z", "100"]]
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.margin_guard.status == "FAIL"
    assert metrics.margin_guard.reason == "MARGIN_BOUND_FAILED"
    assert "MARGIN_BOUND_FAILED" in metrics.diagnostics
    assert "MARGIN_BOUND_FAILED" in metrics.blocking_diagnostics
    assert not metrics.complete


def test_declared_realized_pnl_requires_close_pnl_evidence():
    raw = fixture_report()
    raw["actions"][1].pop("pnl")
    raw["portfolio"] = {"realized_pnl": "0"}
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.status == "INCOMPLETE"
    assert "REALIZED_PNL_UNAVAILABLE" in metrics.diagnostics
    assert "FINANCIAL_RECONCILIATION_UNVERIFIED" in metrics.blocking_diagnostics
    assert "REALIZED_PNL_UNAVAILABLE" in normalize_report(raw).cycles[0].diagnostics


def test_declared_fees_require_fee_evidence_on_each_action():
    raw = fixture_report()
    raw["portfolio"] = {"fees": "0"}
    metrics = calculate_metrics(normalize_report(raw))
    assert metrics.status == "INCOMPLETE"
    assert "FEES_UNAVAILABLE" in metrics.diagnostics
    assert "FINANCIAL_RECONCILIATION_UNVERIFIED" in metrics.blocking_diagnostics


@pytest.mark.parametrize("field", ["fees", "funding", "net_pnl"])
def test_invalid_declared_financial_number_is_typed_blocking(field):
    report = normalize_report(fixture_report())
    report = replace(report, portfolio={field: "not-a-number"})
    metrics = calculate_metrics(report)
    assert "FINANCIAL_RECONCILIATION_FAILED" in metrics.blocking_diagnostics
    assert metrics.status == "INCOMPLETE"
