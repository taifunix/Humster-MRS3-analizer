"""Panel routes for tick-test batches from trade-store READY folders (Panel rework stage 1b).

Contract: docs/specs/2026-10-18-panel-simulation-workflow.md, «Stage 1b contract».
"""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading

import pytest

from mrs3.evaluation_protocol import EvaluationTarget
from mrs3.panel import PanelController, create_panel_server
from mrs3.panel_strategy_batch import validate_strategy_manifest
from tests.test_store_ready_tick import NAME, TARGET, TEMPLATE, _ready


def _controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, template: Path = TEMPLATE) -> PanelController:
    store, _ready_dir = _ready(tmp_path)
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"trade_store": {"root": str(store)}}), encoding="utf-8")
    target = EvaluationTarget(TARGET.base, "Bybit", strategy_template=template)
    monkeypatch.setattr(PanelController, "_evaluation_target", lambda _self, _exchange=None: target)
    return PanelController(tmp_path, config)


def _call(server, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "Content-Length": str(len(data))} if data is not None else {}
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def serve(controller: PanelController):
        server = create_panel_server("127.0.0.1", 0, controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    servers: list = []
    yield serve
    for server in servers:
        server.shutdown()
        server.server_close()


def test_catalog_and_prepare_publish_a_tick_test_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served) -> None:
    controller = _controller(tmp_path, monkeypatch)
    server = served(controller)

    status, catalog = _call(server, "GET", "/api/v2/strategies/store-ready/catalog")
    assert status == 200
    [folder] = catalog["folders"]
    assert (folder["name"], folder["testable"], folder["strategies"]) == (NAME, True, 1)

    status, prepared = _call(server, "POST", "/api/v2/strategies/store-ready/prepare", {"ready": NAME})
    assert status == 200, prepared
    assert set(prepared) == {"ready", "analysis_run_id", "strategy_count", "pairs", "ready_pairs", "reused"}
    assert (prepared["ready"], prepared["strategy_count"], prepared["reused"]) == (NAME, 1, False)
    run_id = prepared["analysis_run_id"]
    assert len(run_id) == 64 and str(tmp_path) not in json.dumps(prepared)        # no local path leaves the Panel

    manifest = controller._fresh_strategy_manifest(run_id)
    assert manifest.parent.parent == (tmp_path / "Output" / "store-ready").resolve()
    validated = validate_strategy_manifest(manifest)
    controller._require_protocol_v2_strategy_dir(validated.strategy_source, controller._evaluation_target())
    status, again = _call(server, "POST", "/api/v2/strategies/store-ready/prepare", {"ready": NAME})
    assert (status, again["analysis_run_id"], again["reused"]) == (200, run_id, True)

    restarted = PanelController(tmp_path, tmp_path / "config.local.json")         # the card restores the batch
    assert restarted._fresh_strategy_manifest(run_id) == manifest
    assert restarted.strategies_fresh_batch() == {"phase": "COMMITTED", "analysis_run_id": run_id, "strategy_count": 1}


@pytest.mark.parametrize("body", [{"ready": "../escape"}, {"ready": "missing"}, {"ready": NAME, "extra": 1}, {}])
def test_prepare_refuses_bad_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served, body: dict) -> None:
    server = served(_controller(tmp_path, monkeypatch))
    status, answer = _call(server, "POST", "/api/v2/strategies/store-ready/prepare", body)
    assert status == 400 and isinstance(answer["error"], str)
    assert not (tmp_path / "Output" / "store-ready").exists()


def test_protocol_v2_refusal_answers_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served) -> None:
    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    template["basic"]["use_fix"] = False
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(template), encoding="utf-8")
    server = served(_controller(tmp_path, monkeypatch, template=broken))
    status, answer = _call(server, "POST", "/api/v2/strategies/store-ready/prepare", {"ready": NAME})
    assert status == 409 and answer["error"]["code"] == "PROTOCOL_V2_RENDER_REJECTED"


def test_catalog_needs_a_configured_trade_store(tmp_path: Path, served) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    server = served(PanelController(tmp_path, config))
    status, answer = _call(server, "GET", "/api/v2/strategies/store-ready/catalog")
    assert status == 400 and "trade_store.root" in answer["error"]


def test_prepare_requires_a_json_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served) -> None:
    # review of stage 1b part A: a cross-site text/plain POST must not reach the early routes
    server = served(_controller(tmp_path, monkeypatch))
    for path in ("/api/v2/strategies/store-ready/prepare", "/api/v2/main/run", "/api/v2/main/stop",
                 "/api/v2/main/plan", "/api/v2/main/decision", "/api/v2/main/setting", "/api/v2/main/view-settings"):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        try:
            data = json.dumps({"ready": NAME}).encode("utf-8")
            connection.request("POST", path, body=data, headers={"Content-Type": "text/plain", "Content-Length": str(len(data))})
            response = connection.getresponse()
            response.read()
            assert response.status == 415, path
        finally:
            connection.close()
    assert not (tmp_path / "Output" / "store-ready").exists()


def test_prepare_maps_file_errors_to_409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served) -> None:
    import mrs3.panel as panel_module

    def busy(*_args, **_kwargs):
        raise PermissionError("busy")

    monkeypatch.setattr(panel_module, "prepare_tick_batch", busy)
    server = served(_controller(tmp_path, monkeypatch))
    status, answer = _call(server, "POST", "/api/v2/strategies/store-ready/prepare", {"ready": NAME})
    assert status == 409 and isinstance(answer["error"], str)
