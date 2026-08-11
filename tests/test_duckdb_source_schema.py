from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import struct
import zlib

import duckdb
import pytest

from mrs3.duckdb_source_schema import (
    NORMALIZATION_CONTRACT_TOLERANCE_BP,
    NORMALIZATION_CONTRACT_VERSION,
    SourceSchemaError,
    canonical_report_key,
    ensure_source_schema,
    migrate_source_database,
    normalize_source_shift,
    validate_source_database,
)


def _delta_blob(values: tuple[int, ...]) -> bytes:
    deltas = (values[0],) + tuple(right - left for left, right in zip(values, values[1:]))
    return zlib.compress(struct.pack(f"<{len(deltas)}q", *deltas))


def _actions_blob(rows: list[dict[str, str]]) -> bytes:
    headers = list(rows[0])
    payload = {"headers": headers, "rows": [[row[header] for header in headers] for row in rows]}
    return zlib.compress(json.dumps(payload).encode("utf-8"))


def _wallet_blob(rows: tuple[tuple[int, int], ...]) -> bytes:
    return zlib.compress(b"".join(struct.pack("<Iq", *row) for row in rows))


def _settings(symbol: str, multiplier: str, open_ma: int = 3) -> str:
    return json.dumps(
        {
            "basic": {
                "symbol": symbol,
                "time_frame": "15m",
                "use_long": True,
                "use_short": False,
            },
            "mrs2": {
                "ma_long": {
                    "type": "ema",
                    "source": "close",
                    "len": open_ma,
                    "multiplier": multiplier,
                },
                "ma_close_long": {"type": "ema", "source": "close", "len": 9},
            },
        },
        sort_keys=True,
    )


def _v4_database(path: Path, *, multiplier: str = "0.99", source_hash: str = "a" * 64) -> Path:
    timestamps = (1_700_000_000_000, 1_700_000_060_000)
    equity = (100_000_000_000, 101_000_000_000)
    actions = [
        {
            "Timestamp": "2023-11-14 22:13:20",
            "Symbol": "BTCUSDT",
            "Action": "opened",
            "Side": "buy",
        }
    ]
    con = duckdb.connect(str(path))
    try:
        con.execute("create table schema_info(key varchar primary key, value varchar not null)")
        con.execute("insert into schema_info values ('schema_version', '4')")
        con.execute(
            """create table point_configs(
                   point_id varchar primary key, symbol varchar not null, side varchar not null,
                   timeframe varchar not null, open_ma_type varchar not null,
                   open_ma_source varchar not null, open_ma_len integer not null,
                   open_multiplier varchar not null, close_ma_type varchar not null,
                   close_ma_source varchar not null, close_ma_len integer not null)"""
        )
        con.execute(
            """create table time_grids(
                   grid_id varchar primary key, sample_count integer not null,
                   start_timestamp_ms bigint not null, end_timestamp_ms bigint not null,
                   timestamps_zlib blob not null)"""
        )
        con.execute(
            """create table report_runs(
                   report_id varchar primary key, source_sha256 varchar unique not null,
                   canonical_key varchar unique not null, point_id varchar not null,
                   grid_id varchar not null, source_file varchar not null, source_size bigint not null,
                   imported_at_utc timestamp not null, settings_json varchar not null,
                   raw_action_count integer not null, equity_sample_count integer not null,
                   wallet_change_count integer not null)"""
        )
        con.execute(
            """create table report_payloads(
                   report_id varchar primary key, series_codec varchar not null,
                   actions_codec varchar not null, actions_zlib blob not null,
                   equity_zlib blob not null, wallet_zlib blob not null)"""
        )
        con.execute(
            "insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?)",
            ["P1", "BTCUSDT", "LONG", "15m", "ema", "close", 3, multiplier, "ema", "close", 9],
        )
        con.execute(
            "insert into time_grids values (?,?,?,?,?)",
            ["G1", len(timestamps), timestamps[0], timestamps[-1], _delta_blob(timestamps)],
        )
        con.execute(
            "insert into report_runs values (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                "R1",
                source_hash,
                "P1|G1",
                "P1",
                "G1",
                "C:/reports/report.html",
                123,
                "2026-08-11T00:00:00Z",
                _settings("BTCUSDT", multiplier),
                len(actions),
                len(equity),
                1,
            ],
        )
        con.execute(
            "insert into report_payloads values (?,?,?,?,?,?)",
            [
                "R1",
                "zlib-int64-delta-v1",
                "zlib-columnar-json-v1",
                _actions_blob(actions),
                _delta_blob(equity),
                _wallet_blob(((0, equity[0]),)),
            ],
        )
    finally:
        con.close()
    return path


