from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import mrs3.performance as performance

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


def test_parser_rejects_malformed_settings_like_pre_alongside_valid_settings() -> None:
    source = (FIXTURES / "report_valid.html").read_bytes() + b'<pre>{"name":"broken",</pre>'
    with pytest.raises(PerformanceParseError, match="malformed settings JSON"):
        parse_performance_report(source)


@pytest.mark.parametrize(
    ("name", "broken"),
    [
        ("implicit_td", (b"<td>Total PnL, %</td>", b"<td>Total PnL, %")),
        ("implicit_tr", (b"</tr>", b"")),
        ("implicit_th", (b"<th>Metric</th>", b"<th>Metric")),
    ],
)
def test_raw_markup_rejects_repairs_a_tree_builder_would_hide(name: str, broken: tuple[bytes, bytes]) -> None:
    """The cross-check parser must not silently accept lxml's recovery.

    A spec-compliant HTML5 tree builder closes these tags implicitly and agrees
    with lxml, which is exactly the agreement this parser exists to withhold.
    """
    original, replacement = broken
    source = (FIXTURES / "report_valid.html").read_bytes()
    assert original in source, f"fixture no longer contains {original!r}"
    damaged = source.replace(original, replacement, 1)

    with pytest.raises(PerformanceParseError):
        parse_performance_report(damaged)


def test_raw_markup_backend_is_a_tokenizer_not_a_tree_builder() -> None:
    """Guard the substitution the third review caught: lexbor et al. repair.

    The cross-check has value only while the second parser *disagrees* with
    lxml about unterminated cells. Asserting the disagreement directly is the
    only form of this test a tree builder cannot satisfy.
    """
    assert performance._raw_markup is performance._stdlib_raw_markup
    unterminated = "<table><thead><tr><th>Metric<th>Value</tr></thead><tbody><tr><td>a<td>b</tr></tbody></table>"
    _pre, tables = performance._raw_markup(unterminated)

    headers, rows = tables[0]
    assert headers == ()
    assert rows == ()

    # lxml, the tree builder this is cross-checking, recovers all four cells.
    document = performance.html.fromstring(unterminated)
    assert [cell.text_content() for cell in document.xpath("//th")] == ["Metric", "Value"]
    assert [cell.text_content() for cell in document.xpath("//td")] == ["a", "b"]


def test_raw_inventory_rejects_structure_before_semantic_dom_can_hide_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (FIXTURES / "report_valid.html").read_bytes() + (
        b'<pre>{"name":"second","basic":{}}</pre>'
    )
    original = performance.html.fromstring

    def hide_extra_pre(value: object) -> object:
        document = original(value)
        for pre in document.xpath("//pre")[1:]:
            pre.getparent().remove(pre)
        return document

    monkeypatch.setattr(performance.html, "fromstring", hide_extra_pre)
    with pytest.raises(PerformanceParseError, match="settings"):
        parse_performance_report(source)


@pytest.mark.parametrize("source", [b"", b"<html><body><"])
def test_parser_normalizes_empty_and_malformed_html(source: bytes) -> None:
    with pytest.raises(PerformanceParseError):
        parse_performance_report(source)


def test_parser_rejects_duplicate_table_headers_before_mapping_rows() -> None:
    source = (FIXTURES / "report_valid.html").read_bytes().replace(
        b"<th>Side</th>", b"<th>Symbol</th>"
    )
    with pytest.raises(PerformanceParseError, match="duplicate table header"):
        parse_performance_report(source)


def test_parser_ignores_duplicate_headers_in_irrelevant_calendar_table() -> None:
    source = (
        (FIXTURES / "report_valid.html").read_bytes()
        .replace(
            b"<table><thead>",
            b'<table class="monthly-heatmap-table"><thead>',
            1,
        )
    )
    source = source.replace(
        b"</html>",
        b"<table><thead><tr><th>Year</th><th>Jan</th><th>Year</th></tr></thead>"
        b"<tbody><tr><td>2026</td><td>1</td><td>2026</td></tr></tbody></table></html>",
    )
    parsed = parse_performance_report(source)
    assert parsed.inventory.trade_row_count == 2


