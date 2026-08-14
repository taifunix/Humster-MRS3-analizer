from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Mapping
import unicodedata

import duckdb
import pandas as pd

from . import duckdb_events
from .duckdb_source_schema import (
    NORMALIZATION_CONTRACT_VERSION,
    SOURCE_SCHEMA_VERSION,
    _canonical_point_key,
    _grid_content_hash,
    _grid_hash,
    _payload_hash,
    _point_hash,
    _report_hash,
    canonical_report_key,
    ensure_source_schema,
    normalize_source_shift,
    validate_source_database,
    validate_source_database_structural,
)
from .locking import OutputDirectoryLock


COMPACT_IMPORT_SCHEMA_VERSION = 4
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _ImportCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ImportRequest:
    root_path: Path
    database_path: Path
    audit_root: Path
    workers: int = 4
    transaction_batch_size: int = 250
    job_id: str | None = None
    cancellation_requested: Callable[[], bool] | None = None
    expected_preflight_token: str | None = None
    preflight: ImportPreflight | None = None


@dataclass(frozen=True, slots=True)
class ImportPreflight:
    """Path-free import snapshot suitable for display and start authorization."""

    token: str
    discovered: int
    source_schema_version: int | None
    target_identity_digest: str
    snapshots: tuple[_Snapshot, ...] = field(default=(), repr=False, compare=False)
    root_path: Path | None = field(default=None, repr=False, compare=False)
    target_identity: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SnapshotProgress:
    discovered: int
    snapshotted: int
    total_bytes: int
    processed_bytes: int


@dataclass(frozen=True, slots=True)
class ImportProgress:
    final_state: str
    discovered: int
    parsed: int
    inserted: int
    replaced: int
    identical: int
    ambiguous: int
    quarantined: int

    @property
    def counts(self) -> dict[str, int]:
        return {
            "parsed": self.parsed,
            "inserted": self.inserted,
            "replaced": self.replaced,
            "identical": self.identical,
            "ambiguous": self.ambiguous,
            "quarantined": self.quarantined,
        }


@dataclass(frozen=True, slots=True)
class ImportJobResult:
    job_id: str
    final_state: str
    discovered: int
    parsed: int
    inserted: int
    replaced: int
    identical: int
    ambiguous: int
    quarantined: int
    safe_to_delete: str
    manifest_path: Path
    manifest_sha256: str
    checklist_path: Path
    checklist_sha256: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Snapshot:
    path: Path
    relative_path: str
    input_sha256: str
    codec_source_sha256: str
    source_size: int
    source_mtime_ns: int


@dataclass(frozen=True, slots=True)
class _PreparedReport:
    snapshot: _Snapshot
    canonical_point_key: str
    canonical_report_key: str
    point: Mapping[str, object]
    grid: Mapping[str, object]
    report: Mapping[str, object]
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _WriteDecision:
    operation: str
    prepared: _PreparedReport
    old_source_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedOutcome:
    snapshot: _Snapshot
    prepared: _PreparedReport | None
    classification: str | None = None
    reason: str | None = None


def _canonical_relative(path: Path, root: Path) -> str:
    return unicodedata.normalize("NFC", path.relative_to(root).as_posix())


def discover_compact_reports(root_path: Path) -> tuple[Path, ...]:
    """Return HTML files below ``root_path`` in stable relative-path order."""
    root = Path(root_path)
    if not root.is_dir():
        raise ValueError(f"HTML root does not exist or is not a directory: {root}")
    reports = (
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".html"
    )
    return tuple(sorted(reports, key=lambda path: _canonical_relative(path, root)))


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _codec_source_sha256(source: bytes) -> str:
    """Match the v3 codec's UTF-8 text identity with universal newlines."""
    try:
        normalized = source.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        return ""
    return sha256(normalized.encode("utf-8")).hexdigest()


def _snapshot_one(root: Path, path: Path) -> _Snapshot:
    before = path.stat()
    source = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f"input HTML changed while preflight was reading it: {path}")
    return _Snapshot(path, _canonical_relative(path, root), sha256(source).hexdigest(), _codec_source_sha256(source), len(source), after.st_mtime_ns)


def _snapshot_reports(root: Path, paths: tuple[Path, ...], workers: int = 1, progress_callback: Callable[[SnapshotProgress], object] | None = None) -> tuple[_Snapshot, ...]:
    total_bytes = sum(path.stat().st_size for path in paths)
    if progress_callback:
        progress_callback(SnapshotProgress(len(paths), 0, total_bytes, 0))
    results: list[_Snapshot | None] = [None] * len(paths)
    processed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_snapshot_one, root, path): index for index, path in enumerate(paths)}
        for completed, future in enumerate(as_completed(futures), start=1):
            snapshot = future.result(); results[futures[future]] = snapshot; processed += snapshot.source_size
            if progress_callback: progress_callback(SnapshotProgress(len(paths), completed, total_bytes, processed))
    return tuple(snapshot for snapshot in results if snapshot is not None)


