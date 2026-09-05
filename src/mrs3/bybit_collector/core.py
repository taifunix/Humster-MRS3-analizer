"""In-memory, copy-then-commit order-book state for Bybit public data."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any


class InvalidationCause(str, Enum):
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    PING_FAILURE = "ping_failure"
    RESUBSCRIBE = "resubscribe"
    CLOCK_DISCONTINUITY = "clock_discontinuity"
    DELTA_BEFORE_SNAPSHOT = "delta_before_snapshot"
    MALFORMED_UPDATE = "malformed_update"
    NON_INCREASING_UPDATE_ID = "non_increasing_update_id"
    IMPOSSIBLE_LOCAL_STATE = "impossible_local_state"


class ResetCause(str, Enum):
    INITIAL = "initial"
    RESUBSCRIBE = "resubscribe"
    RECONNECT = "reconnect"
    EXCHANGE_SNAPSHOT = "exchange_snapshot"


_RESET_PRIORITY = {
    ResetCause.INITIAL.value: 0,
    ResetCause.RESUBSCRIBE.value: 1,
    ResetCause.RECONNECT.value: 2,
    ResetCause.EXCHANGE_SNAPSHOT.value: 3,
}


@dataclass(frozen=True, slots=True)
class BookState:
    symbol: str | None
    bids: Mapping[float, float]
    asks: Mapping[float, float]
    valid: bool
    last_update_id: int | None
    book_reset_count: int
    last_reset_cause: str | None
    reset_cause_counts: Mapping[str, int]
    invalidation_cause: str | None


class _Malformed(ValueError):
    pass


class _Impossible(ValueError):
    pass


class OrderBook:
    """A deterministic one-symbol order book with no network or persistence."""

    def __init__(self, symbol: str | None = None) -> None:
        if symbol is not None and (not isinstance(symbol, str) or not symbol):
            raise ValueError("symbol must be a non-empty string or None")
        self.symbol = symbol
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._valid = False
        self._has_snapshot = False
        self._last_update_id: int | None = None
        self._book_reset_count = 0
        self._last_reset_cause: str | None = None
        self._reset_cause_counts = {cause.value: 0 for cause in ResetCause}
        self._pending_reset_causes = {ResetCause.INITIAL.value}
        self._invalidation_cause: str | None = None

    @property
    def valid(self) -> bool:
        return self._valid

    @property
    def bids(self) -> Mapping[float, float]:
        return MappingProxyType(dict(self._bids))

    @property
    def asks(self) -> Mapping[float, float]:
        return MappingProxyType(dict(self._asks))

    @property
    def last_update_id(self) -> int | None:
        return self._last_update_id

    @property
    def book_reset_count(self) -> int:
        return self._book_reset_count

    @property
    def last_reset_cause(self) -> str | None:
        return self._last_reset_cause

    @property
    def reset_cause_counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self._reset_cause_counts))

    @property
    def invalidation_cause(self) -> str | None:
        return self._invalidation_cause

    def state(self) -> BookState:
        """Return immutable levels; consumers must gate them on ``valid``."""

        return BookState(
            symbol=self.symbol,
            bids=MappingProxyType(dict(self._bids)),
            asks=MappingProxyType(dict(self._asks)),
            valid=self._valid,
            last_update_id=self._last_update_id,
            book_reset_count=self._book_reset_count,
            last_reset_cause=self._last_reset_cause,
            reset_cause_counts=MappingProxyType(dict(self._reset_cause_counts)),
            invalidation_cause=self._invalidation_cause,
        )

    def invalidate(self, cause: InvalidationCause | str) -> None:
        """Mark the book unusable until a full snapshot is accepted."""

        try:
            normalized = InvalidationCause(cause).value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown invalidation cause: {cause!r}") from exc
        self._valid = False
        self._invalidation_cause = normalized
        if normalized in {
            InvalidationCause.DISCONNECT.value,
            InvalidationCause.PING_FAILURE.value,
            InvalidationCause.RECONNECT.value,
        }:
            self._pending_reset_causes.add(ResetCause.RECONNECT.value)
        elif normalized == InvalidationCause.RESUBSCRIBE.value:
            self._pending_reset_causes.add(ResetCause.RESUBSCRIBE.value)

    def on_disconnect(self) -> None:
        self.invalidate(InvalidationCause.DISCONNECT)

    def on_reconnect(self) -> None:
        self.invalidate(InvalidationCause.RECONNECT)

    def on_ping_failure(self) -> None:
        self.invalidate(InvalidationCause.PING_FAILURE)

    def on_resubscribe(self) -> None:
        self.invalidate(InvalidationCause.RESUBSCRIBE)

    def on_clock_discontinuity(self) -> None:
        self.invalidate(InvalidationCause.CLOCK_DISCONTINUITY)

    def apply_snapshot(
        self,
        bids: Any,
        asks: Any = None,
        *,
        update_id: Any = None,
        cause: ResetCause | str | None = None,
    ) -> bool:
        """Replace both sides atomically with a validated full snapshot."""

        try:
            if asks is None and isinstance(bids, Mapping):
                envelope_keys = {"bids", "b", "asks", "a", "update_id", "u", "cause", "s"}
                if not envelope_keys.intersection(bids):
                    raise _Malformed("snapshot is missing asks")
            bids, asks, update_id, cause = self._normalize_payload(bids, asks, update_id, cause)
            update_id = _parse_update_id(update_id)
            if update_id is None:
                raise _Malformed("snapshot is missing update id")
            parsed_bids = _parse_levels(bids, allow_zero=False)
            parsed_asks = _parse_levels(asks, allow_zero=False)
            _validate_local_state(parsed_bids, parsed_asks)
            reset_cause = self._select_reset_cause(cause)
        except _Impossible:
            return self._reject(InvalidationCause.IMPOSSIBLE_LOCAL_STATE)
        except (TypeError, ValueError, _Malformed):
            return self._reject(InvalidationCause.MALFORMED_UPDATE)

        self._bids = parsed_bids
        self._asks = parsed_asks
        self._last_update_id = update_id
        self._valid = True
        self._has_snapshot = True
        self._book_reset_count += 1
        self._last_reset_cause = reset_cause
        self._reset_cause_counts[reset_cause] += 1
        self._pending_reset_causes.clear()
        self._invalidation_cause = None
        return True

    def apply_delta(
        self,
        bids: Any,
        asks: Any = None,
        *,
        update_id: Any = None,
    ) -> bool:
        """Apply inserts, updates, and zero-quantity deletes atomically.

        A non-mapping bid sequence with omitted asks is treated as an empty
        ask side; full snapshots still require both sides.
        """

        if not self._valid:
            if not self._has_snapshot:
                self.invalidate(InvalidationCause.DELTA_BEFORE_SNAPSHOT)
            return False
        try:
            if asks is None and not isinstance(bids, Mapping):
                asks = []
            bids, asks, update_id, _ = self._normalize_payload(bids, asks, update_id, None)
            parsed_bids = _parse_levels(bids, allow_zero=True)
            parsed_asks = _parse_levels(asks, allow_zero=True)
            update_id = _parse_update_id(update_id)
        except (TypeError, ValueError, _Malformed):
            return self._reject(InvalidationCause.MALFORMED_UPDATE)

        if update_id is None:
            return self._reject(InvalidationCause.MALFORMED_UPDATE)
        if self._last_update_id is not None and update_id <= self._last_update_id:
            return self._reject(InvalidationCause.NON_INCREASING_UPDATE_ID)

        next_bids = dict(self._bids)
        next_asks = dict(self._asks)
        _apply_levels(next_bids, parsed_bids)
        _apply_levels(next_asks, parsed_asks)
        try:
            _validate_local_state(next_bids, next_asks)
        except _Impossible:
            return self._reject(InvalidationCause.IMPOSSIBLE_LOCAL_STATE)

        self._bids = next_bids
        self._asks = next_asks
        self._last_update_id = update_id
        return True

    def apply_message(self, message: Mapping[str, Any]) -> bool:
        """Apply a Bybit ``snapshot`` or ``delta`` frame without network code."""

        try:
            if not isinstance(message, Mapping):
                raise _Malformed("message must be a mapping")
            kind = message.get("type")
            data = message.get("data")
            if kind not in {"snapshot", "delta"} or not isinstance(data, Mapping):
                raise _Malformed("message type or data is invalid")
            if self.symbol is not None and data.get("s") not in {None, self.symbol}:
                raise _Malformed("message symbol does not match book")
            if "b" not in data or "a" not in data:
                raise _Malformed("message is missing one order-book side")
            if kind == "delta" and "u" not in data:
                raise _Malformed("delta message is missing update id")
            update_id = data.get("u")
        except _Malformed:
            return self._reject(InvalidationCause.MALFORMED_UPDATE)

        if kind == "snapshot":
            return self.apply_snapshot(data["b"], data["a"], update_id=update_id)
        return self.apply_delta(data["b"], data["a"], update_id=update_id)

    def _normalize_payload(
        self, bids: Any, asks: Any, update_id: Any, cause: ResetCause | str | None
    ) -> tuple[Any, Any, Any, ResetCause | str | None]:
        if asks is None and isinstance(bids, Mapping):
            payload = bids
            envelope_keys = {"bids", "b", "asks", "a", "update_id", "u", "cause", "s"}
            if not envelope_keys.intersection(payload):
                return bids, [], update_id, cause
            if "bids" in payload:
                bids = payload["bids"]
            elif "b" in payload:
                bids = payload["b"]
            else:
                raise _Malformed("payload is missing bids")
            if "asks" in payload:
                asks = payload["asks"]
            elif "a" in payload:
                asks = payload["a"]
            else:
                raise _Malformed("payload is missing asks")
            if update_id is None:
                update_id = payload.get("update_id", payload.get("u"))
            if cause is None:
                cause = payload.get("cause")
            if self.symbol is not None and payload.get("s") not in {None, self.symbol}:
                raise _Malformed("payload symbol does not match book")
        return bids, asks, update_id, cause

    def _select_reset_cause(self, cause: ResetCause | str | None) -> str:
        candidates = set(self._pending_reset_causes)
        if cause is not None:
            try:
                candidates.add(ResetCause(cause).value)
            except (TypeError, ValueError) as exc:
                raise _Malformed("unknown reset cause") from exc
        if not candidates:
            candidates.add(ResetCause.EXCHANGE_SNAPSHOT.value)
        return min(candidates, key=_RESET_PRIORITY.__getitem__)

    def _reject(self, cause: InvalidationCause) -> bool:
        self.invalidate(cause)
        return False


def _parse_update_id(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Malformed("update id must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip(), 10)
        except ValueError as exc:
            raise _Malformed("update id must be an integer") from exc
    raise _Malformed("update id must be an integer")


def _parse_levels(levels: Any, *, allow_zero: bool) -> dict[float, float]:
    if isinstance(levels, Mapping):
        entries: Iterable[Any] = levels.items()
    elif isinstance(levels, Iterable) and not isinstance(levels, (str, bytes, bytearray)):
        entries = levels
    else:
        raise _Malformed("levels must be an iterable")

    result: dict[float, float] = {}
    try:
        for entry in entries:
            if isinstance(entry, Mapping) or isinstance(entry, (str, bytes, bytearray)):
                raise _Malformed("level must be a two-item sequence")
            price_raw, quantity_raw = entry
            price = _parse_number(price_raw, positive=True)
            # Snapshot zero quantity is malformed; only delta zero means delete.
            quantity = _parse_number(quantity_raw, positive=not allow_zero)
            if allow_zero and quantity < 0:
                raise _Malformed("quantity must not be negative")
            if price in result:
                raise _Malformed("duplicate price level")
            result[price] = quantity
    except _Malformed:
        raise
    except (TypeError, ValueError) as exc:
        raise _Malformed("level must be a two-item sequence") from exc
    return result


def _parse_number(value: Any, *, positive: bool) -> float:
    if isinstance(value, bool):
        raise _Malformed("numeric values must not be booleans")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _Malformed("numeric value is invalid") from exc
    if not isfinite(number) or (positive and number <= 0):
        raise _Malformed("numeric value is not finite and positive")
    return number


def _apply_levels(book: dict[float, float], updates: Mapping[float, float]) -> None:
    for price, quantity in updates.items():
        if quantity == 0:
            book.pop(price, None)
        else:
            book[price] = quantity


def _validate_local_state(bids: Mapping[float, float], asks: Mapping[float, float]) -> None:
    if bids and asks and max(bids) >= min(asks):
        raise _Impossible("book is crossed")
