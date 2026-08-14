from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from mrs3.performance import PerformanceParseError, parse_performance_report


FIXTURES = Path(__file__).parent / "fixtures" / "performance"


def test_parser_preserves_inventory_and_complete_semantic_data() -> None:
    parsed = parse_performance_report((FIXTURES / "report_valid.html").read_bytes())

    assert parsed.settings["nested"] == {"keep": [1, True, "x"]}
    assert parsed.metrics == {"Total PnL, %": "12.50", "Total Trades": "2"}
    assert parsed.actions[1]["Side"] == "sell"
    assert len(parsed.wallet_series) == len(parsed.equity_series) == 3
    assert parsed.wallet_series[0] == (1785542400000, Decimal("1000.00"))
    assert parsed.equity_series[-1] == (1785549600000, Decimal("1012.5"))
    assert parsed.inventory.metric_count == 2
    assert parsed.inventory.trade_row_count == 2
    assert parsed.inventory.wallet_sample_count == 3
    assert parsed.inventory.equity_sample_count == 3
    assert parsed.inventory.minimum_timestamp == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert parsed.inventory.maximum_timestamp == datetime(2026, 8, 1, 2, tzinfo=timezone.utc)


def test_parser_rejects_duplicate_equity_series() -> None:
    with pytest.raises(PerformanceParseError, match="exactly one equitySeries"):
        parse_performance_report((FIXTURES / "report_duplicate_equity.html").read_bytes())


@pytest.mark.parametrize(
    "replacement",
    [b"const walletSeries = [];", b"const equitySeries = [[1785542400000, \"NaN\"]];"],
)
def test_parser_rejects_empty_or_invalid_series(replacement: bytes) -> None:
    source = (FIXTURES / "report_valid.html").read_bytes()
    if b"walletSeries" in replacement:
        source = source.replace(
            b"const walletSeries = [[1785542400000,\"1000.00\"],[1785546000000,\"1005.25\"],[1785549600000,\"1012.50\"]];",
            replacement,
        )
    else:
        source = source.replace(
            b"const equitySeries = [[1785542400000,\"1000\"],[1785546000000,\"1005.25\"],[1785549600000,\"1012.5\"]];",
            replacement,
        )
    with pytest.raises(PerformanceParseError):
        parse_performance_report(source)
