from __future__ import annotations

import json
from pathlib import Path

from mrs3.bybit_collector.health import HealthMonitor


def test_health_writes_atomic_json_and_applies_disk_thresholds(tmp_path: Path, monkeypatch) -> None:
    monitor = HealthMonitor(tmp_path)
    monkeypatch.setattr("mrs3.bybit_collector.health.shutil.disk_usage", lambda _: (0, 0, 1_000_000_000))
    snapshot = monitor.update(123, connected=False, pending_rows=4, late_rows=2)
    assert snapshot["status"] == "CRITICAL"
    assert snapshot["connected"] is False
    assert snapshot["pending_rows"] == 4
    path = tmp_path / "status" / "health.json"
    assert json.loads(path.read_text(encoding="utf-8")) == snapshot
    assert not list(path.parent.glob("*.tmp"))


def test_health_exposes_operator_fields_and_degrades_on_data_errors(tmp_path: Path, monkeypatch) -> None:
    monitor = HealthMonitor(tmp_path, collector_version="0.7.0", started_at_ms=0)
    monkeypatch.setattr("mrs3.bybit_collector.health.shutil.disk_usage", lambda _: (0, 0, 20 * 1024**3))
    snapshot = monitor.update(
        60_000,
        connected=True,
        pending_rows=1,
        late_rows=2,
        errors=("conflict",),
        config_revision="abc",
        configured_symbols=("BTCUSDT",),
        active_symbols=("BTCUSDT",),
        last_completed_minute_ms=0,
        last_exported_date="2026-09-05",
    )
    assert snapshot["status"] == "DEGRADED"
    assert snapshot["collector_version"] == "0.7.0"
    assert snapshot["started_at_utc"].endswith("Z")
    assert snapshot["updated_at_utc"].endswith("Z")
    assert snapshot["config_revision"] == "abc"
    assert snapshot["configured_symbols"] == ["BTCUSDT"]
    assert snapshot["active_symbols"] == ["BTCUSDT"]
    assert snapshot["last_completed_minute_ms"] == 0
    assert snapshot["last_exported_date"] == "2026-09-05"
    assert snapshot["free_disk_bytes"] == 20 * 1024**3