def _metadata(multiplier: str = "0.99", *, period_end: int = 1_700_000_060_000) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "15m",
        "open_multiplier": multiplier,
        "open_ma_len": 3,
        "close_ma_len": 9,
        "report_period_start": 1_700_000_000_000,
        "report_period_end": period_end,
        "grid_hash": "integrity-only",
        "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
    }


def test_multiplier_text_and_grid_hash_do_not_change_canonical_report_identity() -> None:
    left = _metadata("0.99")
    right = {**_metadata("0.9900"), "grid_hash": "different-grid"}

    assert normalize_source_shift("0.99", NORMALIZATION_CONTRACT_VERSION) == 100
    assert normalize_source_shift("0.9900", NORMALIZATION_CONTRACT_VERSION) == 100
    assert canonical_report_key(left) == canonical_report_key(right)
    assert canonical_report_key(_metadata(period_end=1_700_000_120_000)) != canonical_report_key(left)


def test_canonical_identity_rejects_multiplier_on_the_wrong_side_of_one() -> None:
    with pytest.raises(SourceSchemaError, match="LONG"):
        canonical_report_key({**_metadata(), "open_multiplier": "1.01"})

    with pytest.raises(SourceSchemaError, match="SHORT"):
        canonical_report_key({**_metadata(), "side": "SHORT", "open_multiplier": "0.99"})


def test_v5_schema_persists_normalization_contract_and_separates_active_hashes_from_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v5.duckdb"
    con = duckdb.connect(str(database))
    try:
        assert ensure_source_schema(con) == 5
        metadata = dict(con.execute("select key,value from schema_info").fetchall())
        assert metadata == {
            "normalization_contract_tolerance_bp": NORMALIZATION_CONTRACT_TOLERANCE_BP,
            "normalization_contract_version": NORMALIZATION_CONTRACT_VERSION,
            "schema_version": "5",
            "storage_mode": "one-active-compact-payload-per-canonical-report",
        }
        columns = {
            row[0]: {column[1] for column in con.execute(f"pragma table_info('{row[0]}')").fetchall()}
            for row in con.execute(
                "select table_name from information_schema.tables where table_schema='main'"
            ).fetchall()
        }
        assert {
            "schema_info",
            "point_configs",
            "time_grids",
            "active_reports",
            "report_payloads",
            "replacement_history",
        }.issubset(columns)

        con.execute(
            "insert into replacement_history values (?,?,?,?,?,?)",
            ["A1", "point|period", "a" * 64, "b" * 64, "2026-08-11", "job-1"],
        )
        con.execute(
            "insert into replacement_history values (?,?,?,?,?,?)",
            ["A2", "point|period", "b" * 64, "a" * 64, "2026-08-11", "job-2"],
        )
        assert con.execute("select count(*) from replacement_history").fetchone() == (2,)
    finally:
        con.close()


def test_migration_is_out_of_place_and_preserves_row_hash_payload_and_source_hash_parity(
    tmp_path: Path,
) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "source-v5.duckdb"
    before = source.read_bytes()

    result = migrate_source_database(source, target)

    assert result.report_count == result.payload_count == result.point_count == result.grid_count == 1
    assert source.read_bytes() == before
    assert target.is_file()
    con = duckdb.connect(str(target), read_only=True)
    try:
        validation = validate_source_database(con)
        assert validation.valid, validation.errors
        assert validation.report_count == validation.payload_count == 1
        assert con.execute("select source_sha256 from active_reports").fetchone() == ("a" * 64,)
        assert con.execute("select length(row_sha256), length(payload_sha256) from active_reports join report_payloads using(report_id)").fetchone() == (64, 64)
    finally:
        con.close()


def test_migration_rejects_same_path_and_validation_failure_without_touching_old_database(
    tmp_path: Path,
) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    before = source.read_bytes()

    with pytest.raises(SourceSchemaError, match="different"):
        migrate_source_database(source, source)
    assert source.read_bytes() == before

    con = duckdb.connect(str(source))
    con.execute("update report_payloads set equity_zlib=?", [b"not-zlib"])
    con.close()
    corrupted = source.read_bytes()
    target = tmp_path / "must-not-exist.duckdb"

    # Trusted production migration copies opaque report payloads.  The explicit
    # full validator remains the opt-in payload-integrity check.
    migrate_source_database(source, target)
    assert source.read_bytes() == corrupted
    con = duckdb.connect(str(target), read_only=True)
    try:
        assert not validate_source_database(con).valid
    finally:
        con.close()


