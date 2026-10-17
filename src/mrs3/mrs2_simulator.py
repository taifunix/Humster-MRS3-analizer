"""MRS2 LONG simulator on the tester's minute candles, reproducing the tester's fixed-lot report metrics.

Contract: docs/specs/2026-10-16-mrs2-period-csv-generator.md («Simulator»).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Sequence

import numpy as np

from .tester_candles import read_day

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
TIMEFRAME_MS = {"5m": 5 * MINUTE_MS, "15m": 15 * MINUTE_MS, "30m": 30 * MINUTE_MS, "45m": 45 * MINUTE_MS,
                "1h": HOUR_MS, "2h": 2 * HOUR_MS, "3h": 3 * HOUR_MS, "4h": 4 * HOUR_MS}


class SimulatorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RunSettings:
    """Settings of the tester runs the generated rows stand for (reference grid750 configs)."""

    lot: float = 10_000.0
    maker_fee: float = 0.00001
    taker_fee: float = 0.0002
    slippage: float = 0.001
    funding_rate: float = 0.00025
    funding_interval_hours: int = 8
    warmup_days: int = 4
    close_multiplier: float = 1.003
    distance_filter: float = 0.3
    limit_fill_plus_tick: bool = True       # tester `order_limit_fill_plus_tick` (standard of the new base, ADR-0077)


@dataclass(frozen=True, slots=True)
class MinuteBars:
    """Real minute candles (only minutes with trades) of one symbol, ascending by start time (ms UTC)."""

    timestamps: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray


@dataclass(frozen=True, slots=True)
class Cycle:
    """One trade: entry and exit time (ms) and price; exit None = still open at the period end."""

    open_ms: int
    entry_price: float
    entry_fee_rate: float
    exit_ms: int | None
    exit_price: float | None
    exit_fee_rate: float | None


@dataclass(slots=True)
class PeriodResult:
    final_balance: float
    trades: list[float] = field(default_factory=list)
    fees: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_percent: float = 0.0
    ulcer_index: float = 0.0
    position_percents: list[float] = field(default_factory=list)   # cost / margin balance: entry, exit, entry, ...
    monthly_returns: dict[str, float] = field(default_factory=dict)
    path: list[tuple[int, float]] = field(default_factory=list)
    marked_final: float = 0.0          # margin balance at the end before the forced market close


# -- data ---------------------------------------------------------------------


def load_minutes(symbol_dir: Path, symbol: str, start_ms: int, end_ms: int) -> MinuteBars:
    """Minute candles of [start_ms, end_ms) from the tester's day files (days without a file have no trades)."""
    first = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    last = datetime.fromtimestamp((end_ms - 1) / 1000, timezone.utc).date()
    parts = []
    day = first
    while day <= last:
        path = Path(symbol_dir) / f"{symbol}{day.isoformat()}_1m.parquet"
        if path.is_file():
            parts.append(read_day(path.read_bytes()))
        day += timedelta(days=1)
    if not parts:
        empty = np.zeros(0)
        return MinuteBars(np.zeros(0, dtype=np.int64), empty, empty, empty, empty)
    columns = {name: np.concatenate([part.column(name).to_numpy() for part in parts])
               for name in ("timestamp", "open", "high", "low", "close")}
    keep = (columns["timestamp"] >= start_ms) & (columns["timestamp"] < end_ms)
    return MinuteBars(columns["timestamp"][keep].astype(np.int64), columns["open"][keep].astype(np.float64),
                      columns["high"][keep].astype(np.float64), columns["low"][keep].astype(np.float64),
                      columns["close"][keep].astype(np.float64))


# -- bars and orders ----------------------------------------------------------------


