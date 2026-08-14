from __future__ import annotations

import hashlib
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd

import duckdb
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.performance_dd5 import regenerate_performance_dd5, run_performance_dd5
from mrs3.performance_import import import_performance_batch
from tests.test_performance_import import (
    _canonical,
    _legacy_rounded_report,
    _replace_report,
    _request,
    _request_with_entries,
)


def _legacy_lots(value: object) -> list[Decimal]:
    found: list[Decimal] = []
    if isinstance(value, dict):
        if 'lot_x' in value:
            found.append(Decimal(str(value['lot_x'])))
        for item in value.values():
            found.extend(_legacy_lots(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_legacy_lots(item))
    return found


def _legacy_settings(aux_lots: list[object]) -> dict[str, object]:
    return {
        'name': 'MRS3 Demo',
        'basic': {'use_long': True, 'symbol': 'ONUSDT', 'side': 'LONG', 'time_frame': '1h'},
        'mrs3': {
            'ma_close_long': {'len': 2, 'lot_x': 1.0, 'multiplier': 1.003},
            'ma_close_short': {'len': 7, 'lot_x': 1.0, 'multiplier': 0.997},
            'ma_long': [
                {'lot_x': 0.4, 'multiplier': 0.996},
                {'lot_x': 0.6, 'multiplier': 0.985},
            ],
            'ma_short': [
                {'lot_x': aux_lots[0], 'multiplier': 1.015},
                {'lot_x': aux_lots[1], 'multiplier': 1.025},
                {'lot_x': aux_lots[2], 'multiplier': 1.0},
            ],
        },
    }


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


def test_dd5_persists_typed_identity_and_full_precision_lot_vector(tmp_path: Path) -> None:
    request = _request(tmp_path)
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": "0.333333333333"}, {"lot_x": "0.666666666667"}]}})],
        )

    artifacts = run_performance_dd5(
        request.database,
        imported.import_id,
        tmp_path / "posttest",
        AlgorithmConfig.defaults(),
    )

    with duckdb.connect(str(request.database), read_only=True) as connection:
        test_run_id, raw_json = connection.execute(
            "select test_run_id, raw_json from dd5_results where dd5_run_id = ?",
            [artifacts.dd5_run_id],
        ).fetchone()
    raw = json.loads(raw_json)
    assert raw["test_run_id"] == test_run_id
    assert raw["lots"] == ["0.333333333333", "0.666666666667"]


def test_dd5_preserves_unavailable_profit_factor_without_excluding_candidate(tmp_path: Path) -> None:
    request = _request(tmp_path)
    report = (request.inbox / "reports" / "entry-1.html").read_bytes().replace(
        b"<td>Profit Factor</td><td>2.5</td>",
        b"<td>Profit Factor</td><td>n/a</td>",
    )
    _replace_report(request, report)
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )

    artifacts = run_performance_dd5(
        request.database, imported.import_id, tmp_path / "posttest", AlgorithmConfig.defaults()
    )

    assert artifacts.manifest_json["raw_result_count"] == 1
    assert artifacts.manifest_json["profit_factor_unavailable_count"] == 1
    with duckdb.connect(str(request.database), read_only=True) as connection:
        raw_json = connection.execute(
            "select raw_json from dd5_results where dd5_run_id = ?", [artifacts.dd5_run_id]
        ).fetchone()[0]
    assert json.loads(raw_json)["profit_factor"] is None
    assert json.loads(raw_json)["profit_factor_status"] == "UNDEFINED_GROSS_LOSS_ZERO"


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