def _inputs_unchanged(snapshots: tuple[_Snapshot, ...]) -> bool:
    """Fast guard for a preflight-authorized tree; content hashes are retained as evidence."""
    return all(
        item.path.is_file()
        and (item.path.stat().st_size, item.path.stat().st_mtime_ns)
        == (item.source_size, item.source_mtime_ns)
        for item in snapshots
    )


def _source_database_lock(database_path: Path) -> OutputDirectoryLock:
    """Return the existing advisory lock pattern scoped to one resolved DB path."""
    resolved = database_path.resolve()
    token = sha256(str(resolved).encode("utf-8")).hexdigest()
    return OutputDirectoryLock(resolved.parent / f".mrs3-import-{token}")


def _database_identity(database_path: Path) -> str | None:
    """Return a cheap target identity captured around staging publication."""
    if not database_path.exists():
        return None
    if not database_path.is_file():
        raise ValueError(f"source database target is not a file: {database_path}")
    stat = database_path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _preflight_token(
    database_path: Path, snapshots: tuple[_Snapshot, ...], target_identity: str | None
) -> str:
    payload = {
        "inputs": [(item.relative_path, item.input_sha256) for item in snapshots],
        "target_identity": target_identity,
        "target_path": str(database_path.resolve()),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _target_identity_digest(target_identity: str | None) -> str:
    encoded = json.dumps(
        {"target_identity": target_identity}, sort_keys=True, separators=(",", ":")
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _preflight_from_snapshots(
    database_path: Path, snapshots: tuple[_Snapshot, ...]
) -> tuple[ImportPreflight, str | None]:
    target_identity = _database_identity(database_path)
    schema_version: int | None = None
    if target_identity is not None:
        try:
            connection = duckdb.connect(str(database_path), read_only=True)
            try:
                validation = validate_source_database_structural(connection)
            finally:
                connection.close()
        except Exception as exc:
            raise ValueError(f"source database validation failed: {exc}") from exc
        if not validation.valid:
            raise ValueError(f"source database validation failed: {validation.errors}")
        if _database_identity(database_path) != target_identity:
            raise ValueError("source database changed during preflight")
        schema_version = validation.schema_version
    return (
        ImportPreflight(
            token=_preflight_token(database_path, snapshots, target_identity),
            discovered=len(snapshots),
            source_schema_version=schema_version,
            target_identity_digest=_target_identity_digest(target_identity),
            target_identity=target_identity,
        ),
        target_identity,
    )


def preflight_html_import(request: ImportRequest, progress_callback: Callable[[SnapshotProgress], object] | None = None) -> ImportPreflight:
    """Snapshot recursive HTML inputs and validate the current source target."""
    root = Path(request.root_path)
    snapshots = _snapshot_reports(root, discover_compact_reports(root), request.workers, progress_callback)
    preflight, _ = _preflight_from_snapshots(Path(request.database_path), snapshots)
    return ImportPreflight(
        preflight.token,
        preflight.discovered,
        preflight.source_schema_version,
        preflight.target_identity_digest,
        snapshots,
        root.resolve(),
        preflight.target_identity,
    )


def _prepare_worker(snapshot: _Snapshot, imported_at: datetime) -> _PreparedOutcome:
    outcome = duckdb_events.read_compact_html_for_process(snapshot.path)
    if getattr(outcome, "error_classification") or getattr(outcome, "record") is None:
        return _PreparedOutcome(snapshot, None, str(getattr(outcome, "error_classification") or "INVALID_REPORT"))
    try:
        return _PreparedOutcome(snapshot, _prepare(snapshot, outcome, imported_at))
    except Exception as exc:
        return _PreparedOutcome(snapshot, None, "INTEGRITY_QUARANTINE", str(exc))


def _stream_prepared_reports(
    snapshots: tuple[_Snapshot, ...], workers: int, imported_at: datetime,
    cancellation_requested: Callable[[], bool] | None = None,
):
    """Yield completed parse/prepare work with bounded process-pool memory."""
    iterator = iter(snapshots)
    with ProcessPoolExecutor(max_workers=workers) as executor:
        pending: dict[Future[_PreparedOutcome], _Snapshot] = {}

        def fill() -> None:
            while len(pending) < workers * 3:
                try:
                    snapshot = next(iterator)
                except StopIteration:
                    return
                pending[executor.submit(_prepare_worker, snapshot, imported_at)] = snapshot

        fill()
        while pending:
            if cancellation_requested and cancellation_requested():
                for future in pending:
                    future.cancel()
                raise _ImportCancelled
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                snapshot = pending.pop(future)
                try:
                    yield future.result()
                except BaseException as exc:
                    yield _PreparedOutcome(snapshot, None, "INVALID_REPORT", f"worker failed: {exc}")
            fill()


def _default_job_id(
    request: ImportRequest, snapshots: tuple[_Snapshot, ...], active_hashes: tuple[str, ...]
) -> str:
    data = {
        "database": str(Path(request.database_path).resolve()),
        "inputs": [(item.relative_path, item.input_sha256) for item in snapshots],
        "root": str(Path(request.root_path).resolve()),
        "active_hashes": active_hashes,
    }
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"import-{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _lock_failure_job_id(
    request: ImportRequest, snapshots: tuple[_Snapshot, ...]
) -> str:
    data = {
        "database": str(Path(request.database_path)),
        "inputs": [(item.relative_path, item.input_sha256) for item in snapshots],
        "root": str(Path(request.root_path)),
    }
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"import-busy-{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _emit(
    callback: Callable[[ImportProgress], object] | None,
    state: str,
    discovered: int,
    counts: Mapping[str, int],
) -> None:
    if callback is None:
        return
    progress = ImportProgress(
        final_state=state,
        discovered=discovered,
        parsed=counts["parsed"],
        inserted=counts["inserted"],
        replaced=counts["replaced"],
        identical=counts["identical"],
        ambiguous=counts["ambiguous"],
        quarantined=counts["quarantined"],
    )
    try:
        callback(progress)
    except Exception:
        # Progress delivery is observational and must not control a transaction.
        pass


