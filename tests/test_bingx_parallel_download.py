"""Parallel BingX minute download: the same days, records and files as one by one, under one request rate.

Contract: docs/specs/2026-10-20-panel-sync-by-exchange.md, «Part 2 contract». Fakes only: nothing leaves the machine.
"""

from __future__ import annotations

from datetime import date
import threading
import time
from pathlib import Path

from mrs3.bingx_downloader import BingXClient
from mrs3.pair_registry.liquidity import ensure_window
from mrs3.pair_registry.minute_days import day_path, verify_bingx_days
from mrs3.pair_registry.refresh import refresh_exchange
from tests.test_minute_days import API, LISTED, NOW, FakeBingX, _full_day, _hours, _minute, _ms, _world

DAYS = [date(2026, 10, day) for day in range(9, 19)]


class SlowBingX(FakeBingX):
    """FakeBingX that holds each request a moment and counts the requests in flight at once."""

    def __init__(self, *args, symbols: tuple[str, ...] = (API,), **kwargs):
        super().__init__(*args, **kwargs)
        self.lock, self.active, self.peak, self.symbols = threading.Lock(), 0, 0, set(symbols)

    def klines(self, symbol, start_ms, end_ms, interval="1m"):
        assert symbol in self.symbols
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.02)
            return super().klines(API, start_ms, end_ms, interval)
        finally:
            with self.lock:
                self.active -= 1


def _gappy(day: date) -> list[dict]:
    return [_minute(day, index) for index in range(1440) if not 600 <= index < 660]


def _client(**kwargs) -> SlowBingX:
    minutes = {day: _full_day(day) for day in DAYS}
    minutes[DAYS[3]] = _gappy(DAYS[3])                       # a gap explained by the hour candles: VERIFIED_GAPS
    return SlowBingX(minutes, {DAYS[3]: _hours(minutes[DAYS[3]], DAYS[3])}, **kwargs)


def _run(root: Path, threads: int):
    client = _client()
    result = verify_bingx_days(root, "AMDUSDT", API, _ms(LISTED), DAYS[0], DAYS[-1], known={}, client=client,
                               now=NOW, threads=threads)
    return result, client


def test_parallel_days_give_the_same_result_as_one_by_one(tmp_path: Path) -> None:
    serial, serial_client = _run(tmp_path / "serial", 1)
    parallel, parallel_client = _run(tmp_path / "parallel", 4)
    assert serial_client.peak == 1 and parallel_client.peak > 1
    assert parallel.accepted == serial.accepted == DAYS and parallel.refused == serial.refused == {}
    strip = lambda records: [{key: value for key, value in record.items() if key != "verified_ms"} for record in records]
    assert strip(parallel.records) == strip(serial.records)                      # in day order
    assert [record["status"] for record in parallel.records].count("VERIFIED_GAPS") == 1
    for day in DAYS:
        assert day_path(tmp_path / "parallel", "AMDUSDT", API, day).read_bytes() == \
            day_path(tmp_path / "serial", "AMDUSDT", API, day).read_bytes()


def test_one_failing_day_never_stops_the_other_days(tmp_path: Path) -> None:
    client = _client()
    original = client.klines

    def failing(symbol, start_ms, end_ms, interval="1m"):
        from mrs3.bingx_downloader import BingXDownloadError

        if start_ms == _ms_day(DAYS[5]):
            raise BingXDownloadError("BingX code 100410: rate limited")
        return original(symbol, start_ms, end_ms, interval)

    client.klines = failing
    result = verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), DAYS[0], DAYS[-1], known={}, client=client,
                               now=NOW, threads=4)
    assert result.refused == {DAYS[5]: "download failed: BingX code 100410: rate limited"}
    assert result.accepted == [day for day in DAYS if day != DAYS[5]]


def _ms_day(day: date) -> int:
    from datetime import datetime, timezone

    return _ms(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))


def test_the_window_downloads_pairs_in_parallel_and_writes_the_records_once(tmp_path: Path) -> None:
    # every pair was listed on 10-18, so each has one window day: only pairs in parallel overlap the requests
    from datetime import datetime, timezone

    con, settings = _world(tmp_path)
    listed = _ms(datetime(2026, 10, 18, tzinfo=timezone.utc))
    others = [f"P{index}USDT" for index in range(5)]
    refresh_exchange(con, "bingx", [
        {"symbol": "AMDUSDT", "status": "TRADING", "api_symbol": API, "listing_ms": listed},
        *({"symbol": symbol, "status": "TRADING", "api_symbol": f"NCSK{symbol[:-4]}2USD-USDT",
           "listing_ms": listed} for symbol in others)], [], now_ms=_ms(NOW))
    settings = type(settings)(root=settings.root, bingx_data_root=settings.bingx_data_root,
                              liquidity_window_weekdays=3, workers=4)
    client = _client(symbols=(API, *(f"NCSK{symbol[:-4]}2USD-USDT" for symbol in others)))
    counts = ensure_window(con, settings, "bingx", date(2026, 10, 19), date(2026, 10, 19), client=client, now=NOW)
    assert client.peak > 1 and len(client.calls) == 6
    assert counts == {"pairs": 6, "accepted": 6, "refused": 0, "failed_pairs": 0, "failed": {}}
    assert con.execute("select count(*), count(distinct symbol) from minute_days").fetchone() == (6, 6)
    again = ensure_window(con, settings, "bingx", date(2026, 10, 19), date(2026, 10, 19), client=client, now=NOW)
    assert again["accepted"] == 6 and len(client.calls) == 6                      # accepted days: no request


