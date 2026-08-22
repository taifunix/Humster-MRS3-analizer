from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from mrs3.panel_surfaces import LocalSurfacesService
from mrs3.panel import PanelController
from mrs3.source_v6_coverage import CoverageCell, ReadyInterval


def _metadata(symbol: str, side: str, timeframe: str) -> SimpleNamespace:
    point = SimpleNamespace(
        symbol=symbol,
        side=side,
        timeframe=timeframe,
        canonical_key=f"{symbol}|{side}|{timeframe}|100|ema|close|3|ema|close|9",
    )
    return SimpleNamespace(
        point=point,
        report_start_ms=1767225600000,
        report_end_ms=1767398400000,
    )


def _service(tmp_path: Path, *, ready: tuple[ReadyInterval, ...] = ()):
    source_db = (tmp_path / "private" / "committed-source.duckdb").resolve()
    calls: dict[str, object] = {"validated": [], "metadata": [], "ready": [], "gaps": [], "materialize": [], "publish": []}
    metadata = (_metadata("BTCUSDT", "LONG", "1h"), _metadata("ETHUSDT", "SHORT", "4h"))

    def validate(path: Path) -> None:
        calls["validated"].append(path)

    def read_metadata(path: Path):
        calls["metadata"].append(path)
        return metadata

    def readiness(items, **kwargs):
        calls["ready"].append((tuple(items), kwargs))
        return ready

    def missing(items, *, start, end, point_keys):
        calls["gaps"].append((tuple(items), start, end, tuple(point_keys)))
        return (CoverageCell("BTCUSDT|LONG|1h|100|ema|close|3|ema|close|9", date(2026, 1, 2), "MISSING"),)

    fragments = (SimpleNamespace(point=metadata[0].point), SimpleNamespace(point=metadata[1].point))

    def read_fragments(path: Path):
        return fragments

    def materialize(items, scope_keys):
        calls["materialize"].append((tuple(items), tuple(scope_keys)))
        return SimpleNamespace(scopes=tuple(scope_keys))

    def publish(target, materialized, *, source_database):
        calls["publish"].append((target, materialized, source_database))
        return Path(target) / "new.surface-v6.duckdb"

    service = LocalSurfacesService(
        validate_source=validate,
        read_metadata=read_metadata,
        readiness=readiness,
        missing_cells=missing,
        read_fragments=read_fragments,
        materialize=materialize,
        publish=publish,
    )
    return service, source_db, calls


def test_preflight_redacts_source_path_and_replaces_token(tmp_path: Path) -> None:
    service, source_db, calls = _service(
        tmp_path,
        ready=(ReadyInterval("BTCUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 2)),),
    )

    first = service.preflight(source_db)
    second = service.preflight(source_db)

    assert first["phase"] == "PREFLIGHT_READY"
    assert first["token"] != second["token"]
    assert str(tmp_path) not in json.dumps(first)
    rows = {row["scope_key"]: row for row in first["rows"]}
    assert rows["BTCUSDT|LONG|1h"]["status"] == "READY"
    assert rows["ETHUSDT|SHORT|4h"]["status"] == "n/r - Check gaps"
    assert calls["validated"] == [source_db, source_db]
    assert calls["metadata"] == [source_db, source_db]


def test_select_rejects_stale_and_non_ready_scopes(tmp_path: Path) -> None:
    service, source_db, _ = _service(
        tmp_path,
        ready=(ReadyInterval("BTCUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 2)),),
    )
    token = service.preflight(source_db)["token"]

    with pytest.raises(ValueError, match="stale coverage token"):
        service.select("old-token", ("BTCUSDT|LONG|1h",))
    with pytest.raises(ValueError, match="READY"):
        service.select(token, ("ETHUSDT|SHORT|4h",))
    with pytest.raises(ValueError, match="at least one"):
        service.select(token, ())
    assert service.select(token, ("BTCUSDT|LONG|1h",))["scopes"] == ["BTCUSDT|LONG|1h"]


def test_gaps_redacts_raw_point_keys_and_paths(tmp_path: Path) -> None:
    service, source_db, calls = _service(tmp_path)
    token = service.preflight(source_db)["token"]

    result = service.gaps(token, "BTCUSDT|LONG|1h")

    assert str(tmp_path) not in json.dumps(result)
    assert result == {
        "scope_key": "BTCUSDT|LONG|1h",
        "status": "n/r - Check gaps",
        "gaps": [{"utc_day": "2026-01-02", "status": "MISSING"}],
    }
    assert calls["gaps"]


def test_publish_uses_fresh_canonical_machinery_and_rejects_existing_target(tmp_path: Path) -> None:
    service, source_db, calls = _service(
        tmp_path,
        ready=(ReadyInterval("BTCUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 2)),),
    )
    token = service.preflight(source_db)["token"]
    target = tmp_path / "surfaces"
    target.mkdir()

    result = service.publish(token, ("BTCUSDT|LONG|1h",), target)

    assert result == {
        "phase": "COMMITTED",
        "target": "new.surface-v6.duckdb",
        "scopes": ["BTCUSDT|LONG|1h"],
    }
    assert calls["materialize"][0][1] == ("BTCUSDT|LONG|1h",)
    assert calls["publish"][0][0] == target
    assert calls["publish"][0][2] == source_db

    occupied = tmp_path / "occupied.surface-v6.duckdb"
    occupied.write_bytes(b"immutable")
    with pytest.raises(FileExistsError, match="target already exists"):
        service.publish(token, ("BTCUSDT|LONG|1h",), occupied)
    assert len(calls["publish"]) == 1


def test_controller_uses_surface_adapter_and_keeps_paths_out_of_response(tmp_path: Path) -> None:
    service, source_db, _ = _service(
        tmp_path,
        ready=(ReadyInterval("BTCUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 2)),),
    )
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller._panel_surfaces = service

    preflight = controller.surface_preflight({"source_db": str(source_db)})
    selected = controller.surface_select({
        "preflight_token": preflight["token"],
        "scope_keys": ["BTCUSDT|LONG|1h"],
    })

    assert selected["scopes"] == ["BTCUSDT|LONG|1h"]
    assert str(tmp_path) not in json.dumps(preflight)
