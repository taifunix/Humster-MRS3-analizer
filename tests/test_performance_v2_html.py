from datetime import datetime, timezone
from decimal import Decimal
import os
import pickle
from pathlib import Path
import subprocess
import sys

import duckdb
import pytest

from mrs3.performance import parse_performance_report
from mrs3.performance_v2_html import (
    PerformanceV2HtmlError,
    _validate_report_integrity,
    parse_current_performance_v2_html,
)
from mrs3.performance_v2_store import PerformanceV2Config


FIXTURES = Path(__file__).parent / "fixtures" / "performance"
CURRENT = FIXTURES / "report_current_v2.html"


def _limits(**kwargs: int) -> PerformanceV2Config:
    return PerformanceV2Config(Path("data/performance-v2"), **kwargs)


def _current() -> bytes:
    return CURRENT.read_bytes()


def test_parser_accepts_only_the_current_typed_layout() -> None:
    parsed = parse_current_performance_v2_html(_current(), _limits())

    assert parsed.settings["exchange"]["use_upnl"] is True
    assert parsed.actions[0].timestamp_utc == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert parsed.actions[0].order_id == 1
    assert parsed.actions[0].fee == Decimal("0.05")
    assert parsed.actions[0].post_size == Decimal("1")
    assert parsed.actions[1].post_side == ""
    assert parsed.wallet_series[0][0] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert parsed.wallet_series[-1][1] == Decimal("1009.9")


def test_parser_rejects_action_rows_incomplete_against_declared_transaction_count() -> None:
    source = _current().replace(
        b"<td>Total transactions (buy/sell)</td><td>2</td>",
        b"<td>Total transactions (buy/sell)</td><td>3</td>",
        1,
    )

    with pytest.raises(PerformanceV2HtmlError, match="transaction count"):
        parse_current_performance_v2_html(source, _limits())


def test_parser_rejects_blank_declared_transaction_count() -> None:
    source = _current().replace(
        b"<td>Total transactions (buy/sell)</td><td>2</td>",
        b"<td>Total transactions (buy/sell)</td><td> </td>",
        1,
    )

    with pytest.raises(PerformanceV2HtmlError, match="Total transactions"):
        parse_current_performance_v2_html(source, _limits())


def test_parser_rejects_final_wallet_incomplete_against_declared_balance() -> None:
    source = _current().replace(
        b"<td>Final balance</td><td>1009.9</td>",
        b"<td>Final balance</td><td>1010.9</td>",
        1,
    )

    with pytest.raises(PerformanceV2HtmlError, match="final wallet"):
        parse_current_performance_v2_html(source, _limits())


def test_integrity_check_rejects_declared_final_balance_without_wallet_sample() -> None:
    with pytest.raises(PerformanceV2HtmlError, match="wallet sample"):
        _validate_report_integrity({"Final balance": "100"}, (), ())


def test_parser_keeps_current_import_compatible_when_optional_integrity_metrics_are_absent() -> None:
    source = _current().replace(
        b"<tr><td>Total transactions (buy/sell)</td><td>2</td></tr>\n", b"", 1
    ).replace(
        b"<tr><td>Final balance</td><td>1009.9</td></tr>\n", b"", 1
    )

    assert len(parse_current_performance_v2_html(source, _limits()).actions) == 2


def test_parser_accepts_event_at_tester_report_end() -> None:
    source = _current().replace(b"2026-01-03T01:00:00+00:00", b"2026-01-09T00:00:00Z", 1)

    assert len(parse_current_performance_v2_html(source, _limits()).actions) == 2


def test_parser_rejects_event_after_tester_report_end() -> None:
    source = _current().replace(b"2026-01-03T01:00:00+00:00", b"2026-01-09T00:00:01Z", 1)

    with pytest.raises(PerformanceV2HtmlError, match="outside Report range"):
        parse_current_performance_v2_html(source, _limits())


@pytest.mark.parametrize(
    ("needle", "replacement", "message"),
    [
        (b"<th>Post Size</th>", b"", "Post Size"),
        (b"<th>Post Side</th>", b"", "Post Side"),
    ],
)
def test_parser_requires_required_current_action_headers(
    needle: bytes, replacement: bytes, message: str
) -> None:
    source = _current().replace(needle, replacement, 1)
    with pytest.raises(PerformanceV2HtmlError, match=message):
        parse_current_performance_v2_html(source, _limits())


