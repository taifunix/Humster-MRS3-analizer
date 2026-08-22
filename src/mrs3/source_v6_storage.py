"""Fresh-only compact DuckDB storage for Source v6 fragments.

The only fact payload is one compressed canonical JSON blob per fragment. The
metadata columns are the index; samples, actions, cycles and events are never
stored as physical rows.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence
from uuid import uuid4
import zlib

import duckdb

from .source_v6 import (
    EncodedSourceV6Fragment,
    PointIdentity,
    SourceV6Error,
    SourceV6Fragment,
    decode_fragment,
    encode_fragment,
)


V6_SCHEMA_VERSION = 6
V6_FINGERPRINT = "source-v6-fresh-compact-v1"
SEGMENT_FINGERPRINT = "source-v6-import-segment-v1"
# Each attached segment holds file descriptors; DuckDB fails around 1020 open
# databases, so a large corpus is copied in bounded groups.
SEGMENT_ATTACH_BATCH = 64
# Verification windows: DuckDB materialises a full result set per execute.
_VERIFY_BATCH = 128
# `ProcessPoolExecutor` raises above this on Windows.
_MAX_VERIFY_WORKERS = 61


class SourceV6StorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ImportReceipt:
    status: str
    fragment_id: str
    database_id: str
    generation: int
    safe_to_delete: str
    inserted: bool
    quarantine_count: int


@dataclass(frozen=True, slots=True)
class SourceV6SegmentOutcome:
    ordinal: int
    relative_path: str
    status: str
    source_sha256: str | None = None
    fragment_id: str | None = None
    reason: str | None = None
    source_size: int | None = None
    source_mtime_ns: int | None = None
    winner_ordinal: int | None = None


@dataclass(frozen=True, slots=True)
class SourceV6SegmentReceipt:
    path: Path
    segment_id: str
    run_token: str
    kind: str
    ordinal_start: int
    ordinal_end: int
    outcome_count: int
    row_count: int
    outcome_digest: str
    compact_digest: str
    database_id: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_id() -> str:
    return str(uuid4())


def source_content_digest(fragment_ids: Iterable[str]) -> str:
    """Hash sorted canonical identities, independent of physical row order."""
    return sha256("".join(sorted(set(str(item) for item in fragment_ids))).encode("ascii")).hexdigest()


def _schema_info(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    try:
        return dict(connection.execute("select key, value from schema_info").fetchall())
    except duckdb.Error as error:
        raise SourceV6StorageError("not a Source v6 database") from error


def _require_fresh(info: dict[str, str]) -> None:
    if info.get("schema_version") != str(V6_SCHEMA_VERSION):
        raise SourceV6StorageError("database is not fresh Source v6")
    if info.get("fingerprint") != V6_FINGERPRINT:
        raise SourceV6StorageError("unknown Source v6 schema fingerprint")


def ensure_source_v6_schema(path: str | Path, *, database_id: str | None = None) -> str:
    return create_v6_database(path, database_id=database_id)


def validate_source_v6_database(path: str | Path) -> dict[str, str]:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        info = _schema_info(connection)
        _require_fresh(info)
        return info
    finally:
        connection.close()


def create_v6_database(path: str | Path, *, database_id: str | None = None) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(target))
    try:
        existing = connection.execute("select table_name from information_schema.tables where table_schema='main'").fetchall()
        if existing:
            info = _schema_info(connection)
            _require_fresh(info)
            return info["database_id"]
        dbid = database_id or _db_id()
        connection.execute("begin")
        connection.execute("create table schema_info(key varchar primary key, value varchar not null)")
        connection.executemany(
            "insert into schema_info values (?, ?)",
            [("schema_version", str(V6_SCHEMA_VERSION)), ("fingerprint", V6_FINGERPRINT), ("database_id", dbid), ("mutation_generation", "0"), ("source_content_digest", source_content_digest(()))],
        )
        connection.execute("""
            create table compact_fragments(
                fragment_id varchar primary key,
                source_sha256 varchar unique not null,
                source_name varchar not null,
                point_key varchar not null,
                report_start_ms bigint not null,
                report_end_ms bigint not null,
                stitchability varchar not null,
                header_json varchar not null,
                header_sha256 varchar not null,
                payload_blob blob not null,
                codec varchar not null,
                payload_sha256 varchar not null,
                action_count integer not null,
                cycle_count integer not null,
                event_count integer not null,
                wallet_sample_count integer not null,
                equity_sample_count integer not null,
                active boolean not null default true,
                inactive_reason varchar,
                winner_fragment_id varchar
            )
        """)
        connection.execute("create index compact_fragments_point_idx on compact_fragments(point_key, report_start_ms, report_end_ms, fragment_id)")
        connection.execute("create index compact_fragments_interval_idx on compact_fragments(report_start_ms, report_end_ms, fragment_id)")
        connection.execute("create table points(point_key varchar primary key)")
        connection.execute("""
            create table fragment_origins(
                fragment_id varchar not null,
                source_sha256 varchar not null,
                source_name varchar not null,
                origin_database_id varchar not null,
                primary key(fragment_id, source_sha256, origin_database_id)
            )
        """)
        connection.execute("create view origins as select fragment_id, source_sha256, source_name, origin_database_id from fragment_origins")
        connection.execute("create view fragments as select fragment_id, source_sha256, source_name, point_key, report_start_ms, report_end_ms, cast(null as decimal(38,18)) as initial_balance, cast(null as decimal(38,18)) as fixed_order_balance, cast(null as decimal(38,18)) as balance_percentage, cast(null as varchar) as settings_fingerprint, stitchability, header_json as metrics_json, active, inactive_reason, winner_fragment_id from compact_fragments")
        connection.execute("create table import_audit(audit_id varchar primary key, fragment_id varchar not null, source_sha256 varchar not null, started_at_utc varchar not null, committed_at_utc varchar, status varchar not null, generation_before bigint not null, generation_after bigint not null, safe_to_delete varchar not null, quarantine_count integer not null, error varchar)")
        connection.execute("create table quarantine(fragment_id varchar not null, source_sha256 varchar not null, reason varchar not null, created_at_utc varchar not null)")
        connection.execute("create table day_ownership(fragment_id varchar not null, utc_day date not null, ownership varchar not null, active boolean not null default true, reason varchar, winner_fragment_id varchar, primary key(fragment_id, utc_day))")
        connection.execute("create table day_dispositions(fragment_id varchar not null, utc_day date not null, disposition varchar not null, note varchar, primary key(fragment_id, utc_day), check(disposition in ('IGNORE_INCOMING', 'EXCLUDE_DAY_AS_GAP')))")
        connection.execute("create table fact_ownership(fact_kind varchar not null, fact_id varchar not null, fragment_id varchar not null, owner_fragment_id varchar, active boolean not null, reason varchar, winner_fragment_id varchar, primary key(fact_kind, fact_id, fragment_id))")
        connection.execute("create table fragment_resolutions(outgoing_fragment_id varchar not null, incoming_fragment_id varchar not null, status varchar not null, reason varchar, boundary_ms bigint, evidence_json varchar, primary key(outgoing_fragment_id, incoming_fragment_id))")
        connection.execute("commit")
        return dbid
    except Exception:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def database_info(path: str | Path) -> dict[str, str]:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        info = _schema_info(connection)
        _require_fresh(info)
        return info
    finally:
        connection.close()


def _table_counts(path: Path) -> tuple[tuple[str, int], ...]:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        tables = connection.execute("select table_name from information_schema.tables where table_schema = 'main' order by table_name").fetchall()
        return tuple((str(row[0]), int(connection.execute(f'select count(*) from "{str(row[0]).replace(chr(34), chr(34) * 2)}"').fetchone()[0])) for row in tables)
    finally:
        connection.close()


def compact_v6_database(source_path: str | Path, target_path: str | Path) -> None:
    """Repack one validated compact DB without changing its logical content."""
    source = Path(source_path).resolve()
    target = Path(target_path).resolve()
    if source == target:
        raise SourceV6StorageError("compact target must differ from source")
    if target.exists():
        raise SourceV6StorageError(f"compact target already exists: {target}")
    source_info = database_info(source)
    source_counts = _table_counts(source)
    connection = duckdb.connect(str(target))
    try:
        target_catalog = str(connection.execute("pragma database_list").fetchone()[1]).replace('"', '""')
        source_literal = str(source).replace("'", "''")
        connection.execute(f"attach '{source_literal}' as source_v6_input (read_only)")
        connection.execute(f'copy from database source_v6_input to "{target_catalog}"')
        connection.execute("checkpoint")
    except Exception:
        connection.close()
        for path in (target, Path(f"{target}.wal"), Path(f"{target}.tmp")):
            path.unlink(missing_ok=True)
        raise
    else:
        connection.close()
    if database_info(target) != source_info or _table_counts(target) != source_counts:
        for path in (target, Path(f"{target}.wal"), Path(f"{target}.tmp")):
            path.unlink(missing_ok=True)
        raise SourceV6StorageError("compacted database metadata mismatch")


def preflight_import(path: str | Path, fragment: SourceV6Fragment) -> str:
    info = validate_source_v6_database(path)
    return f"{info['database_id']}:{info['mutation_generation']}:{fragment.fragment_id}"


def _header(fragment: SourceV6Fragment, encoded: EncodedSourceV6Fragment) -> str:
    return json.dumps({
        "schema_version": fragment.schema_version,
        "source_sha256": fragment.source_sha256,
        "source_name": fragment.source_name,
        "point": fragment.point.canonical_key,
        "report_start_ms": fragment.report_start_ms,
        "report_end_ms": fragment.report_end_ms,
        "stitchability": fragment.stitchability,
        "initial_balance": str(fragment.initial_balance),
        "fixed_order_balance": str(fragment.fixed_order_balance),
        "balance_percentage": str(fragment.balance_percentage),
        "settings_fingerprint": fragment.settings_fingerprint,
        "metrics": dict(fragment.metrics),
        "open_tail_cycle_ids": list(fragment.open_tail_cycle_ids),
        "codec": encoded.codec,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_fragment(row: tuple[object, ...]) -> SourceV6Fragment:
    try:
        header = json.loads(str(row[7]))
        payload = bytes(row[9])
        if sha256(str(row[7]).encode("utf-8")).hexdigest() != str(row[8]) or sha256(payload).hexdigest() != str(row[11]):
            raise SourceV6StorageError("compact checksum mismatch")
        fragment = decode_fragment(payload, codec=str(row[10]), expected_fragment_id=str(row[0]))
        fragment = replace(fragment, source_sha256=str(row[1]), source_name=str(row[2]))
        if (
            header.get("schema_version") != fragment.schema_version
            or header.get("source_sha256") != fragment.source_sha256
            or header.get("source_name") != fragment.source_name
            or header.get("point") != fragment.point.canonical_key
            or header.get("report_start_ms") != fragment.report_start_ms
            or header.get("report_end_ms") != fragment.report_end_ms
            or header.get("stitchability") != fragment.stitchability
            or header.get("settings_fingerprint") != fragment.settings_fingerprint
            or header.get("metrics") != dict(fragment.metrics)
            or tuple(header.get("open_tail_cycle_ids", ())) != fragment.open_tail_cycle_ids
            or Decimal(str(header.get("initial_balance"))) != fragment.initial_balance
            or Decimal(str(header.get("fixed_order_balance"))) != fragment.fixed_order_balance
            or Decimal(str(header.get("balance_percentage"))) != fragment.balance_percentage
            or header.get("codec") != str(row[10])
            or row[3] != fragment.point.canonical_key
            or int(row[4]) != fragment.report_start_ms
            or int(row[5]) != fragment.report_end_ms
            or row[6] != fragment.stitchability
        ):
            raise SourceV6StorageError("compact header mismatch")
        if (len(fragment.actions), len(fragment.cycles), len(fragment.events), len(fragment.wallet_samples), len(fragment.equity_samples)) != tuple(int(row[index]) for index in (12, 13, 14, 15, 16)):
            raise SourceV6StorageError("compact count mismatch")
        return fragment
    except (SourceV6Error, TypeError, ValueError) as error:
        raise SourceV6StorageError(f"corrupt compact fragment {row[0]}") from error


def _select_row(connection: duckdb.DuckDBPyConnection, fragment_id: str) -> tuple[object, ...]:
    row = connection.execute("select * from compact_fragments where fragment_id = ?", [fragment_id]).fetchone()
    if row is None:
        raise SourceV6StorageError("fragment not found")
    return row


def _segment_token(value: str | None) -> str:
    token = value or "adhoc"
    if not token or token in {".", ".."} or any(char in token for char in "\\/"):
        raise SourceV6StorageError("invalid Source v6 segment run token")
    return token


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _sha256_hex(value: object) -> bool:
    text = str(value) if value is not None else ""
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _segment_outcome_payload(outcome: SourceV6SegmentOutcome) -> dict[str, object]:
    return {
        "ordinal": outcome.ordinal,
        "relative_path": outcome.relative_path,
        "status": outcome.status,
        "source_sha256": outcome.source_sha256,
        "fragment_id": outcome.fragment_id,
        "reason": outcome.reason,
        "source_size": outcome.source_size,
        "source_mtime_ns": outcome.source_mtime_ns,
        "winner_ordinal": outcome.winner_ordinal,
    }


def _segment_outcome_digest(outcomes: Sequence[SourceV6SegmentOutcome]) -> str:
    document = "\n".join(
        json.dumps(
            _segment_outcome_payload(outcome),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for outcome in outcomes
    )
    return sha256(document.encode("utf-8")).hexdigest()


def _segment_compact_digest(rows: Sequence[tuple[int, tuple[object, ...]]]) -> str:
    digest = sha256()
    for ordinal, row in rows:
        compact = row[1:] if len(row) == 18 else row
        values = [
            ordinal,
            *compact[:9],
            compact[10],
            compact[11],
            *compact[12:],
        ]
        digest.update(
            json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        digest.update(bytes(compact[9]))
    return digest.hexdigest()


def _segment_row(
    fragment: SourceV6Fragment, encoded: EncodedSourceV6Fragment
) -> tuple[object, ...]:
    header_json = _header(fragment, encoded)
    return (
        fragment.fragment_id,
        fragment.source_sha256,
        fragment.source_name,
        fragment.point.canonical_key,
        fragment.report_start_ms,
        fragment.report_end_ms,
        fragment.stitchability,
        header_json,
        sha256(header_json.encode("utf-8")).hexdigest(),
        encoded.payload,
        encoded.codec,
        sha256(encoded.payload).hexdigest(),
        len(fragment.actions),
        len(fragment.cycles),
        len(fragment.events),
        len(fragment.wallet_samples),
        len(fragment.equity_samples),
    )


def _segment_row_tuple(row: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(row[1:])


def _normalise_segment_prepared(
    prepared_rows: Iterable[tuple[int, SourceV6Fragment, EncodedSourceV6Fragment]],
) -> dict[int, tuple[SourceV6Fragment, EncodedSourceV6Fragment]]:
    prepared: dict[int, tuple[SourceV6Fragment, EncodedSourceV6Fragment]] = {}
    for item in prepared_rows:
        try:
            ordinal, fragment, encoded = item
        except (TypeError, ValueError) as error:
            raise SourceV6StorageError(
                "segment prepared rows must be (ordinal, fragment, encoded)"
            ) from error
        if ordinal in prepared:
            raise SourceV6StorageError("duplicate segment prepared ordinal")
        prepared[int(ordinal)] = (fragment, encoded)
    return prepared


def _validate_segment_outcome_order(
    outcomes: Iterable[SourceV6SegmentOutcome],
) -> tuple[SourceV6SegmentOutcome, ...]:
    ordered = tuple(sorted(outcomes, key=lambda item: item.ordinal))
    if not ordered:
        raise SourceV6StorageError("Source v6 segment cannot be empty")
    ordinals = tuple(item.ordinal for item in ordered)
    if any(
        isinstance(item, bool) or not isinstance(item, int)
        for item in ordinals
    ) or len(set(ordinals)) != len(ordinals):
        raise SourceV6StorageError("segment outcomes have duplicate or invalid ordinal")
    if ordinals != tuple(range(ordinals[0], ordinals[-1] + 1)):
        raise SourceV6StorageError("segment outcomes have an ordinal gap")
    return ordered


def _validate_segment_inputs(
    outcomes: Iterable[SourceV6SegmentOutcome],
    prepared_rows: Iterable[tuple[int, SourceV6Fragment, EncodedSourceV6Fragment]],
) -> tuple[tuple[SourceV6SegmentOutcome, ...], dict[int, tuple[SourceV6Fragment, EncodedSourceV6Fragment]], tuple[tuple[int, tuple[object, ...]], ...]]:
    ordered = _validate_segment_outcome_order(outcomes)
    prepared = _normalise_segment_prepared(prepared_rows)
    rows: list[tuple[int, tuple[object, ...]]] = []
    prepared_ordinals = {item.ordinal for item in ordered if item.status == "PREPARED"}
    if set(prepared) != prepared_ordinals:
        raise SourceV6StorageError("segment outcomes and prepared rows do not correspond")
    for outcome in ordered:
        if outcome.status not in {"PREPARED", "QUARANTINED"}:
            raise SourceV6StorageError("segment contains an infrastructure failure outcome")
        if not _sha256_hex(outcome.source_sha256):
            raise SourceV6StorageError("segment outcome must carry a real source SHA-256")
        if outcome.status == "QUARANTINED" and not outcome.reason:
            raise SourceV6StorageError("quarantined segment outcome requires a reason")
        if outcome.status == "PREPARED":
            fragment, encoded = prepared[outcome.ordinal]
            if fragment.source_sha256 != outcome.source_sha256:
                raise SourceV6StorageError("segment source SHA does not match prepared row")
            if outcome.fragment_id not in {None, fragment.fragment_id}:
                raise SourceV6StorageError("segment fragment identity mismatch")
            if encoded.fragment_id != fragment.fragment_id:
                raise SourceV6StorageError("encoded fragment identity mismatch")
            rows.append((outcome.ordinal, _segment_row(fragment, encoded)))
        elif outcome.ordinal in prepared:
            raise SourceV6StorageError("non-accepted segment outcome has a compact row")
    return ordered, prepared, tuple(rows)


def _validate_segment_compact_rows(
    outcomes: Iterable[SourceV6SegmentOutcome],
    compact_rows: Sequence[tuple[int, tuple[object, ...]]],
) -> tuple[tuple[SourceV6SegmentOutcome, ...], tuple[tuple[int, tuple[object, ...]], ...]]:
    """Validate already-sealed rows for pass-through without decoding payloads."""
    ordered = _validate_segment_outcome_order(outcomes)
    rows_by_ordinal: dict[int, tuple[object, ...]] = {}
    for ordinal, row in compact_rows:
        key = int(ordinal)
        if key in rows_by_ordinal:
            raise SourceV6StorageError("duplicate segment prepared ordinal")
        rows_by_ordinal[key] = _segment_row_tuple(row) if len(row) == 18 else tuple(row)
    prepared_ordinals = {item.ordinal for item in ordered if item.status == "PREPARED"}
    if set(rows_by_ordinal) != prepared_ordinals:
        raise SourceV6StorageError("segment outcomes and prepared rows do not correspond")
    rows: list[tuple[int, tuple[object, ...]]] = []
    for outcome in ordered:
        if outcome.status not in {"PREPARED", "QUARANTINED"}:
            raise SourceV6StorageError("segment contains an infrastructure failure outcome")
        if not _sha256_hex(outcome.source_sha256):
            raise SourceV6StorageError("segment outcome must carry a real source SHA-256")
        if outcome.status == "QUARANTINED" and not outcome.reason:
            raise SourceV6StorageError("quarantined segment outcome requires a reason")
        if outcome.status != "PREPARED":
            continue
        row = rows_by_ordinal[outcome.ordinal]
        if str(row[1]) != outcome.source_sha256:
            raise SourceV6StorageError("segment source SHA does not match prepared row")
        if outcome.fragment_id not in {None, str(row[0])}:
            raise SourceV6StorageError("segment fragment identity mismatch")
        header_json = str(row[7])
        if sha256(header_json.encode("utf-8")).hexdigest() != str(row[8]):
            raise SourceV6StorageError("compact checksum mismatch")
        payload = bytes(row[9])
        if sha256(payload).hexdigest() != str(row[11]):
            raise SourceV6StorageError("compact checksum mismatch")
        rows.append((outcome.ordinal, row))
    return ordered, tuple(rows)


def write_source_v6_segment(
    path: str | Path,
    outcomes: Iterable[SourceV6SegmentOutcome],
    prepared_rows: Iterable[tuple[int, SourceV6Fragment, EncodedSourceV6Fragment]] = (),
    *,
    run_token: str | None = None,
    kind: str = "leaf",
    compact_rows: Sequence[tuple[int, tuple[object, ...]]] | None = None,
) -> SourceV6SegmentReceipt:
    """Write and seal one private segment without creating a Source DB identity."""
    target = Path(path).resolve()
    if target.exists():
        raise SourceV6StorageError(f"segment target already exists: {target}")
    token = _segment_token(run_token)
    if kind not in {"leaf", "intermediate"}:
        raise SourceV6StorageError("invalid Source v6 segment kind")
    if compact_rows is not None:
        if prepared_rows:
            raise SourceV6StorageError("segment accepts prepared rows or compact rows, not both")
        ordered, rows = _validate_segment_compact_rows(outcomes, compact_rows)
    else:
        ordered, _, rows = _validate_segment_inputs(outcomes, prepared_rows)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{token}.tmp")
    if temporary.exists():
        raise SourceV6StorageError(f"segment temporary target already exists: {temporary}")
    segment_id = uuid4().hex
    outcome_digest = _segment_outcome_digest(ordered)
    compact_digest = _segment_compact_digest(rows)
    connection: duckdb.DuckDBPyConnection | None = None
    try:
        connection = duckdb.connect(str(temporary))
        connection.execute(
            "create table segment_manifest("
            "fingerprint varchar primary key, segment_id varchar not null, "
            "run_token varchar not null, kind varchar not null, "
            "ordinal_start bigint not null, ordinal_end bigint not null, "
            "outcome_count bigint not null, row_count bigint not null, "
            "outcome_digest varchar not null, compact_digest varchar not null, "
            "sealed boolean not null, checkpointed boolean not null, "
            "created_at_utc varchar not null)"
        )
        connection.execute(
            "create table segment_outcomes("
            "ordinal bigint primary key, relative_path varchar not null, "
            "status varchar not null, source_sha256 varchar not null, "
            "fragment_id varchar, reason varchar, source_size bigint, "
            "source_mtime_ns bigint, winner_ordinal bigint)"
        )
        connection.execute(
            "create table segment_compact_rows("
            "ordinal bigint primary key, fragment_id varchar not null, "
            "source_sha256 varchar not null, source_name varchar not null, "
            "point_key varchar not null, report_start_ms bigint not null, "
            "report_end_ms bigint not null, stitchability varchar not null, "
            "header_json varchar not null, header_sha256 varchar not null, "
            "payload_blob blob not null, codec varchar not null, "
            "payload_sha256 varchar not null, action_count integer not null, "
            "cycle_count integer not null, event_count integer not null, "
            "wallet_sample_count integer not null, equity_sample_count integer not null)"
        )
        connection.executemany(
            "insert into segment_outcomes values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    outcome.ordinal,
                    outcome.relative_path,
                    outcome.status,
                    outcome.source_sha256,
                    outcome.fragment_id,
                    outcome.reason,
                    outcome.source_size,
                    outcome.source_mtime_ns,
                    outcome.winner_ordinal,
                )
                for outcome in ordered
            ],
        )
        connection.executemany(
            "insert into segment_compact_rows values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(ordinal, *row) for ordinal, row in rows],
        )
        connection.execute(
            "insert into segment_manifest values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                SEGMENT_FINGERPRINT,
                segment_id,
                token,
                kind,
                ordered[0].ordinal,
                ordered[-1].ordinal,
                len(ordered),
                len(rows),
                outcome_digest,
                compact_digest,
                True,
                True,
                _utc_now(),
            ],
        )
        connection.execute("checkpoint")
        connection.close()
        connection = None
        if Path(f"{temporary}.wal").exists():
            raise SourceV6StorageError("sealed segment still has a WAL")
        temporary.replace(target)
        return SourceV6SegmentReceipt(
            target,
            segment_id,
            token,
            kind,
            ordered[0].ordinal,
            ordered[-1].ordinal,
            len(ordered),
            len(rows),
            outcome_digest,
            compact_digest,
        )
    except Exception:
        if connection is not None:
            connection.close()
        _unlink_quietly(temporary)
        _unlink_quietly(Path(f"{temporary}.wal"))
        raise


def _segment_tables(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'main'"
        ).fetchall()
    }


def _read_source_v6_segment(
    path: str | Path,
) -> tuple[SourceV6SegmentReceipt, tuple[SourceV6SegmentOutcome, ...], tuple[tuple[int, tuple[object, ...]], ...]]:
    segment = Path(path).resolve()
    if not segment.is_file():
        raise SourceV6StorageError(f"segment does not exist: {segment}")
    if Path(f"{segment}.wal").exists():
        raise SourceV6StorageError("segment has an uncheckpointed WAL")
    connection = duckdb.connect(str(segment), read_only=True)
    try:
        if _segment_tables(connection) != {
            "segment_manifest",
            "segment_outcomes",
            "segment_compact_rows",
        }:
            raise SourceV6StorageError("segment schema mismatch")
        manifest = connection.execute(
            "select fingerprint, segment_id, run_token, kind, ordinal_start, "
            "ordinal_end, outcome_count, row_count, outcome_digest, "
            "compact_digest, sealed, checkpointed from segment_manifest"
        ).fetchall()
        if len(manifest) != 1:
            raise SourceV6StorageError("segment manifest must contain one row")
        row = manifest[0]
        if row[0] != SEGMENT_FINGERPRINT or not row[10] or not row[11]:
            raise SourceV6StorageError("segment fingerprint or seal mismatch")
        outcomes = tuple(
            SourceV6SegmentOutcome(*item)
            for item in connection.execute(
                "select ordinal, relative_path, status, source_sha256, "
                "fragment_id, reason, source_size, source_mtime_ns, winner_ordinal "
                "from segment_outcomes order by ordinal"
            ).fetchall()
        )
        if len(outcomes) != int(row[6]):
            raise SourceV6StorageError("segment outcome count mismatch")
        ordinals = tuple(item.ordinal for item in outcomes)
        if not outcomes or ordinals != tuple(range(int(row[4]), int(row[5]) + 1)):
            raise SourceV6StorageError("segment ordinal gap or overlap")
        if _segment_outcome_digest(outcomes) != row[8]:
            raise SourceV6StorageError("segment outcome digest mismatch")
        compact_rows = tuple(
            (int(item[0]), tuple(item))
            for item in connection.execute(
                "select ordinal, fragment_id, source_sha256, source_name, point_key, "
                "report_start_ms, report_end_ms, stitchability, header_json, "
                "header_sha256, payload_blob, codec, payload_sha256, action_count, "
                "cycle_count, event_count, wallet_sample_count, equity_sample_count "
                "from segment_compact_rows order by ordinal"
            ).fetchall()
        )
        if len(compact_rows) != int(row[7]):
            raise SourceV6StorageError("segment compact row count mismatch")
        by_ordinal = {item.ordinal: item for item in outcomes}
        for ordinal, compact_row in compact_rows:
            outcome = by_ordinal.get(ordinal)
            if outcome is None or outcome.status != "PREPARED":
                raise SourceV6StorageError("segment compact row has no accepted outcome")
            stored = _segment_row_tuple(compact_row)
            if str(stored[0]) != outcome.fragment_id or str(stored[1]) != outcome.source_sha256:
                raise SourceV6StorageError("segment prepared row correspondence mismatch")
            if sha256(str(stored[7]).encode("utf-8")).hexdigest() != str(stored[8]):
                raise SourceV6StorageError("compact checksum mismatch")
            if sha256(bytes(stored[9])).hexdigest() != str(stored[11]):
                raise SourceV6StorageError("compact checksum mismatch")
        compact_digest = _segment_compact_digest(compact_rows)
        if compact_digest != row[9]:
            raise SourceV6StorageError("segment compact digest mismatch")
        receipt = SourceV6SegmentReceipt(
            segment,
            str(row[1]),
            str(row[2]),
            str(row[3]),
            int(row[4]),
            int(row[5]),
            int(row[6]),
            int(row[7]),
            str(row[8]),
            str(row[9]),
        )
        return receipt, outcomes, compact_rows
    except duckdb.Error as error:
        raise SourceV6StorageError("cannot read Source v6 segment") from error
    finally:
        connection.close()


def _merge_segment_contents(
    paths: Sequence[str | Path],
    *,
    run_token: str | None = None,
) -> tuple[str, tuple[SourceV6SegmentOutcome, ...], tuple[tuple[int, tuple[object, ...]], ...]]:
    if not paths:
        raise SourceV6StorageError("cannot merge an empty segment set")
    loaded = [_read_source_v6_segment(path) for path in paths]
    loaded.sort(key=lambda item: (item[0].ordinal_start, str(item[0].path).encode("utf-8")))
    token = _segment_token(run_token or loaded[0][0].run_token)
    outcomes: list[SourceV6SegmentOutcome] = []
    rows_by_ordinal: dict[int, tuple[object, ...]] = {}
    previous_end: int | None = None
    for receipt, segment_outcomes, compact_rows in loaded:
        if receipt.run_token != token:
            raise SourceV6StorageError("segment run token or namespace mismatch")
        if previous_end is not None and receipt.ordinal_start != previous_end + 1:
            raise SourceV6StorageError("segment ordinal gap or overlap")
        previous_end = receipt.ordinal_end
        outcomes.extend(segment_outcomes)
        for ordinal, row in compact_rows:
            if ordinal in rows_by_ordinal:
                raise SourceV6StorageError("duplicate segment ordinal")
            rows_by_ordinal[ordinal] = row
    by_fragment_id: dict[str, int] = {}
    by_source_sha256: dict[str, int] = {}
    outcome_positions = {outcome.ordinal: index for index, outcome in enumerate(outcomes)}
    winners: set[int] = set()
    for outcome in sorted(outcomes, key=lambda item: (item.ordinal, item.relative_path.encode("utf-8"))):
        if outcome.status != "PREPARED":
            continue
        prior_winners: list[int] = []
        if outcome.fragment_id is not None and outcome.fragment_id in by_fragment_id:
            prior_winners.append(by_fragment_id[outcome.fragment_id])
        if outcome.source_sha256 is not None and outcome.source_sha256 in by_source_sha256:
            prior_winners.append(by_source_sha256[outcome.source_sha256])
        winner = min(prior_winners) if prior_winners else None
        if winner is None:
            if outcome.fragment_id is not None:
                by_fragment_id[outcome.fragment_id] = outcome.ordinal
            if outcome.source_sha256 is not None:
                by_source_sha256[outcome.source_sha256] = outcome.ordinal
            winners.add(outcome.ordinal)
            continue
        outcomes[outcome_positions[outcome.ordinal]] = replace(
            outcome,
            status="QUARANTINED",
            reason="duplicate_fragment",
            winner_ordinal=winner,
        )
    ordered_outcomes = tuple(sorted(outcomes, key=lambda item: item.ordinal))
    ordered_rows = tuple(
        (ordinal, rows_by_ordinal[ordinal])
        for ordinal in sorted(winners)
    )
    return token, ordered_outcomes, ordered_rows


def merge_source_v6_segments(
    segment_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    run_token: str | None = None,
    fan_in: int = 8,
) -> SourceV6SegmentReceipt:
    """Deterministically reduce one consecutive segment group into an intermediate segment."""
    if isinstance(fan_in, bool) or not isinstance(fan_in, int) or not 2 <= fan_in <= 8:
        raise SourceV6StorageError("segment fan-in must be between 2 and 8")
    token, outcomes, rows = _merge_segment_contents(segment_paths, run_token=run_token)
    return write_source_v6_segment(
        output_path,
        outcomes,
        run_token=token,
        kind="intermediate",
        compact_rows=rows,
    )


def _record_segment_quarantines(
    path: Path,
    outcomes: Iterable[SourceV6SegmentOutcome],
) -> None:
    quarantines = tuple(item for item in outcomes if item.status == "QUARANTINED")
    if not quarantines:
        return
    connection = duckdb.connect(str(path))
    try:
        info = _schema_info(connection)
        _require_fresh(info)
        generation = int(info["mutation_generation"])
        connection.execute("begin")
        for outcome in quarantines:
            audit_id = uuid4().hex
            started = _utc_now()
            reason = outcome.reason or "quarantined"
            fragment_id = outcome.fragment_id or str(outcome.source_sha256)
            connection.execute(
                "insert into quarantine values (?, ?, ?, ?)",
                [fragment_id, outcome.source_sha256, reason, started],
            )
            connection.execute(
                "insert into import_audit values (?, ?, ?, ?, ?, 'QUARANTINED', ?, ?, 'NO', 1, ?)",
                [audit_id, fragment_id, outcome.source_sha256, started, started, generation, generation, reason],
            )
        connection.execute("commit")
    except Exception:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def reduce_source_v6_segments(
    segment_paths: Sequence[str | Path],
    target_path: str | Path,
    *,
    run_token: str | None = None,
    fan_in: int = 8,
    database_id: str | None = None,
) -> SourceV6SegmentReceipt:
    """Reduce sealed segments and publish accepted rows into a fresh Source v6 DB.

    Payloads are copied segment-to-target inside DuckDB in one pass; `fan_in` is
    retained for contract compatibility and no longer shapes a merge tree.
    """
    if not segment_paths:
        raise SourceV6StorageError("cannot reduce an empty segment set")
    if isinstance(fan_in, bool) or not isinstance(fan_in, int) or not 2 <= fan_in <= 8:
        raise SourceV6StorageError("segment fan-in must be between 2 and 8")
    target = Path(target_path).resolve()
    if target.exists():
        raise SourceV6StorageError(f"Source v6 target already exists: {target}")
    loaded = [_read_segment_outcomes(path) for path in segment_paths]
    token, outcomes = _resolve_segment_outcomes(loaded, run_token)
    temporary_root = target.parent / f".{target.name}.{token}.segments"
    if temporary_root.exists():
        raise SourceV6StorageError(f"segment recovery namespace already exists: {temporary_root}")
    if outcomes[0].ordinal != 0:
        raise SourceV6StorageError("segment set does not start at the first input ordinal")
    ordered_segments = sorted(loaded, key=lambda item: (item[0].ordinal_start, str(item[0].path).encode("utf-8")))
    accepted = tuple(item for item in outcomes if item.status == "PREPARED")
    try:
        _publish_segments_single_pass(target, ordered_segments, accepted, database_id)
        _record_segment_quarantines(target, outcomes)
        validate_source_v6_database(target)
        info = database_info(target)
        return SourceV6SegmentReceipt(
            path=target,
            segment_id=uuid4().hex,
            run_token=token,
            kind="intermediate",
            ordinal_start=outcomes[0].ordinal,
            ordinal_end=outcomes[-1].ordinal,
            outcome_count=len(outcomes),
            row_count=len(accepted),
            outcome_digest=_segment_outcome_digest(outcomes),
            compact_digest=info["source_content_digest"],
            database_id=info["database_id"],
        )
    except Exception:
        _unlink_quietly(target)
        _unlink_quietly(Path(f"{target}.wal"))
        raise


def _read_segment_outcomes(
    path: str | Path,
) -> tuple[SourceV6SegmentReceipt, tuple[SourceV6SegmentOutcome, ...]]:
    """Read one segment's manifest and outcomes without touching payload bytes."""
    segment = Path(path).resolve()
    if not segment.is_file():
        raise SourceV6StorageError(f"segment does not exist: {segment}")
    if Path(f"{segment}.wal").exists():
        raise SourceV6StorageError("segment has an uncheckpointed WAL")
    try:
        connection = duckdb.connect(str(segment), read_only=True)
    except duckdb.Error as error:
        raise SourceV6StorageError(f"cannot open Source v6 segment: {error}") from error
    try:
        if _segment_tables(connection) != {
            "segment_manifest",
            "segment_outcomes",
            "segment_compact_rows",
        }:
            raise SourceV6StorageError("segment schema mismatch")
        manifest = connection.execute(
            "select fingerprint, segment_id, run_token, kind, ordinal_start, "
            "ordinal_end, outcome_count, row_count, outcome_digest, "
            "compact_digest, sealed, checkpointed from segment_manifest"
        ).fetchall()
        if len(manifest) != 1:
            raise SourceV6StorageError("segment manifest must contain one row")
        row = manifest[0]
        if row[0] != SEGMENT_FINGERPRINT or not row[10] or not row[11]:
            raise SourceV6StorageError("segment fingerprint or seal mismatch")
        outcomes = tuple(
            SourceV6SegmentOutcome(*item)
            for item in connection.execute(
                "select ordinal, relative_path, status, source_sha256, "
                "fragment_id, reason, source_size, source_mtime_ns, winner_ordinal "
                "from segment_outcomes order by ordinal"
            ).fetchall()
        )
        if len(outcomes) != int(row[6]):
            raise SourceV6StorageError("segment outcome count mismatch")
        ordinals = tuple(item.ordinal for item in outcomes)
        if not outcomes or ordinals != tuple(range(int(row[4]), int(row[5]) + 1)):
            raise SourceV6StorageError("segment ordinal gap or overlap")
        if _segment_outcome_digest(outcomes) != row[8]:
            raise SourceV6StorageError("segment outcome digest mismatch")
        # The sealed row count must match what the outcomes claim was prepared.
        prepared = sum(1 for item in outcomes if item.status == "PREPARED")
        stored = int(connection.execute("select count(*) from segment_compact_rows").fetchone()[0])
        if prepared != int(row[7]) or stored != int(row[7]):
            raise SourceV6StorageError("segment compact row count mismatch")
        # Outcomes and compact rows are sealed by two independent digests, so
        # neither can detect that the two were zipped together wrongly. Matching
        # counts do not imply matching pairs: a swap keeps every row and every
        # digest valid while publishing each payload under the other's outcome.
        by_ordinal = {item.ordinal: item for item in outcomes}
        for stored_ordinal, stored_fragment_id, stored_source_sha256 in connection.execute(
            "select ordinal, fragment_id, source_sha256 from segment_compact_rows order by ordinal"
        ).fetchall():
            outcome = by_ordinal.get(int(stored_ordinal))
            if outcome is None or outcome.status != "PREPARED":
                raise SourceV6StorageError("segment compact row has no accepted outcome")
            if (
                str(stored_fragment_id) != outcome.fragment_id
                or str(stored_source_sha256) != outcome.source_sha256
            ):
                raise SourceV6StorageError("segment prepared row correspondence mismatch")
        _verify_segment_compact_digest(connection, str(row[9]), int(row[7]))
        receipt = SourceV6SegmentReceipt(
            path=segment,
            segment_id=str(row[1]),
            run_token=str(row[2]),
            kind=str(row[3]),
            ordinal_start=int(row[4]),
            ordinal_end=int(row[5]),
            outcome_count=int(row[6]),
            row_count=int(row[7]),
            outcome_digest=str(row[8]),
            compact_digest=str(row[9]),
        )
        return receipt, outcomes
    except duckdb.Error as error:
        raise SourceV6StorageError(f"cannot read Source v6 segment: {error}") from error
    finally:
        connection.close()


