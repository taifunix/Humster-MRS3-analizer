import json
from http.client import HTTPConnection
import threading
from types import SimpleNamespace

import mrs3.panel_jobs as panel_jobs_module
import mrs3.panel_portfolio as panel_portfolio_module
from mrs3.panel import PanelController, create_panel_server


def test_portfolio_admission_journal_failure_is_service_unavailable(tmp_path, monkeypatch):
    config_path = tmp_path / "config.local.json"
    config_path.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config_path)
    service = controller._portfolio_service
    monkeypatch.setattr(service, "_config", lambda: (SimpleNamespace(), b"{}", {}))
    monkeypatch.setattr(service, "_normalise_campaign", lambda *_: {"selected_pairs": []})
    monkeypatch.setattr(service, "_snapshot_finalists", lambda *_: ([], []))
    monkeypatch.setattr(panel_portfolio_module, "_load_weighted_template", lambda: ({}, "0" * 64))
    monkeypatch.setattr(panel_portfolio_module, "_validate_frozen_campaign", lambda _campaign: None)
    monkeypatch.setattr(service, "_write_campaign_snapshot", lambda _campaign: tmp_path / "snapshot.json")
    monkeypatch.setattr(service, "_snapshot_path", lambda campaign_id: tmp_path / f"{campaign_id}.json")

    def fail_replace(_source, _destination):
        raise PermissionError(13, "access denied")

    monkeypatch.setattr(panel_jobs_module.os, "replace", fail_replace)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST",
            "/api/v2/portfolio/campaigns",
            json.dumps({"arbitrary": "payload"}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status == 503
    assert body["error"]["code"] == "JOB_PERSISTENCE_FAILED"
    assert controller._panel_jobs.list() == []
    assert service._threads == {}
    assert not (tmp_path / "snapshot.json").exists()
