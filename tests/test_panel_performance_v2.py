from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from http.client import HTTPConnection
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from datetime import datetime, timedelta, timezone

import duckdb
from openpyxl import Workbook
import pytest

from mrs3.performance_v2_store import (
    PerformanceV2Config,
    initialize_performance_v2,
    performance_v2_database_path,
)
from mrs3.panel import PanelController, create_panel_server
from mrs3.panel_performance_v2 import (
    PerformanceV2ApiError,
    PerformanceV2PanelRequest,
    LocalPerformanceV2Service,
    _normalization_30d,
    _window_document,
    calculate_performance_v2_windows,
    performance_v2_catalog,
)
from mrs3.performance_v2_windows import WindowMetrics


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "report_current_v2.html"
UTC = timezone.utc


def _metrics_for_normalization(
    start: datetime,
    end: datetime | None,
    *,
    growth: object = Decimal("1.1"),
    trade_count: object = 5,
) -> WindowMetrics:
    return WindowMetrics(
        1, start, end or start, "test", start, end, "AVAILABLE", None,
        growth, Decimal("1.2345"), Decimal(".01"), Decimal("1.02"), Decimal("3.4"),
        Decimal("2.3"), Decimal(".4"), Decimal("1.5"), trade_count, Decimal("50"),
    )


@pytest.mark.parametrize(
    ("days", "expected_growth", "expected_return", "expected_trade_rate"),
    [
        (10, "1.33100000", "33.1000", "15.0000"),
        (30, "1.10000000", "10.0000", "5.0000"),
        (60, "1.04880885", "4.8809", "2.5000"),
    ],
)
def test_normalization_30d_uses_calendar_duration(days, expected_growth, expected_return, expected_trade_rate) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = _normalization_30d(_metrics_for_normalization(start, start + timedelta(days=days)))
    assert result == {
        "period_days": 30,
        "status": "ok",
        "observed_days": f"{days}.000000",
        "growth_factor": expected_growth,
        "return_pct": expected_return,
        "trade_rate": expected_trade_rate,
    }


@pytest.mark.parametrize(
    ("end", "status", "observed_days"),
    [
        (datetime(2026, 1, 1, tzinfo=UTC), "invalid_duration", None),
        (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(microseconds=86_399_999_999), "too_short", "1.000000"),
        (None, "invalid_duration", None),
    ],
)
def test_normalization_30d_status_and_observed_days(end, status, observed_days) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = _normalization_30d(_metrics_for_normalization(start, end))
    assert result["status"] == status
    assert result["observed_days"] == observed_days
    assert result["growth_factor"] is None
    assert result["return_pct"] is None
    assert result["trade_rate"] is None


@pytest.mark.parametrize("growth", [None, Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_normalization_30d_bad_growth_is_null_without_affecting_trade_rate(growth) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    result = _normalization_30d(_metrics_for_normalization(start, end, growth=growth, trade_count=3))
    assert result["growth_factor"] is None
    assert result["return_pct"] is None
    assert result["trade_rate"] == "3.0000"


def test_normalization_30d_zero_and_overflow_growth_and_bad_trade_count() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=30)
    zero = _normalization_30d(_metrics_for_normalization(start, end, growth=Decimal("0"), trade_count=0))
    assert zero["growth_factor"] == "0.00000000"
    assert zero["return_pct"] == "-100.0000"
    assert zero["trade_rate"] == "0.0000"

    overflow = _normalization_30d(_metrics_for_normalization(start, end, growth=Decimal("1e18"), trade_count=-1))
    assert overflow["growth_factor"] is None
    assert overflow["return_pct"] is None
    assert overflow["trade_rate"] is None


def test_normalization_30d_is_additive_to_window_document() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = _metrics_for_normalization(start, start + timedelta(days=30))
    document = _window_document(metrics)
    assert document["growth_factor"] == "1.1"
    assert document["return_pct"] == "1.2345"
    assert document["trade_count"] == 5
    assert document["normalization_30d"] == {
        "period_days": 30,
        "status": "ok",
        "observed_days": "30.000000",
        "growth_factor": "1.10000000",
        "return_pct": "10.0000",
        "trade_rate": "5.0000",
    }


def _db(tmp_path: Path) -> tuple[duckdb.DuckDBPyConnection, int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(tmp_path / "strategy_performance.duckdb"))
    initialize_performance_v2(connection)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
           order_count, analysis_run_id, candidate_identity, lifecycle_status,
           created_at_utc, updated_at_utc) values ('alpha', 'BTCUSDT', 'LONG', '1h',
           3, 1, 'run', 'candidate', 'ACTIVE', ?, ?) returning strategy_id""",
        [now, now],
    ).fetchone()[0]
    result_id = connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
           commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
           max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
           values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 0, 0, 2, 2, ?) returning result_id""",
        [strategy_id, now, datetime(2026, 1, 5, tzinfo=UTC), now],
    ).fetchone()[0]
    connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])
    connection.execute(
        """insert into analysis_plateaus (analysis_run_id, plateau_id, plateau_point_count, plateau_total_trades)
           values ('run', 'P1', 12, 34)"""
    )
    connection.execute(
        """insert into strategy_orders (strategy_id, order_id, open_ma_len, open_multiplier,
           shift_bp, lot_x, analysis_run_id, plateau_id, base_point_trades)
           values (?, 1, 7, 0.995, 125, 1, 'run', 'P1', 8)""",
        [strategy_id],
    )
    connection.executemany(
        "insert into strategy_actions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (result_id, 0, now, "BTCUSDT", 1, "opened", 1, 1, "long", 0, 1, 100, None),
            (result_id, 1, datetime(2026, 1, 2, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 10, 1, 110, None),
        ],
    )
    connection.executemany(
        "insert into strategy_equity values (?, ?, ?, ?, ?)",
        [
            (result_id, 0, now, 100, 100),
            (result_id, 1, datetime(2026, 1, 2, tzinfo=UTC), 110, 110),
            (result_id, 2, datetime(2026, 1, 5, tzinfo=UTC), 110, 110),
        ],
    )
    return connection, int(result_id)


