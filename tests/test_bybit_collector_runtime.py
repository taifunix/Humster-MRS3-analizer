from __future__ import annotations

from pathlib import Path
import threading

from mrs3.bybit_collector.archive import HourlyExporter
from mrs3.bybit_collector.config import CollectorConfig
from mrs3.bybit_collector.runtime import CollectorRuntime
from mrs3.bybit_collector.storage import SQLiteSpool


def _config(root: Path) -> CollectorConfig:
    return CollectorConfig(
        config_path=root / "collector.toml",
        storage_root=root,
        symbols=("BTCUSDT",),
        logging_level="INFO",
        config_revision="r1",
    )


def _snapshot(symbol: str = "BTCUSDT", update_id: int = 1) -> dict[str, object]:
    return {
        "topic": f"orderbook.1000.{symbol}",
        "type": "snapshot",
        "data": {
            "s": symbol,
            "u": update_id,
            "b": [["100", "2"], ["99", "1"]],
            "a": [["101", "2"], ["102", "1"]],
        },
    }


def test_runtime_wires_frames_scheduler_aggregation_spool_and_archive(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with SQLiteSpool(tmp_path) as spool:
        exporter = HourlyExporter(spool, tmp_path, collector_version="test")
        runtime = CollectorRuntime(config, spool=spool, exporter=exporter)
        assert runtime.handle_ws_message(_snapshot()) is True

        runtime.poll(0, 0)
        for wall_ms in range(5_000, 3_600_001, 5_000):
            runtime.poll(wall_ms, wall_ms)
        runtime.flush(3_720_000)

        assert spool.count_rows(0, "BTCUSDT") == 60
        outcome = exporter.export_hour(0, 3_720_000)
        assert outcome.published
        assert (tmp_path / exporter.file_name(0)).is_file()


def test_runtime_invalidates_all_books_on_connection_loss(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runtime = CollectorRuntime(config)
    runtime.set_connected(True)
    assert runtime.handle_ws_message(_snapshot())
    runtime.set_connected(False)
    assert not runtime.books["BTCUSDT"].valid
    assert runtime.handle_ws_message(_snapshot(update_id=2))


def test_runtime_tracks_book_and_valid_sample_diagnostics(tmp_path: Path) -> None:
    runtime = CollectorRuntime(_config(tmp_path))
    runtime.set_connected(True)
    assert runtime.handle_ws_message(_snapshot(), event_ts_ms=123)
    runtime.poll(0, 0)
    runtime.poll(5_000, 5_000)

    assert runtime.health_diagnostics(5_000) == {
        "BTCUSDT": {
            "book_synchronized": True,
            "last_book_update_ms": 123,
            "last_valid_sample_ms": 5_000,
            "valid_sample_count_recent": 1,
            "coverage_recent": 1.0,
        }
    }


def test_runtime_ignores_malformed_ws_frames(tmp_path: Path) -> None:
    runtime = CollectorRuntime(_config(tmp_path))

    assert runtime.handle_ws_message("not json") is False


def test_runtime_exports_eligible_hours_during_poll(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with SQLiteSpool(tmp_path) as spool:
        exporter = HourlyExporter(spool, tmp_path, collector_version="test")
        runtime = CollectorRuntime(config, spool=spool, exporter=exporter)
        assert runtime.handle_ws_message(_snapshot())
        runtime.poll(0, 0)
        for wall_ms in range(5_000, 3_600_001, 5_000):
            runtime.poll(wall_ms, wall_ms)
        runtime.poll(3_720_000, 3_720_000)

        assert (tmp_path / exporter.file_name(0)).is_file()


def test_runtime_invokes_daily_reference_callback(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], int]] = []
    done = threading.Event()

    runtime = CollectorRuntime(
        _config(tmp_path),
        reference_callback=lambda symbols, now_ms: (calls.append((symbols, now_ms)), done.set()),
    )

    runtime.poll(600_000, 0)
    assert done.wait(1)

    assert calls == [(('BTCUSDT',), 600_000)]


def test_runtime_invokes_reference_callback_at_startup(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], int]] = []
    done = threading.Event()

    runtime = CollectorRuntime(
        _config(tmp_path),
        reference_callback=lambda symbols, now_ms: (calls.append((symbols, now_ms)), done.set()),
    )
    runtime.poll(0, 0)
    assert done.wait(1)
    assert calls == [(('BTCUSDT',), 0)]


def test_reference_failure_is_backed_off(tmp_path: Path) -> None:
    attempts = 0
    done = threading.Event()

    def fail(_symbols: tuple[str, ...], _now_ms: int) -> None:
        nonlocal attempts
        attempts += 1
        done.set()
        raise RuntimeError("temporary")

    runtime = CollectorRuntime(_config(tmp_path), reference_callback=fail)
    runtime.poll(600_000, 0)
    assert done.wait(1)
    runtime.poll(601_000, 1_000)
    assert attempts == 1


def test_runtime_records_symbol_config_events(tmp_path: Path) -> None:
    config = _config(tmp_path)
    updated = CollectorConfig(
        config_path=config.config_path,
        storage_root=tmp_path,
        symbols=("ETHUSDT",),
        logging_level="INFO",
        config_revision="r2",
    )
    with SQLiteSpool(tmp_path) as spool:
        runtime = CollectorRuntime(config, spool=spool)
        runtime.update_config(updated, 123)
        assert [event.symbol for event in spool.symbol_events()] == ["BTCUSDT", "ETHUSDT"]


def test_runtime_refreshes_reference_for_added_symbol(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], int]] = []
    done = threading.Event()
    config = _config(tmp_path)
    updated = CollectorConfig(
        config_path=config.config_path,
        storage_root=tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
        logging_level="INFO",
        config_revision="r2",
    )
    runtime = CollectorRuntime(
        config,
        reference_callback=lambda symbols, now_ms: (calls.append((symbols, now_ms)), done.set()),
    )
    runtime.update_config(updated, 123)
    runtime.poll(123, 123)
    assert done.wait(1)
    assert calls == [(('ETHUSDT',), 123)]
