from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import mrs3.panel_surfaces as panel_surfaces
from mrs3.panel_surfaces import LocalSurfacesService
from mrs3.panel import PanelController
from mrs3.source_v6_coverage import CoverageCell, ReadyInterval


def _metadata(symbol: str, side: str, timeframe: str) -> SimpleNamespace:
    point = SimpleNamespace(
        symbol=symbol,
        side=side,
        timeframe=timeframe,
        canonical_key=f"{symbol}|{side}|{timeframe}|100|ema|close|3|ema|close|9",
        shift_bp=30,
        close_ma_length=2,
    )
    return SimpleNamespace(
        fragment_id=f"fragment-{symbol}-{side}-{timeframe}",
        point=point,
        report_start_ms=1767225600000,
        report_end_ms=1767398400000,
    )


def _service(
    tmp_path: Path,
    *,
    ready: tuple[ReadyInterval, ...] = (),
    missing_result: tuple[CoverageCell, ...] | None = None,
    quarantines: tuple[dict[str, object], ...] = (),
):
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
        return missing_result if missing_result is not None else (CoverageCell("BTCUSDT|LONG|1h|100|ema|close|3|ema|close|9", date(2026, 1, 2), "MISSING"),)

    fragments = (SimpleNamespace(point=metadata[0].point), SimpleNamespace(point=metadata[1].point))

    def read_fragments(path: Path):
        return fragments

    def read_quarantine(path: Path):
        return quarantines

    def materialize(items, scope_keys):
        calls["materialize"].append((tuple(items), tuple(scope_keys)))
        return SimpleNamespace(scopes=tuple(scope_keys))

    def publish(target, materialized, *, source_database, filename=None):
        calls["publish"].append((target, materialized, source_database))
        return Path(target) / "new.surface-v6.duckdb"

    service = LocalSurfacesService(
        validate_source=validate,
        read_metadata=read_metadata,
        readiness=readiness,
        missing_cells=missing,
        read_fragments=read_fragments,
        read_quarantine=read_quarantine,
        materialize=materialize,
        publish=publish,
    )
    return service, source_db, calls


def test_preflight_quarantine_blocks_ready_with_injected_validator(tmp_path: Path) -> None:
    service, source_db, _ = _service(
        tmp_path,
        ready=(ReadyInterval("BTCUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 2)),),
        quarantines=({"source_sha256": "a" * 64, "source_name": "bad.html", "reason": "M7", "fragment_id": "b" * 64},),
    )

    result = service.preflight(source_db)

    assert result["quarantines"]
    assert all(row["status"] != "READY" for row in result["rows"])


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
    assert result["scope_key"] == "BTCUSDT|LONG|1h"
    assert result["status"] == "n/r - Check gaps"
    assert result["reason"] == "coverage gaps detected"
    assert result["gaps"] == [{"utc_day": "2026-01-02", "status": "MISSING"}]
    assert result["missing_witnesses"]
    assert calls["gaps"]


def test_gaps_explains_missing_witnesses_when_non_ready_has_no_day_gaps(tmp_path: Path) -> None:
    service, source_db, _ = _service(tmp_path, missing_result=())
    token = service.preflight(source_db)["token"]

    result = service.gaps(token, "ETHUSDT|SHORT|4h")

    assert result["status"] == "n/r - Check gaps"
    assert result["gaps"] == []
    assert result["reason"] == "canonical witness grid is incomplete"
    assert result["missing_witnesses"]
    assert {"shift_bp", "close_ma_length"} == set(result["missing_witnesses"][0])


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


def test_background_publish_exposes_real_publisher_progress_without_paths(tmp_path: Path) -> None:
    service, source_db, _ = _service(
        tmp_path,
        ready=(ReadyInterval("BTCUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 2)),),
    )
    started = Event()
    release = Event()

    def publish(target, materialized, *, source_database, progress_callback, filename=None):
        progress_callback("WRITING", completed=2, total=4, detail="BTCUSDT|LONG|1h")
        started.set()
        assert release.wait(2)
        progress_callback("VALIDATING", completed=4, total=4)
        return Path(target) / "new.surface-v6.duckdb"

    service._publish = publish
    token = service.preflight(source_db)["token"]
    service.start_publish(token, ("BTCUSDT|LONG|1h",), tmp_path / "surfaces")
    assert started.wait(2)

    running = service.publish_status()
    assert running["running"] is True
    assert running["phase"] == "WRITING"
    assert running["completed"] == 2
    assert running["total"] == 4
    assert "tmp" not in json.dumps(running)

    release.set()
    for _ in range(100):
        result = service.publish_status()
        if not result["running"]:
            break
        Event().wait(0.01)
    assert result == {
        "running": False,
        "phase": "COMMITTED",
        "completed": 4,
        "total": 4,
        "detail": None,
        "target": "new.surface-v6.duckdb",
        "scopes": ["BTCUSDT|LONG|1h"],
        "error": None,
    }


def test_background_publish_hydrates_only_selected_scope_with_configured_workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, source_db, calls = _service(
        tmp_path,
        ready=(ReadyInterval("BTCUSDT|LONG|1h", date(2026, 1, 1), date(2026, 1, 2)),),
    )
    hydrated: list[tuple[Path, tuple[str, ...], int]] = []
    fragments = (SimpleNamespace(point=_metadata("BTCUSDT", "LONG", "1h").point),)

    def parallel(path: Path, ids: tuple[str, ...], *, workers: int, progress_callback):
        hydrated.append((path, ids, workers))
        progress_callback(1, 1)
        return fragments

    monkeypatch.setattr(panel_surfaces, "iter_fragment_ids_parallel", parallel)
    service._read_fragments = None
    service._workers = 3

    def publish(target, materialized, *, source_database, progress_callback, filename=None):
        return Path(target) / "new.surface-v6.duckdb"

    service._publish = publish
    token = service.preflight(source_db)["token"]
    service.start_publish(token, ("BTCUSDT|LONG|1h",), tmp_path / "surfaces")
    for _ in range(100):
        if not service.publish_status()["running"]:
            break
        Event().wait(0.01)

    assert hydrated == [(source_db, ("fragment-BTCUSDT-LONG-1h",), 3)]
    assert calls["materialize"]


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


def test_controller_publish_methods_ignore_request_target_path(tmp_path: Path) -> None:
    configured = r"D:\SHARE\!MN\hamster\MRS-Analizer\data\surfaces"
    (tmp_path / "config.local.json").write_text(
        json.dumps({"panel": {"path_defaults": {"surface_target_path": configured}}}),
        encoding="utf-8",
    )
    calls: list[Path] = []

    class Service:
        def publish(self, token, scopes, target, filename=None):
            calls.append(Path(target))
            return {"phase": "COMMITTED"}

        def start_publish(self, token, scopes, target, filename=None):
            calls.append(Path(target))
            return {"phase": "QUEUED"}

    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller._panel_surfaces = Service()
    request = {"preflight_token": "token", "scope_keys": ["BTCUSDT|LONG|1h"], "target_path": str(tmp_path / "attacker")}

    controller.surface_publish(request)
    controller.surface_publish_start(request)

    assert calls == [Path(configured), Path(configured)]
