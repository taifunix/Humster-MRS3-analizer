from __future__ import annotations

from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
import time
from datetime import datetime, timezone

import duckdb
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
    calculate_performance_v2_windows,
)


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "report_current_v2.html"
UTC = timezone.utc


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
    return PerformanceV2PanelRequest(
        inbox=inbox,
        report_root=reports,
        config=config,
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


def test_v2_panel_controller_uses_committed_tester_job_and_status_endpoint(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("v1 Performance/DD5/workbook service was called")

    import mrs3.panel as panel_module

    monkeypatch.setattr(panel_module, "LocalPerformanceImportJobs", forbidden)
    monkeypatch.setattr(panel_module, "LocalPerformanceDd5Jobs", forbidden)
    monkeypatch.setattr(panel_module, "LocalPerformanceDd5Service", forbidden)
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
    assert status["result"]["database_path"].endswith("strategy_performance.duckdb")
    assert status["result"]["audit_path"].endswith("import_audit.v2.json")
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
        assert document["result"]["database_path"].endswith("strategy_performance.duckdb")

        restarted = PanelController(tmp_path, config_path)
        persisted = restarted.strategies_performance_v2_import_status(v2_job_id)
        assert persisted["state"] == "COMMITTED"
        assert persisted["result"]["imported_count"] == 2
        assert persisted["result"]["audit_path"].endswith("import_audit.v2.json")
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
    assert controller.performance_v2_catalog() == {"strategies": []}
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