def _resolve_segment_outcomes(
    loaded: Sequence[tuple[SourceV6SegmentReceipt, tuple[SourceV6SegmentOutcome, ...]]],
    run_token: str | None,
) -> tuple[str, tuple[SourceV6SegmentOutcome, ...]]:
    """Apply first-wins duplicate policy across segments using metadata only."""
    ordered_segments = sorted(loaded, key=lambda item: (item[0].ordinal_start, str(item[0].path).encode("utf-8")))
    token = _segment_token(run_token or ordered_segments[0][0].run_token)
    outcomes: list[SourceV6SegmentOutcome] = []
    previous_end: int | None = None
    for receipt, segment_outcomes in ordered_segments:
        if receipt.run_token != token:
            raise SourceV6StorageError("segment run token or namespace mismatch")
        if previous_end is not None and receipt.ordinal_start != previous_end + 1:
            raise SourceV6StorageError("segment ordinal gap or overlap")
        previous_end = receipt.ordinal_end
        outcomes.extend(segment_outcomes)
    by_fragment_id: dict[str, int] = {}
    by_source_sha256: dict[str, int] = {}
    outcome_positions = {outcome.ordinal: index for index, outcome in enumerate(outcomes)}
    for outcome in sorted(outcomes, key=lambda item: (item.ordinal, item.relative_path.encode("utf-8"))):
        if outcome.status != "PREPARED":
            continue
        prior_winners: list[int] = []
        if outcome.fragment_id is not None and outcome.fragment_id in by_fragment_id:
            prior_winners.append(by_fragment_id[outcome.fragment_id])
        if outcome.source_sha256 is not None and outcome.source_sha256 in by_source_sha256:
            prior_winners.append(by_source_sha256[outcome.source_sha256])
        winner = min(prior_winners) if prior_winners else None
        if winner is None:
            if outcome.fragment_id is not None:
                by_fragment_id[outcome.fragment_id] = outcome.ordinal
            if outcome.source_sha256 is not None:
                by_source_sha256[outcome.source_sha256] = outcome.ordinal
            continue
        outcomes[outcome_positions[outcome.ordinal]] = replace(
            outcome,
            status="QUARANTINED",
            reason="duplicate_fragment",
            winner_ordinal=winner,
        )
    return token, tuple(sorted(outcomes, key=lambda item: item.ordinal))


