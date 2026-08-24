"""Fresh, surface-bound Source v6 analysis artifacts."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping, Sequence

import duckdb
import pandas as pd

from .locking import OutputDirectoryLock
from .pipeline import ALGORITHM_VERSION, _analyze_points, _base_structure, _canonical
from .source_v6 import _canonical_json
from .source_v6_materializer import analysis_input_row
from .source_v6_stitch import measure_points
from .source_v6_surface_fresh import (
    FINGERPRINT as SURFACE_FINGERPRINT,
    read_multiscope_analysis_input,
    read_multiscope_scope,
    read_multiscope_surface,
)


FINGERPRINT = "analysis-v6-fresh-compact-v1"
_TABLES = ("points", "refine_requests", "plateaus", "close_profiles", "base_one_order", "structures", "structure_diagnostics")
_ADMISSION_ONLY_FIELDS = frozenset({"events_last_30d", "plateau_event_count"})


def _analysis_frame_row(row: Mapping[str, object], window: tuple, listing_dates: Mapping[str, object]) -> dict[str, object]:
    """One precomputed row, completed with this run's inputs (W9).

    The measured half — metrics and independent event ids — comes from the
    surface. `listing_date` and the report window are not measurements: the
    first belongs to the analysis run, the second to the scope witness.
    """
    start, end = window
    symbol = str(row["symbol"])
    if symbol not in listing_dates:
        raise ValueError(f"listing date is missing: {symbol}")
    event_ids = tuple(str(item) for item in row["event_ids"])
    # Stored rows hold their numbers as canonical decimal text, because that is
    # what makes the digest byte-stable across platforms. The analysis frame
    # wants real numbers, and the same coercion serves a row measured in memory.
    shift_bp = int(row["shift_bp"])
    profit_factor = row["profit_factor"]
    return {
        "point_id": str(row["point_id"]), "symbol": symbol, "side": str(row["side"]),
        "timeframe": str(row["timeframe"]),
        "shift_bp": shift_bp, "shift_pct": shift_bp / 100, "open_ma": int(row["open_ma"]),
        "close_ma": int(row["close_ma"]), "pnl_pct": float(row["pnl_pct"]),
        "dd_pct": float(row["dd_pct"]),
        "trades": int(row["trades"]), "wins": int(row["wins"]), "losses": int(row["losses"]),
        "win_rate_pct": float(row["win_rate_pct"]),
        "profit_factor": None if profit_factor is None else float(profit_factor),
        "point_event_count": len(event_ids), "_event_ids": event_ids,
        "events_last_30d": int(row["events_last_30d"]),
        "event_ids_hash": str(row["event_ids_hash"]), "event_mode": str(row["event_mode"]),
        "report_start": pd.Timestamp(start), "report_end": pd.Timestamp(end),
        "listing_date": pd.to_datetime(listing_dates[symbol], utc=True),
    }


def _measured_rows(scope: object) -> tuple[tuple, list[dict[str, object]]]:
    """Measure a scope from its facts — the path for a surface without W8 rows."""
    window = (
        datetime.combine(scope.ready_witness.start, time.min, tzinfo=timezone.utc),
        datetime.combine(scope.ready_witness.end + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )
    start_ms, end_ms = int(window[0].timestamp() * 1000), int(window[1].timestamp() * 1000)
    by_point: dict[str, list[object]] = {}
    for fragment in scope.facts:
        by_point.setdefault(fragment.point.canonical_key, []).append(fragment)
    # Measured once, the same way publication measured: a combination that
    # never traded carries the flat result the tester declared instead of
    # aborting the whole scope.
    measured, _empty = measure_points(
        tuple(scope.facts), {key: (start_ms, end_ms) for key in by_point}
    )
    rows = [
        analysis_input_row(point_key, fragments[0].point, measured[point_key], fragments, (start_ms, end_ms))
        for point_key, fragments in sorted(by_point.items())
    ]
    return window, rows


def _scope_rows(
    surface_path: str, scope_key: str, listing_dates: Mapping[str, object], config: object,
    precomputed: Sequence[Mapping[str, object]] | None = None,
    scope_digest: str | None = None,
    witness: tuple | None = None,
) -> dict[str, object]:
    """Analyse one scope, reading its measurements rather than redoing them (W9).

    Decoding every payload and re-running `calculate_metrics` here repeated,
    exactly, what materialization had already done — the same fragments, the
    same window, the same numbers. When the surface carries them, this reads
    compact rows instead. A surface without them is still analysable: it is
    measured from its facts, as before.
    """
    if precomputed is None or scope_digest is None or witness is None:
        scope = read_multiscope_scope(surface_path, scope_key)
        scope_digest = scope.scope_digest
        witness, precomputed = _measured_rows(scope)
    rows = [_analysis_frame_row(row, witness, listing_dates) for row in precomputed]
    return {"scope_key": scope_key, "scope_digest": scope_digest, "frames": _frames(_analyze_points(pd.DataFrame(rows), config))}


def _frames(stages: object) -> dict[str, list[dict[str, object]]]:
    frames = {}
    for name in _TABLES:
        records = _records(getattr(stages, name))
        frames[name] = records if name == "plateaus" else [_strip_admission_fields(row) for row in records]
    # Fresh analysis publishes BASE candidates through the canonical structure
    # frame. The legacy pipeline's in-memory ``stages.structures`` remains
    # multi-order-only; this projection is deliberately fresh-path-only.
    base_records = [
        _strip_admission_fields({key: _jsonable(value) for key, value in _base_structure(point).items()})
        for _, point in getattr(stages, "base_one_order").iterrows()
    ]
    frames["structures"].extend(sorted(base_records, key=lambda row: str(row["structure_id"])))
    return frames


def _strip_admission_fields(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_admission_fields(item)
            for key, item in value.items()
            if key not in _ADMISSION_ONLY_FIELDS
        }
    if isinstance(value, list):
        return [_strip_admission_fields(item) for item in value]
    return value


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


def _run_scope(
    surface_path: str, scope_key: str, listing_dates: Mapping[str, object], config: object,
    precomputed: Sequence[Mapping[str, object]] | None = None,
    scope_digest: str | None = None,
    witness: tuple | None = None,
) -> dict[str, object]:
    return _scope_rows(surface_path, scope_key, listing_dates, config, precomputed, scope_digest, witness)


def _analysis_id_at(path: Path) -> str | None:
    try:
        connection = duckdb.connect(str(path), read_only=True)
        try:
            row = connection.execute("select value from manifest where key = 'analysis_id'").fetchone()
            return str(row[0]) if row else None
        finally:
            connection.close()
    except duckdb.Error:
        return None


def run_multiscope_analysis(
    surface_path: str | Path, directory: str | Path, config: object, *, listing_dates: Mapping[str, object],
    algorithm_version: str = ALGORITHM_VERSION, workers: int = 1, cancel_check: object | None = None,
    filename: str | None = None,
) -> Path:
    """Analyze each fresh scope independently and write one immutable artifact."""
    surface = read_multiscope_surface(surface_path, decode=False)
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
        explicit_name = filename is not None
        if explicit_name:
            name = Path(filename).name
            if name != filename or not name.endswith(".analysis-v6.duckdb") or len(name) > 180:
                raise ValueError("analysis filename must end with .analysis-v6.duckdb")
        else:
            surface_name = Path(surface_path).name.removesuffix(".surface-v6.duckdb")
            name = re.sub(r"[^A-Za-z0-9_-]+", "_", surface_name) + ".analysis-v6.duckdb"
        target = output / name
        suffix = 2
        while target.exists():
            if explicit_name:
                raise FileExistsError(f"analysis target already exists: {target}")
            if _analysis_id_at(target) == analysis_id:
                return target
            stem = name.removesuffix(".analysis-v6.duckdb")
            target = output / f"{stem}-{suffix}.analysis-v6.duckdb"
            suffix += 1
        keys = tuple(sorted(scope_digests))
        # W9: read the surface's own measurements once, in this process. They
        # are scalars and event ids, so handing each worker its scope's rows is
        # cheaper than every worker decoding the same payloads to rebuild them.
        precomputed = read_multiscope_analysis_input(surface_path) or {}
        extra = {
            key: (
                precomputed[key]["rows"] if key in precomputed else None,
                scope_digests[key] if key in precomputed else None,
                precomputed[key]["witness"] if key in precomputed else None,
            )
            for key in keys
        }
        max_workers = min(max(1, workers), len(keys))
        if max_workers == 1:
            results = []
            for key in keys:
                if callable(cancel_check) and cancel_check():
                    raise RuntimeError("Source v6 analysis cancelled")
                results.append(_run_scope(str(surface_path), key, listing_dates, config, *extra[key]))
        else:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_run_scope, str(surface_path), key, listing_dates, config, *extra[key]) for key in keys]
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
