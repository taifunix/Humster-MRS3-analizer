from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

import duckdb
import pandas as pd

from mrs3 import panel_performance_v2 as panel_module
from mrs3.panel_performance_v2 import PerformanceV2ExportSelection, export_performance_v2
from mrs3.performance_v2_equity_regime_cache import equity_regime_source_revision
from mrs3.performance_v2_equity_cache import current_equity_source_metadata
from mrs3.performance_v2_equity_regime import ALGORITHM_VERSION as EQUITY_REGIME_ALGORITHM_VERSION
from mrs3.performance_v2_store import initialize_performance_v2


UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _add_strategy(connection: duckdb.DuckDBPyConnection, name: str, symbol: str, side: str) -> tuple[int, int]:
    strategy_id = connection.execute(
        """insert into strategies (
               strategy_name, symbol, side, timeframe, close_ma_len, order_count,
               analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc
           ) values (?, ?, ?, '1h', 3, 1, ?, ?, 'ACTIVE', ?, ?)
           returning strategy_id""",
        [name, symbol, side, name, name, NOW, NOW],
    ).fetchone()[0]
    connection.execute(
        "insert into analysis_plateaus (analysis_run_id, plateau_id, plateau_point_count, plateau_total_trades) values (?, 'P1', 1, 1)",
        [name],
    )
    connection.execute(
        """insert into strategy_orders (
               strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
               analysis_run_id, plateau_id, base_point_trades
           ) values (?, 1, 7, .995, 125, 1, ?, 'P1', 1)""",
        [strategy_id, name],
    )
    result_id = connection.execute(
        """insert into strategy_results (
               strategy_id, report_start_utc, report_end_utc, exchange, commission_rate,
               initial_balance, final_balance, total_pnl, total_pnl_pct, max_drawdown,
               max_drawdown_pct, total_fees, total_trades, imported_at_utc
           ) values (?, ?, ?, 'Bybit', .0004, 100, 101, 1, 1, 0, 0, 0, 1, ?)
           returning result_id""",
        [strategy_id, NOW, datetime(2026, 1, 9, tzinfo=UTC), NOW],
    ).fetchone()[0]
    connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
    return int(strategy_id), int(result_id)


def _add_run(
    connection: duckdb.DuckDBPyConnection, run_id: str, symbol: str, side: str,
    created_at: datetime, request_document: dict[str, object] | None = None,
) -> None:
    database_id = connection.execute(
        "select value from schema_info where key = 'database_instance_id'"
    ).fetchone()[0]
    request_json = json.dumps(request_document or {}, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """insert into selection_runs (
               selection_run_id, database_instance_id, symbol, side, selection_contract_version,
               request_json, request_sha256, config_json, config_sha256, candidate_count,
               representative_count, auto_finalist_count, top_n, workbook_sha256, created_at_utc
           ) values (?, ?, ?, ?, 'selection-v1', ?, ?, '{}', ?, 1, 1, 1, 1, ?, ?)""",
        [run_id, database_id, symbol, side, request_json, sha256(request_json.encode()).hexdigest(),
         "b" * 64, "c" * 64, created_at],
    )


def _equity_regime_source_snapshot(
    connection: duckdb.DuckDBPyConnection, sources: dict[int, int],
) -> dict[str, object]:
    return {
        "algorithm_version": EQUITY_REGIME_ALGORITHM_VERSION,
        "sources": {
            str(strategy_id): {
                "result_id": result_id,
                "source_revision": equity_regime_source_revision(
                    current_equity_source_metadata(connection, result_id)
                ),
            }
            for strategy_id, result_id in sources.items()
        },
    }


def _add_selection(
    connection: duckdb.DuckDBPyConnection,
    run_id: str,
    strategy_id: int,
    result_id: int,
    status: str,
    assessment_json: str | None = None,
) -> None:
    connection.execute(
        """insert into selection_results (
               selection_run_id, strategy_id, result_id_at_selection, auto_status, auto_score,
               auto_rank, auto_reason, analog_group_key, auto_analog_of_strategy_id,
               prior_rejected, stage_trace_json, equity_regime_json
           ) values (?, ?, ?, ?, null, null, null, null, null, false, '{}', ?)""",
        [run_id, strategy_id, result_id, status, assessment_json],
    )