def test_dd5_restores_legacy_posttest_inputs_and_regenerates_without_raw_json(
    tmp_path: Path,
) -> None:
    request = _request_with_entries(tmp_path, count=3)
    for report_path in (request.inbox / 'reports').glob('entry-*.html'):
        report = report_path.read_bytes()
        report = report.replace(b'<th>Post Side</th>', b'<th>Post Side</th><th>Post Size</th><th>Side</th>')
        report = report.replace(
            b'<td>ONUSDT</td><td>opened</td><td>0</td><td>long</td>',
            b'<td>ONUSDT</td><td>opened</td><td>0</td><td>long</td><td>1</td><td>buy</td>',
        )
        report = report.replace(
            b'<td>ONUSDT</td><td>closed</td><td>12.5</td><td></td>',
            b'<td>ONUSDT</td><td>closed</td><td>12.5</td><td></td><td>0</td><td>sell</td>',
        )
        report_path.write_bytes(report)
    manifest_path = request.inbox / 'inbox_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for entry in manifest['entries']:
        entry['source_report_sha256'] = hashlib.sha256(
            (request.inbox / entry['report_path']).read_bytes()
        ).hexdigest()
    manifest_path.write_bytes(_canonical(manifest))

    imported = import_performance_batch(request)
    settings = {
        'name': 'MRS3 Demo',
        'basic': {'use_long': True, 'symbol': 'ONUSDT', 'side': 'LONG', 'time_frame': '1h'},
        'mrs3': {
            'ma_long': [
                {'lot_x': '0.333333333333', 'multiplier': '0.997'},
                {'lot_x': '0.666666666667', 'multiplier': '0.989'},
            ],
            'ma_close_long': {'len': 2},
        },
    }
    with duckdb.connect(str(request.database)) as connection:
        connection.execute('update strategy_versions set settings_json = ?', [json.dumps(settings)])

    original = run_performance_dd5(
        request.database, imported.import_id, tmp_path / 'original', AlgorithmConfig.defaults()
    )
    comparison = pd.read_excel(original.workbook, sheet_name='18_Final_Comparison')
    summary = pd.read_excel(original.workbook, sheet_name='00_Selection_Summary')
    cycles = pd.read_excel(original.workbook, sheet_name='19_Position_Holding_Cycles')

    assert {'symbol', 'side', 'timeframe'} <= set(comparison.columns)
    assert comparison['symbol'].eq('ONUSDT').all()
    assert comparison['side'].eq('LONG').all()
    assert comparison['timeframe'].eq('1h').all()
    assert comparison['selection_holding_limit'].notna().all()
    assert comparison['selection_trades_floor'].notna().all()
    assert not comparison['selection_reason'].eq('SELECTION_INPUT_MISSING').any()
    assert comparison['shift_bp_vector'].eq('30 / 110').all()
    assert comparison['lots'].eq(json.dumps(['0.33', '0.67'])).all()
    assert comparison['scaled_lots'].eq(json.dumps(['3.33', '6.67'])).all()
    assert comparison['full_position_cycle_count'].eq(1).all()
    assert len(cycles) == 3
    assert summary.loc[0, ['symbol', 'side', 'timeframe']].tolist() == ['ONUSDT', 'LONG', '1h']
    assert summary.loc[0, 'holding_p95_limit'] == 60.0
    assert summary.loc[0, 'trades_floor'] == 2.0
    assert summary.loc[0, 'final'] == 3

    with duckdb.connect(str(request.database)) as connection:
        connection.execute('update dd5_results set raw_json = ?', [json.dumps({'test_run_id': 'stale'})])
    regenerated = regenerate_performance_dd5(request.database, original.dd5_run_id, tmp_path / 'regenerated')
    regenerated_comparison = pd.read_excel(regenerated.workbook, sheet_name='18_Final_Comparison')

    pd.testing.assert_frame_equal(comparison, regenerated_comparison)


def test_dd5_artifacts_can_be_regenerated_from_stored_run(tmp_path: Path) -> None:
    request = _request(tmp_path)
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )
    original = run_performance_dd5(
        request.database, imported.import_id, tmp_path / "first", AlgorithmConfig.defaults()
    )
    regenerated = regenerate_performance_dd5(
        request.database, original.dd5_run_id, tmp_path / "regenerated"
    )
    assert regenerated.dd5_run_id == original.dd5_run_id
    assert regenerated.workbook.exists()
    with duckdb.connect(str(request.database), read_only=True) as connection:
        config_json = connection.execute(
            "select config_json from dd5_runs where dd5_run_id = ?", [original.dd5_run_id]
        ).fetchone()[0]
    assert set(json.loads(config_json)) == {field.name for field in __import__("dataclasses").fields(AlgorithmConfig)}


def test_dd5_rejects_stored_metric_mismatch_beyond_declared_precision(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        encoded = connection.execute(
            "select metrics_json from backtest_metrics"
        ).fetchone()[0]
        metrics_json = json.loads(encoded)
        metrics_json["Total PnL, %"] = "86.257309941520"
        metrics_json["Max Drawdown, %"] = "4.999999999999"
        connection.execute(
            "update backtest_metrics set metrics_json = ?",
            [json.dumps(metrics_json)],
        )
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )

    output = tmp_path / "posttest"
    with pytest.raises(ValueError, match="invalid precise backtest metrics"):
        run_performance_dd5(
            request.database,
            imported.import_id,
            output,
            AlgorithmConfig.defaults(),
        )

    assert not (output / "posttest.xlsx").exists()