def test_one_failing_pair_never_stops_the_others_in_parallel(tmp_path: Path, monkeypatch) -> None:
    import mrs3.pair_registry.minute_days as minute_days

    con, settings = _world(tmp_path)
    refresh_exchange(con, "bingx", [
        {"symbol": "AMDUSDT", "status": "TRADING", "api_symbol": API, "listing_ms": _ms(LISTED)},
        {"symbol": "BADUSDT", "status": "TRADING", "api_symbol": "NCSKBAD2USD-USDT", "listing_ms": _ms(LISTED)}],
        [], now_ms=_ms(NOW))
    original = minute_days.verify_bingx_days

    def flaky(data_root, symbol, *args, **kwargs):
        if symbol == "BADUSDT":
            raise OSError("disk")
        return original(data_root, symbol, *args, **kwargs)

    monkeypatch.setattr(minute_days, "verify_bingx_days", flaky)
    settings = type(settings)(root=settings.root, bingx_data_root=settings.bingx_data_root,
                              liquidity_window_weekdays=3, workers=4)
    counts = ensure_window(con, settings, "bingx", date(2026, 10, 19), date(2026, 10, 19), client=_client(), now=NOW)
    assert counts["failed"] == {"BADUSDT": "OSError"} and counts["accepted"] == 5


def test_the_client_spaces_requests_of_all_threads() -> None:
    starts: list[float] = []
    lock = threading.Lock()

    def fetch(url, headers):
        with lock:
            starts.append(time.monotonic())
        return {"code": 0, "data": []}

    client = BingXClient(fetch_json=fetch, request_delay_seconds=0.05)
    threads = [threading.Thread(target=lambda: [client.klines(API, 0, 1) for _ in range(3)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    starts.sort()
    assert len(starts) == 12
    assert starts[-1] - starts[0] >= 11 * 0.05 * 0.9                                        # one shared rate


def test_the_client_reserves_slots_in_call_order() -> None:
    sleeps: list[float] = []
    client = BingXClient(fetch_json=lambda url, headers: {"code": 0, "data": []}, sleep=sleeps.append,
                         request_delay_seconds=0.5, clock=lambda: 100.0)              # the clock stands still
    for _ in range(4):
        client.klines(API, 0, 1)
    assert sleeps == [0.5, 1.0, 1.5]                         # the first request at once, then one slot each


def test_a_client_made_for_a_share_of_the_rate_is_slower(tmp_path: Path, monkeypatch) -> None:
    import mrs3.pair_registry.minute_days as minute_days

    made: list[dict] = []
    monkeypatch.setattr(minute_days, "BingXClient", lambda **options: made.append(options) or _client())
    verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), DAYS[0], DAYS[0], known={}, now=NOW, rate_share=2)
    verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), DAYS[0], DAYS[0], known={}, now=NOW)
    assert made == [{"request_delay_seconds": 1.0}, {}]
    import pytest

    with pytest.raises(ValueError):
        verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), DAYS[0], DAYS[0], known={}, now=NOW, rate_share=0)


def test_the_history_prepares_share_the_rate_only_when_two_run_at_once(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    import mrs3.trade_history as trade_history

    class Store:
        root = Path(".")

        def register_points(self, pids) -> None: ...
        def start_run(self, *args, **kwargs) -> None: ...
        def finish_run(self, *args, **kwargs) -> None: ...
        def statuses(self, exchange, symbol) -> dict: return {}

    seen: list[int] = []
    monkeypatch.setattr(trade_history, "ProcessPoolExecutor", ThreadPoolExecutor)      # the patches reach the worker
    monkeypatch.setattr(trade_history, "journal_append", lambda path, record: None)
    monkeypatch.setattr(trade_history, "prepare_pair", lambda *args: seen.append(args[-1]) or ([], {}, None, []))
    for workers, share in ((1, 1), (4, 2)):
        seen.clear()
        trade_history.run_history(Store(), data_root=Path("."), pairs=[("AMDUSDT", LISTED.date())], end_day=DAYS[-1],
                                  instruments={"AMDUSDT": (0.01, 0.01)}, points=[], workers=workers, exchange="bingx",
                                  bingx=trade_history.BingXMinutes(".", {"AMDUSDT": (API, 0)}))
        assert seen == [share]                     # one worker: one prepare at a time, the full rate


def test_the_old_day_by_day_order_stays_for_one_thread(tmp_path: Path) -> None:
    client = _client()
    verify_bingx_days(tmp_path, "AMDUSDT", API, _ms(LISTED), DAYS[0], DAYS[2], known={}, client=client, now=NOW)
    assert client.calls == [("1m", day) for day in DAYS[:3]]
