from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path

import numpy as np
import pytest

from mrs3.mrs2_simulator import HOUR_MS, Cycle, MinuteBars, RunSettings, simulate_mrs2, simulate_period
from mrs3.period_csv import Point, grid, ms_of
from mrs3.tester_candles import encode_day
from mrs3.trade_history import pair_segments, run_history, simulate_segment
from mrs3.trade_store import (
    PRICE_POINT,
    SegmentKey,
    TradeStore,
    TradeStoreError,
    hourly_rows,
    point_id,
    point_params,
)

MIN = 60_000
POINTS = [Point("15m", 70, 2, 2), Point("15m", 140, 3, 2), Point("1h", 70, 2, 3), Point("4h", 550, 7, 7)]


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def walk(first_day: date, days: int, seed: int = 7, gaps: bool = True) -> MinuteBars:
    """Random-walk minute candles (tick 0.01) with some minutes missing, like a thin market."""
    rng = np.random.default_rng(seed)
    count = days * 1440
    stamps = ms_of(first_day) + np.arange(count, dtype=np.int64) * MIN
    close = np.round(100 * np.exp(np.cumsum(rng.normal(0, 0.0015, count))), 2)
    opens = np.r_[close[0], close[:-1]]
    highs = np.round(np.maximum(opens, close) + rng.uniform(0, 0.05, count), 2)
    lows = np.round(np.minimum(opens, close) - rng.uniform(0, 0.05, count), 2)
    keep = rng.uniform(size=count) > (0.3 if gaps else 0.0)
    return MinuteBars(stamps[keep], opens[keep], highs[keep], lows[keep], close[keep])


def test_point_id_is_stable_and_round_trips_for_the_whole_grid():
    assert point_id("1h", 230, 3, 5) == 60023035
    ids = [point_id(p.timeframe, p.shift_bp, p.open_len, p.close_len) for p in grid()]
    assert len(set(ids)) == len(ids) == 3780
    assert PRICE_POINT not in ids
    assert all(point_params(pid) == (p.timeframe, p.shift_bp, p.open_len, p.close_len) for pid, p in zip(ids, grid()))
    with pytest.raises(TradeStoreError):
        point_id("1h", 1000, 2, 2)


def test_period_ledger_holds_every_trade_with_its_amounts():
    data = flat_then_dip()
    trades = [Cycle(ms("2026-08-01T07:00:00"), 100.0, 1e-5, ms("2026-08-01T07:30:00"), 101.0, 1e-5),
              Cycle(ms("2026-08-01T07:40:00"), 100.0, 1e-5, None, None, None)]
    result = simulate_period(trades, data, ms("2026-08-01T06:00:00"), ms("2026-08-01T10:00:00"), qty_step=0.01)
    first, last = result.ledger
    assert (first.exit_kind, first.exit_price) == ("limit", 101.0) and first.qty == pytest.approx(100.0)
    assert first.fee_in == pytest.approx(100 * 100.0 * 1e-5) and first.funding == 0.0
    assert last.exit_kind == "market_end" and last.exit_ms == ms("2026-08-01T09:59:00")
    assert last.exit_price == pytest.approx(98.0 * (1 - 0.001))
    assert last.fee_out == pytest.approx(100 * 98.0 * (1 - 0.001) * 0.0002)
    assert last.funding == pytest.approx(100 * 100.0 * 0.00025)          # 08:00 UTC while in the position
    gross = [row.qty * (row.exit_price - row.entry_price) for row in result.ledger]
    assert gross == pytest.approx(result.trades)
    assert sum(row.fee_in + row.fee_out for row in result.ledger) == pytest.approx(result.fees)
    assert result.final_balance == pytest.approx(
        10_000 + sum(gross) - result.fees - sum(row.funding for row in result.ledger))


def flat_then_dip() -> MinuteBars:
    rows = [(i, 100.0 if i < 239 else 98.0) for i in range(4 * 60)]
    stamps = np.array([ms("2026-08-01T06:00:00") + i * MIN for i, _ in rows], dtype=np.int64)
    prices = np.array([price for _, price in rows])
    return MinuteBars(stamps, prices, prices, prices, prices)