def _cancelled(request: ImportRequest) -> bool:
    return bool(request.cancellation_requested and request.cancellation_requested())


def _prepare(snapshot: _Snapshot, outcome: object, imported_at: datetime) -> _PreparedReport:
    record = getattr(outcome, "record")
    if record is None:
        raise ValueError("compact parser returned no record")
    outcome_source_sha256 = str(getattr(outcome, "source_sha256"))
    if outcome_source_sha256 != snapshot.codec_source_sha256:
        raise ValueError("parser SHA-256 does not match the snapshotted source text")
    if outcome_source_sha256 != str(getattr(record, "source_sha256")):
        raise ValueError("parser SHA-256 does not match the compact record")
    decoded = duckdb_events.decode_compact_record(record)
    timestamps = tuple(int(value) for value in decoded["timestamps_ms"])
    if not timestamps:
        raise ValueError("compact report contains no timestamps")
    if timestamps[0] != int(getattr(record, "start_timestamp_ms")):
        raise ValueError("compact report start timestamp does not match its time grid")
    if timestamps[-1] != int(getattr(record, "end_timestamp_ms")):
        raise ValueError("compact report end timestamp does not match its time grid")
    metadata = {
        "symbol": str(getattr(record, "symbol")),
        "side": str(getattr(record, "side")),
        "timeframe": str(getattr(record, "timeframe")),
        "open_multiplier": str(getattr(record, "open_multiplier")),
        "open_ma_len": int(getattr(record, "open_ma_len")),
        "close_ma_len": int(getattr(record, "close_ma_len")),
        "report_period_start_ms": timestamps[0],
        "report_period_end_ms": timestamps[-1],
    }
    point_key = _canonical_point_key(metadata)
    report_key = canonical_report_key({**metadata, "canonical_point_key": point_key})
    point: dict[str, object] = {
        "canonical_point_key": point_key,
        "symbol": metadata["symbol"],
        "side": metadata["side"],
        "timeframe": metadata["timeframe"],
        "shift_bp": normalize_source_shift(
            metadata["open_multiplier"], NORMALIZATION_CONTRACT_VERSION
        ),
        "open_ma_type": str(getattr(record, "open_ma_type")),
        "open_ma_source": str(getattr(record, "open_ma_source")),
        "open_ma_len": metadata["open_ma_len"],
        "open_multiplier_raw": metadata["open_multiplier"],
        "close_ma_type": str(getattr(record, "close_ma_type")),
        "close_ma_source": str(getattr(record, "close_ma_source")),
        "close_ma_len": metadata["close_ma_len"],
    }
    point["row_sha256"] = _point_hash(point)
    grid: dict[str, object] = {
        "grid_hash": _grid_content_hash(timestamps),
        "sample_count": len(timestamps),
        "start_timestamp_ms": timestamps[0],
        "end_timestamp_ms": timestamps[-1],
        "timestamps_zlib": bytes(getattr(record, "timestamps_zlib")),
    }
    grid["row_sha256"] = _grid_hash(grid)
    report_id = sha256(f"{report_key}\0{snapshot.codec_source_sha256}".encode("utf-8")).hexdigest()
    report: dict[str, object] = {
        "report_id": report_id,
        "canonical_report_key": report_key,
        "canonical_point_key": point_key,
        "grid_hash": grid["grid_hash"],
        "source_sha256": snapshot.codec_source_sha256,
        "source_file": snapshot.relative_path,
        "source_size": snapshot.source_size,
        "imported_at_utc": imported_at,
        "settings_json": str(getattr(record, "settings_json")),
        "raw_action_count": int(getattr(record, "raw_action_count")),
        "equity_sample_count": int(getattr(record, "equity_sample_count")),
        "wallet_change_count": int(getattr(record, "wallet_change_count")),
        "report_period_start_ms": timestamps[0],
        "report_period_end_ms": timestamps[-1],
    }
    report["row_sha256"] = _report_hash(report)
    payload: dict[str, object] = {
        "report_id": report_id,
        "series_codec": str(getattr(record, "series_codec")),
        "actions_codec": str(getattr(record, "actions_codec")),
        "actions_zlib": bytes(getattr(record, "actions_zlib")),
        "equity_zlib": bytes(getattr(record, "equity_zlib")),
        "wallet_zlib": bytes(getattr(record, "wallet_zlib")),
    }
    payload["payload_sha256"] = _payload_hash(payload)
    return _PreparedReport(snapshot, point_key, report_key, point, grid, report, payload)


