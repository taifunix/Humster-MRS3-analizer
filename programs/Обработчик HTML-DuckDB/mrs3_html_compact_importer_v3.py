#!/usr/bin/env python3
"""MRS3 Compact HTML Importer v3.

Imports Hamster Bot MRS2 HTML reports into an append-only DuckDB database.
One report becomes one metadata row and one compressed payload row: it never
creates a row per equity sample or per action.  The source HTML is never
deleted by this program.

Dependencies: py -m pip install duckdb lxml
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from typing import Any, Iterable
import zlib

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - handled by the BAT launcher
    raise SystemExit("Missing dependency: run 'py -m pip install duckdb lxml'") from exc

from lxml import etree, html as lxml_html


SCHEMA_VERSION = "3"
SCALE = Decimal("100000000")
SERIES_CODEC = "zlib-int64-delta-v1"
ACTIONS_CODEC = "zlib-columnar-json-v1"
WALLET_RECORD = struct.Struct("<Iq")


@dataclass(frozen=True, slots=True)
class ParsedReport:
    source_hash: str
    source_file: str
    source_size: int
    settings_json: str
    point: dict[str, Any]
    grid_id: str
    timestamps_ms: tuple[int, ...]
    wallet_scaled: tuple[int, ...]
    equity_scaled: tuple[int, ...]
    actions: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class CompactRecord:
    """Immutable, storage-ready representation of one compact HTML report."""

    source_sha256: str
    source_file: str
    source_size: int
    settings_json: str
    point_id: str
    symbol: str
    side: str
    timeframe: str
    open_ma_type: str
    open_ma_source: str
    open_ma_len: int
    open_multiplier: str
    close_ma_type: str
    close_ma_source: str
    close_ma_len: int
    grid_id: str
    sample_count: int
    start_timestamp_ms: int
    end_timestamp_ms: int
    series_codec: str
    actions_codec: str
    timestamps_zlib: bytes
    actions_zlib: bytes
    equity_zlib: bytes
    wallet_zlib: bytes
    raw_action_count: int
    equity_sample_count: int
    wallet_change_count: int

    @property
    def source_hash(self) -> str:
        """Compatibility spelling used by the original importer internals."""
        return self.source_sha256


@dataclass(frozen=True, slots=True)
class CompactParseOutcome:
    source_file: str
    source_sha256: str
    record: CompactRecord | None
    error_classification: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class ImportResult:
    scanned_reports: int
    imported_reports: int
    skipped_reports: int
    quarantined_reports: int
    raw_trade_action_count: int
    equity_sample_count: int


def _text(node: Any) -> str:
    return " ".join(" ".join(node.itertext()).split())


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    token = str(value).strip().replace("\u00a0", " ").replace(" ", "")
    if not token:
        return Decimal("0")
    if "," in token and "." in token:
        token = token.replace(",", "") if token.rfind(".") > token.rfind(",") else token.replace(".", "").replace(",", ".")
    elif "," in token:
        token = token.replace(",", ".")
    try:
        return Decimal(token)
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc


def _scaled(value: Any) -> int:
    return int((_as_decimal(value) * SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _settings(document: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for pre in document.xpath("//pre"):
        try:
            value = json.loads("".join(pre.itertext()).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("basic"), dict):
            candidates.append(value)
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one strategy settings JSON object, found {len(candidates)}")
    return candidates[0]


def _raw_actions(document: Any) -> list[dict[str, str]]:
    matches: list[list[dict[str, str]]] = []
    for table in document.xpath("//table"):
        headers = [_text(cell) for cell in table.xpath(".//thead/tr[1]/th")]
        if not {"Timestamp", "Action", "PnL", "Balance"}.issubset(headers):
            continue
        rows: list[dict[str, str]] = []
        for row in table.xpath(".//tbody/tr"):
            cells = [_text(cell) for cell in row.xpath("./th|./td")]
            if len(cells) != len(headers):
                raise ValueError("transaction row width differs from its table header")
            rows.append(dict(zip(headers, cells, strict=True)))
        matches.append(rows)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one transaction table, found {len(matches)}")
    return matches[0]


def _series(source: str, name: str) -> list[tuple[int, Decimal]]:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*(\[.*?\]);", source, re.DOTALL)
    if not match:
        raise ValueError(f"missing embedded {name}")
    try:
        payload = json.loads(match.group(1), parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid embedded {name}") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"embedded {name} is empty")
    parsed: list[tuple[int, Decimal]] = []
    for item in payload:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError(f"invalid row in embedded {name}")
        parsed.append((int(item[0]), _as_decimal(item[1])))
    return parsed


def _side(settings: dict[str, Any]) -> str:
    basic = settings["basic"]
    use_long = bool(basic.get("use_long"))
    use_short = bool(basic.get("use_short"))
    if use_long == use_short:
        raise ValueError("report must enable exactly one of use_long/use_short")
    return "LONG" if use_long else "SHORT"


def _point(settings: dict[str, Any], side: str) -> dict[str, Any]:
    basic = settings["basic"]
    mrs2 = settings.get("mrs2")
    if not isinstance(mrs2, dict):
        raise ValueError("settings JSON has no mrs2 object")
    active = "long" if side == "LONG" else "short"
    open_ma = mrs2.get(f"ma_{active}")
    close_ma = mrs2.get(f"ma_close_{active}")
    if not isinstance(open_ma, dict) or not isinstance(close_ma, dict):
        raise ValueError("settings JSON has no active MRS2 MA pair")
    if not str(basic.get("symbol", "")).strip() or not str(basic.get("time_frame", "")).strip():
        raise ValueError("settings basic.symbol/time_frame is missing")
    required = ((open_ma.get("len"), "open MA len"), (close_ma.get("len"), "close MA len"), (open_ma.get("multiplier"), "open multiplier"))
    for value, name in required:
        if value is None:
            raise ValueError(f"settings {name} is missing")
    point = {
        "symbol": str(basic["symbol"]).strip(),
        "side": side,
        "timeframe": str(basic["time_frame"]).strip(),
        "open_ma_type": str(open_ma.get("type", "")),
        "open_ma_source": str(open_ma.get("source", "")),
        "open_ma_len": int(open_ma["len"]),
        "open_multiplier": str(open_ma["multiplier"]),
        "close_ma_type": str(close_ma.get("type", "")),
        "close_ma_source": str(close_ma.get("source", "")),
        "close_ma_len": int(close_ma["len"]),
    }
    canonical = json.dumps(point, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    point["point_id"] = "PT_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return point


def _encode_deltas(values: tuple[int, ...]) -> bytes:
    if not values:
        raise ValueError("cannot encode an empty series")
    deltas = (values[0],) + tuple(right - left for left, right in zip(values, values[1:]))
    return zlib.compress(struct.pack("<" + "q" * len(deltas), *deltas), level=9)


def _decode_deltas(blob: bytes, expected_count: int) -> tuple[int, ...]:
    raw = zlib.decompress(blob)
    if len(raw) != expected_count * 8:
        raise ValueError("compressed series size does not match its declared sample count")
    deltas = struct.unpack("<" + "q" * expected_count, raw)
    values: list[int] = []
    for index, delta in enumerate(deltas):
        values.append(delta if index == 0 else values[-1] + delta)
    return tuple(values)


def _encode_wallet_changes(values: tuple[int, ...]) -> tuple[bytes, int]:
    changes = [(index, value) for index, value in enumerate(values) if index == 0 or value != values[index - 1]]
    raw = b"".join(WALLET_RECORD.pack(index, value) for index, value in changes)
    return zlib.compress(raw, level=9), len(changes)


def _decode_wallet_changes(blob: bytes, expected_count: int) -> tuple[tuple[int, int], ...]:
    raw = zlib.decompress(blob)
    if len(raw) != expected_count * WALLET_RECORD.size:
        raise ValueError("compressed wallet size does not match its declared change count")
    return tuple(WALLET_RECORD.unpack_from(raw, offset) for offset in range(0, len(raw), WALLET_RECORD.size))


def _encode_actions(actions: tuple[dict[str, str], ...]) -> bytes:
    if not actions:
        return zlib.compress(b'{"headers":[],"rows":[]}', level=9)
    headers = list(actions[0])
    if any(list(row) != headers for row in actions):
        raise ValueError("transaction table headers are not stable across rows")
    payload = {"headers": headers, "rows": [[row[header] for header in headers] for row in actions]}
    return zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), level=9)


def _decode_actions(blob: bytes, expected_count: int) -> tuple[dict[str, str], ...]:
    payload = json.loads(zlib.decompress(blob).decode("utf-8"))
    headers = payload.get("headers")
    rows = payload.get("rows")
    if not isinstance(headers, list) or not all(isinstance(header, str) for header in headers) or not isinstance(rows, list):
        raise ValueError("invalid compact action payload")
    if len(rows) != expected_count or any(not isinstance(row, list) or len(row) != len(headers) for row in rows):
        raise ValueError("compressed action count does not match report metadata")
    return tuple(dict(zip(headers, row, strict=True)) for row in rows)


def _compact_record(report: ParsedReport) -> CompactRecord:
    timestamps_zlib = _encode_deltas(report.timestamps_ms)
    actions_zlib = _encode_actions(report.actions)
    equity_zlib = _encode_deltas(report.equity_scaled)
    wallet_zlib, wallet_change_count = _encode_wallet_changes(report.wallet_scaled)
    point = report.point
    return CompactRecord(
        source_sha256=report.source_hash,
        source_file=report.source_file,
        source_size=report.source_size,
        settings_json=report.settings_json,
        point_id=str(point["point_id"]),
        symbol=str(point["symbol"]),
        side=str(point["side"]),
        timeframe=str(point["timeframe"]),
        open_ma_type=str(point["open_ma_type"]),
        open_ma_source=str(point["open_ma_source"]),
        open_ma_len=int(point["open_ma_len"]),
        open_multiplier=str(point["open_multiplier"]),
        close_ma_type=str(point["close_ma_type"]),
        close_ma_source=str(point["close_ma_source"]),
        close_ma_len=int(point["close_ma_len"]),
        grid_id=report.grid_id,
        sample_count=len(report.timestamps_ms),
        start_timestamp_ms=report.timestamps_ms[0],
        end_timestamp_ms=report.timestamps_ms[-1],
        series_codec=SERIES_CODEC,
        actions_codec=ACTIONS_CODEC,
        timestamps_zlib=timestamps_zlib,
        actions_zlib=actions_zlib,
        equity_zlib=equity_zlib,
        wallet_zlib=wallet_zlib,
        raw_action_count=len(report.actions),
        equity_sample_count=len(report.equity_scaled),
        wallet_change_count=wallet_change_count,
    )


def build_compact_record(path: Path) -> CompactRecord:
    """Parse and encode one HTML report into the public compact-record contract."""
    return _compact_record(_parse_report(path))


def read_compact_record(path: Path) -> CompactParseOutcome:
    """Return a stable success/error value for callers such as process workers."""
    source_file = str(path.resolve())
    try:
        record = build_compact_record(path)
    except Exception as exc:
        try:
            source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            classification = "INVALID_REPORT"
        except OSError:
            source_sha256 = "UNREADABLE"
            classification = "SOURCE_UNREADABLE"
        return CompactParseOutcome(source_file, source_sha256, None, classification, str(exc))
    return CompactParseOutcome(source_file, record.source_sha256, record, None, None)


def _parse_report(path: Path) -> ParsedReport:
    try:
        source = path.read_text(encoding="utf-8")
        document = lxml_html.fromstring(source)
    except (OSError, UnicodeDecodeError, etree.ParserError) as exc:
        raise ValueError(f"cannot parse HTML: {path}") from exc
    settings = _settings(document)
    side = _side(settings)
    wallet = _series(source, "walletSeries")
    equity = _series(source, "equitySeries")
    if len(wallet) != len(equity):
        raise ValueError("walletSeries and equitySeries have different lengths")
    timestamps = tuple(timestamp for timestamp, _ in wallet)
    if tuple(timestamp for timestamp, _ in equity) != timestamps:
        raise ValueError("walletSeries and equitySeries use different timestamps")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("equity timestamps must be strictly increasing")
    grid_id = "GRID_" + hashlib.sha256(",".join(map(str, timestamps)).encode("ascii")).hexdigest()[:24]
    return ParsedReport(
        source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        source_file=str(path.resolve()),
        source_size=path.stat().st_size,
        settings_json=json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        point=_point(settings, side),
        grid_id=grid_id,
        timestamps_ms=timestamps,
        wallet_scaled=tuple(_scaled(value) for _, value in wallet),
        equity_scaled=tuple(_scaled(value) for _, value in equity),
        actions=tuple(_raw_actions(document)),
    )


def _table_exists(connection: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(connection.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?", [name]).fetchone())


def _schema(connection: duckdb.DuckDBPyConnection) -> None:
    if _table_exists(connection, "schema_info"):
        row = connection.execute("SELECT value FROM schema_info WHERE key='schema_version'").fetchone()
        actual = str(row[0]) if row else "missing"
        if actual != SCHEMA_VERSION:
            raise ValueError(f"database schema version {actual} is not v{SCHEMA_VERSION}; use the v3 database filename from the BAT file")
    elif _table_exists(connection, "report_runs"):
        raise ValueError("database already has report_runs but no v3 schema marker; do not mix it with compact v3")
    else:
        connection.execute("CREATE TABLE schema_info(key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)")
        connection.execute("INSERT INTO schema_info VALUES ('schema_version', ?)", [SCHEMA_VERSION])
        connection.execute("INSERT INTO schema_info VALUES ('storage_mode', 'one-compressed-payload-row-per-report')")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS point_configs (
            point_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL, side VARCHAR NOT NULL, timeframe VARCHAR NOT NULL,
            open_ma_type VARCHAR NOT NULL, open_ma_source VARCHAR NOT NULL, open_ma_len INTEGER NOT NULL,
            open_multiplier VARCHAR NOT NULL, close_ma_type VARCHAR NOT NULL, close_ma_source VARCHAR NOT NULL, close_ma_len INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS time_grids (
            grid_id VARCHAR PRIMARY KEY, sample_count INTEGER NOT NULL, start_timestamp_ms BIGINT NOT NULL,
            end_timestamp_ms BIGINT NOT NULL, timestamps_zlib BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS report_runs (
            report_id VARCHAR PRIMARY KEY, source_sha256 VARCHAR UNIQUE NOT NULL, canonical_key VARCHAR UNIQUE NOT NULL,
            point_id VARCHAR NOT NULL, grid_id VARCHAR NOT NULL, source_file VARCHAR NOT NULL, source_size BIGINT NOT NULL,
            imported_at_utc TIMESTAMP NOT NULL, settings_json VARCHAR NOT NULL, raw_action_count INTEGER NOT NULL,
            equity_sample_count INTEGER NOT NULL, wallet_change_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS report_payloads (
            report_id VARCHAR PRIMARY KEY, series_codec VARCHAR NOT NULL, actions_codec VARCHAR NOT NULL,
            actions_zlib BLOB NOT NULL, equity_zlib BLOB NOT NULL, wallet_zlib BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rejected_imports (
            source_sha256 VARCHAR PRIMARY KEY, source_file VARCHAR NOT NULL, detected_at_utc TIMESTAMP NOT NULL, reason VARCHAR NOT NULL
        );
        """
    )