def test_hourly_series_aggregate_the_simulator_path():
    data = walk(date(2026, 8, 1), 6)
    start, end = ms("2026-08-05"), ms("2026-08-07")
    cycles = simulate_mrs2(data, start, end, timeframe="15m", open_len=2, open_mult=0.993, close_len=2, tick=0.01)
    result = simulate_period(cycles, data, start, end, qty_step=0.01)
    assert len(result.ledger) > 5
    rows = hourly_rows(7, result, start, end)
    stamps = np.array([stamp for stamp, _ in result.path])
    values = np.array([value for _, value in result.path])
    hour_starts = np.arange(start, end, HOUR_MS)
    assert list(rows["hour_ms"]) == list(hour_starts)
    previous = 10_000.0
    for index, hour in enumerate(hour_starts):
        inside = values[(stamps >= hour) & (stamps < hour + HOUR_MS)]
        close = values[np.searchsorted(stamps, hour + HOUR_MS, "left") - 1]
        assert rows["close"][index] == np.float32(close)
        assert rows["low"][index] == np.float32(min(previous, *inside) if len(inside) else previous)
        assert rows["high"][index] == np.float32(max(previous, *inside) if len(inside) else previous)
        previous = close


def test_segment_metrics_equal_the_simulator_for_every_point():
    data = walk(date(2026, 8, 1), 8)
    start, end = date(2026, 8, 5), date(2026, 8, 9)
    tables = simulate_segment(data, start, end, tick=0.01, qty_step=0.01, points=POINTS)
    metrics = tables.metrics.to_pydict()
    trades = tables.trades.to_pydict()
    for index, point in enumerate(POINTS):
        pid = point_id(point.timeframe, point.shift_bp, point.open_len, point.close_len)
        cycles = simulate_mrs2(data, ms_of(start), ms_of(end), timeframe=point.timeframe, open_len=point.open_len,
                               open_mult=(10_000 - point.shift_bp) / 10_000, close_len=point.close_len, tick=0.01)
        result = simulate_period(cycles, data, ms_of(start), ms_of(end), qty_step=0.01)
        assert metrics["point_id"][index] == pid
        assert metrics["final_balance"][index] == result.final_balance
        assert metrics["ulcer_index"][index] == result.ulcer_index
        assert metrics["max_drawdown_percent"][index] == result.max_drawdown_percent
        assert metrics["trades"][index] == len(result.trades)
        assert metrics["last_ms"][index] == result.path[-1][0]
        mine = [i for i, value in enumerate(trades["point_id"]) if value == pid]
        assert [trades["entry_ms"][i] for i in mine] == [row.entry_ms for row in result.ledger]
        assert [trades["funding"][i] for i in mine] == [row.funding for row in result.ledger]
    price = tables.hourly.filter(tables.hourly.column("point_id").to_numpy() == PRICE_POINT).to_pydict()
    hour_end = ms_of(start) + 5 * HOUR_MS
    assert price["close"][4] == np.float32(data.closes[np.searchsorted(data.timestamps, hour_end, "left") - 1])


def _key(start: str, end: str) -> SegmentKey:
    return SegmentKey("bybit", "TESTUSDT", "simulator", date.fromisoformat(start), date.fromisoformat(end), "eng-1")


