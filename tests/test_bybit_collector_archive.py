from __future__ import annotations

from pathlib import Path
import os
import time

import duckdb
import pytest

from mrs3.bybit_collector.aggregation import LIQUIDITY_1M_COLUMNS
import mrs3.bybit_collector.archive as archive_module
from mrs3.bybit_collector.archive import HourlyExporter
from mrs3.bybit_collector.storage import SQLiteSpool


def _row(minute_ts_ms: int, symbol: str = "BTCUSDT") -> dict[str, object]:
    row: dict[str, object] = {
        "minute_ts_ms": minute_ts_ms,
        "symbol": symbol,
        "sample_count": 12,
        "valid_sample_count": 10,
        "coverage_ratio": 10 / 12,
        "book_reset_count": 1,
        "ws_connected_ratio": 1.0,
        "active_sample_target": 12,
        "mid_median": 100.5,
        "spread_bps_median": 1.0,
        "spread_bps_p95": 1.5,
        "spread_bps_max": 2.0,
    }
    row.update({name: 10.0 for name in LIQUIDITY_1M_COLUMNS[12:]})
    return row


def test_export_hour_publishes_valid_marker_authoritative_parquet(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        spool.write_minute(_row(3_599_999, "ETHUSDT"))
        spool.write_minute(_row(3_600_000, "XRPUSDT"))

        outcome = HourlyExporter(spool, tmp_path, collector_version="test").export_hour(
            0, 3_720_000
        )

        assert outcome.published is True
        final = tmp_path / "liquidity_1m/date=1970-01-01/part-00.parquet"
        assert final.is_file()
        assert spool.reader_files() == ("liquidity_1m/date=1970-01-01/part-00.parquet",)

        connection = duckdb.connect()
        try:
            assert connection.execute(
                f"SELECT * FROM read_parquet('{final.as_posix()}') ORDER BY minute_ts_ms, symbol"
            ).fetchall()[0][0] == 0
            metadata = {
                key.decode(): value.decode()
                for key, value in connection.execute(
                    f"SELECT key, value FROM parquet_kv_metadata('{final.as_posix()}')"
                ).fetchall()
            }
            assert metadata["schema_name"] == "bybit_liquidity_1m"
            assert metadata["schema_version"] == "2"
            assert metadata["collector_version"] == "test"
        finally:
            connection.close()


def test_export_hour_does_not_write_before_eligibility(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        outcome = HourlyExporter(spool, tmp_path).export_hour(0, 3_719_999)
        assert outcome.status == "not_eligible"
        assert not (tmp_path / "liquidity_1m").exists()
        assert spool.reader_files() == ()


def test_export_uses_half_open_hour_rows_and_is_idempotent(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        spool.write_minute(_row(3_599_999, "ETHUSDT"))
        spool.write_minute(_row(3_600_000, "XRPUSDT"))
        exporter = HourlyExporter(spool, tmp_path, collector_version="v1")
        first = exporter.export_hour(0, 3_720_000)
        second = exporter.export_hour(0, 4_000_000)
        assert first.published and second.published
        assert second.status == "already_published"
        assert first.row_count == second.row_count == 2
        assert spool.published_hours()[0].row_count == 2


def test_marked_archive_ignores_late_sqlite_rows(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path, collector_version="v1")
        first = exporter.export_hour(0, 3_720_000)
        assert first.published

        spool.write_minute(_row(60_000, "ETHUSDT"))
        retry = exporter.export_hour(0, 7_200_000)

        assert retry.published and retry.status == "already_published"
        assert retry.row_count == 1
        assert spool.count_rows(0) == 2
        assert spool.late_rows_pending() == 1
        assert exporter.verify_archive().valid


def test_marked_archive_accepts_a_new_collector_version(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        first = HourlyExporter(spool, tmp_path, collector_version="v1")
        assert first.export_hour(0, 3_720_000).published

        retry = HourlyExporter(spool, tmp_path, collector_version="v2").export_hour(
            0, 3_720_001
        )

        assert retry.published and retry.status == "already_published"


def test_unmarked_archive_ignores_late_sqlite_rows(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        writer = HourlyExporter(spool, tmp_path, collector_version="v1")
        exporter = HourlyExporter(spool, tmp_path, collector_version="v2")
        final = tmp_path / exporter.file_name(0)
        final.parent.mkdir(parents=True)
        writer._write_parquet(final, spool.read_hour(0), 3_720_000)
        spool.write_minute(_row(60_000, "ETHUSDT"))

        outcome = exporter.export_hour(0, 3_720_000)

        assert outcome.published and outcome.status == "published"
        assert outcome.row_count == 1
        assert spool.published_hours()[0].row_count == 1


def test_valid_unmarked_final_gets_marker_without_rewrite(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        writer = HourlyExporter(spool, tmp_path, collector_version="v1")
        exporter = HourlyExporter(spool, tmp_path, collector_version="v2")
        final = tmp_path / exporter.file_name(0)
        final.parent.mkdir(parents=True)
        writer._write_parquet(final, spool.read_hour(0), 3_720_000)
        before = final.read_bytes()
        outcome = exporter.export_hour(0, 3_720_000)
        assert outcome.published and outcome.status == "published"
        assert final.read_bytes() == before


def test_invalid_existing_final_is_untouched_and_degraded(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        final = tmp_path / exporter.file_name(0)
        final.parent.mkdir(parents=True)
        original = b"operator-owned invalid file"
        final.write_bytes(original)
        outcome = exporter.export_hour(0, 3_720_000)
        assert not outcome.published and outcome.status == "invalid_existing"
        assert final.read_bytes() == original
        assert spool.reader_files() == ()


def test_existing_final_race_never_overwrites_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        final = tmp_path / exporter.file_name(0)
        primitive_name = "rename" if os.name == "nt" else "link"

        def race(source: Path, destination: Path) -> None:
            final.parent.mkdir(parents=True, exist_ok=True)
            with open(source, "rb") as source_file, open(destination, "wb") as destination_file:
                destination_file.write(source_file.read())
            raise FileExistsError(destination)

        monkeypatch.setattr(archive_module.os, primitive_name, race)
        outcome = exporter.export_hour(0, 3_720_000)
        assert outcome.published
        assert not list(final.parent.glob("*.tmp"))
        assert spool.reader_files() == (exporter.file_name(0),)


def test_publication_uses_atomic_rename_from_validated_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        calls: list[tuple[Path, Path]] = []
        primitive_name = "rename" if os.name == "nt" else "link"
        original = getattr(archive_module.os, primitive_name)

        def record(source: Path, destination: Path) -> None:
            calls.append((Path(source), Path(destination)))
            original(source, destination)

        monkeypatch.setattr(archive_module.os, primitive_name, record)
        outcome = exporter.export_hour(0, 3_720_000)

        assert outcome.published
        assert len(calls) == 1
        assert calls[0][0].name.endswith(".tmp")
        assert calls[0][1].name == "part-00.parquet"


def test_publication_crash_after_validation_leaves_final_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        monkeypatch.setattr(
            exporter,
            "_publish_temp",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("publish crash")),
        )

        outcome = exporter.export_hour(0, 3_720_000)

        assert not outcome.published and outcome.status == "export_failed"
        assert not (tmp_path / exporter.file_name(0)).exists()
        assert list((tmp_path / "liquidity_1m/date=1970-01-01").glob("*.tmp"))
        assert spool.reader_files() == ()


def test_crash_before_publish_leaves_owned_tmp_for_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        monkeypatch.setattr(exporter, "_fsync_file", lambda _path: (_ for _ in ()).throw(OSError("crash")))
        outcome = exporter.export_hour(0, 3_720_000)
        assert not outcome.published and outcome.status == "export_failed"
        tmp_files = list(tmp_path.glob("liquidity_1m/date=*/part-*.parquet.*.tmp"))
        assert len(tmp_files) == 1
        old = time.time() - 2 * archive_module.EXPORT_CYCLE_MS / 1000
        os.utime(tmp_files[0], (old, old))
        recovery = exporter.recover(int(time.time() * 1000))
        assert recovery.removed_tmp == (tmp_files[0].relative_to(tmp_path).as_posix(),)


def test_final_before_marker_is_reconciled_on_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        real_mark = spool.mark_published
        monkeypatch.setattr(spool, "mark_published", lambda *_args: (_ for _ in ()).throw(OSError("crash")))
        first = exporter.export_hour(0, 3_720_000)
        assert not first.published and first.status == "marker_failed"
        monkeypatch.setattr(spool, "mark_published", real_mark)
        second = exporter.export_hour(0, 3_720_001)
        assert second.published


def test_marker_without_final_is_cleared_and_reexported(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        exporter.export_hour(0, 3_720_000)
        (tmp_path / exporter.file_name(0)).unlink()
        outcome = exporter.export_hour(0, 7_200_000)
        assert outcome.published
        assert (tmp_path / exporter.file_name(0)).is_file()


def test_recover_cleans_only_owned_old_tmp_and_reconciles_hours(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        now_ms = 46 * archive_module.HOUR_MS + 3_720_000
        scratch = tmp_path / "liquidity_1m/date=1970-01-01/part-00.parquet.0123456789abcdef0123456789abcdef.tmp"
        scratch.parent.mkdir(parents=True)
        scratch.write_bytes(b"scratch")
        unrelated = scratch.with_name("part-00.parquet.not-owned.tmp")
        unrelated.write_bytes(b"keep")
        old = (now_ms - 2 * archive_module.EXPORT_CYCLE_MS) / 1000
        os.utime(scratch, (old, old))
        recovery = exporter.recover(now_ms)
        assert recovery.removed_tmp == (scratch.relative_to(tmp_path).as_posix(),)
        assert unrelated.is_file()
        assert spool.reader_files() == (exporter.file_name(0),)


def test_recover_discovers_unmarked_hours_beyond_two_days(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        assert exporter.export_hour(0, 3_720_000).published
        old_hour = 100 * archive_module.HOUR_MS
        spool.write_minute(_row(old_hour + 60_000, "ETHUSDT"))

        recovery = exporter.recover(old_hour + archive_module.EXPORT_CYCLE_MS)

        assert len(recovery.outcomes) == 1
        assert recovery.outcomes[0].published
        assert recovery.outcomes[0].file_name == exporter.file_name(old_hour)
        assert recovery.errors == ()


def test_recover_reports_owned_tmp_unlink_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with SQLiteSpool(tmp_path) as spool:
        exporter = HourlyExporter(spool, tmp_path)
        scratch = tmp_path / "liquidity_1m/date=1970-01-01/part-00.parquet.0123456789abcdef0123456789abcdef.tmp"
        scratch.parent.mkdir(parents=True)
        scratch.write_bytes(b"scratch")
        old = time.time() - 2 * archive_module.EXPORT_CYCLE_MS / 1000
        os.utime(scratch, (old, old))
        original_unlink = Path.unlink

        def fail_only_target(path: Path, *args: object, **kwargs: object) -> None:
            if path == scratch:
                raise OSError("permission denied")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", fail_only_target)
        recovery = exporter.recover(int(time.time() * 1000))

        assert recovery.removed_tmp == ()
        assert any("permission denied" in error for error in recovery.errors)
        assert scratch.is_file()


def test_marker_authoritative_verification_reports_missing_and_invalid(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        exporter.export_hour(0, 3_720_000)
        report = exporter.verify_archive()
        assert report.valid and report.checked == (exporter.file_name(0),)
        (tmp_path / exporter.file_name(0)).unlink()
        missing = exporter.verify_archive()
        assert not missing.valid and missing.missing == (exporter.file_name(0),)


def test_verification_ignores_unmarked_file(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        final = tmp_path / exporter.file_name(0)
        final.parent.mkdir(parents=True)
        exporter._write_parquet(final, spool.read_hour(0), 3_720_000)
        report = exporter.verify_archive()
        assert report.valid and report.checked == ()
        assert spool.reader_files() == ()


def test_read_only_verifier_runs_while_writer_spool_holds_lock(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as writer:
        exporter = HourlyExporter(writer, tmp_path)
        writer.write_minute(_row(0))
        assert exporter.export_hour(0, 3_720_000).published

        reader = SQLiteSpool.open_read_only(tmp_path)
        try:
            assert HourlyExporter(reader, tmp_path).verify_archive().valid
        finally:
            reader.close()


def test_invalid_schema_metadata_count_range_and_duplicate_are_rejected(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0))
        exporter = HourlyExporter(spool, tmp_path)
        final = tmp_path / exporter.file_name(0)
        final.parent.mkdir(parents=True)
        connection = duckdb.connect()
        try:
            connection.execute("CREATE TABLE bad (minute_ts_ms BIGINT, symbol VARCHAR)")
            connection.execute("INSERT INTO bad VALUES (3600000, 'BTCUSDT'), (3600000, 'BTCUSDT')")
            connection.execute(
                f"COPY bad TO '{final.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{'schema_name':'wrong'}})"
            )
        finally:
            connection.close()
        outcome = exporter.export_hour(0, 3_720_000)
        assert not outcome.published and outcome.status == "invalid_existing"
        assert any("schema" in error or "metadata" in error or "duplicate" in error for error in outcome.errors)


def test_malformed_nullable_declaration_is_rejected(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        exporter = HourlyExporter(spool, tmp_path)
        description = [
            ("minute_ts_ms", "BIGINT", "YES"),
            ("symbol", "VARCHAR", "NO"),
        ]

        errors = exporter._validate_description(description)

        assert any("must be declared NOT NULL" in error for error in errors)


def test_smallint_counts_are_rejected_before_parquet_export(tmp_path: Path) -> None:
    with SQLiteSpool(tmp_path) as spool:
        spool.write_minute(_row(0) | {"sample_count": 32_768})
        outcome = HourlyExporter(spool, tmp_path).export_hour(0, 3_720_000)

        assert not outcome.published and outcome.status == "export_failed"
        assert any("32767" in error or "SMALLINT" in error for error in outcome.errors)