def test_parser_accepts_current_action_table_with_extra_columns() -> None:
    source = _current()
    for old, new in (
        (
            b"<th>Action</th><th>Fee</th>",
            b"<th>Action</th><th>Side</th><th>Price</th><th>Cost</th><th>Fee</th>",
        ),
        (
            b"<td>opened</td><td>0.05</td>",
            b"<td>opened</td><td>buy</td><td>1</td><td>1</td><td>0.05</td>",
        ),
        (
            b"<td>closed</td><td>0.05</td>",
            b"<td>closed</td><td>sell</td><td>1</td><td>1</td><td>0.05</td>",
        ),
    ):
        previous = source
        source = source.replace(old, new, 1)
        assert source != previous

    parsed = parse_current_performance_v2_html(source, _limits())
    assert parsed.actions[0].order_id == 1
    assert parsed.actions[0].action == "opened"
    assert parsed.actions[0].fee == Decimal("0.05")
    assert parsed.actions[1].pnl == Decimal("9.9")


def test_parser_does_not_treat_report_order_id_as_strategy_order_slot() -> None:
    source = _current().replace(b"<td>1</td><td>opened</td>", b"<td>2</td><td>opened</td>", 1)
    source = source.replace(b"<td>1</td><td>closed</td>", b"<td>2</td><td>closed</td>", 1)

    assert parse_current_performance_v2_html(source, _limits()).actions[0].order_id == 2


def test_parser_rejects_legacy_layout_even_when_v1_accepts_it() -> None:
    source = (FIXTURES / "report_import.html").read_bytes()
    parse_performance_report(source)
    with pytest.raises(PerformanceV2HtmlError, match="required current action header"):
        parse_current_performance_v2_html(source, _limits())


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b"<td>1</td><td>opened</td>", b"<td>one</td><td>opened</td>", "integer"),
        (b"<td>0.05</td><td>0</td>", b"<td>NaN</td><td>0</td>", "finite"),
        (b"<td>999.95</td><td>1</td><td>1</td><td>long</td>", b"<td>999.95</td><td>1</td><td>-1</td><td>long</td>", "Post Size.*negative"),
        (b"<td>1</td><td>long</td>", b"<td>1</td><td></td>", "Post Side"),
        (b"<td>ONUSDT</td><td>1</td>", b"<td>BTCUSDT</td><td>1</td>", "symbol"),
    ],
)
def test_parser_rejects_invalid_typed_action_fields(
    old: bytes, new: bytes, message: str
) -> None:
    # Keep replacements local to an action row; the source fixture has no
    # duplicate metric/series snippets for these exact byte sequences.
    source = _current().replace(old, new, 1)
    with pytest.raises(PerformanceV2HtmlError, match=message):
        parse_current_performance_v2_html(source, _limits())


def test_parser_enforces_action_limit() -> None:
    with pytest.raises(PerformanceV2HtmlError, match="action limit"):
        parse_current_performance_v2_html(_current(), _limits(max_actions_per_report=1))


def test_parser_requires_upnl() -> None:
    source = _current().replace(b'"use_upnl":true', b'"use_upnl":false', 1)
    with pytest.raises(PerformanceV2HtmlError, match="use_upnl"):
        parse_current_performance_v2_html(source, _limits())


def test_worker_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("worker touched external state")

    monkeypatch.setattr(duckdb, "connect", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "unlink", forbidden)
    parse_current_performance_v2_html(_current(), _limits())


def test_parsed_report_can_cross_a_process_pool_boundary() -> None:
    parsed = parse_current_performance_v2_html(_current(), _limits())
    restored = pickle.loads(pickle.dumps(parsed))

    assert restored == parsed
    with pytest.raises(TypeError):
        parsed.settings["new"] = "value"  # type: ignore[index]


def test_parser_loads_and_runs_without_a_duckdb_runtime_import() -> None:
    script = f"""
import builtins
from pathlib import Path
from types import SimpleNamespace

real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "duckdb" or name.startswith("duckdb."):
        raise AssertionError("parser imported duckdb")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from mrs3.performance_v2_html import parse_current_performance_v2_html

data = Path({str(CURRENT)!r}).read_bytes()
parse_current_performance_v2_html(
    data,
    SimpleNamespace(max_html_bytes=len(data), max_actions_per_report=10),
)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