def test_dd5_invalid_stored_side_fails_closed_and_blank_falls_back() -> None:
    import mrs3.performance_dd5 as performance_dd5

    with pytest.raises(ValueError, match="unsupported value"):
        performance_dd5._settings_side({"basic": {"use_long": True}}, "BAD")

    assert (
        performance_dd5._settings_side(
            {"basic": {"use_long": True, "side": "LONG"}}, ""
        )
        == "LONG"
    )


def test_dd5_derives_exact_legacy_metrics_from_stored_equity_and_counts(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _replace_report(request, _legacy_rounded_report())
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            "update backtest_metrics set final_balance=?, total_pnl=?, total_pnl_pct=?, max_drawdown=?, max_drawdown_pct=?, win_rate_pct=?",
            [
                Decimal("1439.53"),
                Decimal("439.53"),
                Decimal("43.95"),
                Decimal("74.44"),
                Decimal("5.80"),
                Decimal("76.45"),
            ],
        )
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )

    artifacts = run_performance_dd5(
        request.database,
        imported.import_id,
        tmp_path / "posttest",
        AlgorithmConfig.defaults(),
    )

    with duckdb.connect(str(request.database), read_only=True) as connection:
        dd5_run_id, raw_json = connection.execute(
            "select dd5_run_id, raw_json from dd5_results where dd5_run_id = ?",
            [artifacts.dd5_run_id],
        ).fetchone()
        projected = connection.execute(
            "select projected_pnl_dd5, projected_dd_pct, projected_pnl30_dd5, scaled_lots_json, capital_requirement_proxy from dd5_results where dd5_run_id = ?",
            [dd5_run_id],
        ).fetchone()
    raw = json.loads(raw_json)
    exact_dd = Decimal("74.44477626") / Decimal("1282.77898396") * 100
    exact_win_rate = Decimal("237") / Decimal("310") * 100
    assert Decimal(raw["pnl_pct"]) == Decimal("43.9532329415")
    assert Decimal(raw["dd_pct"]) == exact_dd
    assert Decimal(raw["win_rate_pct"]) == exact_win_rate

    config = AlgorithmConfig.defaults()
    scale = config.target_dd_pct / exact_dd
    expected_pnl = Decimal("43.9532329415") * scale
    expected_pnl30 = expected_pnl * Decimal("30") / Decimal("1")
    precision = Decimal("0.000000000001")
    assert Decimal(str(projected[0])) == expected_pnl.quantize(precision)
    assert Decimal(str(projected[1])) == Decimal("5")
    assert Decimal(str(projected[2])) == expected_pnl30.quantize(precision)
    assert Decimal(json.loads(projected[3])[0]) == scale
    assert Decimal(str(projected[4])) == (scale + Decimal("0.05")).quantize(precision)

    comparison_csv = pd.read_csv(
        artifacts.csv_directory / "18_Final_Comparison.csv",
        dtype=str,
    )
    assert Decimal(comparison_csv.loc[0, "pnl_pct"]) == Decimal("43.9532329415")
    assert Decimal(comparison_csv.loc[0, "dd_pct"]) == exact_dd
    assert Decimal(comparison_csv.loc[0, "win_rate_pct"]) == exact_win_rate