def _strategy(name: str, orders: int) -> dict[str, object]:
    return {
        "name": name,
        "exchange": {"name": "Bybit", "use_upnl": True},
        "basic": {
            "strategy": "mrs3", "symbol": "ONUSDT", "time_frame": "1h",
            "use_long": True, "use_short": False,
        },
        "mrs3": {
            "ma_long": [
                {"id": order_id, "len": 6 + order_id, "multiplier": 0.995, "lot_x": 1 / orders}
                for order_id in range(1, orders + 1)
            ],
            "ma_short": [],
            "ma_close_long": {"len": 3},
            "ma_close_short": {"len": 3},
        },
    }


def _make_inbox(tmp_path: Path) -> tuple[Path, Path, dict[Path, bytes]]:
    inbox = tmp_path / "inbox"
    strategies = inbox / "strategies"
    reports = tmp_path / "tester-reports"
    strategies.mkdir(parents=True)
    reports.mkdir()
    report = FIXTURE.read_bytes()
    entries: list[dict[str, object]] = []
    diagnostics: dict[str, object] = {}
    for index, (name, orders) in enumerate((("P1", 1), ("P2", 2)), start=1):
        strategy = _strategy(name, orders)
        strategy_bytes = json.dumps(strategy, separators=(",", ":")).encode()
        (strategies / f"{name}.json").write_bytes(strategy_bytes)
        (reports / f"{name}.html").write_bytes(report)
        candidate = f"candidate-{index}"
        diagnostics[candidate] = {
            "order_count": orders,
            "orders": [
                {
                    "order_id": order_id,
                    "plateau_id": "P1" if order_id == 1 else "P2",
                    "plateau_point_count": 4,
                    "base_point_trades": 20,
                    "plateau_total_trades": 80,
                }
                for order_id in range(1, orders + 1)
            ],
        }
        entries.append({
            "manifest_entry_id": f"{index:032x}",
            "strategy_name": name,
            "strategy_version_id": sha256(
                json.dumps(strategy, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "strategy_path": str((strategies / f"{name}.json").resolve()),
            "report_path": str((reports / f"{name}.html").resolve()),
            "wizard_run_id": "run-1",
            "exchange_name": "Bybit",
            "source_strategy_sha256": sha256(strategy_bytes).hexdigest(),
            "source_report_sha256": sha256(report).hexdigest(),
        })
    manifest = {
        "schema_version": 1,
        "batch_id": "panel-v2-test",
        "expected_strategy_names": ["P1", "P2"],
        "tester_config_sha256": "t" * 64,
        "commission_contract": {
            "MakerFee": "0.0002", "TakerFee": "0.0004", "SlippagePercent": "0.01",
            "FundingRate": "0.0001", "FundingIntervalHours": "8",
        },
        "commission_contract_id": "c" * 64,
        "run_mode": "FAST",
        "test_start": "2026-01-01",
        "test_end": "2026-01-09",
        "entries": entries,
        "v6_provenance": {
            "analysis_run_id": "a" * 64,
            "generation_manifest_sha256": "g" * 64,
            "strategy_json_sha256": {f"{name}.json": entries[index]["strategy_version_id"] for index, name in enumerate(("P1", "P2"))},
            "candidate_identity_to_strategy_names": {
                "candidate-1": ["P1"], "candidate-2": ["P2"],
            },
            "candidate_diagnostics": diagnostics,
        },
    }
    manifest_path = inbox / "inbox_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return inbox, reports, {
        path.relative_to(inbox): path.read_bytes()
        for path in inbox.rglob("*") if path.is_file()
    }


def _request(tmp_path: Path) -> tuple[PerformanceV2PanelRequest, dict[Path, bytes]]:
    inbox, reports, snapshot = _make_inbox(tmp_path)
    config = PerformanceV2Config(tmp_path / "performance-v2", workers=2)
    target = performance_v2_database_path(config)
    target.parent.mkdir(parents=True)
    with duckdb.connect(str(target)) as connection:
        initialize_performance_v2(connection)
    dates = tmp_path / "Input" / "dates.xlsx"
    dates.parent.mkdir()
    workbook = Workbook()
    workbook.active.append(["ONUSDT", datetime(2025, 12, 25)])
    workbook.save(dates)
    return PerformanceV2PanelRequest(
        inbox=inbox,
        report_root=reports,
        config=config,
        listing_dates_path=Path("Input/dates.xlsx"),
    ), snapshot


def test_v2_panel_service_imports_committed_inbox_without_eager_window_calculation(tmp_path: Path) -> None:
    request, before = _request(tmp_path)
    result = LocalPerformanceV2Service().run(request)

    assert result.status == "COMMITTED"
    assert result.imported_count == 2
    assert result.skipped_count == result.rejected_count == 0
    assert result.strategy_count == 2
    assert result.order_count == 3
    assert result.plateau_count == 2
    assert result.result_count == 2
    assert result.audit_path is not None and result.audit_path.is_file()
    with duckdb.connect(str(request.config.database_root / "strategy_performance.duckdb"), read_only=True) as connection:
        assert connection.execute("select count(*) from window_metrics").fetchone() == (0,)
        assert connection.execute("select count(*) from strategies").fetchone() == (2,)
        assert connection.execute("select count(*) from strategy_orders").fetchone() == (3,)
        assert connection.execute("select count(*) from strategy_results").fetchone() == (2,)
    assert {
        path.relative_to(request.inbox): path.read_bytes()
        for path in request.inbox.rglob("*") if path.is_file()
    } == before


def test_visible_performance_card_targets_only_v2_import_job_and_status() -> None:
    panel_web = Path(__file__).parents[1] / "src" / "mrs3" / "panel_web"
    html = (panel_web / "index.html").read_text(encoding="utf-8")
    js = (panel_web / "app.js").read_text(encoding="utf-8")
    card = html.split('class="panel-card accordion panel-performance-v2"', 1)[1].split("</details>", 1)[0]
    handler = js.split("importStartV2?.addEventListener", 1)[1].split("const recoverSplitJobs", 1)[0]
    recovery = js.split("const recoverSplitJobs = async", 1)[1].split("const settingsStatus", 1)[0]
    v2_slice = js.split("const importStartV2", 1)[1].split("const settingsStatus", 1)[0]

    assert 'id="performance-import-start"' in card
    assert "delete-tested-html-v2" not in card
    assert "performance-db-refresh" not in card
    assert "performance-audit-open" not in card
    assert 'id="strategy-dd5-card-v2"' not in html
    assert "strategies.performance.v2.import" in handler
    assert "/api/v2/strategies/performance-v2/import/status" in handler
    assert "strategies.performance.import" not in handler
    assert "strategies.dd5.start" not in handler
    assert "dd5-workbook" not in handler
    assert "strategies.performance.v2.import" in recovery
    assert "/api/v2/strategies/performance-v2/import/status" in recovery
    assert "refreshPerformanceCatalog();" not in v2_slice


def test_selection_button_posts_the_current_panel_snapshot_for_xlsx() -> None:
    panel_web = Path(__file__).parents[1] / "src" / "mrs3" / "panel_web"
    js = (panel_web / "app.js").read_text(encoding="utf-8")
    handler = js.split("selectionXlsButton?.addEventListener", 1)[1].split("renderSelectionPreviewOrder", 1)[0]

    assert "/api/v2/strategies/performance-v2/selection" in handler
    assert "selectionPayload()" in handler
    assert "data-selection-stage" in js
    assert "data-selection-scope" in js
    assert "response.blob" in handler


def test_v2_panel_controller_uses_committed_tester_job_and_status_endpoint(tmp_path: Path, monkeypatch) -> None:
    import mrs3.panel as panel_module

    request, _ = _request(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps({"panel_paths": {"tester_report_dir": "wrong-reports"}}), encoding="utf-8")
    (tmp_path / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "performance-v2", "workers": 1}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(panel_module.RunnerConfig, "from_json", lambda _path: type("Runner", (), {"report_dir": request.report_root})())
    controller = PanelController(tmp_path, config_path)
    job = controller._panel_jobs.submit(
        "strategies.tester.start", {"mode": "SINGLE_MODE", "analysis_run_id": "a", "start_date": "2026-01-01", "end_date": "2026-01-09"},
        "tester-v2", ("strategies.tester",), job_id="tester-v2",
    )
    controller._panel_jobs.transition("tester-v2", "RUNNING")
    controller._panel_jobs.sync("tester-v2", {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True}, runtime={"inbox_path": str(request.inbox)})

    started = controller.panel_job_submit({
        "kind": "strategies.performance.v2.import",
        "request": {
            "tester_job_id": "tester-v2",
            "window_a": ["2026-01-01T00:00:00Z", "2026-01-09T00:00:00Z"],
            "window_b": ["2026-01-01T00:00:00Z", "2026-01-03T12:00:00Z"],
            "listing_dates_path": "Input/dates.xlsx",
        },
    })
    v2_job_id = started["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = controller.strategies_performance_v2_import_status(v2_job_id)
        if status["state"] in {"COMMITTED", "FAILED"}:
            break
        time.sleep(0.02)
    assert status["state"] == "COMMITTED"
    assert status["result"]["imported_count"] == 2
    assert "database_path" not in status["result"]
    assert "audit_path" not in status["result"]
    assert status["result"]["order_count"] == 3
    assert status["result"]["window_count"] == 0

    # The controller's new route is independently exposed by the HTTP server.
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", f"/api/v2/strategies/performance-v2/import/status?job_id={v2_job_id}")
            response = connection.getresponse()
            document = json.loads(response.read())
        finally:
            connection.close()
        assert response.status == 200
        assert document["state"] == "COMMITTED"
        assert "database_path" not in document["result"]
        assert "failure_report_path" not in document["result"]

        restarted = PanelController(tmp_path, config_path)
        persisted = restarted.strategies_performance_v2_import_status(v2_job_id)
        assert persisted["state"] == "COMMITTED"
        assert persisted["result"]["imported_count"] == 2
        assert "audit_path" not in persisted["result"]
        assert "failure_report_path" not in persisted["result"]
        assert "windows" not in persisted["result"]
    finally:
        server.shutdown()
        server.server_close()


def test_v2_panel_controller_rejects_committed_job_without_verified_inbox(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller._panel_jobs.submit(
        "strategies.tester.start", {"mode": "SINGLE_MODE"}, "tester", (), job_id="tester"
    )
    controller._panel_jobs.transition("tester", "RUNNING")
    controller._panel_jobs.sync("tester", {"state": "COMMITTED", "phase": "COMMITTED"})

    with pytest.raises(ValueError, match="committed tester inbox"):
        controller.strategies_performance_v2_import({"tester_job_id": "tester"})


def _controller_for_windows(tmp_path: Path) -> tuple[PanelController, Path, int]:
    connection, result_id = _db(tmp_path / "data")
    connection.close()
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"panel_paths": {"performance_db_root": "v1"}}), encoding="utf-8")
    (tmp_path / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "data"}}), encoding="utf-8"
    )
    return PanelController(tmp_path, config), tmp_path / "data" / "strategy_performance.duckdb", result_id


