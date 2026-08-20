"""Fresh-only compact DuckDB storage for Source v6 fragments.

The only fact payload is one compressed canonical JSON blob per fragment. The
metadata columns are the index; samples, actions, cycles and events are never
stored as physical rows.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Iterable, Iterator
from uuid import uuid4

import duckdb

from .source_v6 import (
    EncodedSourceV6Fragment,
    SourceV6Error,
    SourceV6Fragment,
    decode_fragment,
    encode_fragment,
)


V6_SCHEMA_VERSION = 6
V6_FINGERPRINT = "source-v6-fresh-compact-v1"


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
            connection.execute("insert into day_ownership values (?, ?, 'ACTIVE', true, null, null)", [fragment.fragment_id, day])
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
    if status not in {"RESOLVED", "USE_OLD_WITH_SEAM_EXCLUSION", "UNRESOLVED", "PARTIAL"}:
        raise SourceV6StorageError("invalid fragment resolution status")
    connection = duckdb.connect(str(path))
    try:
        connection.execute("begin")
        rows = list(fact_rows)
        if rows:
            connection.executemany("insert or replace into fact_ownership values (?, ?, ?, ?, ?, ?, ?)", rows)
        _apply_resolution(connection, outgoing_fragment_id, incoming_fragment_id, status, reason, boundary_ms, evidence_json)
        connection.execute("commit")
    except Exception:
        connection.execute("rollback")
        raise
    finally:
        connection.close()
