from __future__ import annotations

from http.client import HTTPConnection
from html.parser import HTMLParser
import json
import re
import tomllib
from pathlib import Path

import pytest

from mrs3 import panel as panel_module
from mrs3.panel import PanelController, create_panel_server


@pytest.mark.parametrize("config_text", ["", json.dumps({"panel": {"default_root": "legacy"}})])
def test_root_serves_the_static_panel_and_legacy_routes_are_gone(tmp_path: Path, config_text: str) -> None:
    """Panel rework stage 1a: the legacy single-page panel and its non-v2 API are removed (404)."""
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
                     "/api/source-v6/library", "/api/v2/testing/local/status", "/api/v2/settings/analysis-profile"):
            assert request("GET", path)[0] == 404, path
        for path in ("/api/start", "/api/browse", "/api/duckdb-import/start", "/api/duckdb-direct/start",
                     "/api/analysis/library", "/api/source-v6/start", "/api/v2/testing/local/fill",
                     "/api/v2/testing/screener/fill", "/api/v2/settings/analysis-profile"):
            assert request("POST", path)[0] == 404, path
    finally:
        server.shutdown()
        server.server_close()


def test_v2_local_source_service_uses_source_v6_throughput_settings(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(
        json.dumps({
            "duckdb_import": {"workers": 3},
            "source_v6_import": {
                "write_batch_size": 7,
                "worker_chunk_size": 8,
                "max_in_flight_chunks": 9,
                "segment_writer_limit": 3,
            },
        }),
        encoding="utf-8",
    )

    service, _ = PanelController(tmp_path, config)._local_source_jobs()

    assert service.workers == 3
    assert service.import_options == {
        "write_batch_size": 7,
        "worker_chunk_size": 8,
        "max_in_flight_chunks": 9,
        "segment_writer_limit": 3,
        # Hydrating decodes every fragment payload into memory after the
        # segments are reduced (about 2 MB each), which exhausts RAM on a
        # full 27k-report import. Publication reads the published database.
        "hydrate_fragments": False,
    }


def test_cached_local_source_service_refreshes_common_workers(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 3}}), encoding="utf-8")
    controller = PanelController(tmp_path, config)

    service, _ = controller._local_source_jobs()
    config.write_text(json.dumps({"duckdb_import": {"workers": 6}}), encoding="utf-8")

    refreshed, _ = controller._local_source_jobs()
    assert refreshed is service
    assert refreshed.workers == 6


def test_cached_surface_service_refreshes_common_workers_without_restart(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 3}}), encoding="utf-8")
    controller = PanelController(tmp_path, config)

    service = controller._surfaces()
    config.write_text(json.dumps({"duckdb_import": {"workers": 20}}), encoding="utf-8")

    refreshed = controller._surfaces()
    assert refreshed is service
    assert refreshed._workers == 20


def test_static_panel_shell_contains_only_navigation_contract() -> None:
    panel_web = Path(panel_module.__file__).parent / "panel_web"
    html = (panel_web / "index.html").read_text(encoding="utf-8")
    script = (panel_web / "app.js").read_text(encoding="utf-8")
    for label in ("Testing", "Source DB", "Surfaces", "Strategies and DD5", "Settings", "Portfolio"):
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







def test_direct_error_preserves_non_absolute_slash_text() -> None:
    assert panel_module._safe_direct_error("report/grid mismatch") == "report/grid mismatch"


@pytest.mark.parametrize("path", [
    "source.duckdb", "data/tmp/source", r"\\server\share\db.duckdb", "data/tmp/source.duckdb",
])
def test_direct_error_redacts_local_file_path(path: str) -> None:
    assert panel_module._safe_direct_error(f"failed reading {path}") == "direct build failed"


def test_source_v6_panel_clamps_default_writer_limit_for_partial_config(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 2}, "source_v6_import": {"write_batch_size": 8}}), encoding="utf-8")
    controller = PanelController(tmp_path, config)
    workers, settings, limit = controller._source_v6_import_options()
    assert workers == 2 and settings.write_batch_size == 8 and limit == 2


def test_fresh_source_v6_analysis_uses_configured_worker_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from mrs3.panel import PanelController

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 3}}), encoding="utf-8")
    dates, analysis = tmp_path / "dates.json", tmp_path / "analysis.json"
    dates.write_text("{}", encoding="utf-8")
    analysis.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "mrs3.panel.read_multiscope_surface",
        lambda path, *, decode=True: captured.update({"decode": decode}) or {"surface_id": "surface"},
    )
    monkeypatch.setattr("mrs3.panel.run_multiscope_analysis", lambda surface, directory, loaded, **kwargs: captured.update({"surface": surface, "workers": kwargs["workers"], "cancel_check": kwargs["cancel_check"]}) or directory / "result.analysis-v6.duckdb")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda path: object(), source_v6_listing_dates_loader=lambda path: {"ONUSDT": "2020-01-01"})

    result = controller.source_v6_start_fresh_analysis({"surface_path": "surface.surface-v6.duckdb", "listing_dates_path": str(dates), "config_path": str(analysis)})

    assert result["phase"] == "COMMITTED"
    assert captured["decode"] is False
    assert captured["workers"] == 3
    assert callable(captured["cancel_check"])