def test_inventory_timestamp_bounds_include_equity_series() -> None:
    source = (FIXTURES / "report_valid.html").read_bytes()
    source = source.replace(b"1785549600000,\"1012.50\"", b"1785553200000,\"1012.50\"")
    source = source.replace(b"1785549600000,\"1012.5\"", b"1785553200000,\"1012.5\"")
    parsed = parse_performance_report(source)
    assert parsed.inventory.maximum_timestamp == datetime(2026, 8, 1, 3, tzinfo=timezone.utc)


def test_parser_rejects_wallet_equity_timestamp_mismatch() -> None:
    source = (FIXTURES / "report_valid.html").read_bytes().replace(
        b"1785546000000,\"1005.25\"],[1785549600000,\"1012.5\"]",
        b"1785546000001,\"1005.25\"],[1785549600000,\"1012.5\"]",
    )
    with pytest.raises(PerformanceParseError, match="timestamps"):
        parse_performance_report(source)


def test_parser_interprets_canonical_tester_timestamps_as_utc() -> None:
    source = (FIXTURES / "report_valid.html").read_bytes()
    source = source.replace(b"2026-08-01T00:00:00Z", b"2026-08-01 00:00:00")
    source = source.replace(b"2026-08-01T01:00:00+00:00", b"2026-08-01 01:00:00")
    parsed = parse_performance_report(source)
    assert parsed.actions[0]["Timestamp"] == "2026-08-01 00:00:00"
    assert parsed.inventory.minimum_timestamp == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_parser_aggregates_metric_sections_with_identical_headers() -> None:
    source = (FIXTURES / "report_valid.html").read_bytes()
    source = source.replace(
        b'<tr><td>Total PnL, %</td><td>12.50</td><td>exact</td></tr>\n'
        b'<tr><td>Total Trades</td><td>2</td><td>exact</td></tr>',
        b'<tr><td>Total PnL, %</td><td>12.50</td><td>exact</td></tr></tbody></table>'
        b'<table><thead><tr><th>Metric</th><th>Value</th><th>Notes</th></tr></thead><tbody>'
        b'<tr><td>Total Trades</td><td>2</td><td>exact</td></tr>',
    )
    parsed = parse_performance_report(source)
    assert parsed.metrics == {"Total PnL, %": "12.50", "Total Trades": "2"}
    assert parsed.inventory.metric_count == 2


@pytest.mark.parametrize("timestamp", [b"2026-8-1 00:00:00", b"08/01/2026 00:00:00", b"2026-08-01T00:00:00"])
def test_parser_rejects_ambiguous_or_unzoned_noncanonical_timestamps(timestamp: bytes) -> None:
    source = (FIXTURES / "report_valid.html").read_bytes().replace(b"2026-08-01T00:00:00Z", timestamp)
    with pytest.raises(PerformanceParseError, match="timestamp"):
        parse_performance_report(source)


def test_parser_wraps_epoch_conversion_errors() -> None:
    source = (FIXTURES / "report_valid.html").read_bytes()
    for old, new in (
        (b"1785542400000", b"999999999999999999990"),
        (b"1785546000000", b"999999999999999999991"),
        (b"1785549600000", b"999999999999999999992"),
    ):
        source = source.replace(old, new)
    with pytest.raises(PerformanceParseError, match="invalid UTC timestamp"):
        parse_performance_report(source)


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


ZERO_ACTIVITY = Path(__file__).parent / "fixtures" / "performance" / "source_v6_zero_activity.html"


def test_zero_activity_report_is_rejected_unless_the_caller_opts_in() -> None:
    """Z1: the relaxation is opt-in, so the v1 store keeps its contract.

    `performance_import` derives DD5 candidates from this parser under
    ADR-0006; admitting empty runs there would change that contract silently.
    """
    with pytest.raises(PerformanceParseError, match="must be non-empty"):
        parse_performance_report(ZERO_ACTIVITY.read_bytes())