def _row(connection: duckdb.DuckDBPyConnection, query: str, parameters: list[object]) -> dict[str, object] | None:
    cursor = connection.execute(query, parameters)
    value = cursor.fetchone()
    if value is None:
        return None
    return dict(zip((item[0] for item in cursor.description), value, strict=True))


def _ensure_point(connection: duckdb.DuckDBPyConnection, point: Mapping[str, object]) -> None:
    existing = _row(
        connection,
        "select * from point_configs where canonical_point_key=?",
        [point["canonical_point_key"]],
    )
    columns = (
        "canonical_point_key", "symbol", "side", "timeframe", "shift_bp",
        "open_ma_type", "open_ma_source", "open_ma_len", "open_multiplier_raw",
        "close_ma_type", "close_ma_source", "close_ma_len", "row_sha256",
    )
    if existing is None:
        connection.execute(
            f"insert into point_configs values ({','.join('?' for _ in columns)})",
            [point[name] for name in columns],
        )
        return
    comparable = tuple(name for name in columns if name not in {"open_multiplier_raw", "row_sha256"})
    if any(existing[name] != point[name] for name in comparable):
        raise ValueError("canonical point conflicts with existing normalized metadata")


def _ensure_grid(connection: duckdb.DuckDBPyConnection, grid: Mapping[str, object]) -> None:
    existing = _row(
        connection, "select * from time_grids where grid_hash=?", [grid["grid_hash"]]
    )
    columns = (
        "grid_hash", "sample_count", "start_timestamp_ms", "end_timestamp_ms",
        "timestamps_zlib", "row_sha256",
    )
    if existing is None:
        connection.execute(
            f"insert into time_grids values ({','.join('?' for _ in columns)})",
            [grid[name] for name in columns],
        )
    elif any(existing[name] != grid[name] for name in columns):
        raise ValueError("time-grid hash conflicts with existing grid payload")


def _insert_active(connection: duckdb.DuckDBPyConnection, prepared: _PreparedReport) -> None:
    _ensure_point(connection, prepared.point)
    _ensure_grid(connection, prepared.grid)
    report_columns = (
        "report_id", "canonical_report_key", "canonical_point_key", "grid_hash",
        "source_sha256", "source_file", "source_size", "imported_at_utc", "settings_json",
        "raw_action_count", "equity_sample_count", "wallet_change_count",
        "report_period_start_ms", "report_period_end_ms", "row_sha256",
    )
    connection.execute(
        f"insert into active_reports values ({','.join('?' for _ in report_columns)})",
        [prepared.report[name] for name in report_columns],
    )
    payload_columns = (
        "report_id", "series_codec", "actions_codec", "actions_zlib", "equity_zlib",
        "wallet_zlib", "payload_sha256",
    )
    connection.execute(
        f"insert into report_payloads values ({','.join('?' for _ in payload_columns)})",
        [prepared.payload[name] for name in payload_columns],
    )


def _write_decision(
    connection: duckdb.DuckDBPyConnection,
    decision: _WriteDecision,
    job_id: str,
    imported_at: datetime,
) -> None:
    if decision.operation == "replace":
        connection.execute(
            "delete from active_reports where canonical_report_key=?",
            [decision.prepared.canonical_report_key],
        )
    _insert_active(connection, decision.prepared)
    if decision.operation == "replace":
        old_hash = str(decision.old_source_sha256)
        new_hash = str(decision.prepared.report["source_sha256"])
        audit_id = sha256(
            f"{job_id}\0{decision.prepared.canonical_report_key}\0{old_hash}\0{new_hash}".encode()
        ).hexdigest()
        connection.execute(
            "insert into replacement_history values (?,?,?,?,?,?)",
            [audit_id, decision.prepared.canonical_report_key, old_hash, new_hash, imported_at, job_id],
        )