_PUBLISH_COLUMNS = (
    "fragment_id, source_sha256, source_name, point_key, report_start_ms, "
    "report_end_ms, stitchability, header_json, header_sha256, payload_blob, "
    "codec, payload_sha256, action_count, cycle_count, event_count, "
    "wallet_sample_count, equity_sample_count"
)


def _insert_frame(
    connection: duckdb.DuckDBPyConnection,
    statement: str,
    name: str,
    rows: Sequence[tuple[object, ...]],
    columns: Sequence[str],
) -> None:
    """Insert many rows through a registered frame instead of row-by-row binds.

    `executemany` binds one row per call: measured at 1,730 rows/s against
    1.97M rows/s here. A single corpus emits hundreds of thousands of
    `day_ownership` rows, so the difference is minutes-to-hours, not noise.

    The frame is built with `dtype=object` so DuckDB receives the original
    Python values — `datetime.date` stays a date and `None` stays NULL — rather
    than the `datetime64`/`NaN` coercions pandas applies when inferring dtypes.
    """
    if not rows:
        return
    import pandas as pd

    frame = pd.DataFrame(rows, columns=list(columns), dtype=object)
    connection.register(name, frame)
    try:
        connection.execute(statement)
    finally:
        connection.unregister(name)


_POINT_INSERT = ("insert or ignore into points select point_key from _publish_points", "_publish_points", ("point_key",))
_ORIGIN_INSERT = (
    "insert into fragment_origins select fragment_id, source_sha256, source_name, database_id from _publish_origins",
    "_publish_origins",
    ("fragment_id", "source_sha256", "source_name", "database_id"),
)
# Z4: the label is derived from the stored fact counts rather than passed in,
# so it cannot disagree with the fragment it describes and no producer has to
# carry it. Both writers insert `compact_fragments` before these rows, which
# `test_day_ownership_labels_*` pins — a reordering would silently drop days
# through this join rather than mislabel them.
_DAY_OWNERSHIP_LABEL = (
    "case when fragments.action_count = 0 and fragments.cycle_count = 0 "
    "and fragments.event_count = 0 and fragments.wallet_sample_count = 0 "
    "and fragments.equity_sample_count = 0 then 'ACTIVE_EMPTY' else 'ACTIVE' end"
)
_DAY_INSERT = (
    "insert into day_ownership select days.fragment_id, days.day, "
    f"{_DAY_OWNERSHIP_LABEL}, true, null, null from _publish_days days "
    "join compact_fragments fragments on fragments.fragment_id = days.fragment_id",
    "_publish_days",
    ("fragment_id", "day"),
)
_AUDIT_INSERT = (
    "insert into import_audit select import_id, fragment_id, source_sha256, started_at_utc, "
    "finished_at_utc, status, generation_before, generation_after, safe_to_delete, "
    "quarantine_count, reason from _publish_audit",
    "_publish_audit",
    (
        "import_id", "fragment_id", "source_sha256", "started_at_utc", "finished_at_utc",
        "status", "generation_before", "generation_after", "safe_to_delete",
        "quarantine_count", "reason",
    ),
)


