from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import pickle

import pytest

from mrs3.performance import PerformanceParseError, _compatible_duplicate_metric, parse_performance_report
from mrs3.performance_v2_html import (
    REPORT_FORMAT_2026_10,
    REPORT_FORMAT_LEGACY,
    PerformanceV2HtmlError,
    parse_current_performance_v2_html,
)
from mrs3.performance_v2_store import PerformanceV2Config


FIXTURES = Path(__file__).parent / "fixtures" / "performance"
NEW_FORMAT = FIXTURES / "report_2026_10.html"
LEGACY = FIXTURES / "report_current_v2.html"


def _limits() -> PerformanceV2Config:
    return PerformanceV2Config(Path("data/performance-v2"))


def _new_format(**replacements: str) -> bytes:
    text = NEW_FORMAT.read_text(encoding="utf-8")
    for old, new in replacements.items():
        assert text.count(old) >= 1, old
        text = text.replace(old, new)
    return text.encode("utf-8")


@pytest.mark.parametrize(("canonical", "duplicate", "compatible"), [
    ("4.83", "4.83", True),
    ("n/a", "n/a (single pair)", True),
    ("0.0918", "0.09 · ARKKUSDT · 2026-09-02 13:43", True),
    ("-0.1643", "-0.16 · ARKKUSDT · 2026-08-28 19:32", True),
    ("0.0918", "0.10", False),
    ("0.09", "0.0918", False),
    ("4.83", "n/a", False),
    ("text", "text2", False),
    ("9.9", "9.90", True),
    ("9.90", "9.9", True),
    ("8.69%", "8.7%", True),
    ("0.09", "0.09%", True),
    ("4.83", "4.83e5", False),
    ("9.9", "9.91", False),
])
def test_compatible_duplicate_metric_rule(canonical: str, duplicate: str, compatible: bool) -> None:
    assert _compatible_duplicate_metric(canonical, duplicate) is compatible


def test_shared_parser_keeps_rejecting_duplicates_by_default() -> None:
    with pytest.raises(PerformanceParseError, match="duplicate or empty keys"):
        parse_performance_report(NEW_FORMAT.read_bytes())


def test_new_format_parses_with_analytics() -> None:
    parsed = parse_current_performance_v2_html(NEW_FORMAT.read_bytes(), _limits())

    assert parsed.tester_version == "2026/10/10"
    assert parsed.report_format == REPORT_FORMAT_2026_10
    assert len(parsed.actions) == 2
    assert parsed.metrics["Largest win"] == "9.9184"
    analytics = parsed.analytics
    assert analytics is not None
    assert analytics.metrics["gross_profit_usdt"] == Decimal("10.0")
    assert analytics.metrics["funding_paid_usdt"] == Decimal("0.07")
    assert analytics.metrics["largest_win_usdt"] == Decimal("9.9184")
    assert analytics.metrics["trades_full_exit"] == 1
    assert analytics.metrics["avg_holding_seconds"] == 7200
    assert analytics.metrics["max_holding_seconds"] == ((24 + 22) * 60 + 3) * 60
    assert analytics.metrics["longest_drawdown_seconds"] == (7 * 24 + 13) * 3600
    assert analytics.metrics["sortino_monthly"] is None
    assert analytics.metrics["max_unrealized_dd_at_utc"] == datetime(2026, 1, 2, 13, 15, tzinfo=timezone.utc)
    assert analytics.metrics["avg_mae_usdt"] == Decimal("-0.0455")
    assert analytics.metrics["losing_trades_in_profit"] == 0
    assert analytics.metrics["var95_daily_pct"] == Decimal("0.09")
    assert analytics.metrics["avg_monthly_return_geo_pct"] == Decimal("0.99")
    assert analytics.metrics["worst_mae_usdt"] is None
    assert [(item.regime, item.pair_days, item.pnl_usdt, item.avg_pnl_per_pair_day_usdt) for item in analytics.regimes] == [
        ("TREND_HIGH_VOL", 2, Decimal("9.90"), Decimal("4.9500")),
        ("DOWNTREND", 0, Decimal("0.00"), None),
    ]
    assert analytics.regimes[0].winning_days_pct == Decimal("100.00")
    assert analytics.monthly == ((date(2026, 1, 1), Decimal("0.99")),)


def test_new_format_result_crosses_a_process_boundary() -> None:
    parsed = parse_current_performance_v2_html(NEW_FORMAT.read_bytes(), _limits())
    restored = pickle.loads(pickle.dumps(parsed))
    assert restored.analytics.metrics["gross_profit_usdt"] == Decimal("10.0")
    assert restored.analytics.regimes == parsed.analytics.regimes


def test_legacy_report_has_no_analytics() -> None:
    parsed = parse_current_performance_v2_html(LEGACY.read_bytes(), _limits())

    assert parsed.report_format == REPORT_FORMAT_LEGACY
    assert parsed.tester_version is None
    assert parsed.analytics is None


def test_older_tester_version_keeps_legacy_parsing() -> None:
    data = _new_format(**{"ver.2026/10/10": "ver.2026/09/01"})
    with pytest.raises(PerformanceV2HtmlError, match="duplicate or empty keys"):
        parse_current_performance_v2_html(data, _limits())


def test_new_format_rejects_conflicting_duplicate_metric() -> None:
    data = _new_format(**{"<td>9.92 · ONUSDT · 2026-01-03 01:00</td>": "<td>8.00 · ONUSDT · 2026-01-03 01:00</td>"})
    with pytest.raises(PerformanceV2HtmlError, match="conflicting duplicate metric: Largest win"):
        parse_current_performance_v2_html(data, _limits())


