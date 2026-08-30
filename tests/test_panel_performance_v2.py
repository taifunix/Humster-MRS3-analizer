from __future__ import annotations

from hashlib import sha256
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
import time

import duckdb
import pytest

from mrs3.performance_v2_store import (
    PerformanceV2Config,
    initialize_performance_v2,
    performance_v2_database_path,
)
from mrs3.performance_v2_windows import compare_window_pair_geometrically, get_or_calculate_window_pair
from mrs3.panel import PanelController, create_panel_server
from mrs3.panel_performance_v2 import PerformanceV2PanelRequest, LocalPerformanceV2Service


FIXTURE = Path(__file__).parent / "fixtures" / "performance" / "report_current_v2.html"


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
        window_a=("2026-01-01T00:00:00Z", "2026-01-09T00:00:00Z"),
        window_b=("2026-01-01T00:00:00Z", "2026-01-03T12:00:00Z"),
    ), snapshot


def test_v2_panel_service_imports_committed_inbox_and_caches_ab_without_v1(tmp_path: Path) -> None:
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
    assert len(result.windows) == 2
    with duckdb.connect(str(request.config.database_root / "strategy_performance.duckdb"), read_only=True) as connection:
        assert connection.execute("select count(*) from window_metrics").fetchone() == (4,)
        assert connection.execute("select count(*) from strategies").fetchone() == (2,)
        assert connection.execute("select count(*) from strategy_orders").fetchone() == (3,)
        assert connection.execute("select count(*) from strategy_results").fetchone() == (2,)
    assert {
        path.relative_to(request.inbox): path.read_bytes()
        for path in request.inbox.rglob("*") if path.is_file()
    } == before


def test_v2_panel_service_uses_injected_window_pair_and_comparison(tmp_path: Path) -> None:
    request, _ = _request(tmp_path)
    pair_calls: list[tuple[int, object, object]] = []
    compare_calls: list[tuple[object, object]] = []

    def window_pair(connection, result_id, window_a, window_b):
        pair_calls.append((result_id, window_a, window_b))
        return get_or_calculate_window_pair(connection, result_id, window_a, window_b)

    def compare(window_a, window_b):
        compare_calls.append((window_a, window_b))
        return compare_window_pair_geometrically(window_a, window_b)

    result = LocalPerformanceV2Service(
        window_pair_func=window_pair,
        compare_func=compare,
    ).run(request)

    assert len(pair_calls) == len(compare_calls) == 2
    assert all(call[1:] == (request.window_a, request.window_b) for call in pair_calls)
    assert all(left.result_id == right.result_id for left, right in compare_calls)
    assert result.window_count == 2


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
    config_path.write_text(json.dumps({"panel_paths": {"tester_report_dir": "tester-reports"}}), encoding="utf-8")
    (tmp_path / "config.performance.json").write_text(
        json.dumps({"unified_performance_v2": {"database_root": "performance-v2", "workers": 1}}),
        encoding="utf-8",
    )
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
            "window_a": list(request.window_a),
            "window_b": list(request.window_b),
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
    assert status["result"]["window_count"] == 2

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