def _export_database(
    path: Path, *, publish_current: bool = True, latest_current_run_null: bool = False,
) -> tuple[Path, dict[str, int]]:
    with duckdb.connect(str(path)) as connection:
        initialize_performance_v2(connection)
        first_id, first_result = _add_strategy(connection, "btc-current", "BTCUSDT", "LONG")
        second_id, second_current = _add_strategy(connection, "btc-rank-only", "BTCUSDT", "LONG")
        third_id, third_result = _add_strategy(connection, "eth-rejected", "ETHUSDT", "SHORT")
        first_stale = first_result + 1000
        second_stale = second_current + 1000
        current_json = json.dumps({"state": "GROWING", "decision": "PASS", "rank": "GROWING", "reasons": []})
        stale_json = json.dumps({"state": "DROP", "decision": "DROP", "rank": None, "reasons": ["W28_DOWN"]})

        _add_run(connection, "btc-old", "BTCUSDT", "LONG", datetime(2026, 1, 2, tzinfo=UTC))
        _add_selection(connection, "btc-old", first_id, first_stale, "FINALIST", stale_json)
        _add_selection(connection, "btc-old", second_id, second_stale, "FINALIST", stale_json)

        _add_run(
            connection, "btc-published", "BTCUSDT", "LONG", datetime(2026, 1, 3, tzinfo=UTC),
            {"equity_regime_snapshot": _equity_regime_source_snapshot(
                connection, {first_id: first_result, second_id: second_current},
            )},
        )
        _add_selection(
            connection, "btc-published", first_id, first_result, "FINALIST",
            current_json if publish_current else None,
        )
        _add_selection(connection, "btc-published", second_id, second_current, "FILTERED")

        if latest_current_run_null:
            _add_run(connection, "btc-latest", "BTCUSDT", "LONG", datetime(2026, 1, 4, tzinfo=UTC))
            _add_selection(connection, "btc-latest", first_id, first_result, "FILTERED")
            _add_selection(connection, "btc-latest", second_id, second_current, "FILTERED")

        _add_run(connection, "eth-current", "ETHUSDT", "SHORT", datetime(2026, 1, 5, tzinfo=UTC))
        _add_selection(connection, "eth-current", third_id, third_result, "FILTERED")
        connection.execute(
            """insert into strategy_rejection_sources (
                   strategy_id, source_kind, reason_code, first_result_id, first_selection_run_id,
                   classifier_algo_version, source_revision, facts_sha256, created_at_utc
               ) values (?, 'EQUITY_REGIME_FILTER', 'W28_DOWN', ?, 'eth-current', 'equity-regime-v1', 'rev', ?, ?)""",
            [third_id, third_result, "d" * 64, NOW],
        )
    return path, {"current": first_id, "rank_only": second_id, "rejected": third_id}


def _capture_export(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def capture_writer(result: pd.DataFrame, path: Path, request, metadata, user_reviews):
        captured["result"] = result.copy()
        captured["user_reviews"] = {key: dict(value) for key, value in user_reviews.items()}
        path.write_bytes(b"workbook")
        return path

    monkeypatch.setattr(panel_module, "write_selection_workbook", capture_writer)
    monkeypatch.setattr(panel_module, "_export_cached_candidates", lambda *args: {})
    return captured


def test_export_passes_only_current_published_assessment_and_effective_user_status(
    tmp_path: Path, monkeypatch,
) -> None:
    database, strategies = _export_database(tmp_path / "performance.duckdb")
    captured = _capture_export(monkeypatch)
    before = (sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns)

    export_performance_v2(database, PerformanceV2ExportSelection(all_active=True))

    after = (sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns)
    result = captured["result"]
    assert isinstance(result, pd.DataFrame)
    assert set(result["strategy_id"]) == set(strategies.values())
    by_strategy = result.set_index("strategy_id")
    assert by_strategy.at[strategies["current"], "equity_regime_json"] == json.dumps(
        {"state": "GROWING", "decision": "PASS", "rank": "GROWING", "reasons": []}
    )
    assert pd.isna(by_strategy.at[strategies["rank_only"], "equity_regime_json"])
    user_reviews = captured["user_reviews"]
    assert user_reviews[strategies["rejected"]]["user_status"] == "REJECTED"
    assert strategies["rank_only"] not in user_reviews or user_reviews[strategies["rank_only"]].get("user_status") is None
    assert before == after


def test_export_without_published_assessments_keeps_legacy_candidate_columns(tmp_path: Path, monkeypatch) -> None:
    database, _ = _export_database(tmp_path / "performance.duckdb", publish_current=False)
    captured = _capture_export(monkeypatch)

    export_performance_v2(database, PerformanceV2ExportSelection(all_active=True))

    result = captured["result"]
    assert isinstance(result, pd.DataFrame)
    assert "equity_regime_json" not in result.columns


def test_export_newer_null_publication_suppresses_an_older_assessment(tmp_path: Path, monkeypatch) -> None:
    database, strategies = _export_database(
        tmp_path / "performance.duckdb", latest_current_run_null=True,
    )
    captured = _capture_export(monkeypatch)

    export_performance_v2(database, PerformanceV2ExportSelection(all_active=True))

    result = captured["result"]
    assert isinstance(result, pd.DataFrame)
    assert "equity_regime_json" not in result.columns
    assert strategies["current"] in set(result["strategy_id"])


def test_export_omits_assessment_after_same_result_source_revision_changes(
    tmp_path: Path, monkeypatch,
) -> None:
    database, strategies = _export_database(tmp_path / "performance.duckdb")
    with duckdb.connect(str(database)) as connection:
        result_id = connection.execute(
            "select current_result_id from strategies where strategy_id = ?", [strategies["current"]],
        ).fetchone()[0]
        connection.execute(
            "update strategy_results set report_end_utc = report_end_utc + interval '1 day' "
            "where result_id = ?", [result_id],
        )
    captured = _capture_export(monkeypatch)

    export_performance_v2(database, PerformanceV2ExportSelection(all_active=True))

    result = captured["result"]
    assert isinstance(result, pd.DataFrame)
    assert "equity_regime_json" not in result.columns
