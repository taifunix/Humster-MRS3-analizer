"""Period CSVs in the tester ``reports_history`` format, generated from Source v6 MRS2 histories.

Contract: docs/specs/2026-10-16-source-v6-period-csv-generator.md. A Source v6
fragment gives the trade cycles of one MRS2 point (entry/exit times and
prices); a fresh fixed-lot tester run on one period is replayed from those
cycles and the tester's own minute candles (equity, funding, end-of-period
market close).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from .tester_candles import read_day

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

SHIFTS_BP = (70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550)
TIMEFRAMES = ("15m", "30m", "45m", "1h", "2h", "3h", "4h")
MA_LENGTHS = (2, 3, 4, 5, 6, 7)


class PeriodCsvError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RunSettings:
    """The tester settings of the runs the generated CSVs stand for (reference grid750 configs)."""

    lot: float = 10_000.0
    maker_fee: float = 0.00001
    taker_fee: float = 0.0002
    slippage: float = 0.001
    funding_rate: float = 0.00025
    funding_interval_hours: int = 8
    warmup_days: int = 4


@dataclass(frozen=True, slots=True)
class Cycle:
    """One Source v6 trade cycle: entry = first fill of the open order, exit = first reducing fill."""

    open_ms: int
    entry_price: float
    entry_fee_rate: float
    exit_ms: int | None
    exit_price: float | None
    exit_fee_rate: float | None


@dataclass(frozen=True, slots=True)
class Candles:
    """Minute candles of one symbol: start of minute (ms UTC) and close price, ascending."""

    timestamps: np.ndarray
    closes: np.ndarray

    def last_close_before(self, moment_ms: int) -> tuple[int, float] | None:
        index = int(np.searchsorted(self.timestamps, moment_ms, "left")) - 1
        if index < 0:
            return None
        return int(self.timestamps[index]), float(self.closes[index])


@dataclass(slots=True)
class PeriodResult:
    final_balance: float
    trades: list[float] = field(default_factory=list)
    fees: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_percent: float = 0.0
    ulcer_index: float = 0.0
    position_percents: list[float] = field(default_factory=list)
    monthly_returns: dict[str, float] = field(default_factory=dict)
    path: list[tuple[int, float]] = field(default_factory=list)
    wallet_path: list[tuple[int, float]] = field(default_factory=list)


def cycles_from_fragment(fragment, maker_fee: float = 0.00001) -> tuple[Cycle, ...]:
    """Entry price from the maker fee of the first fill; exit price from the PnL of the first reducing fill."""
    actions = {action.action_id: action for action in fragment.actions}
    cycles = []
    for cycle in sorted(fragment.cycles, key=lambda item: (item.open_timestamp_ms, item.cycle_id)):
        ordered = sorted((actions[action_id] for action_id in cycle.action_ids), key=lambda a: (a.timestamp_ms, a.action_id))
        first = ordered[0]
        size = float(first.size or 0)
        if size <= 0 or float(first.fee) <= 0:
            raise PeriodCsvError(f"cycle {cycle.cycle_id}: entry fill without size or fee")
        entry_price = float(first.fee) / (size * maker_fee)
        exit_ms = exit_price = exit_rate = None
        reducing = [action for action in ordered if action.action in ("decreased", "closed")]
        if cycle.closed and reducing:
            first_exit = reducing[0]
            exit_size = float(first_exit.size)
            exit_price = entry_price + float(first_exit.pnl) / exit_size
            exit_rate = float(first_exit.fee) / (exit_size * exit_price)
            exit_ms = first_exit.timestamp_ms
        cycles.append(Cycle(first.timestamp_ms, entry_price, maker_fee, exit_ms, exit_price, exit_rate))
    return tuple(cycles)


def qty_step_from_fragments(fragments: Iterable) -> float:
    """The finest decimal step among the source fill sizes (Bybit quantity step)."""
    decimals = 0
    for fragment in fragments:
        for action in fragment.actions:
            if action.size is not None:
                exponent = action.size.normalize().as_tuple().exponent
                decimals = max(decimals, -exponent if exponent < 0 else 0)
    return 10.0 ** -decimals


def load_candles(symbol_dir: Path, symbol: str, start_ms: int, end_ms: int) -> tuple[Candles, list[date]]:
    """Minute candles of [start_ms, end_ms) from the tester's day files; also the days without a file."""
    first = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
    last = datetime.fromtimestamp((end_ms - 1) / 1000, timezone.utc).date()
    stamps, closes, missing = [], [], []
    day = first
    while day <= last:
        path = Path(symbol_dir) / f"{symbol}{day.isoformat()}_1m.parquet"
        if path.is_file():
            table = read_day(path.read_bytes())
            stamps.append(table.column("timestamp").to_numpy())
            closes.append(table.column("close").to_numpy())
        else:
            missing.append(day)
        day += timedelta(days=1)
    if not stamps:
        return Candles(np.zeros(0, dtype=np.int64), np.zeros(0)), missing
    ts = np.concatenate(stamps).astype(np.int64)
    cl = np.concatenate(closes).astype(np.float64)
    keep = (ts >= start_ms) & (ts < end_ms)
    return Candles(ts[keep], cl[keep]), missing


