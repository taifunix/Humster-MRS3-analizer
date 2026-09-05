"""Durable minute aggregate spool and published-hour reader index."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
from math import isfinite
from pathlib import Path
import sqlite3
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, TypeVar

from mrs3.locking import OutputDirectoryLock

from .aggregation import LIQUIDITY_1M_COLUMNS, LIQUIDITY_1M_SCHEMA


class StorageWriteError(RuntimeError):
    """A SQLite spool write failed and cannot be safely retried."""


class MarkerConflictError(ValueError):
    """A published-hour marker would overwrite existing reader authority."""


class WriteOutcome(str, Enum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class MarkerOutcome(str, Enum):
    INSERTED = "inserted"
    EXISTING = "existing"


@dataclass(frozen=True, slots=True)
class PublishedHour:
    hour_start_ms: int
    file_name: str
    row_count: int
    validated_at_ms: int


@dataclass(frozen=True, slots=True)
class SymbolEvent:
    event_ts_ms: int
    symbol: str
    event_type: str
    reason: str
    config_revision: str


_INT_COLUMNS = frozenset(
    name for name, data_type, _nullable in LIQUIDITY_1M_SCHEMA if data_type != "DOUBLE"
)
_T = TypeVar("_T")


def _canonical_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if not isinstance(row, Mapping):
        raise ValueError("minute row must be a mapping")
    if set(row) != set(LIQUIDITY_1M_COLUMNS):
        raise ValueError("minute row must contain exactly the canonical keys")
    normalized: dict[str, Any] = {}
    for column in LIQUIDITY_1M_COLUMNS:
        value = row[column]
        if column == "symbol":
            if not isinstance(value, str) or not value:
                raise ValueError("minute row symbol must be a non-empty string")
        elif column in _INT_COLUMNS:
            if type(value) is not int or value < 0:
                raise ValueError(f"minute row {column} must be a non-negative integer")
        elif value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"minute row {column} must be finite JSON-safe")
            value = float(value)
            if not isfinite(value):
                raise ValueError(f"minute row {column} must be finite JSON-safe")
        normalized[column] = value
    try:
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("minute row must contain finite JSON-safe values") from exc
    return normalized, payload


def _canonical_marker(
    hour_start_ms: int, file_name: str, row_count: int, validated_at_ms: int
) -> PublishedHour:
    if type(hour_start_ms) is not int or hour_start_ms < 0:
        raise ValueError("hour_start_ms must be a non-negative integer")
    if not isinstance(file_name, str) or not file_name:
        raise ValueError("file_name must be a non-empty string")
    if type(row_count) is not int or row_count < 0:
        raise ValueError("row_count must be a non-negative integer")
    if type(validated_at_ms) is not int or validated_at_ms < 0:
        raise ValueError("validated_at_ms must be a non-negative integer")
    return PublishedHour(hour_start_ms, file_name, row_count, validated_at_ms)


def _is_busy(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(error).lower()
    return "busy" in message or "locked" in message


class SQLiteSpool:
    """SQLite-backed minute rows and the marker-only reader index.

    WebSocket frames, order books, and five-second samples never enter this
    class; the only persisted payload is one canonical minute JSON document.
    Minute rows are retained for operator-controlled cleanup: late rows for a
    marked hour must remain available without rewriting its immutable Parquet.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.database_path = self.root / "spool" / "collector.sqlite3"
        self._lock = OutputDirectoryLock(self.root)
        self._lock_acquired = False
        self._operation_lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._duplicate_count = 0
        self._conflict_count = 0
        self._lock.__enter__()
        self._lock_acquired = True
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                str(self.database_path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            journal_mode = self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if not journal_mode or str(journal_mode[0]).lower() != "wal":
                raise StorageWriteError(
                    "SQLite spool requires WAL journal mode "
                    f"(got {journal_mode[0] if journal_mode else None!r})"
                )
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS minute_rows (
                    minute_ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    PRIMARY KEY (minute_ts_ms, symbol)
                );
                CREATE TABLE IF NOT EXISTS published_hours (
                    hour_start_ms INTEGER PRIMARY KEY,
                    file_name TEXT NOT NULL UNIQUE,
                    row_count INTEGER NOT NULL,
                    validated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS symbol_events (
                    event_ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    config_revision TEXT NOT NULL,
                    PRIMARY KEY (event_ts_ms, symbol, event_type, config_revision)
                );
                CREATE TABLE IF NOT EXISTS late_rows_pending (
                    hour_start_ms INTEGER NOT NULL,
                    minute_ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    PRIMARY KEY (hour_start_ms, minute_ts_ms, symbol)
                );
                """
            )
        except Exception as exc:
            self.close()
            raise StorageWriteError(f"SQLite spool initialization failed: {exc}") from exc

    @classmethod
    def open_read_only(cls, root: Path) -> SQLiteSpool:
        """Open the spool without taking the writer's output-directory lock."""

        instance = cls.__new__(cls)
        instance.root = Path(root)
        instance.database_path = instance.root / "spool" / "collector.sqlite3"
        instance._lock = OutputDirectoryLock(instance.root)
        instance._lock_acquired = False
        instance._operation_lock = threading.RLock()
        instance._connection = None
        instance._closed = False
        instance._duplicate_count = 0
        instance._conflict_count = 0
        if not instance.database_path.is_file():
            instance.close()
            raise StorageWriteError(f"SQLite spool is missing: {instance.database_path}")
        try:
            instance._connection = sqlite3.connect(
                str(instance.database_path),
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            instance._connection.execute("PRAGMA busy_timeout=5000")
            instance._connection.execute("PRAGMA query_only=ON")
        except Exception as exc:
            instance.close()
            raise StorageWriteError(f"SQLite read-only spool initialization failed: {exc}") from exc
        return instance

    @property
    def connection(self) -> sqlite3.Connection:
        return self._require_connection()

    @property
    def duplicate_count(self) -> int:
        return self._duplicate_count

    @property
    def conflict_count(self) -> int:
        return self._conflict_count

    @property
    def counters(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                "duplicate_count": self._duplicate_count,
                "conflict_count": self._conflict_count,
            }
        )

    def write_minute(self, row: Mapping[str, Any]) -> WriteOutcome:
        _normalized, payload = _canonical_row(row)
        minute_ts_ms = row["minute_ts_ms"]
        symbol = row["symbol"]
        outcome = self._write_with_retries(
            lambda: self._write_minute_once(payload, minute_ts_ms, symbol)
        )
        with self._operation_lock:
            if outcome is WriteOutcome.DUPLICATE:
                self._duplicate_count += 1
            elif outcome is WriteOutcome.CONFLICT:
                self._conflict_count += 1
        return outcome

    def _write_minute_once(
        self, payload: str, minute_ts_ms: int, symbol: str
    ) -> WriteOutcome:
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT row_json FROM minute_rows WHERE minute_ts_ms = ? AND symbol = ?",
                (minute_ts_ms, symbol),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO minute_rows (minute_ts_ms, symbol, row_json) VALUES (?, ?, ?)",
                    (minute_ts_ms, symbol, payload),
                )
                hour_start_ms = (minute_ts_ms // 3_600_000) * 3_600_000
                connection.execute(
                    "INSERT OR IGNORE INTO late_rows_pending (hour_start_ms, minute_ts_ms, symbol) "
                    "SELECT ?, ?, ? WHERE EXISTS "
                    "(SELECT 1 FROM published_hours WHERE hour_start_ms = ?)",
                    (hour_start_ms, minute_ts_ms, symbol, hour_start_ms),
                )
                connection.commit()
                return WriteOutcome.INSERTED
            connection.commit()
            return (
                WriteOutcome.DUPLICATE
                if existing[0] == payload
                else WriteOutcome.CONFLICT
            )
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise

    def read_hour(self, hour_start_ms: int) -> list[dict[str, Any]]:
        if type(hour_start_ms) is not int:
            raise ValueError("hour_start_ms must be an integer")
        rows = self._read(
            "SELECT row_json FROM minute_rows "
            "WHERE minute_ts_ms >= ? AND minute_ts_ms < ? "
            "ORDER BY minute_ts_ms, symbol",
            (hour_start_ms, hour_start_ms + 3_600_000),
        )
        result: list[dict[str, Any]] = []
        for (payload,) in rows:
            try:
                decoded = json.loads(payload)
                normalized, _payload = _canonical_row(decoded)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StorageWriteError("stored minute row is not canonical JSON") from exc
            result.append(normalized)
        return result

    def count_rows(self, hour_start_ms: int | None = None, symbol: str | None = None) -> int:
        if hour_start_ms is not None and type(hour_start_ms) is not int:
            raise ValueError("hour_start_ms must be an integer")
        if symbol is not None and (not isinstance(symbol, str) or not symbol):
            raise ValueError("symbol must be a non-empty string")
        if hour_start_ms is None:
            query = "SELECT COUNT(*) FROM minute_rows"
            params: tuple[Any, ...] = ()
        else:
            query = (
                "SELECT COUNT(*) FROM minute_rows "
                "WHERE minute_ts_ms >= ? AND minute_ts_ms < ?"
            )
            params = (hour_start_ms, hour_start_ms + 3_600_000)
        if symbol is not None:
            query += " AND symbol = ?" if "WHERE" in query else " WHERE symbol = ?"
            params += (symbol,)
        rows = self._read(query, params)
        return int(rows[0][0])

    def mark_published(
        self, hour_start_ms: int, file_name: str, row_count: int, validated_at_ms: int
    ) -> MarkerOutcome:
        """Record a first-winner marker; revalidation keeps its first timestamp."""
        marker = _canonical_marker(hour_start_ms, file_name, row_count, validated_at_ms)
        return self._write_with_retries(lambda: self._mark_published_once(marker))

    def _mark_published_once(self, marker: PublishedHour) -> MarkerOutcome:
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            by_hour = connection.execute(
                "SELECT file_name, row_count, validated_at_ms FROM published_hours "
                "WHERE hour_start_ms = ?",
                (marker.hour_start_ms,),
            ).fetchone()
            by_file = connection.execute(
                "SELECT hour_start_ms FROM published_hours WHERE file_name = ?",
                (marker.file_name,),
            ).fetchone()
            if by_hour is not None:
                if by_hour[:2] == (marker.file_name, marker.row_count):
                    connection.commit()
                    return MarkerOutcome.EXISTING
                raise MarkerConflictError("published marker conflict for hour")
            if by_file is not None:
                raise MarkerConflictError("published marker conflict for file")
            connection.execute(
                "INSERT INTO published_hours "
                "(hour_start_ms, file_name, row_count, validated_at_ms) VALUES (?, ?, ?, ?)",
                (
                    marker.hour_start_ms,
                    marker.file_name,
                    marker.row_count,
                    marker.validated_at_ms,
                ),
            )
            connection.commit()
            return MarkerOutcome.INSERTED
        except MarkerConflictError:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise

    def published_hours(self) -> tuple[PublishedHour, ...]:
        rows = self._read(
            "SELECT hour_start_ms, file_name, row_count, validated_at_ms "
            "FROM published_hours ORDER BY hour_start_ms"
        )
        return tuple(PublishedHour(*row) for row in rows)

    def clear_published_hour(self, hour_start_ms: int) -> None:
        if type(hour_start_ms) is not int or hour_start_ms < 0:
            raise ValueError("hour_start_ms must be a non-negative integer")

        def delete() -> None:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM published_hours WHERE hour_start_ms = ?", (hour_start_ms,)
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        self._write_with_retries(delete)

    def distinct_hour_starts(self, since_ms: int | None = None) -> tuple[int, ...]:
        if since_ms is not None and (type(since_ms) is not int or since_ms < 0):
            raise ValueError("since_ms must be a non-negative integer")
        if since_ms is None:
            rows = self._read(
                "SELECT DISTINCT (minute_ts_ms / 3600000) * 3600000 "
                "FROM minute_rows ORDER BY 1"
            )
        else:
            rows = self._read(
                "SELECT DISTINCT (minute_ts_ms / 3600000) * 3600000 "
                "FROM minute_rows WHERE minute_ts_ms >= ? ORDER BY 1",
                (since_ms,),
            )
        return tuple(int(row[0]) for row in rows)

    def reader_files(self) -> tuple[str, ...]:
        return tuple(marker.file_name for marker in self.published_hours())

    def record_symbol_event(
        self,
        event_ts_ms: int,
        symbol: str,
        event_type: str,
        reason: str,
        config_revision: str,
    ) -> None:
        if type(event_ts_ms) is not int or event_ts_ms < 0:
            raise ValueError("event_ts_ms must be a non-negative integer")
        if any(not isinstance(value, str) or not value for value in (symbol, event_type, reason, config_revision)):
            raise ValueError("symbol event text fields must be non-empty strings")
        def write() -> None:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT OR IGNORE INTO symbol_events "
                    "(event_ts_ms, symbol, event_type, reason, config_revision) VALUES (?, ?, ?, ?, ?)",
                    (event_ts_ms, symbol, event_type, reason, config_revision),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._write_with_retries(write)

    def symbol_events(self) -> tuple[SymbolEvent, ...]:
        rows = self._read(
            "SELECT event_ts_ms, symbol, event_type, reason, config_revision "
            "FROM symbol_events ORDER BY event_ts_ms, symbol, event_type"
        )
        return tuple(SymbolEvent(*row) for row in rows)

    def late_rows_pending(self) -> int:
        rows = self._read("SELECT COUNT(*) FROM late_rows_pending")
        return int(rows[0][0])

    def _read(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._operation_lock:
            connection = self._require_connection()
            try:
                return connection.execute(query, params).fetchall()
            except Exception as exc:
                raise StorageWriteError(f"SQLite read failed: {exc}") from exc

    def _write_with_retries(self, operation: Callable[[], _T]) -> _T:
        delays = (0.1, 0.5)
        with self._operation_lock:
            for attempt in range(3):
                try:
                    return operation()
                except MarkerConflictError:
                    raise
                except Exception as exc:
                    if _is_busy(exc) and attempt < 2:
                        time.sleep(delays[attempt])
                        continue
                    if _is_busy(exc):
                        raise StorageWriteError(
                            f"SQLite write failed after 3 attempts: {exc}"
                        ) from exc
                    raise StorageWriteError(f"SQLite write failed: {exc}") from exc
        raise AssertionError("unreachable")

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise StorageWriteError("SQLite spool is closed")
        return self._connection

    def close(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            connection = self._connection
            self._connection = None
            try:
                if connection is not None:
                    connection.close()
            finally:
                if self._lock_acquired:
                    try:
                        self._lock.__exit__(None, None, None)
                    finally:
                        self._lock_acquired = False

    def __enter__(self) -> SQLiteSpool:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "MarkerConflictError",
    "MarkerOutcome",
    "PublishedHour",
    "SymbolEvent",
    "SQLiteSpool",
    "StorageWriteError",
    "WriteOutcome",
]
