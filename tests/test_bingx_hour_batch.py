"""The BingX gap check takes the hours of up to 40 days in one request (BingX answers at most 1,000 hour candles).

Contract: docs/specs/2026-10-20-panel-sync-by-exchange.md, «Part 3 contract». Fakes only.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mrs3.pair_registry.minute_days import HOUR_DAYS_PER_REQUEST, verify_bingx_days
from tests.test_minute_days import API, LISTED, _hours, _minute, _ms

FIRST = date(2026, 6, 1)


def _day_ms(day: date) -> int:
    return _ms(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))


class RangeBingX:
    """Minutes per day; hour candles of every day in the requested range (like BingX); records each request."""

    def __init__(self, minutes: dict[date, list[dict]], hours: dict[date, list[dict]]):
        self.minutes, self.hours, self.calls = minutes, hours, []

    def klines(self, symbol, start_ms, end_ms, interval="1m"):
        assert symbol == API
        first = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        last = datetime.fromtimestamp(end_ms / 1000, timezone.utc).date()
        self.calls.append((interval, first, last))
        if interval == "1m":
            return list(self.minutes.get(first, []))
        out = [candle for day in sorted(self.hours) if first <= day <= last for candle in self.hours[day]]
        assert len(out) <= 1000                                                   # BingX's cap per request
        return out


def _world(days: int, gappy: set[int]):
    span = [FIRST + timedelta(days=index) for index in range(days)]
    minutes, hours = {}, {}
    for index, day in enumerate(span):
        rows = [_minute(day, minute) for minute in range(1440) if not (index in gappy and 600 <= minute < 660)]
        minutes[day] = rows
        hours[day] = _hours(rows, day)
    return span, RangeBingX(minutes, hours)


def test_the_gap_days_of_one_chunk_share_one_hourly_request(tmp_path: Path) -> None:
    span, client = _world(10, gappy={1, 4, 7})
    now = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)
    result = verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), span[0], span[-1], known={}, client=client,
                               now=now)
    assert result.accepted == span and result.refused == {}
    assert [record["status"] for record in result.records].count("VERIFIED_GAPS") == 3
    hourly = [call for call in client.calls if call[0] == "1h"]
    assert hourly == [("1h", span[0], span[-1])]                                    # one request for the span


def test_a_long_span_is_cut_into_chunks_of_40_days(tmp_path: Path) -> None:
    assert HOUR_DAYS_PER_REQUEST == 40                                              # 960 hours < 1,000
    span, client = _world(90, gappy={3, 50, 85})
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    result = verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), span[0], span[-1], known={}, client=client,
                               now=now, threads=4)
    assert result.accepted == span
    hourly = sorted(call for call in client.calls if call[0] == "1h")
    assert hourly == [("1h", span[0], span[39]), ("1h", span[40], span[79]), ("1h", span[80], span[89])]


def test_no_gap_needs_no_hourly_request(tmp_path: Path) -> None:
    span, client = _world(5, gappy=set())
    verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), span[0], span[-1], known={}, client=client,
                      now=datetime(2026, 6, 20, 12, tzinfo=timezone.utc))
    assert [call for call in client.calls if call[0] == "1h"] == []


def test_a_failed_hourly_request_refuses_only_its_gap_days_and_is_tried_again(tmp_path: Path) -> None:
    from mrs3.bingx_downloader import BingXDownloadError

    span, client = _world(10, gappy={2, 6})
    original, failures = client.klines, []

    def flaky(symbol, start_ms, end_ms, interval="1m"):
        if interval == "1h" and not failures:
            failures.append(1)
            raise BingXDownloadError("BingX code 100410: retry later")
        return original(symbol, start_ms, end_ms, interval)

    client.klines = flaky
    result = verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), span[0], span[-1], known={}, client=client,
                               now=datetime(2026, 6, 20, 12, tzinfo=timezone.utc))
    assert result.refused == {span[2]: "download failed: BingX code 100410: retry later"}
    assert span[6] in result.accepted                                               # the next gap day asks again


def test_a_malformed_hourly_candle_never_crashes_the_run_and_never_passes_a_day(tmp_path: Path) -> None:
    span, client = _world(6, gappy={2})
    original = client.klines

    def broken(symbol, start_ms, end_ms, interval="1m"):
        answer = original(symbol, start_ms, end_ms, interval)
        if interval == "1h":
            answer = [*answer, {"time": 10 ** 400, "open": "1"}, {"time": None}, {"open": "1"}, {"time": "x"}]
        return answer

    client.klines = broken
    result = verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), span[0], span[-1], known={}, client=client,
                               now=datetime(2026, 6, 20, 12, tzinfo=timezone.utc), threads=3)
    assert result.accepted == span and result.refused == {}                          # the garbage belongs to no day


def test_a_day_without_hour_candles_in_a_populated_chunk_is_not_accepted_as_a_gap(tmp_path: Path) -> None:
    span, client = _world(6, gappy={2})
    del client.hours[span[2]]                                                       # BingX has no hours for the gap day
    result = verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), span[0], span[-1], known={}, client=client,
                               now=datetime(2026, 6, 20, 12, tzinfo=timezone.utc))
    assert list(result.refused) == [span[2]] and result.refused[span[2]].startswith("SUSPECT")
    assert span[3] in result.accepted                                                # the other days are not affected