def test_dd5_rejects_missing_declared_metrics_diagnostics(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    imported = import_performance_batch(request)
    with duckdb.connect(str(request.database)) as connection:
        connection.execute("update backtest_metrics set metrics_json = '{}'")
        connection.execute(
            "update strategy_versions set settings_json = ?",
            [json.dumps({"mrs3": {"ma_long": [{"lot_x": 1}]}})],
        )

    output = tmp_path / "posttest"
    with pytest.raises(ValueError, match="missing declared metrics diagnostics"):
        run_performance_dd5(
            request.database,
            imported.import_id,
            output,
            AlgorithmConfig.defaults(),
        )

    assert not (output / "posttest.xlsx").exists()


def test_dd5_uses_active_order_lots_and_restores_scope(tmp_path: Path) -> None:
    request = _request_with_entries(tmp_path, count=3)
    imported = import_performance_batch(request)
    aux_vectors = [
        ['0.5', '0.5', '1.0'],
        ['0.8', '0.4', '1.0'],
        ['0.3', '0.6', '1.0'],
    ]
    with duckdb.connect(str(request.database)) as connection:
        names = [row[0] for row in connection.execute('select strategy_name from strategy_versions order by strategy_name').fetchall()]
        for index, name in enumerate(names):
            settings = _legacy_settings(aux_vectors[index])
            connection.execute(
                'update strategy_versions set settings_json = ?, symbol = ?, side = ?, timeframe = ? where strategy_name = ?',
                [json.dumps(settings), '', '', '', name],
            )
    with duckdb.connect(str(request.database)) as connection:
        runs_before = connection.execute('select count(*) from dd5_runs').fetchone()[0]

    artifacts = run_performance_dd5(
        request.database,
        imported.import_id,
        tmp_path / 'original',
        AlgorithmConfig.defaults(),
    )

    with duckdb.connect(str(request.database)) as connection:
        runs_after = connection.execute('select count(*) from dd5_runs').fetchone()[0]
        persisted_lots = [
            json.loads(row[0])['lots']
            for row in connection.execute(
                'select raw_json from dd5_results where dd5_run_id = ? order by test_run_id',
                [artifacts.dd5_run_id],
            ).fetchall()
        ]
    comparison = pd.read_excel(artifacts.workbook, sheet_name='18_Final_Comparison')

    assert runs_after == runs_before + 1
    assert comparison['symbol'].eq('ONUSDT').all()
    assert comparison['side'].eq('LONG').all()
    assert comparison['timeframe'].eq('1h').all()
    assert comparison['lots'].eq(json.dumps(['0.40', '0.60'])).all()
    assert comparison['scaled_lots'].eq(json.dumps(['4.00', '6.00'])).all()
    assert comparison['capital_requirement_proxy'].round(2).eq(10.05).all()
    assert persisted_lots == [['0.4', '0.6']] * len(names)
    assert not comparison['selection_reason'].eq('SELECTION_INPUT_MISSING').any()

    with duckdb.connect(str(request.database)) as connection:
        connection.execute(
            'update dd5_results set raw_json = ? where dd5_run_id = ?',
            [json.dumps({'test_run_id': 'stale'}), artifacts.dd5_run_id],
        )
    regenerated = regenerate_performance_dd5(
        request.database, artifacts.dd5_run_id, tmp_path / 'regenerated'
    )
    regenerated_comparison = pd.read_excel(regenerated.workbook, sheet_name='18_Final_Comparison')

    pd.testing.assert_frame_equal(comparison, regenerated_comparison)


def test_dd5_legacy_invalid_persisted_run_is_not_falsely_reexported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request_with_entries(tmp_path, count=3)
    imported = import_performance_batch(request)
    aux_vectors = [
        ['0.5', '0.5', '1.0'],
        ['0.8', '0.4', '1.0'],
        ['0.3', '0.6', '1.0'],
    ]
    with duckdb.connect(str(request.database)) as connection:
        names = [row[0] for row in connection.execute('select strategy_name from strategy_versions order by strategy_name').fetchall()]
        for index, name in enumerate(names):
            settings = _legacy_settings(aux_vectors[index])
            connection.execute(
                'update strategy_versions set settings_json = ? where strategy_name = ?',
                [json.dumps(settings), name],
            )

    import mrs3.performance_dd5 as performance_dd5
    original_metadata = performance_dd5._settings_metadata

    def old_metadata(settings: object, side: str) -> dict[str, object]:
        metadata = dict(original_metadata(settings, side))
        metadata['lots'] = _legacy_lots(settings)
        return metadata

    monkeypatch.setattr(performance_dd5, '_settings_metadata', old_metadata)
    old = run_performance_dd5(
        request.database, imported.import_id, tmp_path / 'old', AlgorithmConfig.defaults()
    )
    monkeypatch.undo()

    with duckdb.connect(str(request.database)) as connection:
        count_before = connection.execute(
            'select count(*) from dd5_results where dd5_run_id = ?', [old.dd5_run_id]
        ).fetchone()[0]
        raw_before = connection.execute(
            'select raw_json from dd5_results where dd5_run_id = ? order by test_run_id',
            [old.dd5_run_id],
        ).fetchall()
    with pytest.raises(ValueError, match='DD5 persisted result readback failed'):
        regenerate_performance_dd5(request.database, old.dd5_run_id, tmp_path / 'should-not-exist')

    with duckdb.connect(str(request.database)) as connection:
        count_after = connection.execute(
            'select count(*) from dd5_results where dd5_run_id = ?', [old.dd5_run_id]
        ).fetchone()[0]
        raw_after = connection.execute(
            'select raw_json from dd5_results where dd5_run_id = ? order by test_run_id',
            [old.dd5_run_id],
        ).fetchall()
    assert count_before == count_after
    assert raw_before == raw_after
    assert not (tmp_path / 'should-not-exist' / 'posttest.xlsx').exists()
