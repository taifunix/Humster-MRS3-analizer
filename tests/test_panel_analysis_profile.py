from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading

from mrs3.panel import PanelController, create_panel_server


def _config(tmp_path: Path) -> Path:
    document = json.loads(Path("config.example.json").read_text(encoding="utf-8"))
    document["duckdb_import"] = {"workers": 15, "transaction_batch_size": 2000}
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _request(server, method: str, path: str, body: object | None = None) -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {} if payload is None else {"Content-Type": "application/json"}
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def test_http_analysis_profile_reloads_and_saves_whitelisted_values(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, _config(tmp_path))
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _request(server, "GET", "/api/v2/settings/analysis-profile")
        assert status == 200
        profile = body["profile"]
        assert isinstance(profile, dict)
        profile["economics"]["min_pnl_pct"] = "7"
        status, saved = _request(server, "POST", "/api/v2/settings/analysis-profile", {"profile": profile})
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert status == 200
    assert saved["profile"]["economics"]["min_pnl_pct"] == "7"


def test_http_analysis_profile_returns_safe_validation_error(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, _config(tmp_path))
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _request(server, "POST", "/api/v2/settings/analysis-profile", {"profile": {"remote_runner": {}}})
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert status == 400
    assert body == {"error": "Профиль анализа содержит недопустимые поля."}


def test_http_analysis_profile_hides_invalid_number_details(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, _config(tmp_path))
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, body = _request(server, "GET", "/api/v2/settings/analysis-profile")
        profile = body["profile"]
        assert isinstance(profile, dict)
        profile["economics"]["min_pnl_pct"] = "not-a-number"
        status, response = _request(server, "POST", "/api/v2/settings/analysis-profile", {"profile": profile})
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)

    assert status == 400
    assert response == {"error": "Профиль анализа содержит недопустимые значения."}