def _append_rows(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: tuple[str, ...],
    rows: list[list[object]] | list[tuple[object, ...]],
) -> None:
    if rows:
        connection.append(table, pd.DataFrame.from_records(rows, columns=columns))


def _insert_rows_ignore_conflicts(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    temporary_name: str,
    columns: tuple[str, ...],
    rows: list[list[object]],
) -> None:
    if not rows:
        return
    connection.register(temporary_name, pd.DataFrame.from_records(rows, columns=columns))
    try:
        column_list = ",".join(columns)
        connection.execute(
            f"insert into {table} ({column_list}) select {column_list} from {temporary_name} on conflict do nothing"
        )
    finally:
        connection.unregister(temporary_name)


def _write_decisions_batch(
    connection: duckdb.DuckDBPyConnection,
    decisions: list[_WriteDecision],
    job_id: str,
    imported_at: datetime,
) -> None:
    """Write one atomic batch without issuing SQL once per report field group."""
    replacements = [decision for decision in decisions if decision.operation == "replace"]
    if replacements:
        replacement_keys = [(decision.prepared.canonical_report_key,) for decision in replacements]
        connection.executemany(
            "delete from active_reports where canonical_report_key=?", replacement_keys
        )

    points = {decision.prepared.canonical_point_key: decision.prepared.point for decision in decisions}
    point_columns = (
        "canonical_point_key", "symbol", "side", "timeframe", "shift_bp",
        "open_ma_type", "open_ma_source", "open_ma_len", "open_multiplier_raw",
        "close_ma_type", "close_ma_source", "close_ma_len", "row_sha256",
    )
    _insert_rows_ignore_conflicts(
        connection, "point_configs", "_import_points", point_columns,
        [[point[name] for name in point_columns] for point in points.values()],
    )

    grids = {str(decision.prepared.grid["grid_hash"]): decision.prepared.grid for decision in decisions}
    grid_columns = (
        "grid_hash", "sample_count", "start_timestamp_ms", "end_timestamp_ms",
        "timestamps_zlib", "row_sha256",
    )
    _insert_rows_ignore_conflicts(
        connection, "time_grids", "_import_grids", grid_columns,
        [[grid[name] for name in grid_columns] for grid in grids.values()],
    )

    report_columns = (
        "report_id", "canonical_report_key", "canonical_point_key", "grid_hash",
        "source_sha256", "source_file", "source_size", "imported_at_utc", "settings_json",
        "raw_action_count", "equity_sample_count", "wallet_change_count",
        "report_period_start_ms", "report_period_end_ms", "row_sha256",
    )
    _append_rows(
        connection, "active_reports", report_columns,
        [[decision.prepared.report[name] for name in report_columns] for decision in decisions],
    )
    payload_columns = (
        "report_id", "series_codec", "actions_codec", "actions_zlib", "equity_zlib",
        "wallet_zlib", "payload_sha256",
    )
    _append_rows(
        connection, "report_payloads", payload_columns,
        [[decision.prepared.payload[name] for name in payload_columns] for decision in decisions],
    )
    if replacements:
        history_rows = []
        for decision in replacements:
            old_hash = str(decision.old_source_sha256)
            canonical_key = decision.prepared.canonical_report_key
            new_hash = str(decision.prepared.report["source_sha256"])
            audit_id = sha256(f"{job_id}\0{canonical_key}\0{old_hash}\0{new_hash}".encode()).hexdigest()
            history_rows.append((audit_id, canonical_key, old_hash, new_hash, imported_at, job_id))
        _append_rows(
            connection,
            "replacement_history",
            ("audit_id", "canonical_report_key", "old_source_sha256", "new_source_sha256", "imported_at_utc", "job_id"),
            history_rows,
        )


def _validate_written_batch(
    connection: duckdb.DuckDBPyConnection, decisions: list[_WriteDecision]
) -> None:
    """Confirm the committed metadata for this batch without re-reading historical blobs."""
    expected = {
        (decision.prepared.canonical_report_key, str(decision.prepared.report["source_sha256"]))
        for decision in decisions
    }
    source_hashes = [source_hash for _, source_hash in expected]
    rows = connection.execute(
        "select canonical_report_key,source_sha256 from active_reports where source_sha256 = any(?)",
        [source_hashes],
    ).fetchall()
    actual = {(str(key), str(source_hash)) for key, source_hash in rows}
    if actual != expected:
        raise ValueError("batch write verification failed for active report metadata")
    payload_count = int(connection.execute(
        "select count(*) from report_payloads p join active_reports r using(report_id) "
        "where r.source_sha256 = any(?)",
        [source_hashes],
    ).fetchone()[0])
    if payload_count != len(expected):
        raise ValueError("batch write verification failed for report payload references")


