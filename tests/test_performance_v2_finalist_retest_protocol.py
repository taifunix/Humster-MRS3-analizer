"""Protocol v2 render of the finalist RETEST (protocol v2 §7a)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path

import duckdb
import pytest

from mrs3.evaluation_protocol import EvaluationProtocolError, EvaluationTarget
from mrs3.performance_v2_finalist_retest import build_finalist_retest_manifest, finalist_retest_config_digest
from mrs3.performance_v2_store import initialize_performance_v2
from tests._protocol_v2 import protocol_v2_template

TARGET = EvaluationTarget(Decimal("10000"), "BingX")


def _build(tmp_path: Path, template: dict[str, object]):
    with duckdb.connect(":memory:") as connection:
        initialize_performance_v2(connection)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        connection.execute(
            "insert into strategies (strategy_id, strategy_name, symbol, side, timeframe, close_ma_len, order_count, analysis_run_id, candidate_identity, lifecycle_status, current_result_id, created_at_utc, updated_at_utc) values (1, 'final', 'BTCUSDT', 'LONG', '1h', 5, 1, 'run', 'candidate', 'ACTIVE', 11, ?, ?)",
            [now, now],
        )
        connection.execute(
            "insert into strategy_results (result_id, strategy_id, report_start_utc, report_end_utc, exchange, commission_rate, initial_balance, final_balance, imported_at_utc) values (11, 1, '2026-01-01', '2026-06-01', 'Bybit', .0004, 100, 101, ?)",
            [now],
        )
        connection.execute("insert into analysis_plateaus values ('run', 'P1', 4, 80)")
        connection.execute(
            "insert into strategy_orders (strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x, analysis_run_id, plateau_id, base_point_trades) values (1, 1, 7, .995, 50, 1, 'run', 'P1', 20)"
        )
        connection.execute(
            """insert into selection_runs
            (selection_run_id, database_instance_id, symbol, side, selection_contract_version,
             request_json, request_sha256, config_json, config_sha256, candidate_count,
             representative_count, auto_finalist_count, top_n, workbook_sha256, created_at_utc)
            values ('selection-1', (select value from schema_info where key='database_instance_id'), 'BTCUSDT', 'LONG', 'v1', '{}', ?, '{}', ?, 1, 1, 1, 20, ?, ?)""",
            ["a" * 64, "b" * 64, "c" * 64, now],
        )
        connection.execute(
            "insert into selection_results (selection_run_id, strategy_id, result_id_at_selection, auto_status, auto_rank, prior_rejected, stage_trace_json) values ('selection-1', 1, 11, 'FINALIST', 1, false, '{}')"
        )
        connection.execute(
            """insert into selection_review_imports
               (review_import_id, selection_run_id, workbook_sha256, imported_at_utc, row_count)
               values ('review-1', 'selection-1', ?, ?, 1)""",
            ["d" * 64, now],
        )
        connection.execute(
            """insert into selection_review_rows values ('review-1', 1, 'FINALIST', 1, null, 'keep')"""
        )
        return build_finalist_retest_manifest(
            connection,
            {"LONG": template},
            tmp_path / "output",
            test_start="2026-01-01",
            test_end="2026-06-01",
            listing_dates={"BTCUSDT": date(2025, 12, 1)},
            job_id="bulk-1",
            evaluation=TARGET,
        )


def test_finalist_retest_renders_the_base_and_exchange(tmp_path: Path) -> None:
    template = protocol_v2_template()
    batch = _build(tmp_path, template)

    # The batch digest is the one Panel compares to deduplicate equal requests.
    assert batch.config_sha256 == finalist_retest_config_digest({"LONG": template}, evaluation=TARGET)

    strategy = json.loads((batch.strategies_path / "final.json").read_text(encoding="utf-8"))
    assert strategy["basic"]["my_fix_balance"] == 10000.0
    assert strategy["exchange"]["name"] == "BingX"
    assert strategy["basic"]["use_fix"] is True and strategy["exchange"]["use_upnl"] is False


def test_finalist_retest_refuses_a_dynamic_template(tmp_path: Path) -> None:
    template = protocol_v2_template()
    template["basic"]["use_fix"] = False  # type: ignore[index]
    with pytest.raises(EvaluationProtocolError, match="basic.use_fix"):
        _build(tmp_path, template)


def test_finalist_retest_config_digest_follows_the_base_and_exchange() -> None:
    templates = {"LONG": protocol_v2_template()}
    digests = {
        finalist_retest_config_digest(templates),
        finalist_retest_config_digest(templates, evaluation=TARGET),
        finalist_retest_config_digest(templates, evaluation=EvaluationTarget(Decimal("5000"), "BingX")),
        finalist_retest_config_digest(templates, evaluation=EvaluationTarget(Decimal("10000"), "Bybit")),
    }
    assert len(digests) == 4