def _day_ownership_label(fragment: SourceV6Fragment) -> str:
    """`ACTIVE_EMPTY` for a fragment with no facts at all (ADR-0016, Z4).

    A window that was tested and produced nothing is genuine coverage, so the
    days are owned; it is labelled so no consumer can read it as traded data.
    """
    if fragment.actions or fragment.cycles or fragment.events:
        return "ACTIVE"
    if fragment.wallet_samples or fragment.equity_samples:
        return "ACTIVE"
    return "ACTIVE_EMPTY"


def _publish_metadata_rows(
    connection: duckdb.DuckDBPyConnection,
    point_rows: Sequence[tuple[object, ...]],
    origin_rows: Sequence[tuple[object, ...]],
    day_rows: Sequence[tuple[object, ...]],
    audit_rows: Sequence[tuple[object, ...]],
) -> None:
    """Write the four metadata tables that accompany published fragments."""
    for rows, (statement, name, columns) in (
        (point_rows, _POINT_INSERT),
        (origin_rows, _ORIGIN_INSERT),
        (day_rows, _DAY_INSERT),
        (audit_rows, _AUDIT_INSERT),
    ):
        _insert_frame(connection, statement, name, rows, columns)


def _assert_canonical_matches_columns(canonical: bytes, row: Sequence[object]) -> None:
    """Compare the content-addressed document against the header and columns.

    Without this the published database is only self-consistent: `fragment_id`
    proves the payload is *a* valid fragment, not that the columns describe
    *that* fragment. `fragment_metadata` serves those columns to coverage,
    readiness and stitching without ever decoding, so a mismatch would steer
    analysis silently.

    Every analytical field the header carries is compared here, not just the
    ones the indexed columns duplicate. `stitchability`, the three balances,
    `settings_fingerprint`, `metrics` and `open_tail_cycle_ids` reach analysis
    through the header alone; checking them against a column would be checking
    two halves of the same forgeable row against each other. Only the canonical
    bytes are content-addressed, so only they can settle the question.
    """
    try:
        document = json.loads(canonical)
        header = json.loads(str(row[11]))
        point = PointIdentity(**document["point"]).canonical_key
        counts = (
            len(document["actions"]),
            len(document["cycles"]),
            len(document["events"]),
            len(document["wallet_samples"]),
            len(document["equity_samples"]),
        )
        if (
            point != str(row[3])
            or int(document["report_start_ms"]) != int(row[4])
            or int(document["report_end_ms"]) != int(row[5])
            or counts != tuple(int(item) for item in row[6:11])
            or header.get("schema_version") != int(document["schema_version"])
            or header.get("point") != point
            or header.get("report_start_ms") != int(document["report_start_ms"])
            or header.get("report_end_ms") != int(document["report_end_ms"])
            or header.get("stitchability") != str(document["stitchability"])
            or header.get("settings_fingerprint") != str(document["settings_fingerprint"])
            or header.get("metrics") != dict(document["metrics"])
            or list(header.get("open_tail_cycle_ids", ())) != list(document["open_tail_cycle_ids"])
            or Decimal(str(header.get("initial_balance"))) != Decimal(str(document["initial_balance"]))
            or Decimal(str(header.get("fixed_order_balance")))
            != Decimal(str(document["fixed_order_balance"]))
            or Decimal(str(header.get("balance_percentage")))
            != Decimal(str(document["balance_percentage"]))
        ):
            raise SourceV6StorageError("compact fragment readback mismatch")
    except SourceV6StorageError:
        raise
    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
        SourceV6Error,
    ) as error:
        raise SourceV6StorageError("compact fragment readback mismatch") from error