def bar_ohlc4(minutes: MinuteBars, timeframe_ms: int) -> tuple[np.ndarray, np.ndarray]:
    """TF bars on the full minute grid: a minute without trades is a flat candle at the previous close.

    Returns (bar start ms, ohlc4) for every bar from the first minute's bar to the last minute's bar.
    """
    ts = minutes.timestamps
    first_bar = int(ts[0]) // timeframe_ms * timeframe_ms
    count = (int(ts[-1]) - first_bar) // timeframe_ms + 1
    per_bar = timeframe_ms // MINUTE_MS
    slot = (ts - first_bar) // MINUTE_MS
    size = count * per_bar
    close = np.full(size, np.nan)
    close[slot] = minutes.closes
    index = np.where(~np.isnan(close), np.arange(size), 0)
    np.maximum.accumulate(index, out=index)
    prev_close = close[index]
    prev_close[: slot[0]] = np.nan
    opens, highs, lows = prev_close.copy(), prev_close.copy(), prev_close.copy()
    opens[slot], highs[slot], lows[slot] = minutes.opens, minutes.highs, minutes.lows
    shape = (count, per_bar)
    bar_open = opens.reshape(shape)[:, 0]
    bar_high = np.nanmax(highs.reshape(shape), axis=1)
    bar_low = np.nanmin(lows.reshape(shape), axis=1)
    bar_close = prev_close.reshape(shape)[:, -1]
    bar_open = np.where(np.isnan(bar_open), bar_close, bar_open)
    starts = first_bar + np.arange(count, dtype=np.int64) * timeframe_ms
    return starts, (bar_open + bar_high + bar_low + bar_close) / 4


def order_prices(ohlc4: np.ndarray, length: int, multiplier: float, tick: float) -> np.ndarray:
    """Order price set at the start of each bar: SMA of the `length` closed bars before it × multiplier, to the tick."""
    sma = np.full(len(ohlc4), np.nan)
    csum = np.r_[0.0, np.cumsum(ohlc4)]
    idx = np.arange(length, len(ohlc4))
    sma[idx] = (csum[idx] - csum[idx - length]) / length
    return np.round(sma * multiplier / tick) * tick


def price_tick(minutes: MinuteBars) -> float:
    """Price tick inferred from the decimals of traded prices (fallback when the instrument is unknown)."""
    sample = np.concatenate([minutes.opens[:5000], minutes.closes[:5000], minutes.lows[:5000], minutes.highs[:5000]])
    for decimals in range(9):
        scaled = sample * 10 ** decimals
        if np.all(np.abs(scaled - np.round(scaled)) < 1e-6):
            return 10.0 ** -decimals
    return 1e-8


