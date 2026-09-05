"""Atomic health snapshot for operators."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from collections.abc import Mapping
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any


WARNING_FREE_BYTES = 10 * 1024**3
CRITICAL_FREE_BYTES = 2 * 1024**3
DATA_STARTUP_GRACE_MS = 60_000
DATA_ERROR_AFTER_MS = 5 * 60_000


class HealthMonitor:
    def __init__(
        self,
        root: Path,
        *,
        collector_version: str = "0.7.0",
        started_at_ms: int | None = None,
    ) -> None:
        self.root = Path(root)
        self.path = self.root / "status" / "health.json"
        self.collector_version = collector_version
        self.started_at_ms = int(time.time() * 1000) if started_at_ms is None else started_at_ms

    def update(
        self,
        now_ms: int,
        *,
        connected: bool,
        pending_rows: int = 0,
        late_rows: int = 0,
        errors: tuple[str, ...] = (),
        config_revision: str = "",
        configured_symbols: tuple[str, ...] = (),
        active_symbols: tuple[str, ...] = (),
        last_completed_minute_ms: int | None = None,
        last_exported_date: str | None = None,
        book_diagnostics: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        usage = shutil.disk_usage(self.root)
        free_bytes = int(getattr(usage, "free", usage[2]))
        diagnostics, data_degraded, data_error, data_errors = _data_health(
            now_ms,
            self.started_at_ms,
            connected=connected,
            active_symbols=active_symbols,
            book_diagnostics=book_diagnostics,
        )
        status = (
            "CRITICAL"
            if free_bytes < CRITICAL_FREE_BYTES
            else "ERROR"
            if data_error
            else "WARNING"
            if free_bytes < WARNING_FREE_BYTES
            else "DEGRADED"
            if late_rows or errors or data_degraded
            else "OK"
        )
        all_errors = list(errors) + data_errors
        spool_bytes = sum(path.stat().st_size for path in (self.root / "spool").glob("*") if path.is_file()) if (self.root / "spool").exists() else 0
        snapshot: dict[str, Any] = {
            "updated_at_ms": int(now_ms),
            "collector_version": self.collector_version,
            "started_at_utc": _utc_iso(self.started_at_ms),
            "updated_at_utc": _utc_iso(now_ms),
            "status": status,
            "config_revision": config_revision,
            "configured_symbols": list(configured_symbols),
            "active_symbols": list(active_symbols),
            "last_completed_minute_ms": last_completed_minute_ms,
            "last_exported_date": last_exported_date,
            "connected": bool(connected),
            "data_health": diagnostics,
            "free_bytes": free_bytes,
            "free_disk_bytes": free_bytes,
            "spool_bytes": spool_bytes,
            "pending_rows": int(pending_rows),
            "late_rows": int(late_rows),
            "errors": all_errors,
            "data_errors": data_errors,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent, prefix="health-", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(snapshot, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
        try:
            import os

            descriptor = os.open(temporary, os.O_RDWR)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return snapshot


def _utc_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _optional_utc(value: Any) -> str | None:
    return _utc_iso(int(value)) if isinstance(value, int) and value >= 0 else None


def _data_health(
    now_ms: int,
    started_at_ms: int,
    *,
    connected: bool,
    active_symbols: tuple[str, ...],
    book_diagnostics: Mapping[str, Mapping[str, Any]] | None,
) -> tuple[dict[str, dict[str, Any]], bool, bool, list[str]]:
    if not book_diagnostics or not active_symbols:
        return {}, False, False, []
    diagnostics: dict[str, dict[str, Any]] = {}
    degraded = False
    error = False
    errors: list[str] = []
    grace_elapsed = now_ms - started_at_ms >= DATA_STARTUP_GRACE_MS
    error_elapsed = now_ms - started_at_ms >= DATA_ERROR_AFTER_MS
    for symbol in active_symbols:
        source = book_diagnostics.get(symbol, {})
        synchronized = bool(source.get("book_synchronized", False))
        last_book = source.get("last_book_update_ms")
        last_valid = source.get("last_valid_sample_ms")
        valid_recent = int(source.get("valid_sample_count_recent", 0))
        coverage_recent = float(source.get("coverage_recent", 0.0))
        diagnostics[symbol] = {
            "book_synchronized": synchronized,
            "last_book_update_utc": _optional_utc(last_book),
            "last_valid_sample_utc": _optional_utc(last_valid),
            "valid_sample_count_recent": valid_recent,
            "coverage_recent": coverage_recent,
        }
        if not grace_elapsed:
            continue
        symbol_degraded = not connected or not synchronized or last_valid is None
        last_valid_age = now_ms - last_valid if isinstance(last_valid, int) else None
        symbol_error = (
            error_elapsed
            and (last_valid_age is None or last_valid_age >= DATA_ERROR_AFTER_MS)
        )
        if symbol_degraded:
            degraded = True
            errors.append(f"{symbol}: no synchronized valid samples")
        if symbol_error:
            error = True
    return diagnostics, degraded, error, errors


__all__ = [
    "CRITICAL_FREE_BYTES",
    "DATA_ERROR_AFTER_MS",
    "DATA_STARTUP_GRACE_MS",
    "HealthMonitor",
    "WARNING_FREE_BYTES",
]