def _verify_segment_compact_digest(
    connection: duckdb.DuckDBPyConnection, expected: str, row_count: int
) -> None:
    """Recompute the sealed digest over every compact column, streaming.

    The worker sealed this digest over all eighteen columns, so it is the only
    check that binds `header_json`, the count columns and the payload together.
    Comparing a header against a column cannot do that: both live in this file,
    so a consistent edit of the pair satisfies any such comparison.
    """
    bounds = connection.execute(
        "select min(ordinal), max(ordinal) from segment_compact_rows"
    ).fetchone()
    digest = sha256()
    seen = 0
    if bounds is not None and bounds[0] is not None:
        low, high = int(bounds[0]), int(bounds[1])
        # DuckDB materialises a whole result set on execute, so paging a single
        # cursor does not bound memory. Querying bounded ordinal windows does.
        for start in range(low, high + 1, _VERIFY_BATCH):
            rows = connection.execute(
                "select ordinal, fragment_id, source_sha256, source_name, point_key, "
                "report_start_ms, report_end_ms, stitchability, header_json, header_sha256, "
                "payload_blob, codec, payload_sha256, action_count, cycle_count, event_count, "
                "wallet_sample_count, equity_sample_count from segment_compact_rows "
                "where ordinal between ? and ? order by ordinal",
                [start, min(start + _VERIFY_BATCH - 1, high)],
            ).fetchall()
            for row in rows:
                seen += 1
                compact = tuple(row[1:])
                values = [int(row[0]), *compact[:9], compact[10], compact[11], *compact[12:]]
                digest.update(
                    json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                digest.update(bytes(compact[9]))
    if seen != row_count or digest.hexdigest() != expected:
        raise SourceV6StorageError("segment compact digest mismatch")


def _verify_published_identity(connection: duckdb.DuckDBPyConnection) -> None:
    """Bind every published row to its fragment identity.

    `fragment_id` is the SHA-256 of the canonical document, and the payload is
    that document compressed. Re-deriving the id from the stored bytes therefore
    covers every canonical field transitively, and comparing the header against
    its columns covers the rest. Checksums alone would only prove a row is
    self-consistent, not that it is the row that was sealed.
    """
    _verify_published_columns(connection)
    # Batched by key: DuckDB materialises a whole result set on execute, so a
    # single cursor over the corpus would be resident in full regardless of how
    # it is fetched. Ids are cheap; payloads are pulled a window at a time.
    ids = [
        str(row[0])
        for row in connection.execute("select fragment_id from compact_fragments").fetchall()
    ]
    _verify_fragment_payloads(connection, ids)


def _verify_published_columns(connection: duckdb.DuckDBPyConnection) -> None:
    """Check every column against its header in one query.

    Left in the parent process on the parallel path too: this is a single
    DuckDB statement, which DuckDB already runs across its own threads, and it
    was measured at 59.3 s against the 1,215 s of payload readback beside it.
    """
    mismatched = connection.execute(
        "select count(*) from compact_fragments "
        "where sha256(header_json) != header_sha256 "
        "or sha256(payload_blob) != payload_sha256 "
        "or json_extract_string(header_json, '$.point') != point_key "
        "or json_extract_string(header_json, '$.source_sha256') != source_sha256 "
        "or json_extract_string(header_json, '$.source_name') != source_name "
        "or json_extract_string(header_json, '$.stitchability') != stitchability "
        "or json_extract_string(header_json, '$.codec') != codec "
        "or cast(json_extract(header_json, '$.report_start_ms') as bigint) != report_start_ms "
        "or cast(json_extract(header_json, '$.report_end_ms') as bigint) != report_end_ms"
    ).fetchone()[0]
    if mismatched:
        raise SourceV6StorageError("compact fragment readback mismatch")


def _verify_fragment_payloads(
    connection: duckdb.DuckDBPyConnection, ids: Sequence[str]
) -> None:
    """Re-derive each fragment id from its stored payload, a window at a time.

    The window bounds resident memory *per process*: 128 compressed payloads
    plus the one canonical document being checked, rather than the whole
    corpus. On the parallel path the peak is that times the worker count — at
    16 workers and the 1.4 MB average canonical size, a few hundred MB.
    """
    for start in range(0, len(ids), _VERIFY_BATCH):
        chunk = ids[start : start + _VERIFY_BATCH]
        placeholders = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            "select fragment_id, payload_blob, codec, point_key, report_start_ms, "
            "report_end_ms, action_count, cycle_count, event_count, wallet_sample_count, "
            f"equity_sample_count, header_json from compact_fragments "
            f"where fragment_id in ({placeholders})",
            chunk,
        ).fetchall()
        # Matches `decode_fragment_slice`. On the parallel path the ids were
        # scanned on a different connection than the one reading payloads here,
        # so a row that vanished between them would otherwise be silently
        # unverified — and this readback is what authorises `safe_to_delete`.
        if len(rows) != len(chunk):
            raise SourceV6StorageError("compact fragment readback mismatch")
        for row in rows:
            fragment_id, payload, codec = str(row[0]), row[1], str(row[2])
            if not codec.startswith("json+zlib-v1:"):
                raise SourceV6StorageError("compact fragment readback mismatch")
            try:
                canonical = zlib.decompress(bytes(payload))
            except zlib.error as error:
                raise SourceV6StorageError("compact fragment readback mismatch") from error
            if sha256(canonical).hexdigest() != fragment_id:
                raise SourceV6StorageError("compact fragment readback mismatch")
            _assert_canonical_matches_columns(canonical, row)