def test_trusted_migration_does_not_decode_report_payloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import duckdb_source_schema

    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "source-v5.duckdb"

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("trusted migration must not decode report payloads")

    monkeypatch.setattr(duckdb_source_schema, "decode_compact_actions", forbidden)
    monkeypatch.setattr(duckdb_source_schema, "decode_wallet_changes", forbidden)
    result = migrate_source_database(source, target, workers=2, transaction_batch_size=1)

    assert result.validation.valid


@pytest.mark.parametrize("workers,batch_size", [(0, 1), (1, 0), (True, 1), (1, True)])
def test_trusted_migration_rejects_non_positive_worker_or_batch_values(
    tmp_path: Path, workers: object, batch_size: object
) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    with pytest.raises(SourceSchemaError, match="positive integer"):
        migrate_source_database(source, tmp_path / "target.duckdb", workers=workers, transaction_batch_size=batch_size)  # type: ignore[arg-type]


def test_trusted_migration_rejects_invalid_codec_without_publishing(tmp_path: Path) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "target-v5.duckdb"
    con = duckdb.connect(str(source)); con.execute("update report_payloads set actions_codec='wrong'"); con.close()

    with pytest.raises(SourceSchemaError, match="codec"):
        migrate_source_database(source, target)

    assert not target.exists()


def test_trusted_migration_does_not_replace_target_created_at_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import duckdb_source_schema

    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "target-v5.duckdb"
    rename = duckdb_source_schema.os.rename

    def race(stage: Path, destination: Path) -> None:
        destination.write_bytes(b"racer")
        rename(stage, destination)

    monkeypatch.setattr(duckdb_source_schema.os, "rename", race)
    with pytest.raises(SourceSchemaError, match="already exists"):
        migrate_source_database(source, target)
    assert target.read_bytes() == b"racer"


def test_trusted_migration_prepares_batch_records_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import duckdb_source_schema
    import threading

    source = _v4_database(tmp_path / "source-v4.duckdb")
    con = duckdb.connect(str(source))
    try:
        con.execute("insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?)", ["P2", "BTCUSDT", "LONG", "15m", "ema", "close", 3, "0.98", "ema", "close", 9])
        con.execute("insert into report_runs values (?,?,?,?,?,?,?,?,?,?,?,?)", ["R2", "b" * 64, "P2|G1", "P2", "G1", "C:/reports/report-2.html", 124, "2026-08-11T00:00:00Z", _settings("BTCUSDT", "0.98"), 1, 2, 1])
        payload = con.execute("select * from report_payloads where report_id='R1'").fetchone()
        con.execute("insert into report_payloads values (?,?,?,?,?,?)", ["R2", *payload[1:]])
    finally:
        con.close()
    original = duckdb_source_schema._trusted_prepare_record
    barrier = threading.Barrier(2)
    def prepare(*args: object) -> object:
        barrier.wait(timeout=2)
        return original(*args)  # type: ignore[arg-type]
    monkeypatch.setattr(duckdb_source_schema, "_trusted_prepare_record", prepare)

    migrate_source_database(source, tmp_path / "target-v5.duckdb", workers=2, transaction_batch_size=2)


def test_trusted_migration_paginates_multiple_batches_and_includes_empty_report_id(tmp_path: Path) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    con = duckdb.connect(str(source))
    try:
        con.execute("update report_runs set report_id='' where report_id='R1'")
        con.execute("update report_payloads set report_id='' where report_id='R1'")
        con.execute("insert into point_configs values (?,?,?,?,?,?,?,?,?,?,?)", ["P2", "BTCUSDT", "LONG", "15m", "ema", "close", 3, "0.98", "ema", "close", 9])
        con.execute("insert into report_runs values (?,?,?,?,?,?,?,?,?,?,?,?)", ["R2", "b" * 64, "P2|G1", "P2", "G1", "C:/reports/report-2.html", 124, "2026-08-11T00:00:00Z", _settings("BTCUSDT", "0.98"), 1, 2, 1])
        payload = con.execute("select * from report_payloads where report_id='' ").fetchone()
        con.execute("insert into report_payloads values (?,?,?,?,?,?)", ["R2", *payload[1:]])
    finally:
        con.close()

    result = migrate_source_database(source, tmp_path / "target-v5.duckdb", transaction_batch_size=1)

    assert result.report_count == 2


def test_incompatible_normalization_contract_fails_before_target_preflight_or_write(
    tmp_path: Path,
) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    con = duckdb.connect(str(source))
    con.execute(
        "insert into schema_info values ('normalization_contract_version', 'incompatible')"
    )
    con.close()
    target = tmp_path / "already-there.duckdb"
    target.write_bytes(b"sentinel")
    before_source = source.read_bytes()
    before_target = target.read_bytes()

    with pytest.raises(SourceSchemaError, match="already exists"):
        migrate_source_database(source, target)

    assert source.read_bytes() == before_source
    assert target.read_bytes() == before_target


