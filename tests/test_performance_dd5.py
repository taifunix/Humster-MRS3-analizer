from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.performance_dd5 import run_performance_dd5
from mrs3.performance_import import import_performance_batch
from tests.test_performance_import import _request


def test_dd5_reads_committed_import_and_exports_calculation_only_artifacts(tmp_path: Path) -> None:
    request = _request(tmp_path)
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )

    output = tmp_path / "posttest"
    artifacts = run_performance_dd5(
        request.database,
        imported.import_id,
        output,
        AlgorithmConfig.defaults(),
    )

    assert artifacts.workbook == output / "posttest.xlsx"
    assert artifacts.manifest == output / "posttest_manifest.json"
    assert artifacts.manifest_json["dd5_mode"] == "CALCULATION_ONLY"
    assert json.loads(artifacts.manifest.read_text(encoding="utf-8"))["raw_result_count"] == 1
    with duckdb.connect(str(request.database), read_only=True) as connection:
        assert connection.execute("select count(*) from dd5_runs").fetchone()[0] == 1
        assert connection.execute("select status from dd5_runs").fetchone()[0] == "CALCULATION_ONLY"
        assert connection.execute("select count(*) from dd5_results").fetchone()[0] == 1


def test_dd5_persistence_failure_leaves_no_export_artifacts(tmp_path: Path) -> None:
    request = _request(tmp_path)
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )
        connection.execute("drop table dd5_results")

    output = tmp_path / "posttest"
    with pytest.raises(duckdb.Error):
        run_performance_dd5(request.database, imported.import_id, output, AlgorithmConfig.defaults())

    assert not (output / "posttest.xlsx").exists()
    assert not (output / "posttest_manifest.json").exists()


def test_dd5_retry_uses_skipped_import_file_and_regenerates_from_stored_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    first = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )
    import mrs3.performance_dd5 as performance_dd5
    monkeypatch.setattr(
        performance_dd5,
        "write_posttest_outputs",
        lambda *_: (_ for _ in ()).throw(OSError("export failed")),
    )
    with pytest.raises(OSError, match="export failed"):
        run_performance_dd5(
            request.database, first.import_id, tmp_path / "failed", AlgorithmConfig.defaults()
        )
    retry = import_performance_batch(request)
    assert retry.skipped_count == 1
    monkeypatch.undo()

    artifacts = run_performance_dd5(
        request.database, retry.import_id, tmp_path / "retry", AlgorithmConfig.defaults()
    )

    assert artifacts.manifest_json["dd5_run_id"] == artifacts.dd5_run_id
    assert artifacts.workbook.exists()