def _atomic_json(path: Path, payload: object) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)
    return sha256(encoded).hexdigest()


def _write_evidence(
    request: ImportRequest,
    job_id: str,
    final_state: str,
    counts: Mapping[str, int],
    evidence: list[dict[str, object]],
    error: str | None,
) -> tuple[Path, str, Path, str, str]:
    audit_dir = Path(request.audit_root) / job_id
    audit_dir.mkdir(parents=True, exist_ok=True)
    complete = all(
        item["parity"] == "PASS" and item["validation"] == "PASS"
        for item in evidence
    )
    safe = (
        final_state == "COMMITTED"
        and counts["ambiguous"] == 0
        and counts["quarantined"] == 0
        and complete
    )
    checklist_reports = [
        {
            "classification": item["classification"],
            "input_sha256": item["input_sha256"],
            "parity": item["parity"],
            "relative_path": item["relative_path"],
            "safe_to_delete": "YES" if safe else "NO",
            "validation": item["validation"],
        }
        for item in evidence
    ]
    checklist = {
        "evidence_only": True,
        "html_deleted_by_importer": False,
        "job_id": job_id,
        "reports": checklist_reports,
        "safe_to_delete": "YES" if safe else "NO",
    }
    checklist_path = audit_dir / "html_delete_checklist.json"
    checklist_hash = _atomic_json(checklist_path, checklist)
    manifest_counts = {"discovered": len(evidence), **dict(counts)}
    manifest = {
        "artifacts": {
            "checklist": {
                "path": checklist_path.name,
                "sha256": checklist_hash,
            }
        },
        "compact_import_schema_version": COMPACT_IMPORT_SCHEMA_VERSION,
        "counts": manifest_counts,
        "error": error,
        "final_state": final_state,
        "job_id": job_id,
        "reports": [
            {
                "canonical_report_key": item.get("canonical_report_key"),
                "classification": item["classification"],
                "input_sha256": item["input_sha256"],
                "parity": item["parity"],
                "relative_path": item["relative_path"],
                "validation": item["validation"],
            }
            for item in evidence
        ],
        "safe_to_delete": "YES" if safe else "NO",
        "source_database_schema_version": SOURCE_SCHEMA_VERSION,
    }
    manifest_path = audit_dir / "import_manifest.json"
    manifest_hash = _atomic_json(manifest_path, manifest)
    return manifest_path, manifest_hash, checklist_path, checklist_hash, "YES" if safe else "NO"


def import_html_tree(
    request: ImportRequest, progress_callback: Callable[[ImportProgress], object] | None
) -> ImportJobResult:
    """Import while holding the resolved source database's writer lock."""
    database_lock = _source_database_lock(Path(request.database_path))
    try:
        database_lock.__enter__()
    except Exception as exc:
        return _import_html_tree(request, progress_callback, lock_error=exc)
    try:
        return _import_html_tree(request, progress_callback, lock_error=None)
    finally:
        database_lock.__exit__(None, None, None)