def test_segment_files_are_byte_identical_and_registration_supersedes_overlaps(tmp_path: Path):
    data = walk(date(2026, 8, 1), 8)
    tables = simulate_segment(data, date(2026, 8, 5), date(2026, 8, 9), tick=0.01, qty_step=0.01, points=POINTS)
    first, second = TradeStore(tmp_path / "a"), TradeStore(tmp_path / "b")
    record_a = first.write_files(_key("2026-08-05", "2026-08-09"), tables, settings=RunSettings(), tick_size=0.01,
                                 qty_step=0.01)
    record_b = second.write_files(_key("2026-08-05", "2026-08-09"), tables, settings=RunSettings(), tick_size=0.01,
                                  qty_step=0.01)
    assert record_a["trades_sha256"] == record_b["trades_sha256"]
    assert record_a["hourly_sha256"] == record_b["hourly_sha256"]
    assert record_a["metrics_sha256"] == record_b["metrics_sha256"]
    # a rerun after a crash finds the same bytes in place; different bytes are refused
    again = first.write_files(_key("2026-08-05", "2026-08-09"), tables, settings=RunSettings(), tick_size=0.01,
                              qty_step=0.01)
    assert again["trades_sha256"] == record_a["trades_sha256"]
    other = simulate_segment(data, date(2026, 8, 5), date(2026, 8, 9), tick=0.01, qty_step=0.01, points=POINTS[:1])
    with pytest.raises(TradeStoreError):
        first.write_files(_key("2026-08-05", "2026-08-09"), other, settings=RunSettings(), tick_size=0.01,
                          qty_step=0.01)

    first.register(record_a)
    segment = first.segments("TESTUSDT")[0]
    assert first.read_trades(segment).equals(tables.trades)
    assert first.read_hourly(segment).equals(tables.hourly)
    assert first.read_metrics(segment).equals(tables.metrics)
    with pytest.raises(TradeStoreError):
        first.register(record_a)

    later = first.write_files(_key("2026-08-07", "2026-08-10"), other, settings=RunSettings(), tick_size=0.01,
                              qty_step=0.01)
    apart = first.write_files(_key("2026-08-10", "2026-08-12"), other, settings=RunSettings(), tick_size=0.01,
                              qty_step=0.01)
    first.register(later)
    first.register(apart)
    active = [row["segment_id"] for row in first.segments("TESTUSDT")]
    assert active == [later["segment_id"], apart["segment_id"]]
    with first.connect() as con:
        assert con.execute("select superseded_by from segments where segment_id = ?",
                           [record_a["segment_id"]]).fetchone()[0] == later["segment_id"]

    (tmp_path / "a" / segment["trades_path"]).write_bytes(b"broken")
    with pytest.raises(TradeStoreError):
        first.read_trades(segment)


def test_pair_segments_cut_history_on_the_p2_grid_and_keep_the_latest_window():
    plan = pair_segments(date(2026, 6, 1), date(2026, 8, 30))
    assert plan == [(date(2026, 6, 5), date(2026, 6, 25)), (date(2026, 6, 25), date(2026, 7, 15)),
                    (date(2026, 7, 15), date(2026, 8, 10)), (date(2026, 8, 10), date(2026, 8, 30))]
    assert pair_segments(date(2026, 7, 20), date(2026, 8, 30)) == [(date(2026, 8, 10), date(2026, 8, 30))]
    assert pair_segments(date(2026, 8, 8), date(2026, 8, 30)) == []


def _write_days(root: Path, symbol: str, data: MinuteBars) -> None:
    folder = root / symbol
    folder.mkdir(parents=True)
    days = (data.timestamps // 86_400_000).astype(np.int64)
    for day in np.unique(days):
        keep = days == day
        stamp = date(1970, 1, 1) + timedelta(days=int(day))
        columns = {"timestamp": data.timestamps[keep], "open": data.opens[keep], "high": data.highs[keep],
                   "low": data.lows[keep], "close": data.closes[keep]}
        size = int(keep.sum())
        columns.update(volume=np.ones(size), buy_volume=np.ones(size) / 2, sell_volume=np.ones(size) / 2,
                       trades=np.ones(size, dtype=np.int32))
        (folder / f"{symbol}{stamp.isoformat()}_1m.parquet").write_bytes(encode_day(columns))


def test_history_run_writes_the_planned_segments_once(tmp_path: Path):
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    store = TradeStore(tmp_path / "store")
    kwargs = dict(data_root=data_root, pairs=[("TESTUSDT", date(2026, 6, 1))], end_day=date(2026, 7, 20),
                  instruments={"TESTUSDT": (0.01, 0.01)}, points=POINTS, workers=1, download=False)
    summary = run_history(store, **kwargs)
    assert summary["written"] == 2 and summary["failed"] == 0
    segments = store.segments("TESTUSDT")
    assert [(row["start_ms"], row["end_ms"]) for row in segments] == [
        (ms("2026-06-05"), ms("2026-06-30")), (ms("2026-06-30"), ms("2026-07-20"))]
    assert run_history(store, **kwargs)["written"] == 0
    assert [row["trades_sha256"] for row in store.segments("TESTUSDT")] == [row["trades_sha256"] for row in segments]
    with store.connect() as con:
        assert con.execute("select count(*) from runs").fetchone()[0] == 2
        assert con.execute("select count(*) from points").fetchone()[0] == len(POINTS)


def test_history_run_never_stores_a_segment_with_missing_candle_days(tmp_path: Path):
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 50, gaps=False))
    (data_root / "TESTUSDT" / "TESTUSDT2026-07-10_1m.parquet").unlink()      # e.g. a failed download
    store = TradeStore(tmp_path / "store")
    summary = run_history(store, data_root=data_root, pairs=[("TESTUSDT", date(2026, 6, 1))], end_day=date(2026, 7, 20),
                          instruments={"TESTUSDT": (0.01, 0.01)}, points=POINTS, workers=1, download=False)
    assert (summary["written"], summary["skipped"], summary["failed"]) == (1, 1, 0)
    assert [row["end_ms"] for row in store.segments("TESTUSDT")] == [ms("2026-06-30")]
    journal = (tmp_path / "store" / "journal.jsonl").read_text(encoding="utf-8")
    assert '"MISSING_DAYS"' in journal and "2026-07-10" in journal