def test_zero_activity_report_is_admitted_when_it_declares_emptiness() -> None:
    """Z1/Z2/Z3: a complete report of a run with no trades parses to zero facts."""
    parsed = parse_performance_report(ZERO_ACTIVITY.read_bytes(), allow_zero_activity=True)
    assert parsed.actions == ()
    assert parsed.wallet_series == ()
    assert parsed.equity_series == ()
    assert parsed.inventory.trade_row_count == 0
    assert parsed.inventory.wallet_sample_count == 0
    assert parsed.inventory.equity_sample_count == 0
    # Z3: the window comes from `Report range`, never a sentinel.
    assert parsed.inventory.minimum_timestamp == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parsed.inventory.maximum_timestamp == datetime(2026, 1, 9, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "old, new, message",
    [
        # Z1: the two counters that directly contradict emptiness.
        ("<tr><td>Total Trades</td><td>0</td></tr>", "", "missing 'Total Trades'"),
        (
            "<tr><td>Total transactions (buy/sell)</td><td>0</td></tr>",
            "",
            "missing 'Total transactions",
        ),
        (
            "<tr><td>Total Trades</td><td>0</td></tr>",
            "<tr><td>Total Trades</td><td>3</td></tr>",
            "non-zero 'Total Trades'",
        ),
        # Z1: corroboration — present and contradictory is fatal.
        (
            "<tr><td>Total fees</td><td>0.00</td></tr>",
            "<tr><td>Total fees</td><td>0.20</td></tr>",
            "non-zero 'Total fees'",
        ),
        ("<tr><td>Final balance</td><td>1000</td></tr>", "<tr><td>Final balance</td><td>1005</td></tr>", "changes balance"),
        # Z1: a run with no trades cannot have computed these.
        (
            "<tr><td>Sharpe ratio (monthly)</td><td>n/a</td></tr>",
            "<tr><td>Sharpe ratio (monthly)</td><td>1.4</td></tr>",
            "computes 'Sharpe ratio",
        ),
        # Z3: without a window there is nothing to derive the interval from.
        ("<tr><td>Report range</td><td>2026-01-01 - 2026-01-09</td></tr>", "", "missing 'Report range'"),
    ],
)
def test_zero_activity_report_needs_every_criterion(old: str, new: str, message: str) -> None:
    """Each Z1 criterion is pinned by a report that violates only it.

    Absence of data is never evidence of emptiness — a truncated report has no
    data either — so every one of these must keep the report out.
    """
    source = ZERO_ACTIVITY.read_text(encoding="utf-8")
    assert old in source
    with pytest.raises(PerformanceParseError, match=message):
        parse_performance_report(source.replace(old, new).encode("utf-8"), allow_zero_activity=True)


def test_zero_activity_flag_does_not_admit_a_missing_series_assignment() -> None:
    """Z2: only an explicitly empty array is a claim; a missing one is a defect."""
    source = ZERO_ACTIVITY.read_text(encoding="utf-8").replace("const walletSeries = [];", "")
    with pytest.raises(PerformanceParseError, match="exactly one walletSeries assignment"):
        parse_performance_report(source.encode("utf-8"), allow_zero_activity=True)


def test_zero_activity_flag_does_not_admit_trades_without_samples() -> None:
    """Z2: zero must be zero everywhere at once.

    A report with actions but no samples is not a run in which nothing
    happened; it is a run whose samples did not render.
    """
    source = ZERO_ACTIVITY.read_text(encoding="utf-8").replace(
        "</tbody></table>\n<script>",
        "<tr><td>2026-01-02T01:00:00Z</td><td>ONUSDT</td><td>1</td><td>closed</td>"
        "<td>0</td><td>0</td><td>1000</td><td>1</td><td>0</td><td></td></tr>\n</tbody></table>\n<script>",
    )
    with pytest.raises(
        PerformanceParseError, match="must be non-empty for a report with actions"
    ):
        parse_performance_report(source.encode("utf-8"), allow_zero_activity=True)
