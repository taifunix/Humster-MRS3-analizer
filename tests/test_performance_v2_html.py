from datetime import datetime, timezone
from decimal import Decimal
import pickle
from pathlib import Path

import duckdb
import pytest

from mrs3.performance import parse_performance_report
from mrs3.performance_v2_html import (
    PerformanceV2HtmlError,
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


@pytest.mark.parametrize(
    ("needle", "replacement", "message"),
    [
        (b"<th>Post Size</th>", b"", "Post Size"),
        (b"<th>Post Side</th>", b"", "Post Side"),
        (b"<th>Post Side</th>", b"<th>Post Side</th><th>Unexpected</th>", "exact"),
    ],
)
def test_parser_requires_exact_current_action_header(
    needle: bytes, replacement: bytes, message: str
) -> None:
    source = _current().replace(needle, replacement, 1)
    with pytest.raises(PerformanceV2HtmlError, match=message):
        parse_current_performance_v2_html(source, _limits())


def test_parser_rejects_legacy_layout_even_when_v1_accepts_it() -> None:
    source = (FIXTURES / "report_import.html").read_bytes()
    parse_performance_report(source)
    with pytest.raises(PerformanceV2HtmlError, match="exact current action header"):
        parse_current_performance_v2_html(source, _limits())


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b"<td>1</td><td>opened</td>", b"<td>one</td><td>opened</td>", "integer"),
        (b"<td>0.05</td><td>0</td>", b"<td>NaN</td><td>0</td>", "finite"),
        (b"<td>999.95</td><td>1</td><td>1</td><td>long</td>", b"<td>999.95</td><td>1</td><td>-1</td><td>long</td>", "Post Size.*negative"),
        (b"<td>1</td><td>long</td>", b"<td>1</td><td></td>", "Post Side"),
        (b"<td>ONUSDT</td><td>1</td>", b"<td>BTCUSDT</td><td>1</td>", "symbol"),
        (b"<td>ONUSDT</td><td>1</td>", b"<td>ONUSDT</td><td>2</td>", "order"),
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
