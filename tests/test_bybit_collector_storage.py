from __future__ import annotations

import json
import sqlite3
import gc
import threading
from pathlib import Path

import pytest

from mrs3.bybit_collector.aggregation import LIQUIDITY_1M_COLUMNS
from mrs3.bybit_collector.storage import (
    MarkerConflictError,
    MarkerOutcome,
    SQLiteSpool,
    StorageWriteError,
    WriteOutcome,
)
import mrs3.bybit_collector.storage as storage_module
from mrs3.locking import OutputDirectoryBusyError, OutputDirectoryLock


def row(minute_ts_ms: int = 0, symbol: str = "BTCUSDT", **changes: object) -> dict:
    result = {
        "minute_ts_ms": minute_ts_ms,
        "symbol": symbol,
        "sample_count": 12,
        "valid_sample_count": 10,
        "coverage_ratio": 10 / 12,
        "book_reset_count": 1,
        "ws_connected_ratio": 1.0,
        "active_sample_target": 12,
        "mid_median": 100.5,
        "spread_bps_median": 1.0,
        "spread_bps_p95": 1.5,
        "spread_bps_max": 2.0,
    }
    result.update({name: 10.0 for name in LIQUIDITY_1M_COLUMNS[12:]})
    result.update(changes)
    assert tuple(result) == LIQUIDITY_1M_COLUMNS
    return result