def _funding_times(after_ms: int, until_ms: int, interval_hours: int) -> range:
    step = interval_hours * HOUR_MS
    first = (after_ms // step + 1) * step
    return range(first, until_ms + 1, step)


def simulate_period(cycles: Sequence[Cycle], candles: Candles, start_ms: int, end_ms: int, *,
                    qty_step: float, settings: RunSettings = RunSettings()) -> PeriodResult:
    """Replay a fresh fixed-lot tester run on [start_ms, end_ms): flat at the start, closed at market at the end."""
    lot = settings.lot
    balance = lot
    result = PeriodResult(final_balance=lot)
    path: list[tuple[int, float]] = [(start_ms, lot)]
    wallet: list[tuple[int, float]] = [(start_ms, lot)]
    flat_from = start_ms
    for cycle in cycles:
        if cycle.open_ms < flat_from or cycle.open_ms >= end_ms:
            continue  # before the period (start without a position) or overlapping the previous trade
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
        path.append((cycle.open_ms, balance + qty * 0.0))
        funding = list(_funding_times(cycle.open_ms, segment_end - 1, settings.funding_interval_hours))
        lo = int(np.searchsorted(candles.timestamps, cycle.open_ms - cycle.open_ms % MINUTE_MS, "left"))
        hi = int(np.searchsorted(candles.timestamps, segment_end, "left"))
        next_funding = 0
        for index in range(lo, hi):
            stamp = int(candles.timestamps[index])
            while next_funding < len(funding) and funding[next_funding] <= stamp:
                mark = candles.last_close_before(funding[next_funding])
                if mark is not None:
                    balance -= qty * mark[1] * settings.funding_rate
                next_funding += 1
            path.append((stamp, balance + qty * (float(candles.closes[index]) - cycle.entry_price)))
            wallet.append((stamp, balance))
        while next_funding < len(funding):
            mark = candles.last_close_before(funding[next_funding])
            if mark is not None:
                balance -= qty * mark[1] * settings.funding_rate
            next_funding += 1
        if closes_inside:
            gross = qty * (cycle.exit_price - cycle.entry_price)
            fee_out = qty * cycle.exit_price * cycle.exit_fee_rate
            result.position_percents.append(qty * cycle.exit_price / balance * 100)
            balance += gross - fee_out
            result.fees += fee_out
            result.trades.append(gross)
            path.append((cycle.exit_ms, balance))
            wallet.append((cycle.exit_ms, balance))
            flat_from = cycle.exit_ms
            continue
        last = candles.last_close_before(end_ms)
        if last is None:
            raise PeriodCsvError("no candle before the period end for the open position")
        price = last[1] * (1 - settings.slippage)
        gross = qty * (price - cycle.entry_price)
        fee_out = qty * price * settings.taker_fee
        result.position_percents.append(qty * price / balance * 100)
        balance += gross - fee_out
        result.fees += fee_out
        result.trades.append(gross)
        path.append((last[0], balance))
        wallet.append((last[0], balance))
        break
    result.final_balance = balance
    path.sort(key=lambda item: item[0])
    result.path = path
    result.wallet_path = sorted(wallet, key=lambda item: item[0])
    _drawdowns(result, start_ms, end_ms)
    return result


def _drawdowns(result: PeriodResult, start_ms: int, end_ms: int) -> None:
    values = np.array([value for _, value in result.path], dtype=np.float64)
    peaks = np.maximum.accumulate(values)
    result.max_drawdown = float((peaks - values).max(initial=0.0))
    result.max_drawdown_percent = float(((peaks - values) / peaks * 100).max(initial=0.0))
    stamps = np.array([stamp for stamp, _ in result.path], dtype=np.int64)
    # The tester's margin-balance curve ends at its last action: hours after it are not in the index.
    last_hour_end = min(end_ms, (int(stamps[-1]) // HOUR_MS + 1) * HOUR_MS)
    hours = np.arange(start_ms + HOUR_MS, last_hour_end + 1, HOUR_MS, dtype=np.int64)
    if not len(hours):
        return
    index = np.searchsorted(stamps, hours, "left") - 1
    hourly = values[np.clip(index, 0, None)]
    hourly_peaks = np.maximum.accumulate(hourly)
    result.ulcer_index = float(math.sqrt((((hourly_peaks - hourly) / hourly_peaks * 100) ** 2).mean()))


# -- periods ----------------------------------------------------------------


def split_periods(first_day: date, end_day: date, period_days: int = 20) -> list[tuple[date, date]]:
    """[first_day, end_day) cut into the most full periods; the remainder joins the last one (P2)."""
    total = (end_day - first_day).days
    count = total // period_days
    if count < 1:
        return []
    periods = []
    for index in range(count):
        start = first_day + timedelta(days=index * period_days)
        stop = end_day if index == count - 1 else start + timedelta(days=period_days)
        periods.append((start, stop))
    return periods


def first_period_day(listing: date, warmup_days: int = 4, from_day: date | None = None) -> date:
    """Listing + warm-up (the tester's warm-up must lie on real candles), or a later requested start."""
    first = listing + timedelta(days=warmup_days)
    return max(first, from_day) if from_day is not None else first


def last_covered_end(report_end_ms: int) -> date:
    """`EndDate` = start of the day after the last full UTC day covered by the source (a partial day is dropped)."""
    return datetime.fromtimestamp(report_end_ms // DAY_MS * DAY_MS / 1000, timezone.utc).date()


# -- CSV rows -----------------------------------------------------------------


BASE_COLUMNS = (
    "ReportUtc", "NameComment", "StartDate", "EndDate", "PairsCount", "InitialBalance", "FinalBalance", "TotalPnL",
    "TotalPnLPercent", "TotalTrades", "Win", "Los", "WinRate", "Fills", "MaxDrawdown", "MaxDrawdownPercent",
    "TotalFees", "PositionAvgPercent", "PositionMaxPercent", "ProfitFactor", "UlcerIndex", "Run id", "RunTag",
)
SETTING_COLUMNS = (
    "settings[*].basic.symbol", "settings[*].basic.time_frame", "settings[*].mrs2.ma_close_long.len",
    "settings[*].mrs2.ma_long.len", "settings[*].mrs2.ma_long.multiplier",
)


def month_keys(start_ms: int, end_ms: int) -> list[str]:
    day = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date().replace(day=1)
    last = datetime.fromtimestamp((end_ms - 1) / 1000, timezone.utc).date()
    keys = []
    while day <= last:
        keys.append(f"{day.year:04d}_{day.month:02d}")
        day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


def header(start_ms: int, end_ms: int) -> list[str]:
    return [*BASE_COLUMNS, *(f"MonthlyReturn_{key}" for key in month_keys(start_ms, end_ms)), *SETTING_COLUMNS]


def multiplier_text(shift_bp: int) -> str:
    text = f"{(10_000 - shift_bp) / 10_000:.4f}".rstrip("0")
    return text.rstrip(".")


def _money(value: float) -> str:
    return f"{value:.2f}"


def csv_row(result: PeriodResult, *, start_ms: int, end_ms: int, name: str, report_utc: str, run_id: int, run_total: int,
            symbol: str, timeframe: str, shift_bp: int, open_len: int, close_len: int,
            settings: RunSettings = RunSettings()) -> dict[str, str]:
    lot = settings.lot
    trades = result.trades
    wins = sum(1 for gross in trades if gross > 0)
    gross_profit = sum(gross for gross in trades if gross > 0)
    gross_loss = -sum(gross for gross in trades if gross < 0)
    pnl = result.final_balance - lot
    stamp = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    row = {
        "ReportUtc": report_utc, "NameComment": name, "StartDate": stamp(start_ms), "EndDate": stamp(end_ms),
        "PairsCount": "1", "InitialBalance": _money(lot), "FinalBalance": _money(result.final_balance),
        "TotalPnL": _money(pnl), "TotalPnLPercent": _money(pnl / lot * 100), "TotalTrades": str(len(trades)),
        "Win": str(wins), "Los": str(len(trades) - wins),
        "WinRate": _money(wins / len(trades) * 100 if trades else 0.0), "Fills": str(len(trades)),
        "MaxDrawdown": _money(result.max_drawdown), "MaxDrawdownPercent": _money(result.max_drawdown_percent),
        "TotalFees": _money(result.fees),
        "PositionAvgPercent": _money(sum(result.position_percents) / len(result.position_percents) if result.position_percents else 0.0),
        "PositionMaxPercent": _money(max(result.position_percents, default=0.0)),
        "ProfitFactor": f"{gross_profit / gross_loss:.2f}" if gross_loss else "",
        "UlcerIndex": f"{result.ulcer_index:.4f}" if trades else "",
        "Run id": str(run_id), "RunTag": f"run_{run_id}_of_{run_total}",
    }
    for key in month_keys(start_ms, end_ms):
        row[f"MonthlyReturn_{key}"] = _money(result.monthly_returns.get(key, 0.0))
    row.update({
        "settings[*].basic.symbol": symbol, "settings[*].basic.time_frame": timeframe,
        "settings[*].mrs2.ma_close_long.len": str(close_len), "settings[*].mrs2.ma_long.len": str(open_len),
        "settings[*].mrs2.ma_long.multiplier": multiplier_text(shift_bp),
    })
    return row


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> None:
    """The tester's reports_history.csv: no BOM, LF, comma-separated, no quoting (values never contain commas)."""
    lines = [",".join(columns)]
    for row in rows:
        values = [row[column] for column in columns]
        if any("," in value or "\n" in value for value in values):
            raise PeriodCsvError("a CSV value contains a separator")
        lines.append(",".join(values))
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


# -- MRS2 simulator -------------------------------------------------------------

TIMEFRAME_MS = {"5m": 5 * MINUTE_MS, "15m": 15 * MINUTE_MS, "30m": 30 * MINUTE_MS, "45m": 45 * MINUTE_MS,
                "1h": HOUR_MS, "2h": 2 * HOUR_MS, "3h": 3 * HOUR_MS, "4h": 4 * HOUR_MS}


@dataclass(frozen=True, slots=True)
class MinuteBars:
    """Real minute candles (only minutes with trades) of one symbol, ascending."""

    timestamps: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray

    def as_candles(self) -> Candles:
        return Candles(self.timestamps, self.closes)


def load_minutes(symbol_dir: Path, symbol: str, start_ms: int, end_ms: int) -> MinuteBars:
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
    columns = {name: np.concatenate([part.column(name).to_numpy() for part in parts]) for name in ("timestamp", "open", "high", "low", "close")}
    keep = (columns["timestamp"] >= start_ms) & (columns["timestamp"] < end_ms)
    return MinuteBars(columns["timestamp"][keep].astype(np.int64), columns["open"][keep], columns["high"][keep],
                      columns["low"][keep], columns["close"][keep])


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
    filled = np.where(np.isnan(close), np.nan, close)
    index = np.where(~np.isnan(filled), np.arange(size), 0)
    np.maximum.accumulate(index, out=index)
    prev_close = filled[index]                       # close carried forward (NaN before the first trade)
    opens = prev_close.copy()
    highs = prev_close.copy()
    lows = prev_close.copy()
    opens[slot], highs[slot], lows[slot] = minutes.opens, minutes.highs, minutes.lows
    shape = (count, per_bar)
    bar_open = opens.reshape(shape)[:, 0]
    bar_high = np.nanmax(highs.reshape(shape), axis=1)
    bar_low = np.nanmin(lows.reshape(shape), axis=1)
    bar_close = prev_close.reshape(shape)[:, -1]
    first_open = np.where(np.isnan(bar_open), bar_close, bar_open)
    starts = first_bar + np.arange(count, dtype=np.int64) * timeframe_ms
    return starts, (first_open + bar_high + bar_low + bar_close) / 4


def order_prices(ohlc4: np.ndarray, length: int, multiplier: float, tick: float, real: np.ndarray | None = None) -> np.ndarray:
    """Order price set at the start of each bar: SMA of the `length` closed bars before it × multiplier, to the tick.

    With `real`, bars without any traded minute do not exist for the MA (SMA of the last real bars)."""
    if real is None:
        real = np.ones(len(ohlc4), dtype=bool)
    values = ohlc4[real]
    csum = np.r_[0.0, np.cumsum(values)]
    before = np.cumsum(real) - real            # real bars strictly before each bar
    sma = np.full(len(ohlc4), np.nan)
    ok = before >= length
    sma[ok] = (csum[before[ok]] - csum[before[ok] - length]) / length
    return np.round(sma * multiplier / tick) * tick


def simulate_mrs2(minutes: MinuteBars, start_ms: int, end_ms: int, *, timeframe: str, open_len: int, open_mult: float,
                  close_len: int, close_mult: float, tick: float, maker_fee: float = 0.00001,
                  empty_bars: str = "fill", reprice: str = "time", start_rule: str = "bar",
                  distance_filter: float = 0.3, filter_mode: str = "keep", bar_shift_ms: int = 0,
                  post_only: str | None = None, post_only_sides: str = "both") -> tuple[Cycle, ...]:
    """MRS2 LONG: limit buy at the open MA, limit sell at the close MA, filled at the order price.

    Orders are re-placed at the start of every TF bar; with sparse minute data that happens on the first
    real candle of the bar, after that candle was checked against the previous order.
    """
    tf_ms = TIMEFRAME_MS[timeframe]
    if bar_shift_ms:
        # a minute candle stamped t covers [t - shift, t): it belongs to the bar that contains t - shift
        minutes = MinuteBars(minutes.timestamps - bar_shift_ms, minutes.opens, minutes.highs, minutes.lows, minutes.closes)
        start_ms, end_ms = start_ms - bar_shift_ms, end_ms - bar_shift_ms
    starts, ohlc4 = bar_ohlc4(minutes, tf_ms)
    ts = minutes.timestamps
    bar_of = (ts - starts[0]) // tf_ms
    real = np.bincount(bar_of, minlength=len(starts)) > 0
    if reprice == "time":   # re-placed by the clock at every bar start; a candle at the bar's first minute still meets the old order
        active_bar = bar_of - (ts == starts[bar_of])
    else:                   # re-placed on the first real candle of a bar, after that candle's fill check
        active_bar = np.r_[bar_of[0], bar_of[:-1]]
    buy_bar = order_prices(ohlc4, open_len, open_mult, tick, real if empty_bars == "skip" else None)
    sell_bar = order_prices(ohlc4, close_len, close_mult, tick, real if empty_bars == "skip" else None)
    if distance_filter:
        # MRS2 distance_filter: orders are (re)placed only when the close-MA price is at least
        # `distance_filter` % above the open-MA price; otherwise the previous orders stay.
        placed = (sell_bar - buy_bar) / buy_bar * 100 >= distance_filter
        last = np.where(placed, np.arange(len(placed)), -1)
        np.maximum.accumulate(last, out=last)
        keep = np.maximum(last, 0)
        if filter_mode == "keep":
            buy_bar = np.where(last >= 0, buy_bar[keep], np.nan)
            sell_bar = np.where(last >= 0, sell_bar[keep], np.nan)
        else:   # "no_entry": no new buy order below the distance, the exit order stays
            buy_bar = np.where(placed, buy_bar, np.nan)
            sell_bar = np.where(last >= 0, sell_bar[keep], np.nan)
    if post_only:
        # Post-only limits: a re-placed order that would cross the market at the bar start is rejected and the
        # previous order stays (sell must be above, buy below the last traded price).
        if post_only == "prev_close":
            ref_index = np.searchsorted(ts, starts, "left") - 1
            ref_price = np.where(ref_index >= 0, minutes.closes[np.clip(ref_index, 0, None)], np.nan)
        else:   # the first real candle of the bar ("first_open" / "first_close" / "first_low_high")
            ref_index = np.searchsorted(ts, starts, "left")
            inside = (ref_index < len(ts))
            inside[inside] = (ts[ref_index[inside]] - starts[inside]) < tf_ms
            safe = np.clip(ref_index, 0, len(ts) - 1)
            source = minutes.opens if post_only == "first_open" else minutes.closes
            ref_price = np.where(inside, source[safe], np.nan)
        for name in ("buy", "sell"):
            if post_only_sides != "both" and name != post_only_sides:
                continue
            prices = buy_bar if name == "buy" else sell_bar
            ok = (prices < ref_price) if name == "buy" else (prices > ref_price)
            ok |= np.isnan(ref_price)
            last = np.where(ok, np.arange(len(ok)), -1)
            np.maximum.accumulate(last, out=last)
            kept = np.where(last >= 0, prices[np.maximum(last, 0)], np.nan)
            if name == "buy":
                buy_bar = kept
            else:
                sell_bar = kept
    buy_now = buy_bar[active_bar]
    sell_now = sell_bar[active_bar]
    first_trade_ms = start_ms
    if start_rule == "next_bar":     # the first orders are placed at the first bar start after the first real candle of the test
        first_real = ts[np.searchsorted(ts, start_ms, "left")] if np.searchsorted(ts, start_ms, "left") < len(ts) else start_ms
        first_trade_ms = (int(first_real) // tf_ms + 1) * tf_ms
    elif start_rule == "bar":
        first_trade_ms = -(-start_ms // tf_ms) * tf_ms + (tf_ms if start_ms % tf_ms == 0 else 0)
    elif start_rule.startswith("delay"):
        first_trade_ms = start_ms + int(start_rule[5:]) * MINUTE_MS
    tradable = (ts > first_trade_ms if start_rule == "bar" else ts >= first_trade_ms) & (ts < end_ms)
    buy_hits = np.flatnonzero(tradable & (minutes.lows <= buy_now))
    sell_hits = np.flatnonzero(tradable & (minutes.highs >= sell_now))
    cycles = []
    position = 0
    while True:
        k = int(np.searchsorted(buy_hits, position, "left"))
        if k >= len(buy_hits):
            break
        entry = int(buy_hits[k])
        m = int(np.searchsorted(sell_hits, entry + 1, "left"))
        if m >= len(sell_hits):
            cycles.append(Cycle(int(ts[entry]) + bar_shift_ms, float(buy_now[entry]), maker_fee, None, None, None))
            break
        exit_index = int(sell_hits[m])
        cycles.append(Cycle(int(ts[entry]) + bar_shift_ms, float(buy_now[entry]), maker_fee, int(ts[exit_index]) + bar_shift_ms,
                            float(sell_now[exit_index]), maker_fee))
        position = exit_index + 1
    return tuple(cycles)


def simulate_mrs2_v2(minutes: MinuteBars, start_ms: int, end_ms: int, *, timeframe: str, open_len: int, open_mult: float,
                     close_len: int, close_mult: float, tick: float, maker_fee: float = 0.00001, taker_fee: float = 0.0002,
                     slippage: float = 0.001, distance_filter: float = 0.3, market_ref: str = "prev_close",
                     activation: str = "next") -> tuple[Cycle, ...]:
    """MRS2 LONG with the tester's placement rules (developer answers 2026-10-16).

    - Orders are re-placed at every TF bar start; a candle at the bar's first minute still meets the old order.
    - Open order is post-only: if at placement its price is not below the market, the tester retries until
      it can rest (it becomes active after the market trades above it); no buy order before that.
    - Close order is a plain limit: if at placement its price is not above the market, it executes at market
      (market price × (1 − slippage), taker fee).
    - distance_filter: orders are not (re)placed while the close-MA price is less than `distance_filter` %
      above the open-MA price (the previous orders stay).
    - Trading starts at the first bar start after the period start.
    """
    tf_ms = TIMEFRAME_MS[timeframe]
    starts, ohlc4 = bar_ohlc4(minutes, tf_ms)
    ts = minutes.timestamps
    n = len(ts)
    bar_of = (ts - starts[0]) // tf_ms
    buy_bar = order_prices(ohlc4, open_len, open_mult, tick)
    sell_bar = order_prices(ohlc4, close_len, close_mult, tick)
    if distance_filter:
        placed = (sell_bar - buy_bar) / buy_bar * 100 >= distance_filter
        last = np.where(placed, np.arange(len(placed)), -1)
        np.maximum.accumulate(last, out=last)
        buy_bar = np.where(last >= 0, buy_bar[np.maximum(last, 0)], np.nan)
        sell_bar = np.where(last >= 0, sell_bar[np.maximum(last, 0)], np.nan)
    # market price at each bar's placement moment
    if market_ref == "prev_close":
        ref_index = np.searchsorted(ts, starts, "left") - 1
        market = np.where(ref_index >= 0, minutes.closes[np.clip(ref_index, 0, None)], np.nan)
    else:
        ref_index = np.searchsorted(ts, starts, "left")
        safe = np.clip(ref_index, 0, n - 1)
        inside = (ref_index < n) & ((ts[safe] - starts) < tf_ms)
        market = np.where(inside, minutes.opens[safe], np.nan)
    at_bar_start = ts == starts[bar_of]
    active_bar = bar_of - at_bar_start                  # order in force during each candle
    buy_now = buy_bar[active_bar]
    sell_now = sell_bar[active_bar]
    # post-only buy placed at a bar start: active only once the market traded above it in that bar
    crossing_bar = ~(buy_bar < market)
    above = minutes.highs > buy_now
    first_above = np.full(len(starts), n, dtype=np.int64)
    idx = np.flatnonzero(above)
    np.minimum.at(first_above, active_bar[idx], idx)
    activation_index = np.where(crossing_bar, first_above + (1 if activation == "next" else 0), 0)
    buy_ok_fresh = np.arange(n) >= activation_index[active_bar]         # placed at the bar start
    low_hit = minutes.lows <= buy_now
    # close limit placed at a bar start below the market executes at market on the bar's first real candle
    market_exit_bar = ~(sell_bar > market)
    first_real = np.r_[True, bar_of[1:] != bar_of[:-1]]
    sell_market = first_real & market_exit_bar[bar_of] & ~at_bar_start
    sell_market |= at_bar_start & market_exit_bar[bar_of]
    high_hit = minutes.highs >= sell_now
    first_trade_ms = (start_ms // tf_ms + 1) * tf_ms
    tradable = (ts > first_trade_ms) & (ts < end_ms)
    hits_fresh = np.flatnonzero(tradable & low_hit & buy_ok_fresh)
    hits_any = np.flatnonzero(tradable & low_hit)
    sell_events = np.flatnonzero(tradable & (high_hit | sell_market))
    cycles = []
    position = 0
    last_exit_bar = -1
    while True:
        entry = None
        k = int(np.searchsorted(hits_any, position, "left"))
        if k < len(hits_any) and active_bar[hits_any[k]] == last_exit_bar:
            entry = int(hits_any[k])                    # re-entry in the exit bar: order placed after the exit, not crossing
        if entry is None:
            start = position
            if last_exit_bar >= 0:
                start = max(position, int(np.searchsorted(active_bar, last_exit_bar + 1, "left")))
            k = int(np.searchsorted(hits_fresh, start, "left"))
            if k >= len(hits_fresh):
                break
            entry = int(hits_fresh[k])
        entry_price = float(buy_now[entry])
        entry_bar = active_bar[entry]
        m = int(np.searchsorted(sell_events, entry + 1, "left"))
        exit_index = None
        while m < len(sell_events):
            j = int(sell_events[m])
            if sell_market[j] and active_bar[j] != entry_bar and not high_hit[j] or (sell_market[j] and bar_of[j] != bar_of[entry]):
                exit_index, exit_price, rate = j, float(market[bar_of[j]]) * (1 - slippage), taker_fee
                break
            if high_hit[j]:
                exit_index, exit_price, rate = j, float(sell_now[j]), maker_fee
                break
            m += 1
        if exit_index is None:
            cycles.append(Cycle(int(ts[entry]), entry_price, maker_fee, None, None, None))
            break
        cycles.append(Cycle(int(ts[entry]), entry_price, maker_fee, int(ts[exit_index]), exit_price, rate))
        position = exit_index + 1
        last_exit_bar = active_bar[exit_index]
    return tuple(cycles)


def simulate_mrs2_v3(minutes: MinuteBars, start_ms: int, end_ms: int, *, timeframe: str, open_len: int, open_mult: float,
                     close_len: int, close_mult: float, tick: float, maker_fee: float = 0.00001,
                     distance_filter: float = 0.3, first_minute: str = "old", bar_market: str = "prev_close",
                     start_rule: str = "bar") -> tuple[Cycle, ...]:
    """Sequential MRS2 LONG replay with the tester's placement rules.

    Orders are placed at every TF bar start and right after each fill. A close (sell) order priced at or
    below the market is placed one tick above the market; the post-only open (buy) order priced at or
    above the market rests one tick below it. Limit fills happen at the order price on a later candle.
    """
    tf_ms = TIMEFRAME_MS[timeframe]
    starts, ohlc4 = bar_ohlc4(minutes, tf_ms)
    buy_bar = order_prices(ohlc4, open_len, open_mult, tick)
    sell_bar = order_prices(ohlc4, close_len, close_mult, tick)
    placed_ok = (sell_bar - buy_bar) / buy_bar * 100 >= distance_filter if distance_filter else np.ones(len(starts), bool)
    ts = minutes.timestamps.tolist()
    lows = minutes.lows.tolist()
    highs = minutes.highs.tolist()
    closes = minutes.closes.tolist()
    opens = minutes.opens.tolist()
    first_bar = int(starts[0])
    buy_l = buy_bar.tolist()
    sell_l = sell_bar.tolist()
    ok_l = placed_ok.tolist()
    half = tick / 2

    def above(price: float, market: float) -> float:      # sell: at least one tick above the market
        return price if price > market + half else round((market + tick) / tick) * tick

    def below(price: float, market: float) -> float:      # post-only buy: at least one tick below the market
        return price if price < market - half else round((market - tick) / tick) * tick

    cycles = []
    in_pos = False
    order = None            # current order price (buy when flat, sell in position)
    current_bar = None
    market = None
    entry_ms = entry_price = None
    trade_from = (start_ms // tf_ms + 1) * tf_ms if start_rule == "bar" else start_ms
    i0 = 0
    while i0 < len(ts) and ts[i0] <= (trade_from if start_rule == "bar" else start_ms - 1):
        market = closes[i0]
        i0 += 1
    for i in range(i0, len(ts)):
        t = ts[i]
        if t >= end_ms:
            break
        bar = (t - first_bar) // tf_ms
        if bar != current_bar:
            at_first_minute = t == first_bar + bar * tf_ms
            ref = market if (bar_market == "prev_close" or market is None) else opens[i]
            if current_bar is not None and at_first_minute and first_minute == "old" and order is not None:
                # this candle still meets the previous order, the new one is placed after it
                pass
            new_order = order
            if ok_l[bar] and ref is not None:
                if in_pos:
                    p = sell_l[bar]
                    if p == p:
                        new_order = above(p, ref)
                else:
                    p = buy_l[bar]
                    if p == p:
                        new_order = below(p, ref)
            if not (current_bar is not None and at_first_minute and first_minute == "old"):
                order = new_order
                pending = None
            else:
                pending = new_order
            current_bar = bar
        else:
            pending = None
        filled = False
        if order is not None:
            if not in_pos and lows[i] <= order + 1e-9:
                in_pos, entry_ms, entry_price, filled = True, t, order, True
                p = sell_l[bar]
                order = above(p, closes[i]) if p == p else None
            elif in_pos and highs[i] >= order - 1e-9:
                cycles.append(Cycle(entry_ms, entry_price, maker_fee, t, order, maker_fee))
                in_pos, filled = False, True
                p = buy_l[bar]
                order = below(p, closes[i]) if p == p else None
        if pending is not None and not filled:
            order = pending
        market = closes[i]
    if in_pos:
        cycles.append(Cycle(entry_ms, entry_price, maker_fee, None, None, None))
    return tuple(cycles)


def price_tick(minutes: MinuteBars) -> float:
    """Price tick inferred from the decimals of the traded prices (Bybit price increment)."""
    sample = np.concatenate([minutes.opens[:5000], minutes.closes[:5000], minutes.lows[:5000], minutes.highs[:5000]])
    for decimals in range(0, 9):
        scaled = sample * 10 ** decimals
        if np.all(np.abs(scaled - np.round(scaled)) < 1e-6):
            return 10.0 ** -decimals
    return 1e-8
