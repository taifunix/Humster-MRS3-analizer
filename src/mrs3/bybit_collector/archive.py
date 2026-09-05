"""Crash-safe hourly Parquet publication for the Bybit minute spool."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import time
import uuid
from collections.abc import Sequence
from typing import Any

import duckdb

from .aggregation import LIQUIDITY_1M_COLUMNS, LIQUIDITY_1M_SCHEMA
from .storage import MarkerConflictError, SQLiteSpool


HOUR_MS = 3_600_000
ELIGIBILITY_DELAY_MS = 120_000
EXPORT_CYCLE_MS = HOUR_MS + ELIGIBILITY_DELAY_MS
_TMP_RE = re.compile(r"^part-\d{2}\.parquet\.[0-9a-f]{32}\.tmp$")
_SCHEMA_BY_NAME = {name: (data_type, nullable) for name, data_type, nullable in LIQUIDITY_1M_SCHEMA}
_NON_NULL_COLUMNS = frozenset(
    name for name, _data_type, nullable in LIQUIDITY_1M_SCHEMA if not nullable
)
_SMALLINT_COLUMNS = frozenset(
    name for name, data_type, _nullable in LIQUIDITY_1M_SCHEMA if data_type == "SMALLINT"
)


@dataclass(frozen=True, slots=True)
class ArchiveOutcome:
    published: bool
    status: str
    file_name: str | None = None
    row_count: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveVerification:
    valid: bool
    checked: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.valid


@dataclass(frozen=True, slots=True)
class ArchiveRecovery:
    removed_tmp: tuple[str, ...] = ()
    outcomes: tuple[ArchiveOutcome, ...] = ()
    errors: tuple[str, ...] = ()


class HourlyExporter:
    """Publish immutable UTC hourly files and then commit their SQLite marker."""

    def __init__(self, spool: SQLiteSpool, root: Path, collector_version: str = "unknown") -> None:
        if not isinstance(spool, SQLiteSpool):
            raise TypeError("spool must be a SQLiteSpool")
        if not isinstance(collector_version, str) or not collector_version:
            raise ValueError("collector_version must be a non-empty string")
        self.spool = spool
        self.root = Path(root)
        self.collector_version = collector_version

    def export_hour(self, hour_start_ms: int, now_ms: int) -> ArchiveOutcome:
        self._check_hour(hour_start_ms)
        self._check_clock(now_ms)
        file_name = self.file_name(hour_start_ms)
        if now_ms < hour_start_ms + EXPORT_CYCLE_MS:
            return ArchiveOutcome(False, "not_eligible", file_name=file_name)

        marker = {item.hour_start_ms: item for item in self.spool.published_hours()}.get(
            hour_start_ms
        )
        final = self._safe_path(file_name)
        if final is None:
            return ArchiveOutcome(False, "unsafe_path", file_name=file_name, errors=(file_name,))

        if marker is not None:
            if marker.file_name != file_name:
                return ArchiveOutcome(
                    False,
                    "marker_conflict",
                    file_name=file_name,
                    errors=(f"marker names {marker.file_name!r}, expected {file_name!r}",),
                )
            marker_path = self._safe_path(marker.file_name)
            if marker_path is None:
                return ArchiveOutcome(False, "unsafe_marker", file_name=file_name)
            if not marker_path.exists():
                try:
                    self._clear_marker(hour_start_ms)
                except Exception as exc:
                    return ArchiveOutcome(
                        False, "marker_recovery_failed", file_name=file_name, errors=(str(exc),)
                    )
                marker = None
            else:
                valid, errors = self._validate_file(
                    marker_path, hour_start_ms, expected_count=marker.row_count
                )
                if valid:
                    return ArchiveOutcome(
                        True, "already_published", file_name=file_name, row_count=marker.row_count
                    )
                return ArchiveOutcome(
                    False,
                    "invalid_existing",
                    file_name=file_name,
                    row_count=marker.row_count,
                    errors=tuple(errors),
                )

        if final.exists():
            valid, errors = self._validate_file(final, hour_start_ms)
            if not valid:
                return ArchiveOutcome(
                    False,
                    "invalid_existing",
                    file_name=file_name,
                    errors=tuple(errors),
                )
            row_count = self._parquet_row_count(final)
            return self._mark(hour_start_ms, file_name, row_count, now_ms)

        rows = self.spool.read_hour(hour_start_ms)
        final.parent.mkdir(parents=True, exist_ok=True)
        temporary = final.with_name(f"{final.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._write_parquet(temporary, rows, now_ms)
            self._fsync_file(temporary)
            valid, errors = self._validate_file(
                temporary,
                hour_start_ms,
                rows=rows,
                expected_count=len(rows),
            )
            if not valid:
                return ArchiveOutcome(
                    False,
                    "validation_failed",
                    file_name=file_name,
                    row_count=len(rows),
                    errors=tuple(errors),
                )
            try:
                self._publish_temp(temporary, final)
            except FileExistsError:
                # A concurrent publisher won; never replace or delete its final.
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                valid, errors = self._validate_file(final, hour_start_ms)
                if not valid:
                    return ArchiveOutcome(
                        False,
                        "invalid_existing",
                        file_name=file_name,
                        row_count=len(rows),
                        errors=tuple(errors),
                    )
                row_count = self._parquet_row_count(final)
                return self._mark(hour_start_ms, file_name, row_count, now_ms)
            self._fsync_directory(final.parent)
        except Exception as exc:
            return ArchiveOutcome(
                False,
                "export_failed",
                file_name=file_name,
                row_count=len(rows),
                errors=(str(exc),),
            )
        return self._mark(hour_start_ms, file_name, len(rows), now_ms)

    def verify_archive(self) -> ArchiveVerification:
        checked: list[str] = []
        missing: list[str] = []
        invalid: list[str] = []
        errors: list[str] = []
        for marker in self.spool.published_hours():
            checked.append(marker.file_name)
            path = self._safe_path(marker.file_name)
            if path is None or not path.is_file():
                missing.append(marker.file_name)
                errors.append(f"missing marker path: {marker.file_name}")
                continue
            try:
                valid, file_errors = self._validate_file(
                    path, marker.hour_start_ms, expected_count=marker.row_count
                )
            except Exception as exc:
                valid, file_errors = False, [str(exc)]
            if not valid:
                invalid.append(marker.file_name)
                errors.extend(f"{marker.file_name}: {item}" for item in file_errors)
        return ArchiveVerification(
            not missing and not invalid,
            tuple(checked),
            tuple(missing),
            tuple(invalid),
            tuple(errors),
        )

    def recover(self, now_ms: int | None = None) -> ArchiveRecovery:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        self._check_clock(now)
        removed: list[str] = []
        errors: list[str] = []
        cutoff = now - EXPORT_CYCLE_MS
        for path in self.root.glob("liquidity_1m/date=*/part-*.parquet.*.tmp"):
            if not path.is_file() or not _TMP_RE.fullmatch(path.name):
                continue
            try:
                stale = path.stat().st_mtime * 1000 < cutoff
            except OSError:
                errors.append(f"{path}: unable to inspect temporary file")
                continue
            if stale:
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{path}: unable to remove temporary file: {exc}")
                else:
                    removed.append(path.relative_to(self.root).as_posix())

        markers = self.spool.published_hours()
        marker_hours = {item.hour_start_ms for item in markers}
        outcomes: list[ArchiveOutcome] = []

        def record(outcome: ArchiveOutcome) -> None:
            outcomes.append(outcome)
            if outcome.errors:
                errors.extend(
                    f"{outcome.file_name or outcome.status}: {error}"
                    for error in outcome.errors
                )
            elif not outcome.published:
                errors.append(f"{outcome.file_name or outcome.status}: {outcome.status}")

        for marker in markers:
            marker_path = self._safe_path(marker.file_name)
            if marker_path is not None and marker_path.is_file():
                # Immutable marked files do not need a full-history SQLite comparison.
                continue
            hour_start_ms = marker.hour_start_ms
            if hour_start_ms < 0 or now < hour_start_ms + EXPORT_CYCLE_MS:
                continue
            record(self.export_hour(hour_start_ms, now))

        for hour_start_ms in self.spool.distinct_hour_starts():
            if hour_start_ms in marker_hours or hour_start_ms < 0:
                continue
            if now < hour_start_ms + EXPORT_CYCLE_MS:
                continue
            record(self.export_hour(hour_start_ms, now))
        return ArchiveRecovery(tuple(removed), tuple(outcomes), tuple(errors))

    @staticmethod
    def _publish_temp(temporary: Path, final: Path) -> None:
        # Windows rename is MoveFileEx without replacement; POSIX hard-link is
        # the no-clobber primitive. Both publish the already validated bytes.
        if os.name == "nt":
            os.rename(temporary, final)
        else:
            os.link(temporary, final)
            temporary.unlink()

    @staticmethod
    def file_name(hour_start_ms: int) -> str:
        instant = datetime.fromtimestamp(hour_start_ms / 1000, UTC)
        return (
            f"liquidity_1m/date={instant:%Y-%m-%d}/part-{instant:%H}.parquet"
        )

    def _write_parquet(self, path: Path, rows: list[dict[str, Any]], now_ms: int) -> None:
        for row in rows:
            for column_name in _SMALLINT_COLUMNS:
                value = row[column_name]
                if type(value) is not int or value < 0 or value > 32_767:
                    raise ValueError(
                        f"{column_name}={value!r} exceeds SMALLINT range 0..32767"
                    )
        connection = duckdb.connect()
        try:
            definitions = []
            for column, data_type, nullable in LIQUIDITY_1M_SCHEMA:
                required = " NOT NULL" if not nullable else ""
                definitions.append(f'"{column}" {data_type}{required}')
            connection.execute(f'CREATE TABLE hourly ({", ".join(definitions)})')
            schema_errors = self._validate_description(
                connection.execute("DESCRIBE hourly").fetchall()
            )
            if schema_errors:
                raise ValueError("invalid liquidity_1m schema: " + "; ".join(schema_errors))
            values = [tuple(row[column] for column in LIQUIDITY_1M_COLUMNS) for row in rows]
            if values:
                placeholders = ", ".join("?" for _ in LIQUIDITY_1M_COLUMNS)
                connection.executemany(f"INSERT INTO hourly VALUES ({placeholders})", values)
            metadata = {
                "schema_name": "bybit_liquidity_1m",
                "schema_version": "2",
                "collector_version": self.collector_version,
                "exchange": "bybit",
                "category": "linear",
                "created_at_utc": datetime.fromtimestamp(now_ms / 1000, UTC).isoformat(),
            }
            options = ", ".join(
                f"'{key}': '{str(value).replace(chr(39), chr(39) * 2)}'"
                for key, value in metadata.items()
            )
            connection.execute(
                f"COPY hourly TO '{self._sql_path(path)}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{{options}}})"
            )
        finally:
            connection.close()

    def _validate_file(
        self,
        path: Path,
        hour_start_ms: int,
        rows: list[dict[str, Any]] | None = None,
        expected_count: int | None = None,
    ) -> tuple[bool, list[str]]:
        errors: list[str] = []
        connection = duckdb.connect()
        try:
            description = connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{self._sql_path(path)}', hive_partitioning=false)"
            ).fetchall()
            actual_columns = tuple(str(row[0]) for row in description)
            if actual_columns != LIQUIDITY_1M_COLUMNS:
                errors.append(f"schema columns {actual_columns!r}")
            else:
                errors.extend(self._validate_description(description, reader_relation=True))

            metadata_rows = connection.execute(
                f"SELECT key, value FROM parquet_kv_metadata('{self._sql_path(path)}')"
            ).fetchall()
            metadata: dict[str, list[str]] = {}
            for key, value in metadata_rows:
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                if isinstance(value, bytes):
                    value = value.decode("utf-8")
                metadata.setdefault(str(key), []).append(str(value))
            required = {
                "schema_name": "bybit_liquidity_1m",
                "schema_version": "2",
                "collector_version": None,
                "exchange": "bybit",
                "category": "linear",
                "created_at_utc": None,
            }
            for key, expected in required.items():
                if expected is not None and metadata.get(key) != [expected]:
                    errors.append(f"metadata {key!r} is not {expected!r}")
            if len(metadata.get("collector_version", ())) != 1 or not metadata["collector_version"][0]:
                errors.append("metadata 'collector_version' is missing")
            created_at = metadata.get("created_at_utc", ())
            if len(created_at) != 1 or not created_at[0]:
                errors.append("metadata 'created_at_utc' is missing")
            else:
                try:
                    parsed = datetime.fromisoformat(created_at[0])
                    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
                        raise ValueError
                except ValueError:
                    errors.append("metadata 'created_at_utc' is not UTC ISO-8601")

            actual_rows = connection.execute(
                f"SELECT minute_ts_ms, symbol FROM read_parquet('{self._sql_path(path)}', hive_partitioning=false) "
                "ORDER BY minute_ts_ms, symbol"
            ).fetchall()
            full_rows = connection.execute(
                f"SELECT * FROM read_parquet('{self._sql_path(path)}', hive_partitioning=false)"
            ).fetchall()
            for index, column_name in enumerate(LIQUIDITY_1M_COLUMNS):
                if column_name in _NON_NULL_COLUMNS and any(row[index] is None for row in full_rows):
                    errors.append(f"{column_name} contains NULL")
            if expected_count is not None and len(actual_rows) != expected_count:
                errors.append(f"row count {len(actual_rows)} != {expected_count}")
            if len(set(actual_rows)) != len(actual_rows):
                errors.append("duplicate (minute_ts_ms,symbol) keys")
            if any(ts < hour_start_ms or ts >= hour_start_ms + HOUR_MS for ts, _ in actual_rows):
                errors.append("timestamp outside hour")
            if rows is not None:
                expected_keys = sorted(
                    (row["minute_ts_ms"], row["symbol"]) for row in rows
                )
                if actual_rows != expected_keys:
                    errors.append("row keys differ from SQLite snapshot")
        except Exception as exc:
            errors.append(f"DuckDB validation failed: {exc}")
        finally:
            connection.close()
        return not errors, errors

    @staticmethod
    def _validate_description(
        description: Sequence[Sequence[Any]], *, reader_relation: bool = False
    ) -> list[str]:
        errors: list[str] = []
        for column_name, column_type, nullable, *_ in description:
            expected = _SCHEMA_BY_NAME.get(str(column_name))
            if expected is None:
                errors.append(f"schema column {column_name!r} is unknown")
                continue
            expected_type, expected_nullable = expected
            if str(column_type).upper() != expected_type:
                errors.append(f"{column_name} type {column_type!r}")
            declared_nullable = str(nullable).upper()
            if declared_nullable not in {"YES", "NO"}:
                errors.append(f"{column_name} nullable declaration {nullable!r}")
            elif expected_nullable and declared_nullable == "NO":
                errors.append(f"{column_name} is declared NOT NULL")
            # DuckDB's read_parquet relation currently widens physical fields to YES;
            # required-value enforcement below still rejects actual NULLs.
            elif not expected_nullable and not reader_relation and declared_nullable != "NO":
                errors.append(f"{column_name} must be declared NOT NULL")
        return errors

    def _parquet_row_count(self, path: Path) -> int:
        connection = duckdb.connect()
        try:
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{self._sql_path(path)}', hive_partitioning=false)"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def _mark(self, hour_start_ms: int, file_name: str, row_count: int, now_ms: int) -> ArchiveOutcome:
        try:
            self.spool.mark_published(hour_start_ms, file_name, row_count, now_ms)
        except MarkerConflictError as exc:
            return ArchiveOutcome(
                False, "marker_conflict", file_name=file_name, row_count=row_count, errors=(str(exc),)
            )
        except Exception as exc:
            return ArchiveOutcome(
                False, "marker_failed", file_name=file_name, row_count=row_count, errors=(str(exc),)
            )
        return ArchiveOutcome(True, "published", file_name=file_name, row_count=row_count)

    def _clear_marker(self, hour_start_ms: int) -> None:
        self.spool.clear_published_hour(hour_start_ms)

    def _safe_path(self, file_name: str) -> Path | None:
        root = self.root.resolve()
        candidate = (self.root / Path(file_name)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _sql_path(path: Path) -> str:
        return path.as_posix().replace("'", "''")

    @staticmethod
    def _fsync_file(path: Path) -> None:
        descriptor = os.open(path, os.O_RDWR)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return  # Windows has no portable directory fsync primitive.
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _check_hour(hour_start_ms: int) -> None:
        if type(hour_start_ms) is not int or hour_start_ms < 0 or hour_start_ms % HOUR_MS:
            raise ValueError("hour_start_ms must be a non-negative UTC hour")

    @staticmethod
    def _check_clock(now_ms: int) -> None:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")


__all__ = [
    "ArchiveOutcome",
    "ArchiveRecovery",
    "ArchiveVerification",
    "ELIGIBILITY_DELAY_MS",
    "EXPORT_CYCLE_MS",
    "HOUR_MS",
    "HourlyExporter",
]