def _publish_segments_single_pass(
    target: Path,
    segments: Sequence[tuple[SourceV6SegmentReceipt, tuple[SourceV6SegmentOutcome, ...]]],
    accepted: Sequence[SourceV6SegmentOutcome],
    database_id: str | None,
) -> None:
    """Copy sealed payloads straight into the target inside DuckDB.

    Payload bytes move segment-to-target through SQL, so they are never
    materialised in Python. Only small metadata columns are read back to build
    the point, origin, day-ownership and audit rows.
    """
    create_v6_database(target, database_id=database_id)
    wanted = {outcome.ordinal for outcome in accepted}
    connection = duckdb.connect(str(target))
    attached: list[str] = []
    try:
        info = _schema_info(connection)
        _require_fresh(info)
        resolved_database_id = str(info["database_id"])
        generation = int(info["mutation_generation"])
        planned: list[tuple[Path, list[int]]] = []
        for receipt, _outcomes in segments:
            ordinals = sorted(
                ordinal
                for ordinal in wanted
                if receipt.ordinal_start <= ordinal <= receipt.ordinal_end
            )
            if ordinals:
                planned.append((receipt.path, ordinals))
        # Every attachment holds file descriptors, so a large corpus must be
        # copied in bounded groups. Each group is its own transaction, so this
        # function alone is NOT atomic: on failure it leaves a partial target,
        # which `reduce_source_v6_segments` unlinks. Callers that must never
        # expose a partial database publish through a rename, as the importer
        # does with its staging file.
        metadata: list[tuple[object, ...]] = []
        for start in range(0, len(planned), SEGMENT_ATTACH_BATCH):
            group = planned[start : start + SEGMENT_ATTACH_BATCH]
            aliases: list[str] = []
            for offset, (path, _ordinals) in enumerate(group):
                alias = f"seg_{start + offset}"
                literal = str(path).replace("'", "''")
                connection.execute(f"attach '{literal}' as {alias} (read_only)")
                attached.append(alias)
                aliases.append(alias)
            connection.execute("begin")
            for alias, (_path, ordinals) in zip(aliases, group):
                placeholders = ", ".join("?" for _ in ordinals)
                connection.execute(
                    f"insert into compact_fragments select {_PUBLISH_COLUMNS}, true, null, null "
                    f"from {alias}.segment_compact_rows where ordinal in ({placeholders}) order by ordinal",
                    ordinals,
                )
                metadata.extend(
                    connection.execute(
                        "select ordinal, fragment_id, source_sha256, source_name, point_key, "
                        f"report_start_ms, report_end_ms from {alias}.segment_compact_rows "
                        f"where ordinal in ({placeholders}) order by ordinal",
                        ordinals,
                    ).fetchall()
                )
            connection.execute("commit")
            for alias in aliases:
                connection.execute(f"detach {alias}")
                attached.remove(alias)
        connection.execute("begin")
        metadata.sort(key=lambda item: int(item[0]))
        if len(metadata) != len(wanted):
            raise SourceV6StorageError("segment outcomes and prepared rows do not correspond")
        point_rows: list[tuple[object, ...]] = []
        origin_rows: list[tuple[object, ...]] = []
        day_rows: list[tuple[object, ...]] = []
        audit_rows: list[tuple[object, ...]] = []
        for item in metadata:
            fragment_id = str(item[1])
            source_sha256 = str(item[2])
            source_name = str(item[3])
            started = _utc_now()
            point_rows.append((str(item[4]),))
            origin_rows.append((fragment_id, source_sha256, source_name, resolved_database_id))
            day = datetime.fromtimestamp(int(item[5]) / 1000, timezone.utc).date()
            end = datetime.fromtimestamp(int(item[6]) / 1000, timezone.utc).date()
            while day < end:
                day_rows.append((fragment_id, day))
                day += timedelta(days=1)
            audit_rows.append(
                (str(uuid4()), fragment_id, source_sha256, started, started, "COMMITTED", generation, generation + 1, "YES", 0, None)
            )
            generation += 1
        # No guard on `point_rows`: `_insert_frame` already skips an empty
        # table, and gating all four writes on one of them would silently drop
        # `day_ownership` and `import_audit` the moment point emission stops
        # being unconditional.
        _publish_metadata_rows(connection, point_rows, origin_rows, day_rows, audit_rows)
        _verify_published_identity(connection)
        connection.execute("update schema_info set value = ? where key = 'mutation_generation'", [str(generation)])
        ids = [str(row[0]) for row in connection.execute("select fragment_id from compact_fragments order by fragment_id").fetchall()]
        connection.execute("update schema_info set value = ? where key = 'source_content_digest'", [source_content_digest(ids)])
        connection.execute("commit")
    except Exception:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        raise
    finally:
        for alias in attached:
            try:
                connection.execute(f"detach {alias}")
            except Exception:
                pass
        connection.close()


