"""Fresh, surface-bound Source v6 analysis artifacts."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

import duckdb
import pandas as pd

from .locking import OutputDirectoryLock
from .pipeline import ALGORITHM_VERSION, _analyze_points, _canonical
from .source_v6 import _canonical_json
from .source_v6_stitch import calculate_metrics
from .source_v6_surface_fresh import FINGERPRINT as SURFACE_FINGERPRINT, read_multiscope_scope, read_multiscope_surface


FINGERPRINT = "analysis-v6-fresh-compact-v1"
_TABLES = ("points", "refine_requests", "plateaus", "close_profiles", "base_one_order", "structures", "structure_diagnostics")


def _scope_rows(surface_path: str, scope_key: str, listing_dates: Mapping[str, object], config: object) -> dict[str, object]:
    scope = read_multiscope_scope(surface_path, scope_key)
    start = datetime.combine(scope.ready_witness.start, time.min, tzinfo=timezone.utc)
    end = datetime.combine(scope.ready_witness.end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    by_point: dict[str, list[object]] = {}
    for fragment in scope.facts:
        by_point.setdefault(fragment.point.canonical_key, []).append(fragment)
    rows = []
    for point_key, fragments in sorted(by_point.items()):
        point = fragments[0].point
        if point.symbol not in listing_dates:
            raise ValueError(f"listing date is missing: {point.symbol}")
        metrics = calculate_metrics(tuple(fragments), start_ms=start_ms, end_ms=end_ms)
        event_times = {event.event_id: event.timestamp_ms for fragment in fragments for event in fragment.events}
        event_ids = tuple(sorted(set(metrics.events)))
        if any(event_id not in event_times or not start_ms <= event_times[event_id] < end_ms for event_id in event_ids):
            raise ValueError(f"point events are outside READY witness: {point_key}")
        rows.append({
            "point_id": point_key, "symbol": point.symbol, "side": point.side, "timeframe": point.timeframe,
            "shift_bp": point.shift_bp, "shift_pct": point.shift_bp / 100, "open_ma": point.open_ma_length,
            "close_ma": point.close_ma_length, "pnl_pct": float(metrics.total_pnl_percent),
            "dd_pct": float(metrics.max_equity_drawdown_percent), "trades": metrics.total_trades,
            "wins": metrics.win_trades, "losses": metrics.loss_trades, "win_rate_pct": float(metrics.win_rate_percent),
            "profit_factor": None if metrics.profit_factor is None else float(metrics.profit_factor),
            "point_event_count": len(event_ids), "_event_ids": event_ids,
            "event_ids_hash": sha256("|".join(event_ids).encode("utf-8")).hexdigest(),
            "event_mode": "real_independent_events", "report_start": pd.Timestamp(start),
            "report_end": pd.Timestamp(end), "listing_date": pd.to_datetime(listing_dates[point.symbol], utc=True),
        })
    return {"scope_key": scope_key, "scope_digest": scope.scope_digest, "frames": _frames(_analyze_points(pd.DataFrame(rows), config))}


def _frames(stages: object) -> dict[str, list[dict[str, object]]]:
    return {name: _records(getattr(stages, name)) for name in _TABLES}


def _records(frame: pd.DataFrame) -> list[dict[str, object]]:
    return [{key: _jsonable(value) for key, value in row.items()} for row in frame.to_dict("records")]


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):
        return value


def _run_scope(surface_path: str, scope_key: str, listing_dates: Mapping[str, object], config: object) -> dict[str, object]:
    return _scope_rows(surface_path, scope_key, listing_dates, config)


def run_multiscope_analysis(
    surface_path: str | Path, directory: str | Path, config: object, *, listing_dates: Mapping[str, object],
    algorithm_version: str = ALGORITHM_VERSION, workers: int = 1, cancel_check: object | None = None,
) -> Path:
    """Analyze each fresh scope independently and write one immutable artifact."""
    surface = read_multiscope_surface(surface_path)
    scope_digests = surface["scope_digests"]
    config_json = _canonical_json(_canonical(config))
    config_hash = sha256(config_json.encode("utf-8")).hexdigest()
    listing_json = _canonical_json(dict(sorted((str(key), str(value)) for key, value in listing_dates.items())))
    identity = {"fingerprint": FINGERPRINT, "surface_fingerprint": SURFACE_FINGERPRINT, "surface_id": surface["surface_id"], "source_content_digest": surface["source_content_digest"], "scope_digests": scope_digests, "algorithm_version": algorithm_version, "algorithm_config_sha256": config_hash, "listing_dates_sha256": sha256(listing_json.encode("utf-8")).hexdigest(), "event_mode": "real_independent_events"}
    analysis_id = sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    with OutputDirectoryLock(Path(directory)):
        if callable(cancel_check) and cancel_check():
            raise RuntimeError("Source v6 analysis cancelled")
        output = Path(directory); output.mkdir(parents=True, exist_ok=True)
        target = output / f"{analysis_id}.analysis-v6.duckdb"
        if target.exists():
            return target
        keys = tuple(sorted(scope_digests))
        max_workers = min(max(1, workers), len(keys))
        if max_workers == 1:
            results = []
            for key in keys:
                if callable(cancel_check) and cancel_check():
                    raise RuntimeError("Source v6 analysis cancelled")
                results.append(_run_scope(str(surface_path), key, listing_dates, config))
        else:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_run_scope, str(surface_path), key, listing_dates, config) for key in keys]
                pending = set(futures)
                while pending:
                    if callable(cancel_check) and cancel_check():
                        for future in pending:
                            future.cancel()
                        raise RuntimeError("Source v6 analysis cancelled")
                    completed, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                results = [future.result() for future in futures]
        if {item["scope_key"] for item in results} != set(keys) or any(item["scope_digest"] != scope_digests[item["scope_key"]] for item in results):
            raise ValueError("analysis worker returned mismatched scope lineage")
        if callable(cancel_check) and cancel_check():
            raise RuntimeError("Source v6 analysis cancelled")
        return _publish(directory, target, identity, analysis_id, config_hash, results)


def _publish(directory: str | Path, target: Path, identity: Mapping[str, object], analysis_id: str, config_hash: str, results: list[dict[str, object]]) -> Path:
    handle, temporary = tempfile.mkstemp(prefix=".analysis-v6-", suffix=".staging", dir=Path(directory)); os.close(handle); os.unlink(temporary)
    connection = duckdb.connect(temporary)
    try:
        connection.execute("create table manifest(key varchar primary key, value varchar not null)")
        connection.execute("create table scope_runs(scope_key varchar primary key, scope_digest varchar not null, result_digest varchar not null)")
        for name in _TABLES:
            connection.execute(f"create table {name}(scope_key varchar not null, payload_json varchar not null)")
        manifest = {**identity, "analysis_id": analysis_id, "algorithm_config_sha256": config_hash}
        connection.executemany("insert into manifest values (?, ?)", [(key, value if isinstance(value, str) else _canonical_json(value)) for key, value in manifest.items()])
        for result in sorted(results, key=lambda item: item["scope_key"]):
            canonical = _canonical_json(result["frames"]); digest = sha256(canonical.encode("utf-8")).hexdigest()
            connection.execute("insert into scope_runs values (?, ?, ?)", [result["scope_key"], result["scope_digest"], digest])
            for name, rows in result["frames"].items():
                if rows:
                    connection.executemany(f"insert into {name} values (?, ?)", [(result["scope_key"], _canonical_json(row)) for row in rows])
        connection.execute("checkpoint"); connection.close()
        check = duckdb.connect(temporary, read_only=True)
        try:
            if dict(check.execute("select key, value from manifest").fetchall()).get("analysis_id") != analysis_id:
                raise ValueError("analysis artifact readback identity mismatch")
            if check.execute("select count(*) from scope_runs").fetchone()[0] != len(results):
                raise ValueError("analysis artifact readback scope count mismatch")
        finally:
            check.close()
        os.replace(temporary, target); return target
    finally:
        try: connection.close()
        except Exception: pass
        if os.path.exists(temporary): os.unlink(temporary)