def test_creates_wal_spool_schema_and_keeps_only_minute_and_marker_tables(tmp_path: Path) -> None:
    spool = SQLiteSpool(tmp_path)
    try:
        assert spool.database_path == tmp_path / "spool" / "collector.sqlite3"
        assert spool.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert spool.connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert spool.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        tables = {
            name
            for (name,) in spool.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == {"minute_rows", "published_hours", "symbol_events", "late_rows_pending"}
        columns = [
            name
            for (_cid, name, *_rest) in spool.connection.execute(
                "PRAGMA table_info(minute_rows)"
            )
        ]
        assert columns == ["minute_ts_ms", "symbol", "row_json"]
    finally:
        spool.close()


def test_minute_write_is_first_winner_and_counts_duplicate_and_conflict(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        first = row(60_000)
        assert spool.write_minute(first) == WriteOutcome.INSERTED
        assert spool.write_minute(dict(reversed(tuple(first.items())))) == WriteOutcome.DUPLICATE
        assert spool.write_minute({**first, "mid_median": 999.0}) == WriteOutcome.CONFLICT
        assert spool.duplicate_count == 1
        assert spool.conflict_count == 1
        assert spool.count_rows() == 1
        assert spool.read_hour(0) == [first]


@pytest.mark.parametrize(
    "bad",
    [
        {"minute_ts_ms": 0},
        {**row(), "coverage_ratio": float("nan")},
        {**row(), "mid_median": float("inf")},
        {**row(), "sample_count": True},
        {**row(), "unknown": 1},
    ],
)
def test_write_rejects_noncanonical_or_nonfinite_rows(tmp_path: Path, bad: dict) -> None:
    with SQLiteSpool(tmp_path) as spool:
        with pytest.raises(ValueError, match="canonical|finite|JSON|integer"):
            spool.write_minute(bad)
        assert spool.count_rows() == 0


def test_read_hour_uses_half_open_utc_hour_and_canonical_key_order(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(row(0))
        spool.write_minute(row(3_599_999, "ETHUSDT"))
        spool.write_minute(row(3_600_000))
        rows = spool.read_hour(0)
        assert [(item["minute_ts_ms"], item["symbol"]) for item in rows] == [
            (0, "BTCUSDT"),
            (3_599_999, "ETHUSDT"),
        ]
        assert all(tuple(item) == LIQUIDITY_1M_COLUMNS for item in rows)
        assert spool.count_rows(0) == 2


def test_reopen_recovers_wal_rows_and_markers(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(row(60_000))
        spool.mark_published(0, "liquidity_1m/date=1970-01-01/part-00.parquet", 1, 123)
    with SQLiteSpool(tmp_path) as spool:
        assert spool.read_hour(0) == [row(60_000)]
        assert spool.reader_files() == ("liquidity_1m/date=1970-01-01/part-00.parquet",)
        assert spool.published_hours()[0].row_count == 1


def test_read_only_spool_can_verify_while_writer_holds_root_lock(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as writer:
        writer.write_minute(row(60_000))
        reader = SQLiteSpool.open_read_only(tmp_path)
        try:
            assert reader.read_hour(0) == [row(60_000)]
            assert reader.reader_files() == ()
            with pytest.raises(sqlite3.OperationalError, match="read-only|readonly"):
                reader.connection.execute("CREATE TABLE should_not_write (value INTEGER)")
        finally:
            reader.close()
        with pytest.raises(OutputDirectoryBusyError):
            SQLiteSpool(tmp_path)


def test_marker_is_idempotent_and_conflicts_do_not_overwrite(tmp_path: Path) -> None:
    marker = "liquidity_1m/date=1970-01-01/part-00.parquet"
    with SQLiteSpool(tmp_path) as spool:
        assert spool.mark_published(0, marker, 1, 123) == MarkerOutcome.INSERTED
        assert spool.mark_published(0, marker, 1, 123) == MarkerOutcome.EXISTING
        with pytest.raises(MarkerConflictError, match="conflict"):
            spool.mark_published(0, "other.parquet", 1, 123)
        with pytest.raises(MarkerConflictError, match="conflict"):
            spool.mark_published(3_600_000, marker, 1, 123)
        assert spool.published_hours()[0].file_name == marker
        assert spool.published_hours()[0].row_count == 1


def test_reader_files_are_marker_only_and_immutable(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.mark_published(0, "published.parquet", 0, 123)
        files = spool.reader_files()
        assert files == ("published.parquet",)
        with pytest.raises(TypeError):
            files[0] = "other.parquet"  # type: ignore[index]


def test_public_hour_helpers_clear_markers_and_list_spool_hours(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(row(0))
        spool.write_minute(row(3_600_000))
        spool.mark_published(0, "liquidity_1m/date=1970-01-01/part-00.parquet", 1, 123)

        assert spool.distinct_hour_starts() == (0, 3_600_000)
        spool.clear_published_hour(0)

        assert spool.published_hours() == ()


def test_spool_reuses_existing_output_directory_lock(tmp_path: Path) -> None:
    with OutputDirectoryLock(tmp_path):
        with pytest.raises(OutputDirectoryBusyError):
            SQLiteSpool(tmp_path)


def test_failed_spool_initialization_does_not_release_peer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_lock = OutputDirectoryLock(tmp_path)
    original_lock.__enter__()
    failed_lock_exit_calls: list[object] = []
    original_enter = OutputDirectoryLock.__enter__
    original_exit = OutputDirectoryLock.__exit__

    def fail_after_peer_lock(self: OutputDirectoryLock):
        if self is original_lock:
            return original_enter(self)
        raise OutputDirectoryBusyError("peer lock is held")

    def track_exit(self: OutputDirectoryLock, *args: object) -> None:
        if self is not original_lock:
            failed_lock_exit_calls.append(self)
        original_exit(self, *args)

    monkeypatch.setattr(OutputDirectoryLock, "__enter__", fail_after_peer_lock)
    monkeypatch.setattr(OutputDirectoryLock, "__exit__", track_exit)
    try:
        with pytest.raises(OutputDirectoryBusyError):
            SQLiteSpool(tmp_path)
        gc.collect()
        assert failed_lock_exit_calls == []
        with pytest.raises(OutputDirectoryBusyError):
            SQLiteSpool(tmp_path)
    finally:
        original_lock.__exit__(None, None, None)


def test_initialization_rejects_connection_that_did_not_enable_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect

    class Result:
        def fetchone(self):
            return ("delete",)

    class NonWalConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, *params: object):
            if sql == "PRAGMA journal_mode=WAL":
                return Result()
            return self._connection.execute(sql, *params)

        def executescript(self, script: str):
            return self._connection.executescript(script)

        def close(self) -> None:
            self._connection.close()

    def fake_connect(*args: object, **kwargs: object) -> NonWalConnection:
        return NonWalConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(storage_module.sqlite3, "connect", fake_connect)
    with pytest.raises(StorageWriteError, match="WAL"):
        SQLiteSpool(tmp_path)


def test_marker_revalidation_keeps_first_validation_timestamp(tmp_path: Path) -> None:
    marker = "liquidity_1m/date=1970-01-01/part-00.parquet"
    with SQLiteSpool(tmp_path) as spool:
        assert spool.mark_published(0, marker, 1, 123) == MarkerOutcome.INSERTED
        assert spool.mark_published(0, marker, 1, 456) == MarkerOutcome.EXISTING
        assert spool.published_hours()[0].validated_at_ms == 123


def test_spool_supports_writes_from_worker_thread(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        result: list[WriteOutcome] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                result.append(spool.write_minute(row(60_000)))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert errors == []
        assert result == [WriteOutcome.INSERTED]


def test_real_sqlite_busy_write_retries_after_peer_releases_lock(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        peer = sqlite3.connect(
            str(spool.database_path), timeout=0.0, isolation_level=None
        )
        try:
            peer.execute("BEGIN IMMEDIATE")
            sleeps: list[float] = []

            def release_peer(delay: float) -> None:
                sleeps.append(delay)
                peer.rollback()

            original_sleep = storage_module.time.sleep
            storage_module.time.sleep = release_peer
            try:
                assert spool.write_minute(row()) == WriteOutcome.INSERTED
            finally:
                storage_module.time.sleep = original_sleep
            assert sleeps == [0.1]
        finally:
            peer.close()
    with SQLiteSpool(tmp_path):
        with pytest.raises(OutputDirectoryBusyError):
            SQLiteSpool(tmp_path)


def test_busy_writes_retry_twice_then_succeed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteSpool(tmp_path) as spool:
        attempts = 0
        original = spool._write_minute_once

        def flaky(payload: str, minute_ts_ms: int, symbol: str):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise sqlite3.OperationalError("database is locked")
            return original(payload, minute_ts_ms, symbol)

        delays: list[float] = []
        monkeypatch.setattr(spool, "_write_minute_once", flaky)
        monkeypatch.setattr("mrs3.bybit_collector.storage.time.sleep", delays.append)
        assert spool.write_minute(row()) == WriteOutcome.INSERTED
        assert attempts == 3
        assert delays == [0.1, 0.5]


def test_busy_write_exhaustion_is_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteSpool(tmp_path) as spool:
        attempts = 0

        def blocked(payload: str, minute_ts_ms: int, symbol: str):
            nonlocal attempts
            attempts += 1
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(spool, "_write_minute_once", blocked)
        monkeypatch.setattr("mrs3.bybit_collector.storage.time.sleep", lambda _: None)
        with pytest.raises(StorageWriteError, match="3 attempts"):
            spool.write_minute(row())
        assert attempts == 3


@pytest.mark.parametrize("message", ["database disk image is malformed", "attempt to write a readonly database", "database or disk is full"])
def test_fatal_sqlite_write_errors_are_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    with SQLiteSpool(tmp_path) as spool:
        attempts = 0

        def broken(payload: str, minute_ts_ms: int, symbol: str):
            nonlocal attempts
            attempts += 1
            raise sqlite3.OperationalError(message)

        monkeypatch.setattr(spool, "_write_minute_once", broken)
        with pytest.raises(StorageWriteError, match="SQLite write failed"):
            spool.write_minute(row())
        assert attempts == 1


def test_json_on_disk_uses_canonical_column_order(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(row())
        payload = spool.connection.execute("SELECT row_json FROM minute_rows").fetchone()[0]
    assert tuple(json.loads(payload)) == LIQUIDITY_1M_COLUMNS