def _http_server(controller: PanelController):
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _http_json(connection: HTTPConnection, method: str, path: str, payload: object | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    connection.request(method, path, body=body, headers={"Content-Type": "application/json"} if body is not None else {})
    response = connection.getresponse()
    return response.status, json.loads(response.read().decode("utf-8"))


def test_v2_catalog_and_windows_http_are_typed_and_repeatable(tmp_path: Path) -> None:
    controller, database, result_id = _controller_for_windows(tmp_path)
    facts = {}
    with duckdb.connect(str(database), read_only=True) as connection:
        for table in ("strategies", "analysis_plateaus", "strategy_orders", "strategy_results", "strategy_actions", "strategy_equity", "import_runs", "import_files"):
            facts[table] = connection.execute(f"select count(*) from {table}").fetchone()[0]
    server, thread = _http_server(controller)
    payload = {
        "strategy_id": 1,
        "window_a": ["2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"],
        "window_b": ["2026-01-01T00:00:00+00:00", "2026-01-03T12:00:00+00:00"],
    }
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        status, catalog = _http_json(connection, "GET", "/api/v2/strategies/performance-v2/catalog")
        assert status == 200
        assert catalog["strategies"][0]["result_id"] == result_id
        assert catalog["strategies"][0]["close_ma_len"] == 3
        assert catalog["strategies"][0]["orders"] == [{
            "order_id": 1, "open_ma_len": 7, "open_multiplier": "0.995000000000",
            "shift_bp": 125, "lot_x": "1.000000000000",
        }]
        status, first = _http_json(connection, "POST", "/api/v2/strategies/performance-v2/windows", payload)
        assert status == 200
        assert first["result_id"] == result_id
        assert first["report_start_utc"] == "2026-01-01T00:00:00Z"
        assert first["window_a"]["availability_status"] == "UNAVAILABLE"
        status, second = _http_json(connection, "POST", "/api/v2/strategies/performance-v2/windows", payload)
        assert status == 200 and second == first
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select count(*) from window_metrics").fetchone() == (2,)
        for table, count in facts.items():
            assert connection.execute(f"select count(*) from {table}").fetchone() == (count,)


def test_selection_http_downloads_xlsx_and_persists_exact_selection_state(tmp_path: Path) -> None:
    controller, database, _ = _controller_for_windows(tmp_path)
    controller.strategies_performance_v2_recalculate({"symbol": "BTCUSDT", "side": "LONG"})
    server, thread = _http_server(controller)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST", "/api/v2/strategies/performance-v2/selection",
            body=json.dumps({"symbol": "BTCUSDT", "side": "LONG", "stages": []}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert response.getheader("Content-Type") == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert "attachment; filename=\"performance-v2-finalists-BTCUSDT-LONG.xlsx\"" == response.getheader("Content-Disposition")
        assert body.startswith(b"PK")
        connection.close()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST", "/api/v2/strategies/performance-v2/selection-review-import", body=body,
            headers={"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        )
        response = connection.getresponse()
        imported = json.loads(response.read())
        assert response.status == 200
        assert imported["row_count"] == imported["finalist_count"] == 1
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    with duckdb.connect(str(database), read_only=True) as connection:
        assert connection.execute("select candidate_count, workbook_sha256 from selection_runs").fetchone() == (
            1, sha256(body).hexdigest(),
        )
        assert connection.execute("select count(*) from selection_results").fetchone() == (1,)
        assert connection.execute("select count(*) from selection_review_imports").fetchone() == (1,)


def test_selection_preview_returns_current_stage_counts(tmp_path: Path) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    controller.strategies_performance_v2_recalculate({"symbol": "BTCUSDT", "side": "LONG"})

    preview = controller.strategies_performance_v2_selection_preview({
        "symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "filter_low_trades", "enabled": True, "scope": "pair_side"},
            {"id": "pareto_dd5_capital", "enabled": False, "scope": "pair_side"},
        ],
    })

    assert preview["stages"]["filter_low_trades"] == {"enabled": True, "eliminated": 0, "remaining": 1}
    assert preview["stages"]["pareto_dd5_capital"] == {"enabled": False, "eliminated": 0, "remaining": 1}


def test_selection_preview_reuses_candidates_until_recalculation(tmp_path: Path, monkeypatch) -> None:
    controller, database, _ = _controller_for_windows(tmp_path)
    payload = {"symbol": "BTCUSDT", "side": "LONG", "stages": []}
    controller.strategies_performance_v2_recalculate(payload)
    import mrs3.panel as panel_module
    original = panel_module.load_selection_candidates
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(panel_module, "load_selection_candidates", counted)
    controller.strategies_performance_v2_selection_preview(payload)
    controller.strategies_performance_v2_selection_preview(payload)
    assert calls == 1

    controller.strategies_performance_v2_recalculate(payload)
    controller.strategies_performance_v2_selection_preview(payload)
    assert calls == 2

    config_path = tmp_path / "config.performance.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["unified_performance_v2"]["finalist_selection"] = {"best_trade_max_profit_share_pct": 34}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    controller.strategies_performance_v2_selection_preview(payload)
    assert calls == 3

    with duckdb.connect(str(database)) as connection:
        connection.execute("update window_metrics set return_pct = coalesce(return_pct, 0) + 1")
    controller.strategies_performance_v2_selection_preview(payload)
    assert calls == 4


def test_performance_v2_schema_initialization_is_memoized(tmp_path: Path, monkeypatch) -> None:
    controller, database, _ = _controller_for_windows(tmp_path)
    import mrs3.panel as panel_module
    original = panel_module.initialize_performance_v2
    calls = 0

    def counted(connection):
        nonlocal calls
        calls += 1
        return original(connection)

    monkeypatch.setattr(panel_module, "initialize_performance_v2", counted)
    controller.performance_v2_catalog()
    controller.performance_v2_catalog()

    assert calls == 1

    database.unlink()
    with duckdb.connect(str(database)):
        pass
    controller.performance_v2_catalog()
    assert calls == 2


def test_selection_xlsx_maps_stale_snapshot_to_api_error(tmp_path: Path, monkeypatch) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    controller.strategies_performance_v2_recalculate({"symbol": "BTCUSDT", "side": "LONG"})
    import mrs3.panel as panel_module

    def stale(*_args, **_kwargs):
        raise panel_module.SelectionReviewError("SELECTION_REVIEW_STALE_RESULTS", details=[1])

    monkeypatch.setattr(panel_module, "persist_selection_snapshot", stale)
    with pytest.raises(PerformanceV2ApiError) as raised:
        controller.strategies_performance_v2_selection({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    assert raised.value.code == "SELECTION_REVIEW_STALE_RESULTS"
    assert raised.value.status == 409


def test_selection_xlsx_maps_metadata_database_lock_to_api_error(tmp_path: Path, monkeypatch) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    controller.strategies_performance_v2_recalculate({"symbol": "BTCUSDT", "side": "LONG"})
    import mrs3.panel as panel_module
    monkeypatch.setattr(panel_module, "new_run_metadata", lambda *_args: (_ for _ in ()).throw(duckdb.IOException("locked")))

    with pytest.raises(PerformanceV2ApiError) as raised:
        controller.strategies_performance_v2_selection({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    assert raised.value.code == "PERFORMANCE_V2_LOCKED"
    assert raised.value.status == 409


def test_selection_review_import_maps_database_lock_to_api_error(tmp_path: Path, monkeypatch) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    controller.strategies_performance_v2_recalculate({"symbol": "BTCUSDT", "side": "LONG"})
    import mrs3.panel as panel_module
    monkeypatch.setattr(panel_module, "import_selection_review", lambda *_args: (_ for _ in ()).throw(duckdb.IOException("locked")))

    with pytest.raises(PerformanceV2ApiError) as raised:
        controller.strategies_performance_v2_selection_review_import(b"xlsx")

    assert raised.value.code == "PERFORMANCE_V2_LOCKED"
    assert raised.value.status == 409


@pytest.mark.parametrize("operation", ["selection", "cache_status", "recalculate"])
def test_performance_v2_missing_database_has_typed_not_found_error(tmp_path: Path, operation: str) -> None:
    controller, database, _ = _controller_for_windows(tmp_path)
    database.unlink()
    payload = {"symbol": "BTCUSDT", "side": "LONG", "stages": []}

    with pytest.raises(PerformanceV2ApiError) as raised:
        {
            "selection": lambda: controller.strategies_performance_v2_selection(payload),
            "cache_status": lambda: controller.strategies_performance_v2_selection_cache_status(payload),
            "recalculate": lambda: controller.strategies_performance_v2_recalculate(payload),
        }[operation]()

    assert raised.value.code == "PERFORMANCE_V2_NOT_FOUND"
    assert raised.value.status == 404


def test_selection_cache_status_reports_missing_default_windows(tmp_path: Path) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    payload = {"symbol": "BTCUSDT", "side": "LONG"}

    assert controller.strategies_performance_v2_selection_cache_status(payload) == {
        "total": 1, "missing": 1, "ready": False,
    }
    assert controller.strategies_performance_v2_recalculate(payload) == {"status": "READY"}
    assert controller.strategies_performance_v2_selection_cache_status(payload) == {
        "total": 1, "missing": 0, "ready": True,
    }


def test_selection_recalculate_passes_only_missing_strategy_ids(tmp_path: Path, monkeypatch) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    import mrs3.panel as panel_module
    calls = []
    monkeypatch.setattr(panel_module, "selection_cache_missing_strategy_ids", lambda *_args: (17, 23))
    monkeypatch.setattr(panel_module, "prepare_selection_window_cache", lambda *args: calls.append(args))

    assert controller.strategies_performance_v2_recalculate({"symbol": "BTCUSDT", "side": "LONG"}) == {"status": "READY"}
    assert calls and calls[0][-1] == (17, 23)


def test_normalization_30d_does_not_compress_idle_tail_to_event_span() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = replace(
        _metrics_for_normalization(start, start + timedelta(days=2)),
        requested_end_utc=start + timedelta(days=14),
    )

    assert _normalization_30d(metrics) == {
        "period_days": 30,
        "status": "ok",
        "observed_days": "14.000000",
        "growth_factor": "1.22658772",
        "return_pct": "22.6588",
        "trade_rate": "10.7143",
    }


def test_normalization_30d_clamps_to_report_calendar_interval_and_rejects_empty_overlap() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    metrics = _metrics_for_normalization(start, start + timedelta(days=14))

    clamped = _normalization_30d(metrics, start + timedelta(days=3), start + timedelta(days=10))
    empty = _normalization_30d(metrics, start + timedelta(days=20), start + timedelta(days=25))

    assert clamped["observed_days"] == "7.000000"
    assert empty == {
        "period_days": 30,
        "status": "invalid_duration",
        "observed_days": None,
        "growth_factor": None,
        "return_pct": None,
        "trade_rate": None,
    }
def test_selection_cache_status_requires_an_active_candidate(tmp_path: Path) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)

    assert controller.strategies_performance_v2_selection_cache_status({"symbol": "ETHUSDT", "side": "LONG"}) == {
        "total": 0, "missing": 0, "ready": False,
    }


def test_selection_recalculate_all_skips_pairs_with_ready_facts(tmp_path: Path) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)

    first = controller.strategies_performance_v2_recalculate_all()
    second = controller.strategies_performance_v2_recalculate_all()

    assert first == {"status": "READY", "total_pairs": 1, "recalculated_pairs": 1, "ready_pairs": 0}
    assert second == {"status": "READY", "total_pairs": 1, "recalculated_pairs": 0, "ready_pairs": 1}


def test_selection_xlsx_rejects_incomplete_cache(tmp_path: Path) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)

    with pytest.raises(PerformanceV2ApiError, match="recalculation") as raised:
        controller.strategies_performance_v2_selection({"symbol": "BTCUSDT", "side": "LONG", "stages": []})

    assert raised.value.code == "SELECTION_CACHE_INCOMPLETE"
    assert raised.value.status == 409


def test_selection_still_exports_when_parallel_cache_warmup_fails(tmp_path: Path, monkeypatch) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    controller.strategies_performance_v2_recalculate({"symbol": "BTCUSDT", "side": "LONG"})
    import mrs3.panel as panel_module
    monkeypatch.setattr(panel_module, "prepare_selection_window_cache", lambda *_args: (_ for _ in ()).throw(OSError("warmup unavailable")))

    filename, workbook = controller.strategies_performance_v2_selection(
        {"symbol": "BTCUSDT", "side": "LONG", "stages": []}
    )

    assert filename.endswith(".xlsx")
    assert workbook.startswith(b"PK")


def test_v2_catalog_ignores_active_strategy_without_current_result(tmp_path: Path) -> None:
    connection, _ = _db(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    orphan_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
           order_count, analysis_run_id, candidate_identity, lifecycle_status,
           created_at_utc, updated_at_utc) values ('orphan', 'BTCUSDT', 'LONG', '1h',
           3, 1, 'run', 'orphan', 'ACTIVE', ?, ?) returning strategy_id""",
        [now, now],
    ).fetchone()[0]
    connection.execute(
        """insert into strategy_orders (strategy_id, order_id, open_ma_len, open_multiplier,
           shift_bp, lot_x, analysis_run_id, plateau_id, base_point_trades)
           values (?, 1, 7, 0.995, 125, 1, 'run', 'P1', 8)""",
        [orphan_id],
    )

    catalog = performance_v2_catalog(connection)

    assert [strategy["strategy_name"] for strategy in catalog["strategies"]] == ["alpha"]


def test_v2_catalog_returns_empty_orders_for_current_strategy(tmp_path: Path) -> None:
    connection, _ = _db(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy_id = connection.execute(
        """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
           order_count, analysis_run_id, candidate_identity, lifecycle_status,
           created_at_utc, updated_at_utc) values ('empty', 'BTCUSDT', 'LONG', '1h',
           3, 1, 'run', 'empty', 'ACTIVE', ?, ?) returning strategy_id""",
        [now, now],
    ).fetchone()[0]
    result_id = connection.execute(
        """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
           commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
           max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
           values (?, ?, ?, 'Bybit', .0004, 100, 110, 10, 10, 0, 0, 2, 2, ?) returning result_id""",
        [strategy_id, now, datetime(2026, 1, 5, tzinfo=UTC), now],
    ).fetchone()[0]
    connection.execute("update strategies set current_result_id = ? where strategy_id = ?", [result_id, strategy_id])

    catalog = performance_v2_catalog(connection)

    assert catalog["strategies"][0]["strategy_name"] == "alpha"
    assert catalog["strategies"][1]["strategy_name"] == "empty"
    assert catalog["strategies"][1]["orders"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"strategy_id": True, "window_a": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], "window_b": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]},
        {"strategy_id": 1, "window_a": ["2026-01-01T00:00:00", "2026-01-02T00:00:00Z"], "window_b": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]},
        {"strategy_id": 1, "window_a": ["2026-01-01T00:00:00z", "2026-01-02T00:00:00Z"], "window_b": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]},
        {"strategy_id": 1, "window_a": ["2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"], "window_b": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"]},
        {"strategy_id": 1, "window_a": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], "window_b": ["2026-01-01T00:00:00Z"], "extra": 1},
    ],
)
def test_v2_windows_rejects_strict_payload_errors(tmp_path: Path, payload: dict) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    with pytest.raises(PerformanceV2ApiError) as raised:
        controller.performance_v2_windows(payload)
    assert raised.value.status == 400
    assert raised.value.code == "INVALID_REQUEST"


def test_v2_windows_accepts_datetime_local_utc_shapes(tmp_path: Path) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    result = controller.performance_v2_windows({
        "strategy_id": 1,
        "window_a": ["2026-01-01T00:00Z", "2026-01-02T00:00Z"],
        "window_b": ["2026-01-01T00:00:00.123Z", "2026-01-02T00:00:00.123Z"],
    })
    assert result["window_a"]["requested_start_utc"] == "2026-01-01T00:00:00Z"
    assert result["window_b"]["requested_start_utc"] == "2026-01-01T00:00:00.123000Z"


def test_v2_window_transaction_exception_reads_complete_persisted_pair_or_conflicts(tmp_path: Path, monkeypatch) -> None:
    controller, database, _ = _controller_for_windows(tmp_path)
    payload = {
        "strategy_id": 1,
        "window_a": ["2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"],
        "window_b": ["2026-01-01T00:00:00Z", "2026-01-03T12:00:00Z"],
    }
    first = controller.performance_v2_windows(payload)

    def transaction_error(*_args, **_kwargs):
        raise duckdb.TransactionException("simulated transaction conflict")

    import mrs3.panel_performance_v2 as module
    monkeypatch.setattr(module, "get_or_calculate_window_pair", transaction_error)
    assert controller.performance_v2_windows(payload) == first
    with duckdb.connect(str(database)) as connection:
        connection.execute("delete from window_metrics where requested_end_utc = ?", ["2026-01-03 12:00:00+00:00"])
    with pytest.raises(PerformanceV2ApiError) as raised:
        controller.performance_v2_windows(payload)
    assert raised.value.code == "PERFORMANCE_V2_CACHE_CONFLICT"


def test_v2_window_lock_maps_to_typed_conflict(tmp_path: Path, monkeypatch) -> None:
    _, database, _ = _controller_for_windows(tmp_path)
    import mrs3.panel_performance_v2 as module
    monkeypatch.setattr(module.duckdb, "connect", lambda *_args, **_kwargs: (_ for _ in ()).throw(duckdb.IOException("Could not set lock on file: Conflicting lock is held")))
    with pytest.raises(PerformanceV2ApiError) as raised:
        calculate_performance_v2_windows(database, 1, ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"), ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"))
    assert raised.value.status == 409
    assert raised.value.code == "PERFORMANCE_V2_LOCKED"


def test_v2_window_non_transaction_failure_rolls_back_and_releases_db(tmp_path: Path) -> None:
    _, database, _ = _controller_for_windows(tmp_path)

    def failed_pair(*_args, **_kwargs):
        raise RuntimeError("simulated metrics failure")

    with pytest.raises(RuntimeError, match="simulated metrics failure"):
        calculate_performance_v2_windows(
            database, 1,
            ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            window_pair_func=failed_pair,
        )
    assert calculate_performance_v2_windows(
        database, 1,
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
    )["strategy_id"] == 1


def test_v2_windows_http_invalid_request_has_typed_json_error(tmp_path: Path) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    server, thread = _http_server(controller)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        status, body = _http_json(
            connection,
            "POST",
            "/api/v2/strategies/performance-v2/windows",
            {
                "strategy_id": 1,
                "window_a": ["2026-01-01T00:00:00", "2026-01-02T00:00:00Z"],
                "window_b": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
            },
        )
        connection.close()
        assert status == 400
        assert body["error"]["code"] == "INVALID_REQUEST"
        assert isinstance(body["error"]["message"], str)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_v2_windows_http_hides_unexpected_error(tmp_path: Path, monkeypatch) -> None:
    controller, _, _ = _controller_for_windows(tmp_path)
    import mrs3.panel as panel_module
    monkeypatch.setattr(panel_module, "calculate_performance_v2_windows", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("sensitive path")))
    server, thread = _http_server(controller)
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        status, body = _http_json(connection, "POST", "/api/v2/strategies/performance-v2/windows", {
            "strategy_id": 1,
            "window_a": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
            "window_b": ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
        })
        connection.close()
        assert status == 500
        assert body == {"error": {"code": "INTERNAL", "message": "Performance v2 calculation failed"}}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_v2_stale_current_result_pointer_is_excluded_and_returns_404(tmp_path: Path) -> None:
    controller, database, _ = _controller_for_windows(tmp_path)
    with duckdb.connect(str(database)) as connection:
        connection.execute("update strategies set current_result_id = 999 where strategy_id = 1")
    assert controller.performance_v2_catalog() == {"strategies": [], "selection_pairs_with_runs": []}
    with pytest.raises(PerformanceV2ApiError) as raised:
        controller.performance_v2_windows(
            {
                "strategy_id": 1,
                "window_a": ["2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"],
                "window_b": ["2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"],
            }
        )
    assert raised.value.status == 404
    assert raised.value.code == "PERFORMANCE_V2_NOT_FOUND"


def test_v2_current_result_switch_does_not_reuse_r1_window_cache(tmp_path: Path) -> None:
    controller, database, r1_id = _controller_for_windows(tmp_path)
    payload = {
        "strategy_id": 1,
        "window_a": ["2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"],
        "window_b": ["2026-01-01T00:00:00Z", "2026-01-05T00:00:00Z"],
    }
    r1 = controller.performance_v2_windows(payload)
    assert r1["result_id"] == r1_id
    assert r1["window_a"]["availability_status"] == "UNAVAILABLE"

    with duckdb.connect(str(database)) as connection:
        cached_r1 = connection.execute("select * from window_metrics where result_id = ?", [r1_id]).fetchall()
        now = datetime(2026, 1, 1, tzinfo=UTC)
        connection.execute(
            """insert into strategies (strategy_name, symbol, side, timeframe, close_ma_len,
               order_count, analysis_run_id, candidate_identity, lifecycle_status,
               created_at_utc, updated_at_utc) values ('old-holder', 'BTCUSDT', 'LONG', '1h',
               1, 1, 'old-run', 'old-candidate', 'DISCARDED', ?, ?)""",
            [now, now],
        )
        old_holder_id = connection.execute(
            "select strategy_id from strategies where strategy_name = 'old-holder'"
        ).fetchone()[0]
        connection.execute("update strategies set current_result_id = null where strategy_id = 1")
        connection.execute("delete from window_metrics where result_id = ?", [r1_id])
        connection.execute("delete from strategy_actions where result_id = ?", [r1_id])
        connection.execute("delete from strategy_equity where result_id = ?", [r1_id])
        connection.execute("update strategy_results set strategy_id = ? where result_id = ?", [old_holder_id, r1_id])
        r2_id = connection.execute(
            """insert into strategy_results (strategy_id, report_start_utc, report_end_utc, exchange,
               commission_rate, initial_balance, final_balance, total_pnl, total_pnl_pct,
               max_drawdown, max_drawdown_pct, total_fees, total_trades, imported_at_utc)
               values (?, ?, ?, 'Bybit', .0004, 100, 125, 25, 25, 0, 0, 0, 2, ?)
               returning result_id""",
            [1, now, datetime(2026, 1, 5, tzinfo=UTC), now],
        ).fetchone()[0]
        connection.executemany(
            "insert into strategy_actions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (r2_id, 0, now, "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 100, None),
                (r2_id, 1, datetime(2026, 1, 2, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 5, 0, 105, None),
                (r2_id, 2, datetime(2026, 1, 3, tzinfo=UTC), "BTCUSDT", 1, "opened", 1, 1, "long", 0, 0, 105, None),
                (r2_id, 3, datetime(2026, 1, 4, tzinfo=UTC), "BTCUSDT", 1, "closed", 1, 0, "", 20, 0, 125, None),
            ],
        )
        connection.executemany(
            "insert into strategy_equity values (?, ?, ?, ?, ?)",
            [
                (r2_id, 0, now, 100, 100),
                (r2_id, 1, datetime(2026, 1, 2, tzinfo=UTC), 105, 105),
                (r2_id, 2, datetime(2026, 1, 3, tzinfo=UTC), 105, 105),
                (r2_id, 3, datetime(2026, 1, 4, tzinfo=UTC), 125, 125),
                (r2_id, 4, datetime(2026, 1, 5, tzinfo=UTC), 125, 125),
            ],
        )
        connection.executemany(
            "insert into window_metrics values (" + ",".join("?" for _ in range(21)) + ")",
            cached_r1,
        )
        connection.execute("update strategies set current_result_id = ? where strategy_id = 1", [r2_id])

    r2 = controller.performance_v2_windows(payload)
    assert r2["result_id"] == r2_id != r1_id
    assert r2["window_a"]["availability_status"] == "AVAILABLE"
    assert r2["window_a"]["return_pct"] == "19.047619047619"