def simulate_mrs2(minutes: MinuteBars, start_ms: int, end_ms: int, *, timeframe: str, open_len: int, open_mult: float,
                  close_len: int, tick: float, settings: RunSettings = RunSettings(),
                  start_rule: str = "minute1", replace_delay_min: float = 1.0,
                  sell_rebar: str = "keep", buy_rebar: str = "place", fill: str | None = None,
                  ticks: bool = False, retry_check: str = "close",
                  buy_after_exit: str = "retry", sell_after_entry: str = "tick_retry",
                  strict_cross: bool = True, filter_close: bool = False,
                  exit_filter: bool = True) -> tuple[Cycle, ...]:
    """MRS2 LONG trades on [start_ms, end_ms) with the tester's order placement rules (spec «Simulator»)."""
    if not len(minutes.timestamps):
        return ()
    tf_ms = TIMEFRAME_MS[timeframe]
    starts, ohlc4 = bar_ohlc4(minutes, tf_ms)
    buy_bar = order_prices(ohlc4, open_len, open_mult, tick)
    sell_bar = order_prices(ohlc4, close_len, settings.close_multiplier, tick)
    with np.errstate(invalid="ignore"):
        placeable = ((sell_bar - buy_bar) / buy_bar * 100 >= settings.distance_filter).tolist()
    buys, sells = buy_bar.tolist(), sell_bar.tolist()
    ts = minutes.timestamps.tolist()
    lows, highs, closes = minutes.lows.tolist(), minutes.highs.tolist(), minutes.closes.tolist()
    opens = minutes.opens.tolist()
    first_bar = int(starts[0])
    half = tick / 2
    # crossing the market: strictly below it for a sell, strictly above it for a buy ("equal" rests)
    edge = half if strict_cross in (True, "1", "true") else -half
    filter_close = filter_close in (True, "1", "true")
    exit_filter = exit_filter in (True, "1", "true")

    def sell_at(price: float, market: float) -> float:     # a sell below the market rests one tick above it
        return price if price > market - edge else round((market + tick) / tick) * tick

    def buy_at(price: float, market: float) -> float:      # post-only buy above the market rests one tick below it
        return price if price < market + edge else round((market - tick) / tick) * tick

    def _replace(current: float | None, bar: int, holding: bool, at_market: float | None) -> float | None:
        """Order re-placed at a bar start; kept when the distance filter or a missing MA forbids placing."""
        if at_market is None or (not placeable[bar] and (not holding or filter_close)):
            return current            # distance_filter blocks entries only (MRS2 filter_close_position = false)
        price = sells[bar] if holding else buys[bar]
        if price != price:
            return current
        crossing = price <= at_market - edge if holding else price >= at_market + edge
        if crossing and not holding and buy_rebar == "place":
            retry[0] = None
            return price              # the bar-start buy is placed at its MA price even above the market
        post_only_wait = not holding and buy_rebar == "retry"
        if crossing and (post_only_wait or (current is not None and (sell_rebar if holding else buy_rebar) == "keep")):
            # a re-placement that would cross the market leaves the previous order; the new price is retried
            # after every candle and placed once the market trades below it (sell) / above it (buy)
            retry[0] = price
            return current
        retry[0] = None
        return sell_at(price, at_market) if holding else buy_at(price, at_market)

    retry: list[float | None] = [None]
    exit_retry: float | None = None
    trade_from = (start_ms // tf_ms + 1) * tf_ms if start_rule == "bar" else start_ms - 1
    if start_rule == "minute1":
        # first orders are placed after the period's second minute candle (start + 1 min) when it exists;
        # otherwise at the first bar start after the period start
        first_minute = start_ms + MINUTE_MS
        trade_from = first_minute if first_minute in set(ts[: min(len(ts), 20000)]) else (start_ms // tf_ms + 1) * tf_ms
    cycles: list[Cycle] = []
    in_position = False
    order: float | None = None
    current_bar: int | None = None
    market: float | None = None
    entry_ms = 0
    entry_price = 0.0
    pending_bar: int | None = None
    replace_delay_ms = int(float(replace_delay_min) * MINUTE_MS)
    # order_limit_fill_plus_tick: a limit fills only when the price trades at least one tick beyond it
    if fill is None:
        fill = "plus_tick" if settings.limit_fill_plus_tick else "cross"
    beyond = tick if fill == "plus_tick" else 0.0
    ticks = ticks in (True, "1", "true", "True")
    for i, t in enumerate(ts):
        if t <= trade_from:
            market = closes[i]
            continue
        if t >= end_ms:
            break
        bar = (t - first_bar) // tf_ms
        if bar != current_bar:
            if current_bar is not None:
                # a bar still pending, and bars without any trade, re-place the order by the clock
                start = current_bar if pending_bar == current_bar else current_bar + 1
                for skipped in range(start, bar):
                    order = _replace(order, skipped, in_position, market)
                pending_bar = bar      # candles before the activation moment still meet the previous order
            else:
                order = _replace(order, bar, in_position, market)
                pending_bar = None
            current_bar = bar
        if pending_bar is not None and t >= first_bar + pending_bar * tf_ms + replace_delay_ms:
            order = _replace(order, pending_bar, in_position, market)
            pending_bar = None
        filled = False
        if ticks and pending_bar is not None and order is not None:
            # 4 ticks per minute: the open tick of the bar's first candle meets the previous order, then the
            # order is re-placed against that tick and the rest of the candle (high, low, close) meets the new one
            o = opens[i]
            if not in_position and o <= order - beyond + 1e-9:
                in_position, entry_ms, entry_price, filled = True, t, order, True
                price = sells[bar]
                order = sell_at(price, closes[i]) if price == price else None
            elif in_position and o >= order + beyond - 1e-9:
                cycles.append(Cycle(entry_ms, entry_price, settings.maker_fee, t, order, settings.maker_fee))
                in_position, filled = False, True
                price = buys[bar]
                order = buy_at(price, closes[i]) if price == price else None
            else:
                order = _replace(order, pending_bar, in_position, o)
            pending_bar = None
        if order is not None and not filled:
            if not in_position and lows[i] <= order - beyond + 1e-9 and (fill != "touch" or highs[i] >= order - 1e-9):
                in_position, entry_ms, entry_price, filled = True, t, order, True
                price = sells[bar]
                if price != price:
                    order = None
                elif sell_after_entry == "retry" and price <= closes[i] - edge:
                    order, exit_retry = None, price    # retried after each candle until the market is below it
                elif sell_after_entry == "tick_retry" and price <= closes[i] - edge:
                    # a plain limit sell below the market rests one tick above it and is moved to the MA price
                    # once a candle closes below that price
                    order, exit_retry = sell_at(price, closes[i]), price
                else:
                    order = sell_at(price, closes[i])
            elif in_position and highs[i] >= order + beyond - 1e-9 and (fill != "touch" or lows[i] <= order + 1e-9):
                cycles.append(Cycle(entry_ms, entry_price, settings.maker_fee, t, order, settings.maker_fee))
                in_position, filled = False, True
                price = buys[bar]
                if price != price or (exit_filter and not placeable[bar]):
                    order = None                       # no entry order while the distance filter blocks it
                elif buy_after_exit == "retry" and price >= closes[i] + edge:
                    order, exit_retry = None, price    # post-only buy above the market is retried until it rests
                else:
                    order = buy_at(price, closes[i])
        if filled:
            pending_bar = None         # the opposite order was just placed from this bar's prices
            retry[0] = exit_retry
            exit_retry = None
        elif retry[0] is not None and pending_bar is None:
            waiting = retry[0]
            low_ref = lows[i] if retry_check == "low" else closes[i]
            high_ref = highs[i] if retry_check == "low" else closes[i]
            # placeable once it no longer crosses the market (same strictness as `crossing`)
            if (in_position and waiting > low_ref - edge) or (not in_position and waiting < high_ref + edge):
                order, retry[0] = waiting, None     # placed after this candle, can fill from the next one
        market = closes[i]
    if in_position:
        cycles.append(Cycle(entry_ms, entry_price, settings.maker_fee, None, None, None))
    return tuple(cycles)


# -- fixed-lot period metrics ---------------------------------------------------------


def _funding_times(after_ms: int, until_ms: int, interval_hours: int) -> range:
    step = interval_hours * HOUR_MS
    return range((after_ms // step + 1) * step, until_ms + 1, step)


def _last_close_before(stamps: np.ndarray, closes: np.ndarray, moment_ms: int) -> tuple[int, float] | None:
    index = int(np.searchsorted(stamps, moment_ms, "left")) - 1
    return None if index < 0 else (int(stamps[index]), float(closes[index]))


def simulate_period(cycles: Sequence[Cycle], minutes: MinuteBars, start_ms: int, end_ms: int, *, qty_step: float,
                    settings: RunSettings = RunSettings()) -> PeriodResult:
    """Fixed-lot balance path of the trades on [start_ms, end_ms): fees, funding, market close at the end."""
    stamps, closes = minutes.timestamps, minutes.closes
    lot = settings.lot
    balance = lot
    result = PeriodResult(final_balance=lot)
    path: list[tuple[int, float]] = [(start_ms, lot)]
    flat_from = start_ms
    for cycle in cycles:
        if cycle.open_ms < flat_from or cycle.open_ms >= end_ms:
            continue
        qty = math.floor(lot / cycle.entry_price / qty_step + 1e-9) * qty_step
        if qty <= 0:
            continue
        cost = qty * cycle.entry_price
        result.position_percents.append(cost / balance * 100)
        fee_in = cost * cycle.entry_fee_rate
        balance -= fee_in
        result.fees += fee_in
        closes_inside = cycle.exit_ms is not None and cycle.exit_ms < end_ms
        segment_end = cycle.exit_ms if closes_inside else end_ms
        path.append((cycle.open_ms, balance))
        funding = list(_funding_times(cycle.open_ms, segment_end - 1, settings.funding_interval_hours))
        lo = int(np.searchsorted(stamps, cycle.open_ms, "left"))
        hi = int(np.searchsorted(stamps, segment_end, "left"))
        next_funding = 0
        for index in range(lo, hi):
            stamp = int(stamps[index])
            while next_funding < len(funding) and funding[next_funding] <= stamp:
                mark = _last_close_before(stamps, closes, funding[next_funding])
                if mark is not None:
                    balance -= qty * mark[1] * settings.funding_rate
                next_funding += 1
            path.append((stamp, balance + qty * (float(closes[index]) - cycle.entry_price)))
        while next_funding < len(funding):
            mark = _last_close_before(stamps, closes, funding[next_funding])
            if mark is not None:
                balance -= qty * mark[1] * settings.funding_rate
            next_funding += 1
        if closes_inside:
            gross = qty * (cycle.exit_price - cycle.entry_price)
            fee_out = qty * cycle.exit_price * cycle.exit_fee_rate
            result.position_percents.append(qty * cycle.exit_price / (balance + gross) * 100)  # margin balance incl. PnL
            balance += gross - fee_out
            result.fees += fee_out
            result.trades.append(gross)
            path.append((cycle.exit_ms, balance))
            flat_from = cycle.exit_ms
            continue
        last = _last_close_before(stamps, closes, end_ms)
        if last is None:
            raise SimulatorError("no candle before the period end for the open position")
        result.marked_final = balance + qty * (last[1] - cycle.entry_price)
        price = last[1] * (1 - settings.slippage)
        gross = qty * (price - cycle.entry_price)
        fee_out = qty * price * settings.taker_fee
        result.position_percents.append(qty * price / (balance + gross) * 100)
        balance += gross - fee_out
        result.fees += fee_out
        result.trades.append(gross)
        path.append((last[0], balance))
        break
    result.final_balance = balance
    if not result.marked_final:
        result.marked_final = balance
    path.sort(key=lambda item: item[0])
    result.path = path
    _drawdowns(result, start_ms, end_ms)
    result.monthly_returns = monthly_returns(result, start_ms, end_ms, lot)
    return result


def month_starts(start_ms: int, end_ms: int) -> list[tuple[str, int]]:
    """(YYYY_MM, first ms of the month clipped to the period) for every month the period touches."""
    day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date().replace(day=1)
    last = datetime.fromtimestamp((end_ms - 1) / 1000, timezone.utc).date()
    out = []
    while day <= last:
        moment = int(datetime(day.year, day.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
        out.append((f"{day.year:04d}_{day.month:02d}", max(moment, start_ms)))
        day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def monthly_returns(result: PeriodResult, start_ms: int, end_ms: int, lot: float) -> dict[str, float]:
    """Month return = margin balance at the month end / at the previous month end − 1 (in %);
    the last month ends at the marked balance before the forced close."""
    stamps = [stamp for stamp, _ in result.path]
    values = [value for _, value in result.path]
    months = month_starts(start_ms, end_ms)
    out = {}
    previous = lot
    for index, (key, _) in enumerate(months):
        if index + 1 < len(months):
            boundary = months[index + 1][1]
            position = int(np.searchsorted(stamps, boundary, "left")) - 1
            value = values[position] if position >= 0 else lot
        else:
            value = result.marked_final
        out[key] = (value / previous - 1) * 100 if previous else 0.0
        previous = value
    return out


def _drawdowns(result: PeriodResult, start_ms: int, end_ms: int) -> None:
    values = np.array([value for _, value in result.path], dtype=np.float64)
    peaks = np.maximum.accumulate(values)
    result.max_drawdown = float((peaks - values).max(initial=0.0))
    result.max_drawdown_percent = float(((peaks - values) / peaks * 100).max(initial=0.0))
    stamps = np.array([stamp for stamp, _ in result.path], dtype=np.int64)
    # The tester's margin-balance curve ends at its last action: later hours are not in the index.
    last_hour_end = min(end_ms, (int(stamps[-1]) // HOUR_MS + 1) * HOUR_MS)
    hours = np.arange(start_ms + HOUR_MS, last_hour_end + 1, HOUR_MS, dtype=np.int64)
    if not len(hours):
        return
    hourly = values[np.clip(np.searchsorted(stamps, hours, "left") - 1, 0, None)]
    hourly_peaks = np.maximum.accumulate(hourly)
    result.ulcer_index = float(math.sqrt((((hourly_peaks - hourly) / hourly_peaks * 100) ** 2).mean()))
