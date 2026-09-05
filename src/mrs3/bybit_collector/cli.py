"""Standalone command line entry point for the Bybit collector."""

from __future__ import annotations

import argparse
from collections import deque
import json
import logging
from pathlib import Path
import sys
import threading
import time

from .archive import HourlyExporter
from .config import ConfigError, ConfigManager
from .health import HealthMonitor
from .reference import ReferenceDataCollector
from .runtime import CollectorRuntime
from .storage import SQLiteSpool
from .websocket import BybitWebSocketSession


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bybit-market-collector")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "validate-config", "health", "verify-archive"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manager = ConfigManager(args.config)
        config = manager.active
        logging.basicConfig(level=getattr(logging, config.logging_level))
    except ConfigError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    if args.command == "validate-config":
        print(json.dumps({"config_revision": config.config_revision, "symbols": config.symbols}))
        return 0
    if args.command == "health":
        health_path = config.storage_root / "status" / "health.json"
        if not health_path.is_file():
            print(json.dumps({"status": "UNKNOWN", "error": "health file is missing"}))
            return 1
        print(health_path.read_text(encoding="utf-8"))
        return 0
    if args.command == "verify-archive":
        with SQLiteSpool.open_read_only(config.storage_root) as spool:
            exporter = HourlyExporter(spool, config.storage_root, collector_version="0.7.0")
            result = exporter.verify_archive()
            print(json.dumps({"valid": result.valid, "errors": result.errors}))
            return 0 if result.valid else 1
    exit_code = 0
    with SQLiteSpool(config.storage_root) as spool:
        exporter = HourlyExporter(spool, config.storage_root, collector_version="0.7.0")
        reference = ReferenceDataCollector(config.storage_root)

        def collect_reference(symbols: tuple[str, ...], now_ms: int) -> None:
            snapshot = reference.collect(symbols, captured_at_ms=now_ms)
            for event in snapshot.symbol_events:
                spool.record_symbol_event(
                    now_ms, event.symbol, event.event_type, event.reason, runtime.config.config_revision
                )

        runtime = CollectorRuntime(
            config,
            spool=spool,
            exporter=exporter,
            reference_callback=collect_reference,
        )
        monitor = HealthMonitor(config.storage_root, collector_version="0.7.0")
        stop = threading.Event()
        session = BybitWebSocketSession(config.symbols, _default_transport)
        thread = threading.Thread(
            target=session.run_forever,
            args=(runtime.handle_ws_message,),
            kwargs={
                "stop": stop.is_set,
                "on_connect": lambda: runtime.set_connected(True),
                "on_disconnect": lambda: runtime.set_connected(False),
            },
            daemon=True,
        )
        thread.start()
        try:
            last_health = 0
            last_reload = 0
            runtime_errors: deque[str] = deque(maxlen=100)
            while not stop.is_set():
                now_ms = int(time.time() * 1000)
                try:
                    poll = runtime.poll(now_ms, time.monotonic_ns() // 1_000_000)
                    runtime_errors.extend(poll.errors)
                except Exception as exc:
                    runtime_errors.append(f"poll: {exc}")
                    poll = None
                if now_ms - last_reload >= 30_000:
                    try:
                        previous = manager.active
                        reloaded = manager.reload()
                        if reloaded.accepted:
                            if reloaded.restart_required:
                                reason = "storage root changed; restart required"
                                runtime_errors.append(reason)
                                print(json.dumps({"error": reason}), file=sys.stderr)
                                try:
                                    monitor.update(
                                        now_ms,
                                        connected=runtime.connected,
                                        pending_rows=spool.count_rows(),
                                        late_rows=spool.late_rows_pending(),
                                        errors=tuple(runtime_errors),
                                        config_revision=runtime.config.config_revision,
                                        configured_symbols=runtime.config.symbols,
                                        active_symbols=tuple(runtime.books),
                                        last_completed_minute_ms=runtime.last_completed_minute_ms,
                                        last_exported_date=_last_exported_date(spool),
                                    )
                                except Exception as exc:
                                    print(json.dumps({"error": f"health: {exc}"}), file=sys.stderr)
                                exit_code = 3
                                stop.set()
                            else:
                                try:
                                    if reloaded.added_symbols or reloaded.removed_symbols:
                                        runtime.update_config(reloaded.config, now_ms)
                                        session.update_symbols(reloaded.config.symbols)
                                        session.request_reconnect()
                                    if reloaded.logging_level_changed:
                                        logging.getLogger().setLevel(
                                            getattr(logging, reloaded.config.logging_level)
                                        )
                                except Exception:
                                    manager.restore(previous)
                                    raise
                    except Exception as exc:
                        runtime_errors.append(f"config reload: {exc}")
                    last_reload = now_ms
                if now_ms - last_health >= 60_000:
                    try:
                        monitor.update(
                            now_ms,
                            connected=runtime.connected,
                            pending_rows=spool.count_rows(),
                            late_rows=spool.late_rows_pending(),
                            errors=tuple(runtime_errors),
                            config_revision=runtime.config.config_revision,
                            configured_symbols=runtime.config.symbols,
                            active_symbols=tuple(runtime.books),
                            last_completed_minute_ms=runtime.last_completed_minute_ms,
                            last_exported_date=_last_exported_date(spool),
                        )
                        runtime_errors.clear()
                    except Exception as exc:
                        runtime_errors.append(f"health: {exc}")
                    last_health = now_ms
                wait_ms = (
                    poll.scheduler.next_boundary_monotonic_ms - time.monotonic_ns() // 1_000_000
                    if poll is not None
                    else 1_000
                )
                stop.wait(max(0.05, min(1.0, wait_ms / 1000)))
        except KeyboardInterrupt:
            return 0
        finally:
            stop.set()
            thread.join(timeout=5)
            try:
                runtime.flush(int(time.time() * 1000))
            except Exception as exc:
                print(json.dumps({"error": f"flush: {exc}"}, ensure_ascii=False))
    return exit_code


def _default_transport() -> object:
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("run requires the optional websocket-client package") from exc
    connection = websocket.create_connection(
        "wss://stream.bybit.com/v5/public/linear", timeout=10
    )

    class Transport:
        def send(self, payload: str) -> None:
            connection.send(payload)

        def recv(self, timeout: float = 1) -> str | None:
            connection.settimeout(timeout)
            try:
                return connection.recv()
            except Exception as error:
                if error.__class__.__name__ == "WebSocketTimeoutException":
                    return None
                raise

        def close(self) -> None:
            connection.close()

    return Transport()


def _last_exported_date(spool: SQLiteSpool) -> str | None:
    dates = [
        Path(marker.file_name).parent.name.removeprefix("date=")
        for marker in spool.published_hours()
        if Path(marker.file_name).parent.name.startswith("date=")
    ]
    return max(dates) if dates else None


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
