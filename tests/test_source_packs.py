from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import mrs3.source_packs as source_packs
from mrs3.source_packs import SourcePackError, build_csv_package, require_single_event_mode


WINDOW_START = "2026-07-15T00:00:00Z"
WINDOW_END = "2026-08-06T00:00:00Z"


def _write_csv(path: Path, rows: list[dict[str, object]]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_csv_package_keeps_exact_window_and_maps_trades_to_point_events(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "long.csv",
        [
            {"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 7},
            {"StartDate": "2026-07-16 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 99},
        ],
    )

    package = build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")

    points = pd.read_csv(package.points_csv)
    assert points[["event_mode", "point_event_count", "event_ids_hash"]].to_dict("records") == [
        {"event_mode": "legacy_trades_proxy", "point_event_count": 7, "event_ids_hash": "LEGACY_PROXY_NO_EVENT_IDS"}
    ]
    assert package.manifest["accepted_rows"] == 1
    assert package.manifest["rejected_rows"] == 1
    assert json.loads(package.manifest_path.read_text(encoding="utf-8"))["event_mode"] == "legacy_trades_proxy"


def test_csv_package_audits_non_exact_period_rows(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "short.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-05 00:00:00", "TotalTrades": 7}],
    )

    package = build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")

    audit = pd.read_csv(package.audit_csv)
    assert audit[["status", "reason"]].to_dict("records") == [
        {"status": "REJECTED", "reason": "PERIOD_NOT_EXACT"}
    ]


def test_mixed_event_modes_are_rejected() -> None:
    with pytest.raises(SourcePackError, match="mixed event modes"):
        require_single_event_mode(pd.DataFrame({"event_mode": ["legacy_trades_proxy", "real_independent_events"]}))


def test_missing_event_mode_is_rejected() -> None:
    with pytest.raises(SourcePackError, match="missing event mode"):
        require_single_event_mode(pd.DataFrame({"event_mode": ["legacy_trades_proxy", None]}))


def test_csv_package_rejects_fractional_total_trades(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "fractional.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 3.5}],
    )

    with pytest.raises(SourcePackError, match="non-negative integers"):
        build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")


def test_csv_package_manifest_keeps_generator_source_hashes(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path / "long.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 7}],
    )

    package = build_csv_package((path for path in [source]), WINDOW_START, WINDOW_END, tmp_path / "package")

    assert package.manifest["source_files"] == [{"name": "long.csv", "sha256": package.manifest["source_files"][0]["sha256"]}]


def test_csv_package_cleans_staging_after_write_failure(tmp_path: Path, monkeypatch) -> None:
    source = _write_csv(
        tmp_path / "long.csv",
        [{"StartDate": "2026-07-15 00:00:00", "EndDate": "2026-08-06 00:00:00", "TotalTrades": 7}],
    )
    original = pd.DataFrame.to_csv
    calls = 0

    def fail_second_csv(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(source_packs.pd.DataFrame, "to_csv", fail_second_csv)

    with pytest.raises(OSError, match="disk full"):
        build_csv_package([source], WINDOW_START, WINDOW_END, tmp_path / "package")
    assert not (tmp_path / "package").exists()
