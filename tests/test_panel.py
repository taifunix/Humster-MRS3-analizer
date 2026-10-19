from __future__ import annotations

from http.client import HTTPConnection
import json
import re
import tomllib
from pathlib import Path

import pytest

from mrs3 import panel as panel_module
from mrs3.panel import PanelController, create_panel_server


@pytest.mark.parametrize("config_text", ["", json.dumps({"panel": {"default_root": "legacy"}})])
def test_root_serves_the_static_panel_and_legacy_routes_are_gone(tmp_path: Path, config_text: str) -> None:
    """Panel rework stages 1a/1b: the legacy panel, its non-v2 API and the retired screens answer 404."""
    config = tmp_path / "config.local.json"
    if config_text:
        config.write_text(config_text, encoding="utf-8")
    server = create_panel_server("127.0.0.1", 0, PanelController(tmp_path, config))
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method: str, path: str) -> tuple[int, bytes]:
        # One connection per request and no request body: a 404 sent before the body is read would make
        # Windows reset the socket on close (WinError 10053) and turn the check into a race.
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, headers={"Content-Length": "0"} if method == "POST" else {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    try:
        status, body = request("GET", "/")
        assert status == 200
        assert body == (Path(panel_module.__file__).parent / "panel_web" / "index.html").read_bytes()
        for path in ("/legacy", "/api/status", "/api/ui/bootstrap", "/api/duckdb-import/settings",
                     "/api/source-v6/library", "/api/v2/testing/local/status", "/api/v2/settings/analysis-profile",
                     # stage 1b part B: Source DB, surfaces, fresh analysis and shortlist
                     "/api/v2/source/local/catalog", "/api/v2/source/local/jobs", "/api/v2/surfaces/catalog",
                     "/api/v2/strategies/analysis/catalog", "/api/v2/strategies/fresh/generate/status"):
            assert request("GET", path)[0] == 404, path
        for path in ("/api/start", "/api/browse", "/api/duckdb-import/start", "/api/duckdb-direct/start",
                     "/api/analysis/library", "/api/source-v6/start", "/api/v2/testing/local/fill",
                     "/api/v2/testing/screener/fill", "/api/v2/settings/analysis-profile",
                     "/api/v2/source/local/import/start", "/api/v2/source/remote/start", "/api/v2/surfaces/preflight",
                     "/api/v2/strategies/fresh/analyze", "/api/v2/strategies/fresh/generate",
                     "/api/v2/strategies/fresh/shortlist", "/api/v2/strategies/fresh/open"):
            assert request("POST", path)[0] == 404, path
    finally:
        server.shutdown()
        server.server_close()


def test_static_panel_shell_contains_only_navigation_contract() -> None:
    panel_web = Path(panel_module.__file__).parent / "panel_web"
    html = (panel_web / "index.html").read_text(encoding="utf-8")
    script = (panel_web / "app.js").read_text(encoding="utf-8")
    for label in ("Пары и стратегии", "Портфели", "Настройки"):
        assert label in html
    for excluded in ("Artefacts", "CSV", "DUCKDB_DIRECT", "credential", "password", "token"):
        assert excluded.casefold() not in html.casefold()
    for excluded in ("Artefacts", "DUCKDB_DIRECT", "credential", "password", "authorization", "bearer"):
        assert excluded.casefold() not in script.casefold()
    token_identifiers = set(re.findall(r"\b[a-z_]*token\b", script.casefold()))
    # The content-bound shortlist identifier is not an authentication token.
    assert token_identifiers <= {"selection_token"}
    assert "/api/v2/" in script
    assert "/api/ui/" not in script
    assert "/api/duckdb" not in script
    assert script.encode("utf-8").decode("utf-8") == script
    assert 'aria-disabled="true"' in html
    assert 'disabled aria-disabled="true"' in html
    assert "onclick" not in html


def test_panel_package_data_includes_static_assets() -> None:
    document = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package_data = document["tool"]["setuptools"]["package-data"]["mrs3"]
    assert {"panel_web/*.html", "panel_web/*.css", "panel_web/*.js"}.issubset(package_data)








def test_panel_rejects_non_loopback_bind(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json")

    with pytest.raises(ValueError, match="loopback"):
        create_panel_server("0.0.0.0", 0, controller)