def _import_html_tree(
    request: ImportRequest,
    progress_callback: Callable[[ImportProgress], object] | None,
    *,
    lock_error: Exception | None,
) -> ImportJobResult:
    """Import a recursive HTML tree through read-only parsing and one writer."""
    if request.workers < 1 or request.transaction_batch_size < 1:
        raise ValueError("workers and transaction_batch_size must be at least one")
    root = Path(request.root_path)
    paths: tuple[Path, ...]
    snapshots: tuple[_Snapshot, ...]
    active_hashes: tuple[str, ...] = ()
    database_path = Path(request.database_path)
    expected_target_identity: str | None = None
    if request.preflight is not None:
        snapshots = request.preflight.snapshots
        paths = tuple(item.path for item in snapshots)
        if request.expected_preflight_token != request.preflight.token:
            lock_error = ValueError("preflight token does not match current import inputs and target")
        elif request.preflight.root_path != root.resolve():
            lock_error = ValueError("preflight token does not match current import inputs and target")
        elif not _inputs_unchanged(snapshots):
            lock_error = ValueError("input HTML snapshot changed after preflight")
        else:
            expected_target_identity = request.preflight.target_identity
            if _database_identity(database_path) != expected_target_identity:
                lock_error = ValueError("preflight token does not match current import inputs and target")
    else:
        paths = discover_compact_reports(root)
        snapshots = _snapshot_reports(root, paths, request.workers)
    if lock_error is None and request.preflight is None and request.expected_preflight_token is not None:
        try:
            current_preflight, expected_target_identity = _preflight_from_snapshots(
                database_path, snapshots
            )
            if current_preflight.token != request.expected_preflight_token:
                raise ValueError("preflight token does not match current import inputs and target")
        except Exception as exc:
            lock_error = exc
    if lock_error is None and database_path.is_file():
        try:
            connection = duckdb.connect(str(database_path), read_only=True)
            try:
                tables = {row[0] for row in connection.execute("show tables").fetchall()}
                if "active_reports" in tables:
                    active_hashes = tuple(
                        row[0]
                        for row in connection.execute(
                            "select source_sha256 from active_reports order by source_sha256"
                        ).fetchall()
                    )
            finally:
                connection.close()
        except Exception as exc:
            lock_error = exc
    if request.job_id is not None:
        job_id = request.job_id
    elif lock_error is not None:
        job_id = _lock_failure_job_id(request, snapshots)
    else:
        job_id = _default_job_id(request, snapshots, active_hashes)
    if not _JOB_ID.fullmatch(job_id):
        raise ValueError("job_id must be a safe 1-128 character identifier")
    counts = {name: 0 for name in ("parsed", "inserted", "replaced", "identical", "ambiguous", "quarantined")}
    evidence: list[dict[str, object]] = [
        {
            "relative_path": item.relative_path,
            "input_sha256": item.input_sha256,
            "classification": "NOT_PARSED",
            "parity": "NOT_RUN",
            "validation": "NOT_RUN",
        }
        for item in snapshots
    ]
    evidence_by_path = {item["relative_path"]: item for item in evidence}
    imported_at = datetime.now(timezone.utc).replace(tzinfo=None)
    final_state = "RUNNING"
    error: str | None = None
    prepared: list[_PreparedReport] = []
    _emit(progress_callback, "PARSING", len(snapshots), counts)
    if lock_error is not None:
        final_state = "FAILED"
        error = f"{type(lock_error).__name__}: {lock_error}"
    elif _cancelled(request):
        final_state = "CANCELLED"
    else:
        try:
            for outcome in _stream_prepared_reports(
                snapshots, request.workers, imported_at, request.cancellation_requested
            ):
                snapshot = outcome.snapshot
                item = evidence_by_path[snapshot.relative_path]
                if outcome.prepared is None:
                    item["classification"] = str(outcome.classification or "INVALID_REPORT")
                    item["parity"] = "FAIL"
                    item["validation"] = "FAIL"
                    if outcome.reason:
                        item["reason"] = outcome.reason
                    counts["quarantined"] += 1
                    _emit(progress_callback, "PARSING", len(snapshots), counts)
                    continue
                counts["parsed"] += 1
                value = outcome.prepared
                if _payload_hash(value.payload) != value.payload["payload_sha256"]:
                    item["classification"] = "INTEGRITY_QUARANTINE"
                    item["parity"] = "FAIL"
                    item["validation"] = "FAIL"
                    item["reason"] = "prepared payload hash changed before write"
                    counts["quarantined"] += 1
                    _emit(progress_callback, "PARSING", len(snapshots), counts)
                    continue
                item["canonical_report_key"] = value.canonical_report_key
                item["parity"] = "PASS"
                item["validation"] = "PASS"
                prepared.append(value)
                _emit(progress_callback, "PARSING", len(snapshots), counts)

            _emit(progress_callback, "GROUPING", len(snapshots), counts)
            grouped: dict[str, list[_PreparedReport]] = {}
            for item in prepared:
                grouped.setdefault(item.canonical_report_key, []).append(item)
            candidates: list[_PreparedReport] = []
            for key in sorted(grouped):
                group = sorted(grouped[key], key=lambda item: item.snapshot.relative_path)
                if len({item.snapshot.codec_source_sha256 for item in group}) > 1:
                    for item in group:
                        evidence_by_path[item.snapshot.relative_path]["classification"] = "AMBIGUOUS_BATCH_DUPLICATE"
                    counts["ambiguous"] += len(group)
                    continue
                candidates.append(group[0])
                for duplicate in group[1:]:
                    evidence_by_path[duplicate.snapshot.relative_path]["classification"] = "SKIPPED_BATCH_IDENTICAL"
                    counts["identical"] += 1

            if counts["ambiguous"]:
                raise ValueError(
                    "AMBIGUOUS_BATCH_DUPLICATE: the complete import batch was not published"
                )
            _emit(progress_callback, "STAGING", len(snapshots), counts)
            database_path.parent.mkdir(parents=True, exist_ok=True)
            preflight_target_identity = _database_identity(database_path)
            if (
                request.expected_preflight_token is not None
                and preflight_target_identity != expected_target_identity
            ):
                raise ValueError("preflight token does not match current import inputs and target")
            with tempfile.TemporaryDirectory(prefix="mrs3-import-", dir=database_path.parent) as stage_dir:
                stage_path = Path(stage_dir) / database_path.name
                if database_path.is_file():
                    shutil.copyfile(database_path, stage_path)
                connection = duckdb.connect(str(stage_path))
                try:
                    ensure_source_schema(connection)
                    if request.preflight is None:
                        preflight = validate_source_database_structural(connection)
                        if not preflight.valid:
                            raise ValueError(f"source database validation failed: {preflight.errors}")
                    active_rows = connection.execute(
                        "select canonical_report_key,source_sha256 from active_reports"
                    ).fetchall()
                    active_by_key = {str(key): str(value) for key, value in active_rows}
                    active_by_hash = {str(value): str(key) for key, value in active_rows}
                    decisions: list[_WriteDecision] = []
                    for item in candidates:
                        source_hash = item.snapshot.codec_source_sha256
                        evidence_item = evidence_by_path[item.snapshot.relative_path]
                        active_hash_key = active_by_hash.get(source_hash)
                        if active_hash_key is not None:
                            if active_hash_key != item.canonical_report_key:
                                evidence_item["classification"] = "ACTIVE_HASH_CONFLICT"
                                evidence_item["validation"] = "FAIL"
                                counts["quarantined"] += 1
                            else:
                                evidence_item["classification"] = "SKIPPED_IDENTICAL"
                                counts["identical"] += 1
                            continue
                        previous_hash = active_by_key.get(item.canonical_report_key)
                        operation = "replace" if previous_hash is not None else "insert"
                        decisions.append(_WriteDecision(operation, item, previous_hash))

                    replacements = [item for item in decisions if item.operation == "replace"]
                    if replacements:
                        # DuckDB enforces this foreign key across a transaction boundary.
                        connection.execute("begin transaction")
                        try:
                            connection.executemany(
                                "delete from report_payloads where report_id=(select report_id from active_reports where canonical_report_key=?)",
                                [(item.prepared.canonical_report_key,) for item in replacements],
                            )
                            connection.execute("commit")
                        except BaseException:
                            connection.execute("rollback")
                            raise
                    _emit(progress_callback, "WRITING", len(snapshots), counts)
                    for offset in range(0, len(decisions), request.transaction_batch_size):
                        if _cancelled(request):
                            final_state = "CANCELLED"
                            break
                        batch = decisions[offset : offset + request.transaction_batch_size]
                        connection.execute("begin transaction")
                        try:
                            _write_decisions_batch(connection, batch, job_id, imported_at)
                            _validate_written_batch(connection, batch)
                            connection.execute("commit")
                        except BaseException:
                            connection.execute("rollback")
                            raise
                        for decision in batch:
                            classification = "INSERTED" if decision.operation == "insert" else "REPLACED"
                            evidence_by_path[decision.prepared.snapshot.relative_path]["classification"] = classification
                            counter = "inserted" if decision.operation == "insert" else "replaced"
                            counts[counter] += 1
                        _emit(progress_callback, "WRITING", len(snapshots), counts)
                    else:
                        final_state = "COMMITTED"
                finally:
                    connection.close()
                if final_state == "COMMITTED":
                    _emit(progress_callback, "PUBLISHING", len(snapshots), counts)
                    if not _inputs_unchanged(snapshots):
                        raise ValueError("input HTML snapshot changed during import")
                    if _database_identity(database_path) != preflight_target_identity:
                        raise ValueError("source database changed during import")
                    os.replace(stage_path, database_path)
        except _ImportCancelled:
            final_state = "CANCELLED"
        except Exception as exc:
            final_state = "FAILED"
            error = f"{type(exc).__name__}: {exc}"

    if final_state != "COMMITTED":
        counts["inserted"] = 0
        counts["replaced"] = 0
        unpublished = "CANCELLED" if final_state == "CANCELLED" else "NOT_IMPORTED_FAILURE"
        for item in evidence:
            if item["classification"] in {"INSERTED", "REPLACED"}:
                item["classification"] = unpublished
    if not _inputs_unchanged(snapshots):
        final_state = "FAILED"
        error = "input HTML snapshot changed during import"
    for item in evidence:
        if item["classification"] == "NOT_PARSED":
            item["classification"] = "CANCELLED" if final_state == "CANCELLED" else "NOT_IMPORTED_FAILURE"
    manifest_path, manifest_hash, checklist_path, checklist_hash, safe = _write_evidence(
        request, job_id, final_state, counts, evidence, error
    )
    _emit(progress_callback, final_state, len(snapshots), counts)
    result = ImportJobResult(
        job_id=job_id,
        final_state=final_state,
        discovered=len(snapshots),
        parsed=counts["parsed"],
        inserted=counts["inserted"],
        replaced=counts["replaced"],
        identical=counts["identical"],
        ambiguous=counts["ambiguous"],
        quarantined=counts["quarantined"],
        safe_to_delete=safe,
        manifest_path=manifest_path,
        manifest_sha256=manifest_hash,
        checklist_path=checklist_path,
        checklist_sha256=checklist_hash,
        error=error,
    )
    return result
