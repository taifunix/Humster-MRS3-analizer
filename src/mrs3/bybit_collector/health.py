"""Atomic health snapshot for operators."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any


WARNING_FREE_BYTES = 10 * 1024**3
CRITICAL_FREE_BYTES = 2 * 1024**3


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
    ) -> dict[str, Any]:
        usage = shutil.disk_usage(self.root)
        free_bytes = int(getattr(usage, "free", usage[2]))
        status = (
            "CRITICAL"
            if free_bytes < CRITICAL_FREE_BYTES
            else "WARNING"
            if free_bytes < WARNING_FREE_BYTES
            else "DEGRADED"
            if late_rows or errors
            else "OK"
        )
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
            "free_bytes": free_bytes,
            "free_disk_bytes": free_bytes,
            "spool_bytes": spool_bytes,
            "pending_rows": int(pending_rows),
            "late_rows": int(late_rows),
            "errors": list(errors),
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


__all__ = ["CRITICAL_FREE_BYTES", "HealthMonitor", "WARNING_FREE_BYTES"]
