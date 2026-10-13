"""Smoke test for the protocol v2 evidence scripts E1-E6 (scripts/equity_research)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from mrs3.performance_v2_store import initialize_performance_v2, load_performance_v2_config, performance_v2_database_path

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts" / "equity_research"
UTC = timezone.utc


def _database(root: Path) -> None:
    """One pool (BTCUSDT LONG) of ten DYNAMIC strategies, three two-level cycles each."""
    (root / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "performance-v2"}}), encoding="utf-8",
    )
    target = performance_v2_database_path(load_performance_v2_config(root / "config.performance.json"))
    target.parent.mkdir(parents=True)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with duckdb.connect(str(target)) as connection:
        initialize_performance_v2(connection)
        connection.execute("insert into analysis_plateaus values ('run', 'P1', 12, 34)")
        for number in range(1, 11):
            strategy_id = connection.execute(
                """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len, order_count,
                   analysis_run_id, candidate_identity, lifecycle_status, created_at_utc, updated_at_utc)
                   values (?, 'BTCUSDT', 'LONG', '1h', 3, 2, 'run', ?, 'ACTIVE', ?, ?) returning strategy_id""",
                [f"s{number}", f"c{number}", start, start],
            ).fetchone()[0]
            result_id = connection.execute(
                """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
                   commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct, max_drawdown,
                   max_drawdown_pct, total_fees, total_trades, imported_at_utc, sizing_use_upnl)
                   values (?, ?, ?, 'Bybit', .0004, 1000, 1000, 0, 0, 0, 0, 0, 3, ?, true) returning result_id""",
                [strategy_id, start, start + timedelta(days=40), start],
            ).fetchone()[0]
            connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
            connection.executemany(
                """insert into strategy_orders (strategy_id, order_id, open_ma_len, open_multiplier, shift_bp, lot_x,
                   analysis_run_id, plateau_id, base_point_trades) values (?, ?, 7, .99, ?, .5, 'run', 'P1', 8)""",
                [(strategy_id, 1, 100), (strategy_id, 2, 200)],
            )
            balance = Decimal("1000")
            rows = []
            for cycle in range(3):
                opened = start + timedelta(days=12 * cycle + 1)
                pnl = Decimal(number + cycle) - Decimal("4")
                rows += [
                    (result_id, 4 * cycle, opened, "opened", Decimal("5"), Decimal("5"), "long", 0, Decimal("0.1"), balance - Decimal("0.1"), 100, 500),
                    (result_id, 4 * cycle + 1, opened + timedelta(hours=6), "increased", Decimal("5.05"), Decimal("10.05"), "long", 0, Decimal("0.1"), balance - Decimal("0.2"), 99, Decimal("499.95")),
                    (result_id, 4 * cycle + 2, opened + timedelta(days=1), "decreased", Decimal("5"), Decimal("5.05"), "long", pnl / 2, Decimal("0.1"), balance + pnl / 2 - Decimal("0.3"), 101, 505),
                    (result_id, 4 * cycle + 3, opened + timedelta(days=2), "closed", Decimal("5.05"), Decimal("0"), "", pnl / 2, Decimal("0.1"), balance + pnl - Decimal("0.4"), 101, Decimal("510.05")),
                ]
                balance += pnl - Decimal("0.4")
            connection.executemany(
                """insert into strategy_actions (result_id, action_index, timestamp_utc, symbol, order_id, action, size,
                   post_size, post_side, pnl, fee, balance, price, cost, raw_action_json)
                   values (?, ?, ?, 'BTCUSDT', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)""",
                rows,
            )


@pytest.mark.parametrize(("script", "markers"), [
    ("sizing_evidence.py", ("[E1]", "[E2]", "[E3]", "[E4]", "[E5]", "done")),
    ("sizing_fills.py", ("[E4]", "[E5]", "done")),
    ("base_size.py", ("base 10000:", "symbols 1")),
])
def test_sizing_evidence_script_runs_read_only_on_a_small_database(tmp_path: Path, script: str, markers: tuple[str, ...]) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _database(root)
    target = performance_v2_database_path(load_performance_v2_config(root / "config.performance.json"))
    before = target.read_bytes()
    spill = tmp_path / "spill"
    spill.mkdir()
    arguments = [str(root)] if script == "base_size.py" else [str(root), str(spill)]

    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *arguments],
        capture_output=True, text=True, encoding="utf-8", timeout=300,
        env={**os.environ, "PYTHONPATH": str(REPOSITORY / "src"), "PYTHONIOENCODING": "utf-8"},
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    for marker in markers:
        assert marker in completed.stdout, (marker, completed.stdout[-2000:])
    assert target.read_bytes() == before