def test_history_run_reactivates_a_superseded_plan_and_ignores_duplicate_pairs(tmp_path: Path):
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 51, gaps=False))
    store = TradeStore(tmp_path / "store")
    kwargs = dict(data_root=data_root, pairs=[("TESTUSDT", date(2026, 6, 1))] * 2,
                  instruments={"TESTUSDT": (0.01, 0.01)}, points=POINTS, workers=1, download=False)
    first = run_history(store, end_day=date(2026, 7, 20), **kwargs)
    second = run_history(store, end_day=date(2026, 7, 21), **kwargs)
    third = run_history(store, end_day=date(2026, 7, 20), **kwargs)
    assert (first["written"], first["failed"]) == (2, 0)
    assert (second["written"], second["failed"]) == (2, 0)
    assert (third["written"], third["reactivated"], third["failed"]) == (0, 2, 0)
    assert [(row["start_ms"], row["end_ms"]) for row in store.segments("TESTUSDT")] == [
        (ms("2026-06-05"), ms("2026-06-30")), (ms("2026-06-30"), ms("2026-07-20"))]
    with store.connect() as con:
        assert con.execute("select count(*) from runs where finished_utc is null").fetchone()[0] == 0


def test_history_run_resimulates_a_superseded_segment_with_damaged_files(tmp_path: Path):
    data_root = tmp_path / "data"
    _write_days(data_root, "TESTUSDT", walk(date(2026, 6, 1), 51, gaps=False))
    _write_days(data_root, "OTHERUSDT", walk(date(2026, 6, 1), 51, seed=11, gaps=False))
    store = TradeStore(tmp_path / "store")
    kwargs = dict(data_root=data_root, pairs=[("TESTUSDT", date(2026, 6, 1)), ("OTHERUSDT", date(2026, 6, 1))],
                  instruments={"TESTUSDT": (0.01, 0.01), "OTHERUSDT": (0.01, 0.01)}, points=POINTS, workers=1,
                  download=False)
    run_history(store, end_day=date(2026, 7, 20), **kwargs)
    original = {row["segment_id"]: row["trades_sha256"] for row in store.segments("TESTUSDT")}
    run_history(store, end_day=date(2026, 7, 21), **kwargs)
    damaged = store.segments("TESTUSDT", include_superseded=True)[0]
    assert damaged["status"] == "superseded"
    (store.root / damaged["trades_path"]).write_bytes(b"damaged")
    third = run_history(store, end_day=date(2026, 7, 20), **kwargs)
    assert (third["written"], third["reactivated"], third["failed"]) == (1, 3, 0)
    for symbol in ("TESTUSDT", "OTHERUSDT"):
        assert [row["end_ms"] for row in store.segments(symbol)] == [ms("2026-06-30"), ms("2026-07-20")]
    assert {row["segment_id"]: row["trades_sha256"] for row in store.segments("TESTUSDT")} == original
    assert store.read_trades(store.segments("TESTUSDT")[0]).num_rows > 0
    assert "REACTIVATE_FAILED" in (store.root / "journal.jsonl").read_text(encoding="utf-8")