def test_active_source_hash_cannot_belong_to_two_canonical_reports(tmp_path: Path) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "source-v5.duckdb"
    migrate_source_database(source, target)
    con = duckdb.connect(str(target))
    try:
        row = list(con.execute("select * from active_reports").fetchone())
        row[0] = "R2"
        row[1] = "another-point|period"
        with pytest.raises(duckdb.ConstraintException):
            con.execute(
                "insert into active_reports values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
    finally:
        con.close()


def test_validation_rejects_schema_that_copied_columns_but_lost_constraints(
    tmp_path: Path,
) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    valid = tmp_path / "valid-v5.duckdb"
    migrate_source_database(source, valid)
    unconstrained = tmp_path / "unconstrained-v5.duckdb"
    original = duckdb.connect(str(valid), read_only=True)
    copy = duckdb.connect(str(unconstrained))
    try:
        for table in (
            "schema_info",
            "point_configs",
            "time_grids",
            "active_reports",
            "report_payloads",
            "replacement_history",
        ):
            rows = original.execute(f"select * from {table}").fetchall()
            schema = original.execute(f"describe {table}").fetchall()
            columns = ",".join(f'"{row[0]}" {row[1]}' for row in schema)
            copy.execute(f"create table {table}({columns})")
            if rows:
                placeholders = ",".join("?" for _ in schema)
                copy.executemany(f"insert into {table} values ({placeholders})", rows)
    finally:
        original.close()
        copy.close()

    con = duckdb.connect(str(unconstrained), read_only=True)
    try:
        validation = validate_source_database(con)
    finally:
        con.close()

    assert not validation.valid
    assert "constraint" in validation.errors[0]


def test_transaction_failure_removes_stage_and_keeps_source_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mrs3 import duckdb_source_schema

    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "source-v5.duckdb"
    before = source.read_bytes()

    def fail_copy(*_: object) -> None:
        raise duckdb.TransactionException("injected transaction failure")

    monkeypatch.setattr(duckdb_source_schema, "_trusted_prepare_batch", fail_copy)

    with pytest.raises(duckdb.TransactionException, match="injected"):
        migrate_source_database(source, target)

    assert source.read_bytes() == before
    assert not target.exists()
    assert list(tmp_path.glob(".source-v5.duckdb.migration-*.duckdb")) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "update active_reports set row_sha256='0000000000000000000000000000000000000000000000000000000000000000'",
        "update report_payloads set payload_sha256='0000000000000000000000000000000000000000000000000000000000000000'",
    ],
)
def test_validation_recomputes_row_and_payload_hashes(tmp_path: Path, mutation: str) -> None:
    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "source-v5.duckdb"
    migrate_source_database(source, target)
    con = duckdb.connect(str(target))
    con.execute(mutation)
    con.close()

    con = duckdb.connect(str(target), read_only=True)
    try:
        validation = validate_source_database(con)
    finally:
        con.close()

    assert not validation.valid
    assert "hash" in validation.errors[0]


def test_validation_rejects_report_period_that_differs_from_referenced_grid_bounds(
    tmp_path: Path,
) -> None:
    from mrs3 import duckdb_source_schema

    source = _v4_database(tmp_path / "source-v4.duckdb")
    target = tmp_path / "source-v5.duckdb"
    migrate_source_database(source, target)
    con = duckdb.connect(str(target))
    try:
        cursor = con.execute("select * from active_reports")
        columns = [column[0] for column in cursor.description]
        report = dict(zip(columns, cursor.fetchone(), strict=True))
        payload = con.execute("select * from report_payloads").fetchone()
        con.execute("delete from report_payloads")
        report["report_period_end_ms"] = int(report["report_period_end_ms"]) + 1
        report["canonical_report_key"] = canonical_report_key(report)
        report["row_sha256"] = duckdb_source_schema._report_hash(report)
        con.execute(
            """update active_reports
                  set canonical_report_key=?,report_period_end_ms=?,row_sha256=?
                where report_id=?""",
            [
                report["canonical_report_key"],
                report["report_period_end_ms"],
                report["row_sha256"],
                report["report_id"],
            ],
        )
        con.execute("insert into report_payloads values (?,?,?,?,?,?,?)", payload)
    finally:
        con.close()

    con = duckdb.connect(str(target), read_only=True)
    try:
        validation = validate_source_database(con)
    finally:
        con.close()

    assert not validation.valid
    assert validation.errors == ("active report period does not match referenced time-grid bounds",)
