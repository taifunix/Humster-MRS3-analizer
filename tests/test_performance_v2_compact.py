from __future__ import annotations

from pathlib import Path
import os

import duckdb
import pytest
import shutil

import mrs3.performance_v2_compact as compact_module
from mrs3.performance_v2_compact import compact_performance_v2


def test_compact_refuses_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    target = tmp_path / "target.duckdb"
    source.write_bytes(b"fixture")
    target.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        compact_performance_v2(source, target, workers=1)

    assert target.read_bytes() == b"keep"


def test_compact_copies_complete_small_v8_fixture(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    (tmp_path / "source").mkdir()
    connection = _candidate_db(tmp_path / "source")
    source = tmp_path / "source" / "strategy_performance.duckdb"
    before = {
        table: connection.execute(f"select count(*) from {table}").fetchone()[0]
        for table in (
            "strategies", "analysis_plateaus", "strategy_orders", "strategy_results",
            "strategy_actions", "strategy_equity", "window_metrics", "import_runs",
            "import_files", "selection_runs", "selection_results",
            "selection_review_imports", "selection_review_rows", "strategy_tags",
            "optimizer_prepared_inputs", "equity_quality_metrics",
        )
    }
    connection.close()
    target = tmp_path / "target" / "candidate.duckdb"
    target.parent.mkdir()

    report = compact_performance_v2(source, target, workers=2)

    assert report["source_stat_unchanged"] is True
    assert report["source_schema_version"] == 8
    assert report["target"]["schema_version"] == 8
    assert report["memory_limit"] == "16GB"
    assert report["table_counts"] == {name: int(count) for name, count in before.items()}
    assert report["verified_table_counts"] == report["table_counts"]


def test_compact_transforms_prepared_payload_before_insert(tmp_path: Path) -> None:
    from tests.test_performance_v2_optimizer import _typed_candidate_database
    from mrs3.performance_v2_optimizer import (
        decode_prepared_storage,
        prepare_current_optimizer_inputs,
    )

    source, (result_id,) = _typed_candidate_database(tmp_path / "source")
    prepare_current_optimizer_inputs(str(source), [result_id], workers=2)
    target = tmp_path / "target" / "candidate.duckdb"
    target.parent.mkdir()
    with source.open("rb") as handle:
        source_before = handle.read()
    report = compact_performance_v2(source, target, workers=2)

    import duckdb

    with duckdb.connect(str(source), read_only=True) as source_connection, duckdb.connect(
        str(target), read_only=True
    ) as target_connection:
        source_payload = source_connection.execute(
            "select prepared_json from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]
        target_payload = target_connection.execute(
            "select prepared_json from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]
    assert target_payload.startswith("mrs3-zlib-v1:")
    assert decode_prepared_storage(target_payload) == decode_prepared_storage(source_payload)
    assert report["prepared"]["prepared_rows"] == 1
    assert source.read_bytes() == source_before


def test_compact_preserves_semantically_invalid_legacy_payload(tmp_path: Path) -> None:
    from tests.test_performance_v2_optimizer import _typed_candidate_database
    from mrs3.performance_v2_optimizer import decode_prepared_storage, prepare_current_optimizer_inputs

    source, (result_id,) = _typed_candidate_database(tmp_path / "source")
    prepare_current_optimizer_inputs(str(source), [result_id], workers=1)
    legacy_payload = '{"not_a_prepared_input":true}'
    with duckdb.connect(str(source)) as connection:
        connection.execute(
            "update optimizer_prepared_inputs set prepared_json = ? where result_id = ?",
            [legacy_payload, result_id],
        )
    target = tmp_path / "target.duckdb"

    compact_performance_v2(source, target, workers=1)

    with duckdb.connect(str(target), read_only=True) as connection:
        target_payload = connection.execute(
            "select prepared_json from optimizer_prepared_inputs where result_id = ?", [result_id]
        ).fetchone()[0]
    assert decode_prepared_storage(target_payload) == legacy_payload


def test_compact_migrates_empty_v6_schema_to_v8(tmp_path: Path) -> None:
    from tests.test_performance_v2_store import _initialize_v6_fixture

    source = tmp_path / "source.duckdb"
    with duckdb.connect(str(source)) as connection:
        _initialize_v6_fixture(connection)
    target = tmp_path / "target.duckdb"

    report = compact_performance_v2(source, target, workers=1)

    assert report["source_schema_version"] == 6
    assert report["target"]["schema_version"] == 8


@pytest.mark.parametrize("version", [6, 7])
def test_compact_preserves_populated_legacy_v6_v7_and_sequence_gap(
    tmp_path: Path, version: int
) -> None:
    from tests.test_performance_v2_optimizer import _source
    from tests.test_performance_v2_store import (
        _initialize_v6_fixture,
        _insert_result_with_children,
        _migrate_schema_v6_to_v7,
        _strategy,
    )
    from mrs3.performance_v2_optimizer import build_prepared_input, source_digest

    source = tmp_path / f"source-{version}.duckdb"
    with duckdb.connect(str(source)) as connection:
        _initialize_v6_fixture(connection)
        if version == 7:
            _migrate_schema_v6_to_v7(connection)
        result_id = _insert_result_with_children(connection, _strategy(connection, name=f"legacy-{version}"))
        typed = _source(result_id=int(result_id))
        connection.execute(
            "update optimizer_prepared_inputs set preparation_version = ?, source_digest = ?, prepared_json = ? where result_id = ?",
            ["5", source_digest(typed), build_prepared_input(typed).to_json(), result_id],
        )
        connection.execute("select nextval('performance_v2_result_id_seq')")
    with duckdb.connect(str(source), read_only=True) as connection:
        source_sequence = connection.execute(
            "select last_value from duckdb_sequences() where sequence_name = 'performance_v2_result_id_seq'"
        ).fetchone()[0]
    target = tmp_path / f"target-{version}.duckdb"

    report = compact_performance_v2(source, target, workers=2)

    assert report["source_schema_version"] == version
    assert report["target"]["sha256"] == compact_module._sha256(target)
    with duckdb.connect(str(target), read_only=True) as connection:
        assert connection.execute(
            "select last_value from duckdb_sequences() where sequence_name = 'performance_v2_result_id_seq'"
        ).fetchone()[0] == source_sequence


def test_compact_rejects_source_stat_change_during_copy(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    original_mtime = source.stat().st_mtime_ns
    changed = False

    def progress(_event: dict[str, object]) -> None:
        nonlocal changed
        if not changed:
            os.utime(source, ns=(original_mtime, original_mtime + 123456789))
            changed = True

    target = tmp_path / "target.duckdb"
    with pytest.raises(ValueError, match="source database changed"):
        compact_performance_v2(source, target, workers=1, progress_callback=progress)
    assert changed
    assert not target.exists()


def test_compact_rejects_alias_unknown_catalog_and_low_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    alias = tmp_path / "alias.duckdb"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="aliases"):
        compact_performance_v2(source, alias, workers=1)

    def low_space(_path: Path):
        return shutil._ntuple_diskusage(total=1, used=1, free=0)

    monkeypatch.setattr(compact_module.shutil, "disk_usage", low_space)
    with pytest.raises(OSError, match="10 GiB"):
        compact_performance_v2(source, tmp_path / "low-space.duckdb", workers=1)

    monkeypatch.undo()
    with duckdb.connect(str(source)) as connection:
        connection.execute("create view unexpected_view as select 1")
    with pytest.raises(ValueError, match="catalog"):
        compact_performance_v2(source, tmp_path / "view.duckdb", workers=1)

def test_compact_rejects_nonzero_wal_sidecar(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    Path(str(source) + ".wal").write_bytes(b"wal")

    with pytest.raises(ValueError, match="nonzero WAL"):
        compact_performance_v2(source, tmp_path / "target.duckdb", workers=1)


def test_compact_rejects_unknown_base_table(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.execute("create table unexpected_table (value integer)")
    connection.close()
    source = source_dir / "strategy_performance.duckdb"

    with duckdb.connect(str(source), read_only=True) as connection:
        with pytest.raises(ValueError, match="unknown catalog objects"):
            compact_module._unknown_catalog_objects(connection)
    with pytest.raises(ValueError, match="catalog"):
        compact_performance_v2(source, tmp_path / "target.duckdb", workers=1)


def test_compact_rejects_planted_stage_wal_before_publish(tmp_path: Path) -> None:
    from tests.test_performance_v2_selection import _candidate_db

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    connection = _candidate_db(source_dir)
    connection.close()
    source = source_dir / "strategy_performance.duckdb"
    target = tmp_path / "target.duckdb"
    planted = False

    def progress(event: dict[str, object]) -> None:
        nonlocal planted
        if planted or event.get("phase") != "verify" or event.get("table") != "equity_quality_metrics":
            return
        stages = tuple(target.parent.glob(f".{target.name}.*.staging.duckdb"))
        assert len(stages) == 1
        Path(str(stages[0]) + ".wal").write_bytes(b"wal")
        planted = True

    with pytest.raises(ValueError, match="nonzero WAL"):
        compact_performance_v2(source, target, workers=1, progress_callback=progress)

    assert planted
    assert not target.exists()


def test_compact_rejects_corrupt_prepared_stream_without_publishing(tmp_path: Path) -> None:
    from tests.test_performance_v2_optimizer import _typed_candidate_database
    from mrs3.performance_v2_optimizer import prepare_current_optimizer_inputs

    source, (result_id,) = _typed_candidate_database(tmp_path / "source")
    prepare_current_optimizer_inputs(str(source), [result_id], workers=1)
    with duckdb.connect(str(source)) as connection:
        connection.execute(
            "update optimizer_prepared_inputs set prepared_json = ? where result_id = ?",
            ["mrs3-zlib-v1:1:bad:not-base64", result_id],
        )
    target = tmp_path / "target.duckdb"

    with pytest.raises(ValueError, match="prepared storage|prepared artifact"):
        compact_performance_v2(source, target, workers=1)

    assert not target.exists()


def test_typed_comparison_preserves_float_signed_zero() -> None:
    assert not compact_module._equal(-0.0, 0.0)
    assert compact_module._equal(float("nan"), float("nan"))
