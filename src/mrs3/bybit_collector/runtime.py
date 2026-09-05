"""Minimal runtime wiring for the public collector pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
from collections import deque
import threading
import time
from typing import Any

from .aggregation import FiveSecondScheduler, MarketSample, MinuteAggregator
from .archive import HourlyExporter
from .config import CollectorConfig
from .core import InvalidationCause, OrderBook
from .storage import SQLiteSpool, WriteOutcome
from .websocket import decode_orderbook_frame


MINUTE_MS = 60_000
SAMPLES_PER_MINUTE = 12


@dataclass(frozen=True, slots=True)
class RuntimePollResult:
    scheduler: Any
    rows_written: int
    duplicate_rows: int
    errors: tuple[str, ...] = ()


class CollectorRuntime:
    """Connect decoded frames, clock boundaries, aggregation, spool and export."""

    def __init__(
        self,
        config: CollectorConfig,
        *,
        spool: SQLiteSpool | None = None,
        exporter: HourlyExporter | None = None,
        scheduler: FiveSecondScheduler | None = None,
        reference_callback: Any = None,
    ) -> None:
        self.config = config
        self.spool = spool
        self.exporter = exporter
        self.reference_callback = reference_callback
        self.scheduler = scheduler or FiveSecondScheduler()
        self.books = {symbol: OrderBook(symbol) for symbol in config.symbols}
        self._connected = False
        self._aggregators: dict[tuple[str, int], MinuteAggregator] = {}
        self._last_reset_counts = {symbol: 0 for symbol in config.symbols}
        self._duplicate_rows = 0
        self._finalized_minutes: dict[str, int] = {}
        self._last_export_check_ms: int | None = None
        self._reference_day: str | None = None
        self._reference_startup_pending = True
        self._reference_pending_symbols: set[str] = set()
        self._reference_next_attempt_ms = 0
        self._reference_backoff_ms = 15 * 60_000
        self._reference_inflight = False
        self._reference_lock = threading.Lock()
        self._reference_errors: deque[str] = deque(maxlen=20)
        self._books_lock = threading.RLock()
        self._last_book_update_ms: dict[str, int | None] = {
            symbol: None for symbol in config.symbols
        }
        self._last_valid_sample_ms: dict[str, int | None] = {
            symbol: None for symbol in config.symbols
        }
        self._recent_samples: dict[str, deque[tuple[int, bool]]] = {
            symbol: deque() for symbol in config.symbols
        }

    @property
    def connected(self) -> bool:
        with self._books_lock:
            return self._connected

    @property
    def last_completed_minute_ms(self) -> int | None:
        return max(self._finalized_minutes.values(), default=None)

    def handle_ws_message(
        self, message: str | bytes | dict[str, Any], *, event_ts_ms: int | None = None
    ) -> bool:
        if isinstance(message, dict):
            frame = message
            if not isinstance(frame.get("topic"), str) or not str(frame["topic"]).startswith("orderbook."):
                return False
            if frame.get("type") not in {"snapshot", "delta"} or not isinstance(frame.get("data"), Mapping):
                return False
        else:
            try:
                frame = decode_orderbook_frame(message)
            except (TypeError, ValueError):
                return False
        data = frame.get("data")
        if not isinstance(data, Mapping):
            return False
        symbol = data.get("s")
        if not isinstance(symbol, str):
            return False
        with self._books_lock:
            book = self.books.get(symbol)
        if book is None:
            return False
        accepted = book.apply_message(frame)
        if accepted:
            with self._books_lock:
                self._last_book_update_ms[symbol] = (
                    int(time.time() * 1000) if event_ts_ms is None else event_ts_ms
                )
        return accepted

    def set_connected(self, connected: bool) -> None:
        connected = bool(connected)
        with self._books_lock:
            if connected == self._connected:
                return
            self._connected = connected
            books = tuple(self.books.values())
        for book in books:
            if connected:
                book.invalidate(InvalidationCause.RECONNECT)
            else:
                book.on_disconnect()

    def update_config(self, config: CollectorConfig, event_ts_ms: int) -> None:
        """Apply accepted symbol changes without switching the storage root."""

        if config.storage_root != self.config.storage_root:
            raise ValueError("storage root changes require a restart")
        with self._books_lock:
            previous = set(self.books)
        current = set(config.symbols)
        removed = sorted(previous - current)
        pending_errors: list[str] = []
        for symbol in removed:
            errors: list[str] = []
            self._finalize_before(symbol, None, errors)
            pending_errors.extend(errors)
        if pending_errors:
            raise RuntimeError("cannot remove symbols with pending rows: " + "; ".join(pending_errors))
        for symbol in removed:
            with self._books_lock:
                self.books.pop(symbol, None)
                self._last_reset_counts.pop(symbol, None)
                self._last_book_update_ms.pop(symbol, None)
                self._last_valid_sample_ms.pop(symbol, None)
                self._recent_samples.pop(symbol, None)
            if self.spool is not None:
                self.spool.record_symbol_event(event_ts_ms, symbol, "removed", "config", config.config_revision)
        for symbol in sorted(current - previous):
            with self._books_lock:
                self.books[symbol] = OrderBook(symbol)
                self._last_reset_counts[symbol] = 0
                self._last_book_update_ms[symbol] = None
                self._last_valid_sample_ms[symbol] = None
                self._recent_samples[symbol] = deque()
            if self.spool is not None:
                self.spool.record_symbol_event(event_ts_ms, symbol, "added", "config", config.config_revision)
            with self._reference_lock:
                self._reference_pending_symbols.add(symbol)
        self.config = config

    def poll(self, wall_ms: int, monotonic_ms: int) -> RuntimePollResult:
        result = self.scheduler.poll(wall_ms, monotonic_ms)
        errors: list[str] = []
        if result.clock_discontinuity:
            with self._books_lock:
                books = tuple(self.books.values())
                symbols = tuple(self.books)
            for book in books:
                book.on_clock_discontinuity()
            if result.missed_boundaries:
                minute_start = (wall_ms // MINUTE_MS) * MINUTE_MS
                for symbol in symbols:
                    if self._finalized_minutes.get(symbol, -1) >= minute_start:
                        continue
                    self._aggregator(symbol, minute_start).record_missed_boundary(
                        result.missed_boundaries
                    )
        rows_written = 0
        duplicates_before = self._duplicate_rows
        if result.missed_boundary_range_ms is not None and result.missed_boundaries:
            start, end = result.missed_boundary_range_ms
            for minute_start, count in _partition_missed(start, end, self.scheduler.interval_ms):
                with self._books_lock:
                    symbols = tuple(self.books)
                for symbol in symbols:
                    aggregator = self._aggregator(symbol, minute_start)
                    aggregator.record_missed_boundary(count)

        for boundary in result.due_boundaries:
            minute_start = (boundary // MINUTE_MS) * MINUTE_MS
            with self._books_lock:
                items = tuple(self.books.items())
            for symbol, book in items:
                rows_written += self._finalize_before(symbol, minute_start, errors)
                aggregator = self._aggregator(symbol, minute_start)
                reset_total = book.book_reset_count
                reset_delta = max(0, reset_total - self._last_reset_counts[symbol])
                self._last_reset_counts[symbol] = reset_total
                state = book.state()
                try:
                    sample = MarketSample(
                        local_timestamp_ms=boundary,
                        bids=state.bids,
                        asks=state.asks,
                        book_valid=state.valid,
                        ws_connected=self.connected,
                        reset_count=reset_delta,
                    )
                    valid_sample = aggregator.add_sample(sample)
                    recent = self._recent_samples[symbol]
                    recent.append((boundary, valid_sample))
                    if valid_sample:
                        self._last_valid_sample_ms[symbol] = boundary
                    while recent and recent[0][0] < boundary - 5 * MINUTE_MS:
                        recent.popleft()
                except ValueError as exc:
                    errors.append(f"{symbol}@{boundary}: {exc}")
        if self.exporter is not None and (
            self._last_export_check_ms is None or wall_ms - self._last_export_check_ms >= MINUTE_MS
        ):
            try:
                recovery = self.exporter.recover(wall_ms)
                errors.extend(recovery.errors)
            except Exception as exc:
                errors.append(f"archive recovery: {exc}")
            self._last_export_check_ms = wall_ms
        if self.reference_callback is not None:
            instant = datetime.fromtimestamp(wall_ms / 1000, timezone.utc)
            day = instant.date().isoformat()
            with self._reference_lock:
                pending_symbols = tuple(sorted(self._reference_pending_symbols.intersection(self.books)))
                due = (
                    (
                        self._reference_startup_pending
                        or bool(pending_symbols)
                        or ((instant.hour, instant.minute) >= (0, 10) and self._reference_day != day)
                    )
                    and wall_ms >= self._reference_next_attempt_ms
                    and not self._reference_inflight
                )
                if due:
                    self._reference_inflight = True
                    with self._books_lock:
                        symbols = pending_symbols or tuple(self.books)
                    threading.Thread(
                        target=self._reference_worker,
                        args=(symbols, wall_ms, day),
                        daemon=True,
                    ).start()
                errors.extend(self._reference_errors)
                self._reference_errors.clear()
        duplicates = self._duplicate_rows - duplicates_before
        return RuntimePollResult(result, rows_written, duplicates, tuple(errors))

    def health_diagnostics(self, now_ms: int) -> dict[str, dict[str, Any]]:
        """Return current book and recent-sample state for the health snapshot."""

        with self._books_lock:
            symbols = tuple(self.books)
            diagnostics: dict[str, dict[str, Any]] = {}
            for symbol in symbols:
                recent = self._recent_samples[symbol]
                while recent and recent[0][0] < now_ms - 5 * MINUTE_MS:
                    recent.popleft()
                valid_count = sum(1 for _timestamp, valid in recent if valid)
                diagnostics[symbol] = {
                    "book_synchronized": self.books[symbol].valid,
                    "last_book_update_ms": self._last_book_update_ms[symbol],
                    "last_valid_sample_ms": self._last_valid_sample_ms[symbol],
                    "valid_sample_count_recent": valid_count,
                    "coverage_recent": valid_count / len(recent) if recent else 0.0,
                }
            return diagnostics

    def _reference_worker(self, symbols: tuple[str, ...], now_ms: int, day: str) -> None:
        try:
            self.reference_callback(symbols, now_ms)
        except Exception as exc:
            with self._reference_lock:
                self._reference_errors.append(f"reference: {exc}")
                self._reference_next_attempt_ms = now_ms + self._reference_backoff_ms
                self._reference_backoff_ms = min(6 * 60 * 60_000, self._reference_backoff_ms * 2)
        else:
            with self._reference_lock:
                self._reference_day = day
                self._reference_startup_pending = False
                self._reference_pending_symbols.difference_update(symbols)
                self._reference_next_attempt_ms = now_ms + 24 * 60 * 60_000
                self._reference_backoff_ms = 15 * 60_000
        finally:
            with self._reference_lock:
                self._reference_inflight = False

    def flush(self, now_ms: int | None = None) -> int:
        errors: list[str] = []
        written = 0
        for symbol in self.books:
            written += self._finalize_before(symbol, None, errors)
        if errors:
            raise RuntimeError("collector flush failed: " + "; ".join(errors))
        if self.exporter is not None and now_ms is not None:
            self.exporter.recover(now_ms)
        return written

    def _aggregator(self, symbol: str, minute_start: int) -> MinuteAggregator:
        key = (symbol, minute_start)
        aggregator = self._aggregators.get(key)
        if aggregator is None:
            aggregator = MinuteAggregator(symbol, minute_start, SAMPLES_PER_MINUTE)
            self._aggregators[key] = aggregator
        return aggregator

    def _finalize_before(self, symbol: str, minute_start: int | None, errors: list[str]) -> int:
        completed = [
            key for key in self._aggregators
            if key[0] == symbol and (minute_start is None or key[1] < minute_start)
        ]
        written = 0
        for key in sorted(completed, key=lambda item: item[1]):
            aggregator = self._aggregators[key]
            row = aggregator.finalize()
            if row is None or self.spool is None:
                self._aggregators.pop(key, None)
                self._finalized_minutes[key[0]] = max(self._finalized_minutes.get(key[0], -1), key[1])
                continue
            try:
                outcome = self.spool.write_minute(row)
                if outcome is WriteOutcome.INSERTED:
                    written += 1
                elif outcome is WriteOutcome.DUPLICATE:
                    self._duplicate_rows += 1
                elif outcome is WriteOutcome.CONFLICT:
                    errors.append(f"{symbol}@{key[1]}: conflicting minute row")
                self._aggregators.pop(key, None)
                self._finalized_minutes[key[0]] = max(self._finalized_minutes.get(key[0], -1), key[1])
            except Exception as exc:
                errors.append(f"{symbol}@{key[1]}: {exc}")
        return written


def _partition_missed(start: int, end: int, interval_ms: int) -> tuple[tuple[int, int], ...]:
    if end < start or interval_ms <= 0:
        return ()
    result: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        minute = (cursor // MINUTE_MS) * MINUTE_MS
        last = min(end, minute + MINUTE_MS - interval_ms)
        count = ((last - cursor) // interval_ms) + 1
        result.append((minute, count))
        cursor += count * interval_ms
    return tuple(result)


__all__ = ["CollectorRuntime", "RuntimePollResult"]