def test_new_format_rejects_unparsable_analytics_value() -> None:
    data = _new_format(**{"<td>Funding paid</td><td>0.07</td>": "<td>Funding paid</td><td>about 0.07</td>"})
    with pytest.raises(PerformanceV2HtmlError, match="REPORT_ANALYTICS_INVALID: Funding paid"):
        parse_current_performance_v2_html(data, _limits())


def test_new_format_rejects_unparsable_regime_row() -> None:
    data = _new_format(**{"<td>Trend · High vol</td><td>2</td>": "<td>Trend · High vol</td><td>two</td>"})
    with pytest.raises(PerformanceV2HtmlError, match="REPORT_ANALYTICS_INVALID: Regime Trend · High vol"):
        parse_current_performance_v2_html(data, _limits())


@pytest.mark.parametrize("token", ["inf", "-inf", "∞", "nan"])
def test_new_format_stores_non_finite_analytics_as_null(token: str) -> None:
    data = _new_format(**{"<td>Funding paid</td><td>0.07</td>": f"<td>Funding paid</td><td>{token}</td>"})
    parsed = parse_current_performance_v2_html(data, _limits())
    assert parsed.analytics.metrics["funding_paid_usdt"] is None


def test_new_format_treats_annotated_na_as_null() -> None:
    data = _new_format(**{"<td>Sortino ratio (monthly)</td><td>n/a</td>": "<td>Sortino ratio (monthly)</td><td>n/a (no losing months)</td>"})
    parsed = parse_current_performance_v2_html(data, _limits())
    assert parsed.analytics.metrics["sortino_monthly"] is None


def test_new_format_accepts_more_precise_equal_duplicate() -> None:
    data = _new_format(**{"<td>Total PnL</td><td>9.9</td></tr>\n<tr><td>PnL without best pair</td>":
                          "<td>Total PnL</td><td>9.90</td></tr>\n<tr><td>PnL without best pair</td>"})
    parsed = parse_current_performance_v2_html(data, _limits())
    assert parsed.metrics["Total PnL"] == "9.9"


def test_new_format_parses_ongoing_duration() -> None:
    data = _new_format(**{"<td>7d 13h 0m</td>": "<td>7d 13h 0m (ongoing)</td>"})
    parsed = parse_current_performance_v2_html(data, _limits())
    assert parsed.analytics.metrics["longest_drawdown_seconds"] == (7 * 24 + 13) * 3600


def test_report_with_two_tester_versions_is_rejected() -> None:
    data = _new_format(**{"<pre>": '<div><span>hb/tester/</span><span>ver.2026/10/11</span></div><pre>'})
    with pytest.raises(PerformanceV2HtmlError, match="more than one tester version"):
        parse_current_performance_v2_html(data, _limits())


def test_new_format_reads_multi_month_heatmap() -> None:
    data = _new_format(**{
        "<tr><th>Year</th><th>Jan</th><th>Feb</th><th>Year</th></tr>": "<tr><th>Year</th><th>Jan</th><th>Feb</th><th>Mar</th><th>Year</th></tr>",
        "<tr><td>2026</td><td>0.99%</td><td>—</td><td>0.99%</td></tr>": "<tr><td>2026</td><td>0.99%</td><td>—</td><td>-1.50%</td><td>-0.52%</td></tr>",
    })
    parsed = parse_current_performance_v2_html(data, _limits())
    assert parsed.analytics.monthly == ((date(2026, 1, 1), Decimal("0.99")), (date(2026, 3, 1), Decimal("-1.50")))


@pytest.mark.parametrize("replacements", [
    {"<tr><th>Year</th><th>Jan</th><th>Feb</th><th>Year</th></tr>": "<tr><th>Year</th><th>Jan</th><th>Feb</th><th>Mar</th></tr>"},
    {"<tr><td>2026</td><td>0.99%</td><td>—</td><td>0.99%</td></tr>": "<tr><td>2026</td><td>0.99%</td><td>—</td><td>0.99%</td></tr><tr><td>2026</td><td>0.50%</td><td>—</td><td>0.50%</td></tr>"},
])
def test_new_format_rejects_malformed_heatmap(replacements: dict[str, str]) -> None:
    with pytest.raises(PerformanceV2HtmlError, match="REPORT_ANALYTICS_INVALID: Monthly"):
        parse_current_performance_v2_html(_new_format(**replacements), _limits())


def test_new_format_rejects_truncated_regime_row() -> None:
    data = _new_format(**{"<td>Trend · High vol</td><td>2</td><td>9.90</td><td>4.9500</td><td>100.00%</td><td>100.00%</td>":
                          "<td>Trend · High vol</td><td>2</td><td>9.90</td>"})
    with pytest.raises(PerformanceV2HtmlError, match="REPORT_ANALYTICS_INVALID: Regime Trend · High vol"):
        parse_current_performance_v2_html(data, _limits())


def test_source_v6_keeps_rejecting_the_new_format() -> None:
    from mrs3.source_v6 import normalize_source_v6

    with pytest.raises(Exception, match="duplicate or empty keys"):
        normalize_source_v6(NEW_FORMAT.read_bytes(), source_name="report_2026_10.html")


def test_frozen_v12_metric_catalog_matches_parser_labels() -> None:
    from mrs3.performance_v2_html import REPORT_ANALYTICS_LABELS
    from mrs3.performance_v2_store import REPORT_METRIC_COLUMNS

    sql_types = {"decimal": "DOUBLE", "integer": "BIGINT", "count_of": "BIGINT", "duration": "BIGINT", "timestamp": "TIMESTAMPTZ"}
    assert REPORT_METRIC_COLUMNS == tuple(
        (column, sql_types[kind]) for column, _label, kind in REPORT_ANALYTICS_LABELS
    )