def _load_known(connection: duckdb.DuckDBPyConnection) -> tuple[dict[str, tuple[str, int, int, int]], dict[str, str], set[str], set[str]]:
    hashes = {str(row[0]): (str(row[1]), int(row[2]), int(row[3]), int(row[4])) for row in connection.execute(
        "SELECT source_sha256,report_id,raw_action_count,equity_sample_count,wallet_change_count FROM report_runs").fetchall()}
    canonical = {str(row[0]): str(row[1]) for row in connection.execute("SELECT canonical_key,report_id FROM report_runs").fetchall()}
    grids = {str(row[0]) for row in connection.execute("SELECT grid_id FROM time_grids").fetchall()}
    points = {str(row[0]) for row in connection.execute("SELECT point_id FROM point_configs").fetchall()}
    return hashes, canonical, grids, points


def _insert_report(connection: duckdb.DuckDBPyConnection, report: ParsedReport, known_canonical: dict[str, str], known_grids: set[str], known_points: set[str]) -> tuple[str, int]:
    return _insert_compact_record(connection, _compact_record(report), known_canonical, known_grids, known_points)


def _insert_compact_record(connection: duckdb.DuckDBPyConnection, report: CompactRecord, known_canonical: dict[str, str], known_grids: set[str], known_points: set[str]) -> tuple[str, int]:
    report_id = "RP_" + report.source_sha256[:24]
    canonical_key = f"{report.point_id}|{report.grid_id}"
    if canonical_key in known_canonical:
        raise ValueError(f"DUPLICATE_POINT_WINDOW_CONFLICT: already imported as {known_canonical[canonical_key]}")
    if report.point_id not in known_points:
        connection.execute("INSERT INTO point_configs VALUES (?,?,?,?,?,?,?,?,?,?,?)", [report.point_id, report.symbol, report.side, report.timeframe, report.open_ma_type, report.open_ma_source, report.open_ma_len, report.open_multiplier, report.close_ma_type, report.close_ma_source, report.close_ma_len])
        known_points.add(report.point_id)
    if report.grid_id not in known_grids:
        connection.execute("INSERT INTO time_grids VALUES (?,?,?,?,?)", [report.grid_id, report.sample_count, report.start_timestamp_ms, report.end_timestamp_ms, report.timestamps_zlib])
        known_grids.add(report.grid_id)
    connection.execute("INSERT INTO report_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [report_id, report.source_sha256, canonical_key, report.point_id, report.grid_id, report.source_file, report.source_size, datetime.now(timezone.utc), report.settings_json, report.raw_action_count, report.equity_sample_count, report.wallet_change_count])
    connection.execute("INSERT INTO report_payloads VALUES (?,?,?,?,?,?)", [report_id, report.series_codec, report.actions_codec, report.actions_zlib, report.equity_zlib, report.wallet_zlib])
    known_canonical[canonical_key] = report_id
    return report_id, report.wallet_change_count


def load_report_payload(connection: duckdb.DuckDBPyConnection, report_id: str) -> dict[str, Any]:
    """Return losslessly reconstructed action rows, equity values, and wallet changes."""
    row = connection.execute("""SELECT r.raw_action_count,r.equity_sample_count,r.wallet_change_count,p.actions_zlib,p.equity_zlib,p.wallet_zlib
        FROM report_runs r JOIN report_payloads p USING(report_id) WHERE r.report_id=?""", [report_id]).fetchone()
    if not row:
        raise ValueError(f"unknown report_id: {report_id}")
    return {"actions": _decode_actions(bytes(row[3]), int(row[0])), "equity_scaled": _decode_deltas(bytes(row[4]), int(row[1])), "wallet_changes": _decode_wallet_changes(bytes(row[5]), int(row[2]))}


def load_time_grid(connection: duckdb.DuckDBPyConnection, report_id: str) -> tuple[int, ...]:
    row = connection.execute("""SELECT g.sample_count,g.timestamps_zlib FROM report_runs r JOIN time_grids g USING(grid_id)
        WHERE r.report_id=?""", [report_id]).fetchone()
    if not row:
        raise ValueError(f"unknown report_id: {report_id}")
    return _decode_deltas(bytes(row[1]), int(row[0]))


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _write_audit(audit_dir: Path, result: ImportResult, checklist: list[dict[str, str]], quarantine: list[dict[str, str]]) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(audit_dir / "html_delete_checklist.csv", ["source_file", "sha256", "import_status", "report_id", "raw_actions", "equity_samples", "wallet_change_samples", "safe_to_delete", "reason"], checklist)
    _write_csv(audit_dir / "quarantine.csv", ["source_file", "sha256", "reason"], quarantine)
    manifest = {"importer": "MRS3 Compact HTML Importer v3", "schema_version": SCHEMA_VERSION, "storage_mode": "one-lossless-compressed-payload-per-report", "scanned_reports": result.scanned_reports, "imported_reports": result.imported_reports, "skipped_reports": result.skipped_reports, "quarantined_reports": result.quarantined_reports, "raw_trade_action_count": result.raw_trade_action_count, "equity_sample_count": result.equity_sample_count, "safe_delete_rule": "Delete only HTML files marked safe_to_delete=YES after reviewing this manifest."}
    temporary = audit_dir / "import_manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(audit_dir / "import_manifest.json")


def import_html_reports(html_dir: Path, database_path: Path, audit_dir: Path, *, progress_every: int = 10, batch_size: int = 250) -> ImportResult:
    if not html_dir.is_dir():
        raise ValueError(f"HTML directory does not exist: {html_dir}")
    if progress_every < 1 or batch_size < 1:
        raise ValueError("progress_every and batch_size must be at least one")
    files = sorted(path for path in html_dir.rglob("*") if path.is_file() and path.suffix.casefold() == ".html")
    print("MRS3 Compact HTML Importer v3", flush=True)
    print(f"database: {database_path.resolve()}", flush=True)
    print(f"found {len(files)} HTML report(s); progress will be shown every {progress_every} file(s)", flush=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(database_path))
    imported = skipped = quarantined = raw_actions = equity_samples = pending = 0
    checklist: list[dict[str, str]] = []
    quarantine: list[dict[str, str]] = []
    try:
        _schema(connection)
        known_hashes, known_canonical, known_grids, known_points = _load_known(connection)
        connection.execute("BEGIN TRANSACTION")
        for index, path in enumerate(files, 1):
            source_file = str(path.resolve())
            source_hash = ""
            try:
                report = build_compact_record(path)
                source_hash = report.source_sha256
                existing = known_hashes.get(source_hash)
                if existing:
                    skipped += 1
                    checklist.append({"source_file": source_file, "sha256": source_hash, "import_status": "SKIPPED_IDENTICAL", "report_id": existing[0], "raw_actions": str(existing[1]), "equity_samples": str(existing[2]), "wallet_change_samples": str(existing[3]), "safe_to_delete": "YES", "reason": "already imported by identical SHA-256"})
                else:
                    report_id, wallet_count = _insert_compact_record(connection, report, known_canonical, known_grids, known_points)
                    known_hashes[source_hash] = (report_id, report.raw_action_count, report.equity_sample_count, wallet_count)
                    imported += 1
                    raw_actions += report.raw_action_count
                    equity_samples += report.equity_sample_count
                    pending += 1
                    checklist.append({"source_file": source_file, "sha256": source_hash, "import_status": "OK", "report_id": report_id, "raw_actions": str(report.raw_action_count), "equity_samples": str(report.equity_sample_count), "wallet_change_samples": str(wallet_count), "safe_to_delete": "YES", "reason": ""})
                    if pending >= batch_size:
                        connection.execute("COMMIT")
                        connection.execute("BEGIN TRANSACTION")
                        pending = 0
            except Exception as exc:
                quarantined += 1
                reason = str(exc)
                if not source_hash:
                    try:
                        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    except OSError:
                        source_hash = "UNREADABLE"
                connection.execute("INSERT INTO rejected_imports VALUES (?,?,?,?) ON CONFLICT (source_sha256) DO UPDATE SET source_file=excluded.source_file, detected_at_utc=excluded.detected_at_utc, reason=excluded.reason", [source_hash, source_file, datetime.now(timezone.utc), reason])
                checklist.append({"source_file": source_file, "sha256": source_hash, "import_status": "QUARANTINE", "report_id": "", "raw_actions": "", "equity_samples": "", "wallet_change_samples": "", "safe_to_delete": "NO", "reason": reason})
                quarantine.append({"source_file": source_file, "sha256": source_hash, "reason": reason})
            if index % progress_every == 0 or index == len(files):
                print(f"processed {index}/{len(files)}; imported={imported}; skipped={skipped}; quarantine={quarantined}", flush=True)
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    finally:
        connection.close()
    result = ImportResult(len(files), imported, skipped, quarantined, raw_actions, equity_samples)
    _write_audit(audit_dir, result, checklist, quarantine)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=250)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = import_html_reports(args.html_dir, args.database, args.audit_dir, progress_every=args.progress_every, batch_size=args.batch_size)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"scanned_reports": result.scanned_reports, "imported_reports": result.imported_reports, "skipped_reports": result.skipped_reports, "quarantined_reports": result.quarantined_reports, "raw_trade_action_count": result.raw_trade_action_count, "equity_sample_count": result.equity_sample_count}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