def test_fresh_source_v6_analysis_uses_the_editable_analysis_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.panel import PanelController

    dates, analysis = tmp_path / "dates.json", tmp_path / "analysis.json"
    dates.write_text("{}", encoding="utf-8")
    analysis.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}
    target = tmp_path / "chosen" / "ON_2026-01-01_2026-01-31.analysis-v6.duckdb"
    monkeypatch.setattr("mrs3.panel.read_multiscope_surface", lambda path, *, decode=True: {"surface_id": "surface"})
    monkeypatch.setattr(
        "mrs3.panel.run_multiscope_analysis",
        lambda surface, directory, loaded, **kwargs: captured.update({"directory": directory, "filename": kwargs["filename"]}) or target,
    )
    controller = PanelController(tmp_path, tmp_path / "config.local.json", analysis_config_loader=lambda path: object(), source_v6_listing_dates_loader=lambda path: {})

    result = controller.source_v6_start_fresh_analysis({
        "surface_path": "surface.surface-v6.duckdb", "listing_dates_path": str(dates), "config_path": str(analysis),
        "target_path": str(target),
    })

    assert result["phase"] == "COMMITTED"
    assert captured == {"directory": target.parent, "filename": target.name}


def test_fresh_source_v6_analysis_treats_a_target_directory_as_a_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.panel import PanelController

    dates, analysis = tmp_path / "dates.xlsx", tmp_path / "config.local.json"
    dates.write_text("dates", encoding="utf-8")
    analysis.write_text("{}", encoding="utf-8")
    target_directory = tmp_path / "analysis"; target_directory.mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setattr("mrs3.panel.read_multiscope_surface", lambda *_args, **_kwargs: {"surface_id": "surface"})
    monkeypatch.setattr(
        "mrs3.panel.run_multiscope_analysis",
        lambda _surface, directory, _config, **kwargs: captured.update({"directory": directory, "filename": kwargs["filename"]}) or directory / "result.analysis-v6.duckdb",
    )
    controller = PanelController(tmp_path, analysis, analysis_config_loader=lambda _path: object(), source_v6_listing_dates_loader=lambda _path: {})

    result = controller.source_v6_start_fresh_analysis({
        "surface_path": "surface.surface-v6.duckdb", "listing_dates_path": str(dates), "config_path": str(analysis),
        "target_path": str(target_directory),
    })

    assert result["phase"] == "COMMITTED"
    assert captured == {"directory": target_directory, "filename": None}


def test_fresh_source_v6_analysis_reports_requested_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.panel import PanelController

    dates, analysis = tmp_path / "dates.json", tmp_path / "analysis.json"
    dates.write_text("{}", encoding="utf-8")
    analysis.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("mrs3.panel.read_multiscope_surface", lambda path, *, decode=True: {"surface_id": "surface"})
    monkeypatch.setattr("mrs3.panel.run_multiscope_analysis", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Source v6 analysis cancelled")))
    controller = PanelController(tmp_path, tmp_path / "config.local.json", analysis_config_loader=lambda path: object(), source_v6_listing_dates_loader=lambda path: {})

    result = controller.source_v6_start_fresh_analysis({"surface_path": "surface.surface-v6.duckdb", "listing_dates_path": str(dates), "config_path": str(analysis)})

    assert result["phase"] == "CANCELLED"


def test_surface_catalog_treats_invalid_import_settings_as_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.panel import PanelController

    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    monkeypatch.setattr(controller, "_import_settings", lambda: (_ for _ in ()).throw(ValueError("bad settings")))

    assert controller.surface_catalog() == {"surfaces": []}


def test_fresh_analysis_keeps_the_terminal_error_for_the_static_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.panel import PanelController

    dates = tmp_path / "dates.xlsx"; dates.write_text("dates", encoding="utf-8")
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    monkeypatch.setattr(controller, "source_v6_start_fresh_analysis", lambda _payload: {"phase": "FAILED", "error": "analysis input is invalid"})
    monkeypatch.setattr(controller, "_workflow_default", lambda _name: dates)

    assert controller.strategies_fresh_analyze({"surface_path": "surface.surface-v6.duckdb"}) == {
        "phase": "FAILED", "error": "Fresh analysis setup failed. Check the selected surface and Analysis profile."
    }


class _ImportUiParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.input_ids: set[str] = set()
        self.actions: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id"):
            self.input_ids.add(str(attributes["id"]))
        if tag == "button" and attributes.get("onclick"):
            self.actions.add(str(attributes["onclick"]))


def test_panel_rejects_non_loopback_bind(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json")

    with pytest.raises(ValueError, match="loopback"):
        create_panel_server("0.0.0.0", 0, controller)