def import_fragment_batch(path: str | Path, items: Iterable[tuple[SourceV6Fragment, EncodedSourceV6Fragment]]) -> tuple[ImportReceipt, ...]:
    """Publish one bounded batch through one connection and transaction."""
    prepared = tuple(items)
    if not prepared:
        return ()
    connection = duckdb.connect(str(path))
    try:
        info = _schema_info(connection)
        _require_fresh(info)
        database_id = str(info["database_id"])
        generation = int(info["mutation_generation"])
        receipts: list[ImportReceipt] = []
        connection.execute("begin")
        existing_ids: set[str] = set()
        existing_by_sha: dict[str, str] = {}
        if connection.execute("select count(*) from compact_fragments").fetchone()[0]:
            for row in connection.execute("select fragment_id, source_sha256 from compact_fragments").fetchall():
                existing_ids.add(str(row[0]))
                existing_by_sha[str(row[1])] = str(row[0])
        audit_rows: list[tuple[object, ...]] = []
        point_rows: list[tuple[object, ...]] = []
        fragment_rows: list[tuple[object, ...]] = []
        origin_rows: list[tuple[object, ...]] = []
        day_rows: list[tuple[object, ...]] = []
        for fragment, encoded in prepared:
            if fragment.fragment_id in existing_ids:
                receipts.append(ImportReceipt("IDEMPOTENT", fragment.fragment_id, database_id, generation, "YES", False, 0))
                continue
            if fragment.source_sha256 in existing_by_sha:
                receipts.append(ImportReceipt("IDEMPOTENT", existing_by_sha[fragment.source_sha256], database_id, generation, "YES", False, 0))
                continue
            if encoded.fragment_id != fragment.fragment_id:
                raise SourceV6StorageError("encoded fragment identity mismatch")
            existing_ids.add(fragment.fragment_id)
            existing_by_sha[fragment.source_sha256] = fragment.fragment_id
            started = _utc_now()
            header_json = _header(fragment, encoded)
            header_sha = sha256(header_json.encode("utf-8")).hexdigest()
            payload_sha = sha256(encoded.payload).hexdigest()
            audit_rows.append(
                (str(uuid4()), fragment.fragment_id, fragment.source_sha256, started, started, "COMMITTED", generation, generation + 1, "YES", 0, None)
            )
            point_rows.append((fragment.point.canonical_key,))
            fragment_rows.append(
                (
                    fragment.fragment_id, fragment.source_sha256, fragment.source_name,
                    fragment.point.canonical_key, fragment.report_start_ms, fragment.report_end_ms,
                    fragment.stitchability, header_json, header_sha, encoded.payload, encoded.codec,
                    payload_sha, len(fragment.actions), len(fragment.cycles), len(fragment.events),
                    len(fragment.wallet_samples), len(fragment.equity_samples),
                )
            )
            origin_rows.append((fragment.fragment_id, fragment.source_sha256, fragment.source_name, database_id))
            day = datetime.fromtimestamp(fragment.report_start_ms / 1000, timezone.utc).date()
            end = datetime.fromtimestamp(fragment.report_end_ms / 1000, timezone.utc).date()
            label = _day_ownership_label(fragment)
            while day < end:
                day_rows.append((fragment.fragment_id, day, label))
                day += timedelta(days=1)
            generation += 1
            receipts.append(ImportReceipt("COMMITTED", fragment.fragment_id, database_id, generation, "YES", True, 0))
        if fragment_rows:
            connection.executemany("insert or ignore into points values (?)", point_rows)
            connection.executemany(
                "insert into compact_fragments values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true, null, null)",
                fragment_rows,
            )
            connection.executemany("insert into fragment_origins values (?, ?, ?, ?)", origin_rows)
            if day_rows:
                connection.executemany("insert into day_ownership values (?, ?, ?, true, null, null)", day_rows)
            connection.executemany("insert into import_audit values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", audit_rows)
            _verify_published_identity(connection)
        connection.execute("update schema_info set value = ? where key = 'mutation_generation'", [str(generation)])
        ids = [str(row[0]) for row in connection.execute("select fragment_id from compact_fragments order by fragment_id").fetchall()]
        connection.execute("update schema_info set value = ? where key = 'source_content_digest'", [source_content_digest(ids)])
        connection.execute("commit")
        return tuple(receipts)
    except Exception:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def import_fragment(path: str | Path, fragment: SourceV6Fragment, *, preflight_token: str | None = None, fail_after: str | None = None, cancel_check: Callable[[], bool] | None = None, encoded: EncodedSourceV6Fragment | None = None) -> ImportReceipt:
    connection = duckdb.connect(str(path))
    audit_id = str(uuid4())
    try:
        if cancel_check is not None and cancel_check():
            raise SourceV6StorageError("import cancelled before publication")
        info = _schema_info(connection)
        _require_fresh(info)
        if preflight_token is None:
            raise SourceV6StorageError("preflight token required")
        before = int(info["mutation_generation"])
        expected = f"{info['database_id']}:{before}:{fragment.fragment_id}"
        if preflight_token != expected:
            raise SourceV6StorageError("stale preflight token")
        duplicate = connection.execute("select fragment_id from compact_fragments where source_sha256 = ? or fragment_id = ?", [fragment.source_sha256, fragment.fragment_id]).fetchone()
        if duplicate:
            return ImportReceipt("IDEMPOTENT", str(duplicate[0]), info["database_id"], before, "YES", False, 0)
        encoded = encoded or encode_fragment(fragment)
        if encoded.fragment_id != fragment.fragment_id:
            raise SourceV6StorageError("encoded fragment identity mismatch")
        started = _utc_now()
        connection.execute("begin")
        connection.execute("insert into import_audit values (?, ?, ?, ?, null, 'STARTED', ?, ?, 'NO', 0, null)", [audit_id, fragment.fragment_id, fragment.source_sha256, started, before, before])
        connection.execute("insert or ignore into points values (?)", [fragment.point.canonical_key])
        header_json = _header(fragment, encoded)
        connection.execute("insert into compact_fragments values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, true, null, null)", [fragment.fragment_id, fragment.source_sha256, fragment.source_name, fragment.point.canonical_key, fragment.report_start_ms, fragment.report_end_ms, fragment.stitchability, header_json, sha256(header_json.encode("utf-8")).hexdigest(), encoded.payload, encoded.codec, sha256(encoded.payload).hexdigest(), len(fragment.actions), len(fragment.cycles), len(fragment.events), len(fragment.wallet_samples), len(fragment.equity_samples)])
        connection.execute(
            "insert into fragment_origins values (?, ?, ?, ?)",
            [fragment.fragment_id, fragment.source_sha256, fragment.source_name, info["database_id"]],
        )
        if fail_after == "facts":
            raise SourceV6StorageError("forced import failure")
        if cancel_check is not None and cancel_check():
            raise SourceV6StorageError("import cancelled before commit")
        day = datetime.fromtimestamp(fragment.report_start_ms / 1000, timezone.utc).date()
        end = datetime.fromtimestamp(fragment.report_end_ms / 1000, timezone.utc).date()
        while day < end:
            connection.execute(
                "insert into day_ownership values (?, ?, ?, true, null, null)",
                [fragment.fragment_id, day, _day_ownership_label(fragment)],
            )
            day += timedelta(days=1)
        restored = _row_fragment(_select_row(connection, fragment.fragment_id))
        if restored != fragment:
            raise SourceV6StorageError("compact fragment readback mismatch")
        after = before + 1
        connection.execute("update schema_info set value = ? where key = 'mutation_generation'", [str(after)])
        ids = [str(row[0]) for row in connection.execute("select fragment_id from compact_fragments order by fragment_id").fetchall()]
        connection.execute("update schema_info set value = ? where key = 'source_content_digest'", [source_content_digest(ids)])
        connection.execute("update import_audit set committed_at_utc = ?, status = 'COMMITTED', generation_after = ?, safe_to_delete = 'YES' where audit_id = ?", [_utc_now(), after, audit_id])
        connection.execute("commit")
        return ImportReceipt("COMMITTED", fragment.fragment_id, info["database_id"], after, "YES", True, 0)
    except Exception:
        try:
            connection.execute("rollback")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def reconstruct_fragment(path: str | Path, fragment_id: str) -> SourceV6Fragment:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        _require_fresh(_schema_info(connection))
        return _row_fragment(_select_row(connection, fragment_id))
    finally:
        connection.close()


def iter_fragments(path: str | Path, *, point_key: str | None = None, start_ms: int | None = None, end_ms: int | None = None) -> Iterator[SourceV6Fragment]:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        _require_fresh(_schema_info(connection))
        clauses: list[str] = []
        params: list[object] = []
        if point_key is not None:
            clauses.append("point_key = ?")
            params.append(point_key)
        if start_ms is not None:
            clauses.append("report_end_ms > ?")
            params.append(start_ms)
        if end_ms is not None:
            clauses.append("report_start_ms < ?")
            params.append(end_ms)
        where = " where " + " and ".join(clauses) if clauses else ""
        rows = connection.execute(f"select * from compact_fragments{where} order by fragment_id", params).fetchall()
        fragments = tuple(_row_fragment(row) for row in rows)
        return iter(fragments)
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class SourceV6FragmentMetadata:
    """Every fragment field except the fact collections.

    Coverage, readiness and stitching read only these fields, so they can be
    served straight from indexed columns and the stored header without
    decompressing a payload. It is deliberately a distinct type: anything that
    needs actions, cycles, events or samples must ask for a real fragment.
    """

    schema_version: int
    fragment_id: str
    source_sha256: str
    source_name: str
    point: PointIdentity
    report_start_ms: int
    report_end_ms: int
    initial_balance: Decimal
    fixed_order_balance: Decimal
    balance_percentage: Decimal
    settings_fingerprint: str
    stitchability: str
    open_tail_cycle_ids: tuple[str, ...]
    metrics: Mapping[str, str]


def fragment_metadata(path: str | Path) -> tuple[SourceV6FragmentMetadata, ...]:
    """Read every fragment's metadata in `fragment_id` order without decoding."""
    try:
        connection = duckdb.connect(str(path), read_only=True)
        try:
            _require_fresh(_schema_info(connection))
            rows = connection.execute(
                "select fragment_id, source_sha256, source_name, point_key, "
                "report_start_ms, report_end_ms, stitchability, header_json, header_sha256 "
                "from compact_fragments order by fragment_id"
            ).fetchall()
        finally:
            connection.close()
    except duckdb.Error as error:
        raise SourceV6StorageError(f"cannot read Source v6 fragment metadata: {error}") from error
    result: list[SourceV6FragmentMetadata] = []
    try:
        for row in rows:
            header_json = str(row[7])
            if sha256(header_json.encode("utf-8")).hexdigest() != str(row[8]):
                raise SourceV6StorageError("compact checksum mismatch")
            header = json.loads(header_json)
            # The header is the authority; every column must agree with it.
            if (
                header.get("point") != str(row[3])
                or header.get("source_sha256") != str(row[1])
                or header.get("source_name") != str(row[2])
                or header.get("stitchability") != str(row[6])
                or int(header["report_start_ms"]) != int(row[4])
                or int(header["report_end_ms"]) != int(row[5])
            ):
                raise SourceV6StorageError("compact header mismatch")
            result.append(
                SourceV6FragmentMetadata(
                    schema_version=int(header["schema_version"]),
                    fragment_id=str(row[0]),
                    source_sha256=str(row[1]),
                    source_name=str(row[2]),
                    point=PointIdentity.from_canonical_key(str(row[3])),
                    report_start_ms=int(row[4]),
                    report_end_ms=int(row[5]),
                    initial_balance=Decimal(str(header["initial_balance"])),
                    fixed_order_balance=Decimal(str(header["fixed_order_balance"])),
                    balance_percentage=Decimal(str(header["balance_percentage"])),
                    settings_fingerprint=str(header["settings_fingerprint"]),
                    stitchability=str(row[6]),
                    open_tail_cycle_ids=tuple(header.get("open_tail_cycle_ids", ())),
                    metrics=dict(header.get("metrics", {})),
                )
            )
    except SourceV6StorageError:
        raise
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, SourceV6Error) as error:
        raise SourceV6StorageError(f"malformed Source v6 fragment metadata: {error}") from error
    return tuple(result)


def decode_fragment_slice(path: str | Path, ids: Sequence[str]) -> tuple[SourceV6Fragment, ...]:
    """Decode one explicit id slice; safe to run in a worker process."""
    if not ids:
        return ()
    connection = duckdb.connect(str(path), read_only=True)
    try:
        _require_fresh(_schema_info(connection))
        placeholders = ", ".join("?" for _ in ids)
        rows = connection.execute(
            f"select * from compact_fragments where fragment_id in ({placeholders}) order by fragment_id",
            list(ids),
        ).fetchall()
        if len(rows) != len(ids):
            raise SourceV6StorageError("fragment slice is incomplete")
        return tuple(_row_fragment(row) for row in rows)
    finally:
        connection.close()


def iter_fragments_parallel(
    path: str | Path, *, workers: int = 1, chunk_size: int = 256
) -> tuple[SourceV6Fragment, ...]:
    """Decode every fragment in `fragment_id` order, across processes when asked.

    Decoding dominates the single-writer tail of an import and is independent
    per fragment, so it parallelises cleanly. The result is identical to
    `iter_fragments` for the same database.
    """
    ids = fragment_ids(path)
    if not ids:
        return ()
    if workers < 2 or len(ids) <= chunk_size:
        return tuple(iter_fragments(path))
    slices = [ids[start : start + chunk_size] for start in range(0, len(ids), chunk_size)]
    target = str(path)
    decoded: list[tuple[SourceV6Fragment, ...]] = []
    with ProcessPoolExecutor(max_workers=min(workers, len(slices))) as executor:
        futures = [executor.submit(decode_fragment_slice, target, item) for item in slices]
        for future in futures:
            decoded.append(future.result())
    return tuple(fragment for group in decoded for fragment in group)


def verify_fragment_slice(path: str | Path, ids: Sequence[str]) -> None:
    """Verify one explicit id slice; safe to run in a worker process.

    Opens its own read-only connection, so the caller's writer must already be
    closed: DuckDB will not let a second process open a file held for writing.
    Returns nothing — the verdict is the absence of an exception, which is what
    keeps the fan-out cheap compared with C6's decoding.
    """
    if not ids:
        return
    connection = duckdb.connect(str(path), read_only=True)
    try:
        _verify_fragment_payloads(connection, list(ids))
    finally:
        connection.close()


def verify_published_identity_parallel(
    path: str | Path, *, workers: int = 1, chunk_size: int = _VERIFY_BATCH * 4
) -> None:
    """Run the C3a readback over a closed database, across processes when asked.

    Equivalent to `_verify_published_identity` on an open connection, and the
    equivalence is what makes `workers` safe to vary: the slices are disjoint
    id sets, every worker only reads, and nothing is written, so the verdict
    cannot depend on the worker count.

    The payload half is 96.1% Python — `json.loads` of the canonical document,
    `zlib.decompress` and `sha256`, in that order of cost — and independent per
    fragment, which is why splitting it across processes is worth the spawn.
    The column half stays here: it is one DuckDB statement.

    Not cancellable, and a mismatch does not short-circuit: `with` shuts the
    pool down with `wait=True`, so queued slices finish before the failure
    propagates. That wait is load-bearing on Windows — it is what guarantees no
    worker still holds the staging file open when the caller deletes it.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    connection = duckdb.connect(str(path), read_only=True)
    try:
        _require_fresh(_schema_info(connection))
        _verify_published_columns(connection)
        ids = [
            str(row[0])
            for row in connection.execute("select fragment_id from compact_fragments").fetchall()
        ]
    finally:
        connection.close()
    if not ids:
        return
    if workers < 2 or len(ids) <= chunk_size:
        verify_fragment_slice(path, ids)
        return
    slices = [ids[start : start + chunk_size] for start in range(0, len(ids), chunk_size)]
    target = str(path)
    # `workers` reaches here from a user setting, and Windows raises above 61.
    # Measured throughput plateaus at 16 anyway, so the cap costs nothing real
    # and turns an abort after the whole copy phase into a slightly narrower run.
    width = min(workers, len(slices), _MAX_VERIFY_WORKERS)
    with ProcessPoolExecutor(max_workers=width) as executor:
        futures = [executor.submit(verify_fragment_slice, target, item) for item in slices]
        for future in futures:
            # `result()` re-raises the worker's SourceV6StorageError here, so a
            # mismatch in any slice fails the whole verification.
            future.result()


def fragment_ids(path: str | Path) -> tuple[str, ...]:
    """List committed fragment ids without decoding any payload."""
    connection = duckdb.connect(str(path), read_only=True)
    try:
        _require_fresh(_schema_info(connection))
        return tuple(
            str(row[0])
            for row in connection.execute(
                "select fragment_id from compact_fragments order by fragment_id"
            ).fetchall()
        )
    finally:
        connection.close()


def read_fragment(path: str | Path, fragment_id: str) -> dict[str, list[tuple]]:
    fragment = reconstruct_fragment(path, fragment_id)
    return {
        "fragments": [(fragment.fragment_id, fragment.source_sha256, fragment.source_name, fragment.point.canonical_key, fragment.report_start_ms, fragment.report_end_ms, fragment.initial_balance, fragment.fixed_order_balance, fragment.balance_percentage, fragment.settings_fingerprint, fragment.stitchability, json.dumps(dict(fragment.metrics), sort_keys=True, ensure_ascii=False, separators=(",", ":")), True, None, None)],
        "actions": [(item.action_id, fragment.fragment_id, item.timestamp_ms, item.symbol, item.order_id, item.action, item.fee, item.pnl, item.balance, item.size, item.post_size, item.post_side) for item in fragment.actions],
        "cycles": [(item.cycle_id, fragment.fragment_id, item.symbol, item.order_id, json.dumps(list(item.action_ids), separators=(",", ":")), item.open_timestamp_ms, item.close_timestamp_ms, item.realized_pnl, item.fees) for item in fragment.cycles],
        "events": [(item.event_id, fragment.fragment_id, item.timestamp_ms, item.action_id) for item in fragment.events],
        "samples": [(fragment.fragment_id, series, item.timestamp_ms, item.value, item.upnl) for series, samples in (("equity", fragment.equity_samples), ("wallet", fragment.wallet_samples)) for item in samples],
    }


def set_day_disposition(path: str | Path, fragment_id: str, utc_day: str, disposition: str, *, note: str = "") -> None:
    if disposition not in {"IGNORE_INCOMING", "EXCLUDE_DAY_AS_GAP"}:
        raise SourceV6StorageError("BRIDGE_NOT_COVERED is automatic, not a manual disposition")
    connection = duckdb.connect(str(path))
    try:
        connection.execute("begin")
        connection.execute("insert or replace into day_dispositions values (?, ?, ?, ?)", [fragment_id, utc_day, disposition, note])
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise
    finally:
        connection.close()


def record_fact_ownership(path: str | Path, *, fact_kind: str, fact_ids: Iterable[str], fragment_id: str, owner_fragment_id: str | None, active: bool, reason: str | None = None, winner_fragment_id: str | None = None) -> None:
    rows = [(fact_kind, fact_id, fragment_id, owner_fragment_id, active, reason, winner_fragment_id) for fact_id in fact_ids]
    connection = duckdb.connect(str(path))
    try:
        connection.execute("begin")
        if rows:
            connection.executemany("insert or replace into fact_ownership values (?, ?, ?, ?, ?, ?, ?)", rows)
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise
    finally:
        connection.close()


def _apply_resolution(connection: duckdb.DuckDBPyConnection, outgoing_fragment_id: str, incoming_fragment_id: str, status: str, reason: str | None, boundary_ms: int | None = None, evidence_json: str | None = None) -> None:
    if status == "RESOLVED":
        connection.execute("update compact_fragments set active = false, inactive_reason = 'REPLACED_BY_INCOMING', winner_fragment_id = ? where fragment_id = ?", [incoming_fragment_id, outgoing_fragment_id])
        connection.execute("update day_ownership set active = false, reason = 'REPLACED_BY_INCOMING', winner_fragment_id = ? where fragment_id = ?", [incoming_fragment_id, outgoing_fragment_id])
        connection.execute("update compact_fragments set active = true, inactive_reason = null, winner_fragment_id = null where fragment_id = ?", [incoming_fragment_id])
    elif status == "USE_OLD_WITH_SEAM_EXCLUSION":
        diagnostic = reason or "INCOMPLETE_SEAM_CYCLE_EXCLUDED"
        connection.execute("update compact_fragments set active = true, inactive_reason = null, winner_fragment_id = null where fragment_id in (?, ?)", [outgoing_fragment_id, incoming_fragment_id])
        connection.execute("update day_ownership set active = true, reason = ?, winner_fragment_id = null where fragment_id in (?, ?)", [diagnostic, outgoing_fragment_id, incoming_fragment_id])
    else:
        failure = reason or "UNRESOLVED"
        connection.execute("update compact_fragments set active = false, inactive_reason = ?, winner_fragment_id = ? where fragment_id = ?", [failure, outgoing_fragment_id, incoming_fragment_id])
        connection.execute("update day_ownership set active = false, reason = ?, winner_fragment_id = ? where fragment_id = ?", [failure, outgoing_fragment_id, incoming_fragment_id])
    connection.execute("insert or replace into fragment_resolutions values (?, ?, ?, ?, ?, ?)", [outgoing_fragment_id, incoming_fragment_id, status, reason, boundary_ms, evidence_json])


def apply_fragment_resolution(path: str | Path, *, outgoing_fragment_id: str, incoming_fragment_id: str, status: str, reason: str | None = None) -> None:
    if status not in {"RESOLVED", "USE_OLD_WITH_SEAM_EXCLUSION", "UNRESOLVED", "PARTIAL"}:
        raise SourceV6StorageError("invalid fragment resolution status")
    connection = duckdb.connect(str(path))
    try:
        connection.execute("begin")
        boundary = connection.execute("select report_end_ms from compact_fragments where fragment_id = ?", [outgoing_fragment_id]).fetchone()
        _apply_resolution(connection, outgoing_fragment_id, incoming_fragment_id, status, reason, int(boundary[0]) if boundary and status == "USE_OLD_WITH_SEAM_EXCLUSION" else None)
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise
    finally:
        connection.close()


def persist_fragment_resolution(path: str | Path, *, outgoing_fragment_id: str, incoming_fragment_id: str, status: str, fact_rows: Iterable[tuple[str, str, str, str | None, bool, str | None, str | None]], reason: str | None = None, boundary_ms: int | None = None, evidence_json: str | None = None) -> None:
    persist_fragment_resolutions(
        path,
        (
            {
                "outgoing_fragment_id": outgoing_fragment_id,
                "incoming_fragment_id": incoming_fragment_id,
                "status": status,
                "fact_rows": fact_rows,
                "reason": reason,
                "boundary_ms": boundary_ms,
                "evidence_json": evidence_json,
            },
        ),
    )


def persist_fragment_resolutions(
    path: str | Path,
    resolutions: Iterable[Mapping[str, object]],
) -> None:
    """Apply many resolver decisions through one connection and transaction.

    Decisions are ordered and applied exactly as sequential single writes would
    apply them, so activation state and lineage are unchanged.
    """
    ordered = [dict(item) for item in resolutions]
    if not ordered:
        return
    for item in ordered:
        if item.get("status") not in {"RESOLVED", "USE_OLD_WITH_SEAM_EXCLUSION", "UNRESOLVED", "PARTIAL"}:
            raise SourceV6StorageError("invalid fragment resolution status")
    connection = duckdb.connect(str(path))
    try:
        connection.execute("begin")
        for item in ordered:
            rows = list(item.get("fact_rows") or ())
            if rows:
                connection.executemany("insert or replace into fact_ownership values (?, ?, ?, ?, ?, ?, ?)", rows)
            _apply_resolution(
                connection,
                str(item["outgoing_fragment_id"]),
                str(item["incoming_fragment_id"]),
                str(item["status"]),
                item.get("reason"),
                item.get("boundary_ms"),
                item.get("evidence_json"),
            )
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise
    finally:
        connection.close()
