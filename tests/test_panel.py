from __future__ import annotations

from http.client import HTTPConnection
from html.parser import HTMLParser
import hashlib
import io
import json
import tomllib
from dataclasses import replace
from pathlib import Path
import time
from hashlib import sha256
from types import MappingProxyType, SimpleNamespace

import pytest

from mrs3 import panel as panel_module
from mrs3.panel import PanelController, _DirectJob, _Job, create_panel_server
from mrs3.source_v6_importer import SourceV6WorkerFailure, source_v6_import_lock
from mrs3.config import DirectMaterializationSettings, load_direct_materialization_settings
from mrs3.duckdb_import import ImportJobResult, ImportPreflight, ImportProgress
from mrs3.duckdb_direct import (
    publish_direct_surfaces,
    DirectBuildRequest,
    CoverageIssue,
    CoverageReviewRow,
    DirectCoverage,
    DirectMaterializationError,
    DirectPreflight,
    DirectQueueResult,
    DirectScope,
    READINESS_CONTRACT_VERSION,
    READINESS_MAX_SHIFT_BP,
    V2_GRID_CONTRACT_KIND,
    _CoverageScan,
)


class _FakeProcess:
    def __init__(self, command: list[str], **_: object) -> None:
        self.command = command
        self.pid = 12345
        self.stdout = io.StringIO("started\nfinished\n")
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode


class _SynchronousThread:
    def __init__(
        self,
        target: object = None,
        args: tuple[object, ...] = (),
        kwargs: dict[str, object] | None = None,
        **_: object,
    ) -> None:
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self) -> None:
        if self.target is not None:
            self.target(*self.args, **self.kwargs)


class _PerformanceProgressProcess(_FakeProcess):
    def __init__(self, command: list[str], **kwargs: object) -> None:
        super().__init__(command, **kwargs)
        self.stdout = io.StringIO(
            "ordinary diagnostic\n"
            '{"performance_progress":{"stage":"READBACK_VERIFIED","completed":1,"total":1,"quarantined":0,"scheduled":1,"prepared":1,"imported":1,"skipped":0,"phase_seconds":{"PARSE_PREPARE":0.1}}}\n'
            '{"performance_progress":{"stage":"READBACK_VERIFIED","completed":1,"total":1,"quarantined":0,"scheduled":1,"prepared":1,"imported":1,"skipped":0,"phase_seconds":{"PARSE_PREPARE":0.1},"terminal_error":"ValueError"}}\n'
        )
        self.returncode = 1


def _wait_finished(controller: PanelController) -> dict[str, object]:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        snapshot = controller.snapshot()
        if snapshot["job"] and not snapshot["job"]["running"]:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("panel job did not finish")


def test_panel_rejects_performance_dd5_without_inbox_manifest(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    with pytest.raises(ValueError, match="inbox_manifest.json is missing"):
        PanelController(tmp_path, tmp_path / "config.json")._build_command(
            "performance-dd5",
            {"config": "config.json", "database": "performance.duckdb", "inbox": "inbox", "output_dir": "posttest"},
        )


def test_panel_rejects_performance_dd5_without_commission_contract(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "inbox_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": "batch-1",
                "expected_strategy_names": ["Demo"],
                "tester_config_sha256": "0" * 64,
                "entries": [{}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="commission contract"):
        PanelController(tmp_path, tmp_path / "config.json")._build_command(
            "performance-dd5",
            {"config": "config.json", "database": "performance.duckdb", "inbox": "inbox", "output_dir": "posttest"},
        )


def test_panel_autofills_performance_inbox_from_completed_workflow() -> None:
    html = panel_module.PANEL_HTML
    autofill = html.split("function autofillPerformanceInbox(workflow)", 1)[1]
    assert "workflow.state !== 'COMPLETED'" in autofill
    assert "workflow.inbox_path" in autofill
    assert "document.getElementById('performance_inbox')" in autofill
    assert "input.value = workflow.inbox_path" in autofill
    render = html.split("function render(data)", 1)[1]
    assert "autofillPerformanceInbox(workflow);" in render


@pytest.mark.parametrize(
    ("config_text", "expected"),
    [
        (json.dumps({"panel": {"default_root": "static"}}), "static"),
        (json.dumps({"panel": {"default_root": "legacy"}}), "legacy"),
        ("", "legacy"),
        ("{not-json", "legacy"),
        (json.dumps({"panel": {"default_root": "other"}}), "legacy"),
        (json.dumps({"panel": {"default_root": ""}}), "legacy"),
        (json.dumps({"panel": {"default_root": "STATIC"}}), "legacy"),
        (json.dumps({"panel": {"default_root": 1}}), "legacy"),
        (json.dumps({"panel": {"default_root": []}}), "legacy"),
        (json.dumps({"panel": []}), "legacy"),
        (json.dumps({"panel": 1}), "legacy"),
    ],
)
def test_panel_root_mode_uses_safe_local_config_fallback(
    tmp_path: Path, config_text: str, expected: str
) -> None:
    config = tmp_path / "config.local.json"
    if config_text:
        config.write_text(config_text, encoding="utf-8")
    controller = PanelController(tmp_path, config, browse_factory=lambda *_: ())
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200
        assert body == (
            (Path(panel_module.__file__).parent / "panel_web" / "index.html").read_bytes()
            if expected == "static"
            else panel_module.PANEL_HTML.encode("utf-8")
        )
        connection.request("GET", "/legacy")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == panel_module.PANEL_HTML.encode("utf-8")
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_panel_static_routes_and_legacy_compatibility_are_bounded(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"panel": {"default_root": "static"}}), encoding="utf-8")
    controller = PanelController(tmp_path, config, browse_factory=lambda *_: (tmp_path / "picked",))
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        for path, content_type in (
            ("/panel-web/app.css", "text/css"),
            ("/panel-web/app.js", "text/javascript"),
        ):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader("Content-Type", "").startswith(content_type)
            assert "; charset=utf-8" in response.getheader("Content-Type", "")
            asset_name = path.rsplit("/", 1)[-1]
            assert response.read() == (Path(panel_module.__file__).parent / "panel_web" / asset_name).read_bytes()
        for path in (
            "/panel-web/missing",
            "/panel-web/../panel.py",
            "/panel-web/%2e%2e/panel.py",
        ):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status >= 400
            response.read()
        connection.request("GET", "/api/v2/unknown")
        response = connection.getresponse()
        body = response.read()
        assert response.status >= 400
        assert body == b'{"error": "not found"}'
        assert panel_module.PANEL_HTML.encode("utf-8") not in body
        connection.request("GET", "/legacy")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == panel_module.PANEL_HTML.encode("utf-8")
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["defaults"]["config"] == str(config.resolve())
        connection.request("GET", "/api/ui/bootstrap")
        response = connection.getresponse()
        bootstrap = json.loads(response.read())
        assert response.status == 200
        assert "config" not in bootstrap
        assert "path" not in json.dumps(bootstrap)
        body = json.dumps({"kind": "directory", "multiple": False}).encode()
        connection.request(
            "POST",
            "/api/browse",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["paths"] == [str((tmp_path / "picked").resolve())]
        connection.request("GET", "/", headers={"Host": "evil.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
    finally:
        connection.close()
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

    service, _ = PanelController(tmp_path, config, browse_factory=lambda *_: ())._local_source_jobs()

    assert service.workers == 3
    assert service.import_options == {
        "write_batch_size": 7,
        "worker_chunk_size": 8,
        "max_in_flight_chunks": 9,
        "segment_writer_limit": 3,
        "hydrate_fragments": True,
    }


def test_static_panel_shell_contains_only_navigation_contract() -> None:
    panel_web = Path(panel_module.__file__).parent / "panel_web"
    html = (panel_web / "index.html").read_text(encoding="utf-8")
    script = (panel_web / "app.js").read_text(encoding="utf-8")
    for label in ("Testing", "Source DB", "Surfaces", "Strategies and DD5", "Settings", "Portfolio"):
        assert label in html
    for excluded in ("Artefacts", "CSV", "DUCKDB_DIRECT", "credential", "password", "token"):
        assert excluded.casefold() not in html.casefold()
        assert excluded.casefold() not in script.casefold()
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


def _import_result(tmp_path: Path, *, final_state: str = "COMMITTED", tampered: bool = False) -> ImportJobResult:
    audit = tmp_path / "audit" / "job-1"
    audit.mkdir(parents=True)
    checklist = audit / "html_delete_checklist.json"
    checklist.write_text(json.dumps({"job_id": "job-1", "safe_to_delete": "YES"}), encoding="utf-8")
    manifest = audit / "import_manifest.json"
    manifest.write_text(json.dumps({"job_id": "job-1", "final_state": final_state, "safe_to_delete": "YES", "artifacts": {"checklist": {"sha256": sha256(checklist.read_bytes()).hexdigest()}}}), encoding="utf-8")
    if tampered:
        checklist.write_text("not json", encoding="utf-8")
    return ImportJobResult("job-1", final_state, 3, 2, 1, 0, 1, 0, 0, "YES", manifest, sha256(manifest.read_bytes()).hexdigest(), checklist, sha256(checklist.read_bytes()).hexdigest())


def _wait_import_finished(controller: PanelController) -> dict[str, object]:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        document = controller.snapshot()["duckdb_import"]
        if document and not document["running"]:
            return document
        time.sleep(.01)
    raise AssertionError("panel import did not finish")


def _wait_direct_finished(controller: PanelController) -> dict[str, object]:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        document = controller.snapshot()["duckdb_direct"]
        if document and not document["running"]:
            return document
        time.sleep(.01)
    raise AssertionError("panel direct build did not finish")


def _fake_coverage_scan(tmp_path: Path) -> _CoverageScan:
    long_row = CoverageReviewRow(
        "BTCUSDT",
        "LONG",
        "1h",
        True,
        "2024-01-01T00:00:00.000+00:00",
        "2024-01-02T00:00:00.000+00:00",
        (),
    )
    short_row = CoverageReviewRow(
        "BTCUSDT",
        "SHORT",
        "1h",
        True,
        "2024-01-01T00:00:00.000+00:00",
        "2024-01-02T00:00:00.000+00:00",
        (),
    )
    coverage = DirectCoverage(
        (
            DirectScope("BTCUSDT", "LONG", "1h"),
            DirectScope("BTCUSDT", "SHORT", "1h"),
        ),
        (long_row, short_row),
        (),
    )
    inventory = tmp_path / "surface_coverage" / "coverage_inventory.csv"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_bytes(b"pair,side\n")
    return _CoverageScan(
        "coverage-token",
        coverage,
        inventory,
        hashlib.sha256(inventory.read_bytes()).hexdigest(),
        "s" * 64,
        (),
    )


def _install_fake_stage_b(monkeypatch: pytest.MonkeyPatch, controller: PanelController) -> None:
    """Keep panel unit tests source-free while exercising the Stage-B shape."""
    def freeze(_source: object, requests: tuple[DirectBuildRequest, ...], **_: object) -> tuple[tuple[DirectBuildRequest, ...], tuple[DirectPreflight, ...]]:
        bound: list[DirectBuildRequest] = []
        preflights: list[DirectPreflight] = []
        for request in requests:
            audit = b"pair,side\nfixture," + request.side.encode() + b"\n"
            request = replace(
                request,
                audit_artifact_name=f"surface_coverage_audit_{request.side}.csv",
                audit_schema_version=1,
                audit_size_bytes=len(audit),
                audit_row_count=1,
                audit_sha256=hashlib.sha256(audit).hexdigest(),
                audit_bytes=audit,
            )
            symbol, timeframe = request.selected_scopes[0].split("|", maxsplit=1)
            point = f"{symbol}|{request.side}|{timeframe}|30|3|2"
            preflights.append(DirectPreflight(
                {symbol: (timeframe,)}, {}, (),
                MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
                ("a" * 64,), (("report-1", "a" * 64),), (point,),
                tuple(controller._direct_coverage_scan.coverage.rows) if controller._direct_coverage_scan else (),
                audit_artifact_name=request.audit_artifact_name,
                audit_schema_version=1,
                audit_size_bytes=len(audit),
                audit_row_count=1,
                audit_sha256=request.audit_sha256,
                audit_bytes=audit,
            ))
            bound.append(request)
        return tuple(bound), tuple(preflights)
    monkeypatch.setattr(panel_module, "freeze_direct_preflights", freeze)

    def replay(
        source: object,
        requests: tuple[DirectBuildRequest, ...],
        _preflights: tuple[DirectPreflight, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        materialization_settings: object = None,
    ) -> tuple[object, ...]:
        return controller._direct_prepare_func(
            source,
            requests,
            audit_root=audit_root,
            coverage_scan=coverage_scan,
            cancellation=cancellation,
            progress_callback=lambda *_args, **_kwargs: None,
        )

    monkeypatch.setattr(panel_module, "replay_direct_preflights", replay)


def _wait_analysis_finished(controller: PanelController) -> dict[str, object]:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        document = controller.snapshot()["analysis"]
        if document and not document["running"]:
            return document
        time.sleep(.01)
    raise AssertionError("panel analysis did not finish")


def test_analysis_library_rerun_compare_and_export_use_only_analysis_database(tmp_path: Path) -> None:
    opened: list[tuple[str, bool]] = []
    calls: list[object] = []

    class Connection:
        def close(self) -> None: pass

    def connect(path: str, *, read_only: bool) -> Connection:
        opened.append((Path(path).name, read_only)); return Connection()

    class Points:
        def __getitem__(self, _key: str) -> object:
            return SimpleNamespace(iloc=["LONG"])

    points = Points()
    pipeline_input = SimpleNamespace(surface_id="surface-1", points=points)
    pipeline_result = SimpleNamespace(surface_id="surface-1", statistics={
        "unique_point_count": 12, "economic_eligible_point_count": 10,
        "event_eligible_point_count": 8, "plateau_count": 3,
        "ready_candidate_count": 2,
    })

    controller = PanelController(
        tmp_path, tmp_path / "config.local.json",
        direct_connection_factory=connect,
        analysis_library_func=lambda _connection, **filters: calls.append(("library", filters)) or ({"surface_id": "surface-1", "runs": ()},),
        analysis_compare_func=lambda _connection, left, right: calls.append(("compare", left, right)) or {"left": {"run_id": left}, "right": {"run_id": right}},
        analysis_load_func=lambda _connection, surface_id: calls.append(("load", surface_id)) or pipeline_input,
        analysis_run_func=lambda loaded, dates, side, config, comparison_run_id=None: calls.append(("run", loaded.surface_id, side.value, comparison_run_id)) or pipeline_result,
        analysis_publish_func=lambda _connection, result: calls.append(("publish", result.surface_id)) or SimpleNamespace(run_id="run-2", surface_id=result.surface_id),
        analysis_export_func=lambda _connection, run_id, output: calls.append(("export", run_id, Path(output).name)) or SimpleNamespace(output_path=Path(output), manifest_path=Path(output) / "manifest.json", run_id=run_id, surface_id="surface-1", row_counts={"surface_points": 12}),
        analysis_config_loader=lambda path: calls.append(("config", Path(path).name)) or object(),
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})

    assert controller.analysis_library({"side": "LONG", "symbol": "BTCUSDT"})[0]["surface_id"] == "surface-1"
    comparison = controller.compare_analysis({"left_run_id": "run-1", "right_run_id": "run-2"})
    assert comparison == {"left": {"run_id": "run-1"}, "right": {"run_id": "run-2"}}
    exported = controller.export_analysis({"run_id": "run-2", "output_path": str(tmp_path / "export")})
    assert exported["manifest"] == "manifest.json"
    controller.start_analysis_rerun({"surface_id": "surface-1", "dates_path": "dates.csv", "config_path": "config.json", "comparison_run_id": "run-1"})
    status = _wait_analysis_finished(controller)
    assert status["surface_id"] == "surface-1" and status["run_id"] == "run-2"
    assert status["statistics"]["ready_candidate_count"] == 2
    assert opened == [
        ("analysis.duckdb", True), ("analysis.duckdb", True),
        ("analysis.duckdb", True), ("analysis.duckdb", False),
    ]
    assert all(name != "source.duckdb" for name, _ in opened)


def test_analysis_refine_validates_and_passes_explicit_parent_before_source_work(tmp_path: Path) -> None:
    opened: list[tuple[str, bool]] = []
    parents: list[str | None] = []

    class Connection:
        def __init__(self, analysis: bool) -> None: self.analysis = analysis
        def execute(self, _query: str, values: object = None) -> object:
            return SimpleNamespace(fetchone=lambda: (1,) if values == ["surface-1"] else None)
        def close(self) -> None: pass

    def connect(path: str, *, read_only: bool) -> Connection:
        opened.append((Path(path).name, read_only)); return Connection(Path(path).name == "analysis.duckdb")

    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",),
    )
    def build(*_args: object, parent_surface_id: str | None = None) -> object:
        parents.append(parent_surface_id); return SimpleNamespace(surface_id="surface-2", points=(object(),))

    controller = PanelController(tmp_path, tmp_path / "config.local.json", direct_connection_factory=connect, direct_preflight_func=lambda *_: preflight, direct_build_func=build)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    token = controller.duckdb_direct_preflight(payload)["token"]
    with pytest.raises(ValueError, match="latest preflight token is required"):
        controller.start_duckdb_direct({**payload, "preflight_token": token, "parent_surface_id": "surface-1"})
    assert opened == [("source.duckdb", True)]
    assert parents == []


def test_analysis_library_ui_and_routes_are_exposed() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    for marker in ("Analysis Library", "analysis_side", "analysis_symbol", "analysis_surface_id", "analysis_run_id", "analysis_dates", "analysis_config", "analysis_output", "analysis_unique", "analysis_economic", "analysis_event", "analysis_plateaus", "analysis_ready", "analysisStatus"):
        assert marker in html
    for endpoint in ("/api/analysis/initialize", "/api/analysis/library", "/api/analysis/rerun", "/api/analysis/compare", "/api/analysis/export"):
        assert endpoint in html or endpoint in __import__("mrs3.panel", fromlist=["_PanelHandler"])._PanelHandler.do_POST.__code__.co_consts


def test_direct_coverage_review_ui_is_exposed_in_right_panel() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    assert 'id="coverageReview"' in html
    assert "DuckDB coverage review" in html
    assert "function renderDirectCoverageReview" in html
    assert 'name="direct_scope"' in html or "direct_scope" in html
    assert "/api/duckdb-direct/coverage" in html
    assert "const flight=await duckdbRequest('/api/duckdb-direct/preflight',{...base,coverage_token:directPreflightToken,selected_scopes:scopes})" in html
    assert "symbols:selectedSymbols" in html
    assert 'id="progressBar"' in html
    assert 'id="logs"' in html


def test_direct_coverage_scan_returns_both_sides_token_and_inventory_artifact(tmp_path: Path) -> None:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)

    def scan_func(
        _connection: object, *, audit_root: object, symbols: tuple[str, ...]
    ) -> _CoverageScan:
        assert symbols == ()
        return scan

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=scan_func,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})

    document = controller.duckdb_direct_coverage({"symbols": []})

    assert [row["side"] for row in document["coverage_rows"]] == ["LONG", "SHORT"]
    assert document["token"] == "coverage-token"
    assert document["artifacts"] == {"coverage_inventory": "coverage_inventory.csv"}
    assert controller.artifact("coverage_inventory") == ("coverage_inventory.csv", scan.inventory_path.read_bytes())


def test_panel_rejects_stale_coverage_token(tmp_path: Path) -> None:
    class Connection:
        def close(self) -> None:
            pass

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: _fake_coverage_scan(tmp_path),
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})

    with pytest.raises(ValueError, match="stale coverage token"):
        controller.start_duckdb_direct(
            {
                "coverage_token": "stale",
                "selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}],
            }
        )


def test_stage_b_does_not_install_when_coverage_scan_changes_during_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: _fake_coverage_scan(tmp_path),
    )
    controller.duckdb_import_settings({
        "source_duckdb_path": "source.duckdb",
        "analysis_duckdb_path": "analysis.duckdb",
        "audit_root": "audit",
    })
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)
    frozen = panel_module.freeze_direct_preflights

    def freeze_and_replace_scan(*args: object, **kwargs: object) -> object:
        result = frozen(*args, **kwargs)
        assert controller._direct_coverage_scan is not None
        controller._direct_coverage_scan = replace(controller._direct_coverage_scan, token="changed")
        return result

    monkeypatch.setattr(panel_module, "freeze_direct_preflights", freeze_and_replace_scan)
    payload = {
        "coverage_token": "coverage-token",
        "selected_scopes": [
            {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"},
            {"symbol": "BTCUSDT", "side": "SHORT", "timeframe": "1h"},
        ],
    }

    with pytest.raises(ValueError, match="stale coverage token"):
        controller.duckdb_direct_preflight(payload)

    assert controller._direct_selected_preflight is None
    assert controller._direct_preflight is None


def test_selected_common_intervals_are_returned_as_dates_before_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def close(self) -> None:
            pass

    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (),
        MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",),
    )
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: _fake_coverage_scan(tmp_path),
        direct_preflight_func=lambda *_args, **_kwargs: preflight,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)
    payload = {
        "coverage_token": "coverage-token",
        "start_utc": "2024-01-01T00:00:00Z",
        "end_utc": "2024-01-02T00:00:00Z",
        "side": "LONG",
        "symbols": ["BTCUSDT"],
        "required_shifts_bp": [100],
        "selected_scopes": [
            {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"},
            {"symbol": "BTCUSDT", "side": "SHORT", "timeframe": "1h"},
        ],
    }

    document = controller.duckdb_direct_preflight(payload)
    intervals = document["selected_intervals"]

    assert set(intervals) == {"long", "short"}
    assert intervals["long"]["start_date"] == "2024-01-01"
    assert intervals["long"]["end_date"] == "2024-01-02"
    assert intervals["long"]["display"] == "2024-01-01 .. 2024-01-02"
    assert intervals["short"]["start_date"] == "2024-01-01"
    assert document["token"] != "coverage-token"
    assert document["preflight_token"] == document["token"]


def test_direct_start_derives_one_common_interval_per_side(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def close(self) -> None:
            pass

    captured: list[object] = []
    scan = _fake_coverage_scan(tmp_path)

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        progress_callback: object,
    ) -> tuple[object, ...]:
        captured.extend(requests)
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=""))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        cancellation: object,
        progress_callback: object,
        parent_surface_id: str | None = None,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(
                SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=())
                for surface in surfaces
            ),
        )

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)
    payload = {
        "coverage_token": "coverage-token",
        "selected_scopes": [
            {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"},
            {"symbol": "BTCUSDT", "side": "SHORT", "timeframe": "1h"},
        ],
    }

    token = controller.duckdb_direct_preflight(payload)["preflight_token"]
    controller.start_duckdb_direct({"preflight_token": token, "selected_scopes": payload["selected_scopes"]})
    status = _wait_direct_finished(controller)

    assert status["publication_state"] == "PUBLISHED"
    assert len(captured) == 2
    assert [getattr(request, "side") for request in captured] == ["LONG", "SHORT"]
    assert [getattr(request, "start_utc") for request in captured] == [
        "2024-01-01T00:00:00.000+00:00",
        "2024-01-01T00:00:00.000+00:00",
    ]
    assert [getattr(request, "end_utc") for request in captured] == [
        "2024-01-02T00:00:00.000+00:00",
        "2024-01-02T00:00:00.000+00:00",
    ]


def test_preview_contract_matches_direct_start_requests_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)
    captured: list[DirectBuildRequest] = []
    published: list[object] = []

    def prepare(
        _source: object,
        requests: tuple[DirectBuildRequest, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        progress_callback: object,
    ) -> tuple[object, ...]:
        captured.extend(requests)
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=''))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        cancellation: object,
        progress_callback: object,
        parent_surface_id: str | None = None,
    ) -> DirectQueueResult:
        published.append(True)
        return DirectQueueResult(
            'PUBLISHED',
            tuple(
                SimpleNamespace(surface_id=f'surface-{surface.request.side}', points=())
                for surface in surfaces
            ),
        )

    monkeypatch.setattr('mrs3.panel.threading.Thread', _SynchronousThread)
    controller = PanelController(
        tmp_path,
        tmp_path / 'config.local.json',
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings({'source_duckdb_path': 'source.duckdb', 'analysis_duckdb_path': 'analysis.duckdb', 'audit_root': 'audit'})
    controller.duckdb_direct_coverage({'symbols': []})
    _install_fake_stage_b(monkeypatch, controller)
    payload = {
        'coverage_token': 'coverage-token',
        'selected_scopes': [
            {'symbol': 'BTCUSDT', 'side': 'LONG', 'timeframe': '1h'},
            {'symbol': 'BTCUSDT', 'side': 'SHORT', 'timeframe': '1h'},
        ],
    }

    preview = controller.duckdb_direct_preflight(payload)
    preview["preflight_token"] = preview["token"]
    controller.start_duckdb_direct({**payload, "coverage_token": None, "preflight_token": preview["preflight_token"]})
    status = controller.snapshot()['duckdb_direct']

    assert status['publication_state'] == 'PUBLISHED'
    assert published == [True]
    assert len(captured) == 2
    for side in ('LONG', 'SHORT'):
        interval = preview['selected_intervals'][side.lower()]
        request = next(item for item in captured if item.side == side)
        assert request.start_utc == interval['start_utc']
        assert request.end_utc == interval['end_utc']
        assert request.selected_scopes == ('BTCUSDT|1h',)
        assert request.symbols == ('BTCUSDT',)
        assert request.grid_contract_kind == V2_GRID_CONTRACT_KIND
        assert request.readiness_contract_version == READINESS_CONTRACT_VERSION
        assert request.readiness_max_shift_bp == READINESS_MAX_SHIFT_BP


def test_direct_prepare_failure_never_calls_publish_and_snapshot_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)
    published: list[object] = []

    def prepare(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise DirectMaterializationError('active coverage scan changed after preflight')

    def publish(*_args: object, **_kwargs: object) -> DirectQueueResult:
        published.append(True)
        return DirectQueueResult('PUBLISHED', ())

    monkeypatch.setattr('mrs3.panel.threading.Thread', _SynchronousThread)
    controller = PanelController(
        tmp_path,
        tmp_path / 'config.local.json',
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings({'source_duckdb_path': 'source.duckdb', 'analysis_duckdb_path': 'analysis.duckdb', 'audit_root': 'audit'})
    controller.duckdb_direct_coverage({'symbols': []})
    _install_fake_stage_b(monkeypatch, controller)
    payload = {
        'coverage_token': 'coverage-token',
        'selected_scopes': [
            {'symbol': 'BTCUSDT', 'side': 'LONG', 'timeframe': '1h'},
            {'symbol': 'BTCUSDT', 'side': 'SHORT', 'timeframe': '1h'},
        ],
    }

    token = controller.duckdb_direct_preflight(payload)["preflight_token"]
    controller.start_duckdb_direct({"preflight_token": token, "selected_scopes": payload["selected_scopes"]})
    status = controller.snapshot()['duckdb_direct']

    assert status['publication_state'] == 'FAILED'
    assert status['phase'] == 'FAILED'
    assert status['surface_id'] is None
    assert status['error'] == 'active coverage scan changed after preflight'
    assert published == []


def test_malformed_frozen_v2_preflight_never_falls_back_to_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    scan = _fake_coverage_scan(tmp_path)
    request = DirectBuildRequest(
        "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z", "LONG", ("BTCUSDT",),
        (100,), "v1", "a" * 64, grid_contract_kind=V2_GRID_CONTRACT_KIND,
    )
    malformed = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        (), (), (),
    )
    prepared: list[object] = []

    def prepare(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        prepared.append(True)
        return ()

    controller._direct_prepare_func = prepare
    monkeypatch.setattr(
        panel_module,
        "replay_direct_preflights",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DirectMaterializationError("STALE_PREFLIGHT")),
    )
    job = _DirectJob(
        requests=(request,),
        coverage_scan=scan,
        audit_root=tmp_path / "audit",
        frozen_preflights=(malformed,),
    )

    controller._run_duckdb_direct(job)

    assert prepared == []
    assert job.publication_state == "FAILED"
    assert job.error == "STALE_PREFLIGHT"


def _run_direct_coverage_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prepare: object = None,
    publish: object = None,
    sides: tuple[str, ...] = ("LONG",),
) -> dict[str, object]:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)

    def default_prepare(
        _source: object,
        requests: tuple[object, ...],
        **_kwargs: object,
    ) -> tuple[object, ...]:
        return tuple(
            SimpleNamespace(
                request=request,
                points=(),
                preflight=SimpleNamespace(audit_sha256=""),
            )
            for request in requests
        )

    def default_publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        **_kwargs: object,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(
                SimpleNamespace(
                    surface_id=f"surface-{surface.request.side}",
                    points=(),
                )
                for surface in surfaces
            ),
        )

    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare or default_prepare,
        direct_publish_func=publish or default_publish,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)
    selected_payload = {
        "selected_scopes": [
            {"symbol": "BTCUSDT", "side": side, "timeframe": "1h"}
            for side in sides
        ],
    }
    token = controller.duckdb_direct_preflight({"coverage_token": "coverage-token", **selected_payload})["preflight_token"]
    controller.start_duckdb_direct(
        {
            "preflight_token": token,
            **selected_payload,
        }
    )
    return controller.snapshot()["duckdb_direct"]


def test_direct_job_has_small_typed_side_ordinal_total_defaults() -> None:
    job = _DirectJob()

    assert job.side is None
    assert job.ordinal == 0
    assert job.total == 0
    assert isinstance(job.ordinal, int)
    assert isinstance(job.total, int)


def test_direct_coverage_job_progress_side_ordinal_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def prepare(
        _source: object,
        requests: tuple[object, ...],
        *,
        progress_callback: object,
        **_kwargs: object,
    ) -> tuple[object, ...]:
        progress_callback("PREPARING_LONG", side="LONG", ordinal=1, total=2)
        progress_callback("PREPARED_LONG", side="LONG", ordinal=1, total=2, materialized_points=3)
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=""))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        progress_callback: object,
        **_kwargs: object,
    ) -> DirectQueueResult:
        progress_callback("PUBLISHING_SHORT", side="SHORT", ordinal=2, total=2)
        progress_callback("PUBLISHED_SHORT", side="SHORT", ordinal=2, total=2, materialized_points=5)
        return DirectQueueResult(
            "PUBLISHED",
            tuple(
                SimpleNamespace(
                    surface_id=f"surface-{surface.request.side}",
                    points=(object(), object(), object(), object(), object()),
                )
                for surface in surfaces
            ),
        )

    status = _run_direct_coverage_job(
        tmp_path, monkeypatch, prepare=prepare, publish=publish, sides=("LONG", "SHORT")
    )

    assert status["side"] == "SHORT"
    assert status["ordinal"] == 2
    assert status["total"] == 2
    assert status["point_count"] == 10
    assert status["phase"] == "PUBLISHED"
@pytest.mark.parametrize(
    ("publication_state", "phase", "error"),
    [
        ("FAILED", "FAILED", "selected scope is unavailable"),
        ("PARTIAL", "PARTIAL", "SHORT publication failed"),
    ],
)
def test_direct_queue_result_publication_state_and_error_are_copied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_state: str,
    phase: str,
    error: str,
) -> None:
    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        **_kwargs: object,
    ) -> DirectQueueResult:
        published = () if publication_state == "FAILED" else tuple(
            SimpleNamespace(surface_id="surface-LONG", points=())
            for _ in surfaces
        )
        return DirectQueueResult(publication_state, published, phase=phase, error=error)

    status = _run_direct_coverage_job(tmp_path, monkeypatch, publish=publish)

    assert status["publication_state"] == publication_state
    assert status["phase"] == phase
    assert status["error"] == error
    if publication_state == "PARTIAL":
        assert status["surface_id"] == "surface-LONG"
    else:
        assert status["surface_id"] is None


@pytest.mark.parametrize("raw_error", [
    "path=/tmp/private.duckdb",
    "failed;/tmp/private.duckdb",
    "db:/home/bob/private.duckdb",
    "source{/var/private.db}",
    r"failed near \\server\share\private.duckdb",
    r"failed near \Users\alice\private.duckdb",
    r"source=C:\Users\alice\private.duckdb",
])
def test_direct_queue_result_error_with_local_path_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw_error: str
) -> None:
    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        **_kwargs: object,
    ) -> DirectQueueResult:
        return DirectQueueResult("FAILED", (), phase="FAILED", error=raw_error)

    status = _run_direct_coverage_job(tmp_path, monkeypatch, publish=publish)

    assert status["publication_state"] == "FAILED"
    assert status["phase"] == "FAILED"
    assert status["error"] == "direct build failed"
    assert raw_error not in json.dumps(status)
    assert raw_error not in panel_module.PANEL_HTML


def test_direct_publish_unexpected_error_is_generic_in_panel_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_secret(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("secret internal state")

    monkeypatch.setattr("mrs3.duckdb_direct.publish_surface", raise_secret)
    status = _run_direct_coverage_job(
        tmp_path,
        monkeypatch,
        publish=publish_direct_surfaces,
    )

    assert status["publication_state"] == "FAILED"
    assert status["phase"] == "FAILED"
    assert status["error"] == "V2 preflight contract is required"
    payload = json.dumps(status)
    assert "secret internal state" not in payload
    assert "RuntimeError" not in payload


def test_direct_error_preserves_non_absolute_slash_text() -> None:
    assert panel_module._safe_direct_error("report/grid mismatch") == "report/grid mismatch"


@pytest.mark.parametrize("prepare_error", [
    RuntimeError("unexpected boom near " + r"C:\Users\alice\secrets\source.duckdb" + " and /home/bob/secrets/analysis.duckdb"),
    DirectMaterializationError("failed near " + r"C:\Users\alice\secrets\source.duckdb" + " and /home/bob/secrets/analysis.duckdb"),
])
def test_direct_prepare_unexpected_or_controlled_path_error_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepare_error: BaseException
) -> None:
    windows_path = r"C:\Users\alice\secrets\source.duckdb"
    posix_path = "/home/bob/secrets/analysis.duckdb"

    def prepare(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise prepare_error

    status = _run_direct_coverage_job(tmp_path, monkeypatch, prepare=prepare)

    assert status["publication_state"] == "FAILED"
    assert status["phase"] == "FAILED"
    assert status["error"] == "direct build failed"
    payload = json.dumps(status)
    assert windows_path not in payload
    assert posix_path not in payload
    if isinstance(prepare_error, RuntimeError):
        assert "boom" not in payload
        assert "RuntimeError" not in payload
    assert windows_path not in panel_module.PANEL_HTML
    assert posix_path not in panel_module.PANEL_HTML


def test_direct_status_renders_publication_state_error_and_side_coordinate() -> None:
    html = panel_module.PANEL_HTML
    direct_status = html.split("const direct = data.duckdb_direct;", 1)[1]
    direct_render = html.split("function render(data)", 1)[1].split("const job = data.job;", 1)[0]

    assert direct_render.count("document.getElementById('directStatus').textContent =") == 1
    assert "direct.side" in direct_status
    assert "direct.ordinal" in direct_status
    assert "direct.total" in direct_status
    assert "direct.publication_state" in direct_status
    assert "direct.error" in direct_status
    assert "${direct.ordinal}/${direct.total}" in direct_status


def test_direct_status_keeps_existing_progress_scale_unchanged() -> None:
    html = panel_module.PANEL_HTML

    assert "document.getElementById('barFill').style.width = percent + '%';" in html
    assert "document.getElementById('progressBar').setAttribute('aria-valuenow', String(Math.round(percent)));" in html
    assert "directProgress" not in html
    assert "directBar" not in html


def test_v2_selected_preflight_ignores_legacy_window_and_returns_distinct_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)

    def legacy_preflight(*_args: object, **_kwargs: object) -> DirectPreflight:
        raise AssertionError("legacy preflight must not run")

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_preflight_func=legacy_preflight,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)

    document = controller.duckdb_direct_preflight(
        {
            "coverage_token": "coverage-token",
            "selected_scopes": [{"symbol": "BTCUSDT", "side": "SHORT", "timeframe": "1h"}],
            "start_utc": "",
            "end_utc": "",
            "side": "LONG",
            "symbols": ["BTCUSDT", "ETHUSDT"],
        }
    )

    assert document["token"] != "coverage-token"
    assert document["preflight_token"] == document["token"]
    assert set(document["selected_intervals"]) == {"short"}
    assert document["selected_intervals"]["short"]["start_date"] == "2024-01-01"
    assert document["selected_intervals"]["short"]["end_date"] == "2024-01-02"


def test_selected_preflight_requires_explicit_coverage_token(tmp_path: Path) -> None:
    class Connection:
        def close(self) -> None:
            pass

    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (),
        MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",),
    )
    legacy_calls: list[object] = []

    def legacy_preflight(_source: object, request: object) -> DirectPreflight:
        legacy_calls.append(request)
        return preflight

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: _fake_coverage_scan(tmp_path),
        direct_preflight_func=legacy_preflight,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})

    with pytest.raises(ValueError, match="selected scopes require a valid coverage token"):
        controller.duckdb_direct_preflight(
            {
                "start_utc": "2024-01-01T00:00:00Z",
                "end_utc": "2024-01-02T00:00:00Z",
                "side": "LONG",
                "symbols": ["BTCUSDT"],
                "required_shifts_bp": [100],
                "selected_scopes": [{"symbol": "BTCUSDT", "timeframe": "1h"}],
            }
        )
    assert legacy_calls == []


def test_selected_start_requires_live_selected_preflight_state(tmp_path: Path) -> None:
    class Connection:
        def close(self) -> None:
            pass

    preflight = DirectPreflight(
        usable_timeframes={"BTCUSDT": ("1h",)},
        unavailable_symbols={},
        coverage_issues=(),
        grid_contract=MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        source_hashes=("a" * 64,),
        manifest=(("report-1", "a" * 64),),
        accepted_point_keys=("BTCUSDT|LONG|1h|100|3|9",),
        coverage_rows=(
            CoverageReviewRow(
                "BTCUSDT",
                "LONG",
                "1h",
                True,
                "2024-01-01T00:00:00.000+00:00",
                "2024-01-02T00:00:00.000+00:00",
                (),
            ),
        ),
    )

    def build(
        _source: object,
        _analysis: object,
        _request: object,
        _cancellation: object,
        progress: object,
    ) -> object:
        progress("PUBLISHED", materialized_points=1)
        return SimpleNamespace(surface_id="surface-1", points=(object(),))

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: _fake_coverage_scan(tmp_path),
        direct_preflight_func=lambda *_args, **_kwargs: preflight,
        direct_build_func=build,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})
    payload = {
        "start_utc": "2024-01-01T00:00:00Z",
        "end_utc": "2024-01-02T00:00:00Z",
        "side": "LONG",
        "symbols": ["BTCUSDT"],
        "required_shifts_bp": [100],
        "selected_scopes": [{"symbol": "BTCUSDT", "timeframe": "1h"}],
    }
    controller._direct_preflight = (
        PanelController._direct_request(payload),
        preflight,
        "coverage-token",
    )

    with pytest.raises(ValueError, match="latest preflight token"):
        controller.start_duckdb_direct({**payload, "preflight_token": "coverage-token"})


def test_v2_selected_start_rejects_scope_without_explicit_side(tmp_path: Path) -> None:
    class Connection:
        def close(self) -> None:
            pass

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: _fake_coverage_scan(tmp_path),
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})

    with pytest.raises(ValueError, match="coverage token.*cannot start"):
        controller.start_duckdb_direct(
            {
                "coverage_token": "coverage-token",
                "selected_scopes": [{"symbol": "BTCUSDT", "timeframe": "1h"}],
                "start_utc": "",
                "end_utc": "",
                "side": "LONG",
                "symbols": ["BTCUSDT"],
            }
        )


def test_v2_selected_start_short_only_ignores_legacy_side_and_blank_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def close(self) -> None:
            pass

    captured: list[object] = []
    scan = _fake_coverage_scan(tmp_path)

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        progress_callback: object,
    ) -> tuple[object, ...]:
        captured.extend(requests)
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=""))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        cancellation: object,
        progress_callback: object,
        parent_surface_id: str | None = None,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=()) for surface in surfaces),
        )

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)

    selected_payload = {
        "selected_scopes": [{"symbol": "BTCUSDT", "side": "SHORT", "timeframe": "1h"}],
    }
    token = controller.duckdb_direct_preflight({"coverage_token": "coverage-token", **selected_payload})["preflight_token"]
    controller.start_duckdb_direct(
        {
            "preflight_token": token,
            **selected_payload,
            "start_utc": "",
            "end_utc": "",
            "side": "LONG",
            "symbols": ["BTCUSDT", "ETHUSDT"],
        }
    )
    status = _wait_direct_finished(controller)

    assert status["publication_state"] == "PUBLISHED"
    assert len(captured) == 1
    assert captured[0].side == "SHORT"
    assert captured[0].start_utc == "2024-01-01T00:00:00.000+00:00"
    assert captured[0].end_utc == "2024-01-02T00:00:00.000+00:00"
    assert captured[0].symbols == ("BTCUSDT",)


def test_direct_selected_start_forwards_local_materialization_settings_to_replay_and_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"direct_materialization": {"workers": 7}}), encoding="utf-8")

    scan = _fake_coverage_scan(tmp_path)
    replay_settings: list[object] = []
    prepare_settings: list[object] = []

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        progress_callback: object,
        materialization_settings: object,
    ) -> tuple[object, ...]:
        prepare_settings.append(materialization_settings)
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=""))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        cancellation: object,
        progress_callback: object,
        parent_surface_id: str | None = None,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=()) for surface in surfaces),
        )

    def replay(
        _source: object,
        requests: tuple[object, ...],
        _preflights: tuple[object, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        materialization_settings: object,
    ) -> tuple[object, ...]:
        replay_settings.append(materialization_settings)
        return prepare(
            _source,
            requests,
            audit_root=audit_root,
            coverage_scan=coverage_scan,
            cancellation=cancellation,
            progress_callback=lambda *_args, **_kwargs: None,
            materialization_settings=materialization_settings,
        )

    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path,
        config,
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)
    monkeypatch.setattr(panel_module, "replay_direct_preflights", replay)
    payload = {
        "coverage_token": "coverage-token",
        "selected_scopes": [
            {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"},
            {"symbol": "BTCUSDT", "side": "SHORT", "timeframe": "1h"},
        ],
    }

    token = controller.duckdb_direct_preflight(payload)["preflight_token"]
    controller.start_duckdb_direct({"preflight_token": token, "selected_scopes": payload["selected_scopes"]})
    status = controller.snapshot()["duckdb_direct"]

    assert status["publication_state"] == "PUBLISHED"
    assert replay_settings == [DirectMaterializationSettings(workers=7)]
    assert prepare_settings == [DirectMaterializationSettings(workers=7)]


def test_run_duckdb_direct_normal_prepare_uses_job_settings_and_retries_legacy_callable(
    tmp_path: Path,
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"direct_materialization": {"workers": 7}}), encoding="utf-8")
    settings = load_direct_materialization_settings(config)

    calls: list[dict[str, object]] = []

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        **kwargs: object,
    ) -> tuple[object, ...]:
        calls.append(dict(kwargs))
        if "materialization_settings" in kwargs:
            message = "prepare() got an unexpected keyword argument " + "'materialization_settings'"
            raise TypeError(message)
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=""))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        cancellation: object,
        progress_callback: object,
        parent_surface_id: str | None = None,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=()) for surface in surfaces),
        )

    controller = PanelController(
        tmp_path,
        config,
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    request = DirectBuildRequest(
        "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z", "LONG", ("BTCUSDT",), (100,), "v1", "a" * 64,
    )
    job = _DirectJob(
        requests=(request,),
        coverage_scan=_fake_coverage_scan(tmp_path),
        audit_root=tmp_path / "audit",
        materialization_settings=settings,
    )
    controller._run_duckdb_direct(job)

    assert job.publication_state == "PUBLISHED"
    assert len(calls) == 2
    assert calls[0]["materialization_settings"].workers == 7
    assert "materialization_settings" not in calls[1]


def test_direct_snapshot_exposes_materialization_telemetry(tmp_path: Path) -> None:
    class Connection:
        def close(self) -> None:
            pass

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        *,
        progress_callback: object,
        **_kwargs: object,
    ) -> tuple[object, ...]:
        progress_callback(
            "MATERIALIZING",
            materialized_points=3,
            total_points=5,
            workers=4,
            elapsed_seconds=1.5,
            points_per_second=2.0,
        )
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=""))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        progress_callback: object,
        **_kwargs: object,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(
                SimpleNamespace(
                    surface_id=f"surface-{surface.request.side}",
                    points=(object(), object(), object()),
                )
                for surface in surfaces
            ),
        )

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    request = DirectBuildRequest(
        "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z", "LONG", ("BTCUSDT",), (100,), "v1", "a" * 64,
    )
    job = _DirectJob(
        requests=(request,),
        coverage_scan=_fake_coverage_scan(tmp_path),
        audit_root=tmp_path / "audit",
    )
    controller._direct_job = job
    controller._run_duckdb_direct(job)

    status = controller.snapshot()["duckdb_direct"]
    assert status["workers"] == 4
    assert status["elapsed_seconds"] == 1.5
    assert status["points_per_second"] == 2.0
    assert status["total_points"] == 5
    assert status["point_count"] == 3
    assert status["phase"] == "PUBLISHED"


def test_dual_side_replay_progress_is_side_aware_and_globally_coherent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    long_manifest = (("long-1", "a" * 64), ("long-2", "a" * 64))
    short_manifest = (("short-1", "a" * 64), ("short-2", "a" * 64), ("short-3", "a" * 64))
    long_preflight = DirectPreflight(
        {}, {}, (), MappingProxyType({"kind": V2_GRID_CONTRACT_KIND}),
        ("a" * 64,), long_manifest, ("LONG|1", "LONG|2"),
    )
    short_preflight = DirectPreflight(
        {}, {}, (), MappingProxyType({"kind": V2_GRID_CONTRACT_KIND}),
        ("a" * 64,), short_manifest, ("SHORT|1", "SHORT|2", "SHORT|3"),
    )

    observed_counts: list[int] = []
    manifests = {"LONG": long_manifest, "SHORT": short_manifest}

    def replay(
        _source: object,
        requests: tuple[object, ...],
        _preflights: object,
        *,
        progress_callback: object,
        **_kwargs: object,
    ) -> tuple[object, ...]:
        for request in requests:
            side = request.side
            local_total = len(manifests[side])
            for completed in range(1, local_total + 1):
                progress_callback(
                    "MATERIALIZING",
                    side=side,
                    materialized_points=completed,
                    total_points=local_total,
                    workers=4,
                    elapsed_seconds=1.0,
                    points_per_second=2.0,
                )
                observed_counts.append(controller._direct_job.point_count)
        return tuple(
            SimpleNamespace(
                request=request,
                points=tuple(object() for _ in range(len(manifests[request.side]))),
                preflight=SimpleNamespace(audit_sha256=""),
            )
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        progress_callback: object,
        **_kwargs: object,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(
                SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=surface.points)
                for surface in surfaces
            ),
        )

    monkeypatch.setattr(panel_module, "replay_direct_preflights", replay)
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    long_request = DirectBuildRequest(
        "2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z", "LONG", ("BTCUSDT",), (100,), "v1", "a" * 64,
        grid_contract_kind=V2_GRID_CONTRACT_KIND,
    )
    short_request = replace(long_request, side="SHORT")
    job = _DirectJob(
        requests=(long_request, short_request),
        frozen_preflights=(long_preflight, short_preflight),
        coverage_scan=_fake_coverage_scan(tmp_path),
        audit_root=tmp_path / "audit",
    )
    controller._direct_job = job
    controller._run_duckdb_direct(job)

    status = controller.snapshot()["duckdb_direct"]
    assert observed_counts == [1, 2, 3, 4, 5]
    assert status["point_count"] == 5
    assert status["total_points"] == 5
    assert status["side"] == "SHORT"
    assert status["workers"] == 4
    assert status["elapsed_seconds"] == 1.0
    assert status["points_per_second"] == 2.0
    assert status["phase"] == "PUBLISHED"


def test_direct_csv_artifacts_serve_immutable_bytes_after_backing_file_mutation_or_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)
    inventory_bytes = scan.inventory_path.read_bytes()
    audit_bytes = {"LONG": b"long,audit\\n", "SHORT": b"short,audit\\n"}
    audit_paths: dict[str, Path] = {}

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        progress_callback: object,
    ) -> tuple[object, ...]:
        prepared: list[object] = []
        for request in requests:
            side = request.side
            data = audit_bytes[side]
            audit_sha = sha256(data).hexdigest()
            filename = f"surface_coverage_audit_{side}.csv"
            path = Path(audit_root) / "surface_coverage" / audit_sha / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            audit_paths[f"surface_coverage_audit_{side.lower()}"] = path
            prepared.append(
                SimpleNamespace(
                    request=request,
                    points=(),
                    preflight=SimpleNamespace(audit_sha256=audit_sha, audit_bytes=data, audit_artifact_name=filename),
                )
            )
        return tuple(prepared)

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        cancellation: object,
        progress_callback: object,
        parent_surface_id: str | None = None,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=()) for surface in surfaces),
        )

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"})
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)
    selected_payload = {
        "selected_scopes": [
            {"symbol": "BTCUSDT", "side": side, "timeframe": "1h"}
            for side in ("LONG", "SHORT")
        ],
    }
    token = controller.duckdb_direct_preflight({"coverage_token": "coverage-token", **selected_payload})["preflight_token"]
    controller.start_duckdb_direct(
        {
            "preflight_token": token,
            **selected_payload,
        }
    )
    assert _wait_direct_finished(controller)["publication_state"] == "PUBLISHED"

    scan.inventory_path.write_bytes(b"tampered")
    for path in audit_paths.values():
        path.unlink()

    status = controller.snapshot()["duckdb_direct"]
    assert status["artifacts"] == {
        "coverage_inventory": "coverage_inventory.csv",
        "surface_coverage_audit_long": "surface_coverage_audit_LONG.csv",
        "surface_coverage_audit_short": "surface_coverage_audit_SHORT.csv",
    }
    assert controller.artifact("coverage_inventory") == ("coverage_inventory.csv", inventory_bytes)
    assert controller.artifact("surface_coverage_audit_long") == ("surface_coverage_audit_LONG.csv", audit_bytes["LONG"])
    assert controller.artifact("surface_coverage_audit_short") == ("surface_coverage_audit_SHORT.csv", audit_bytes["SHORT"])

    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        expected = [
            ("coverage_inventory", inventory_bytes),
            ("surface_coverage_audit_long", audit_bytes["LONG"]),
            ("surface_coverage_audit_short", audit_bytes["SHORT"]),
        ]
        for name, data in expected:
            connection.request("GET", f"/api/artifact?name={name}")
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == data
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_panel_root_mode_can_be_forced_to_static_for_the_dedicated_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"panel": {"default_root": "legacy"}}), encoding="utf-8")
    monkeypatch.setenv("MRS3_PANEL_ROOT", "static")

    assert PanelController(tmp_path, config).panel_default_root() == "static"


def test_source_v6_panel_lifecycle_has_bound_token_progress_and_library(tmp_path: Path) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "performance"
    reports = tmp_path / "reports"
    reports.mkdir()
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        (reports / name).write_bytes((fixture_dir / name).read_bytes())
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    preflight = controller.source_v6_preflight({"root_path": str(reports), "database_path": "source-v6.duckdb"})
    assert preflight["parsed"] == 2 and preflight["token"]
    with pytest.raises(ValueError, match="latest Source v6 preflight"):
        controller.source_v6_start({"preflight_token": "stale"})
    result = controller.source_v6_start({"preflight_token": preflight["token"]})
    assert result["phase"] == "PUBLISHED"
    assert result["progress"] == 1.0
    library = controller.source_v6_library()
    assert any(item["status"] == "VALID" for item in library)


def test_source_v6_panel_preflight_snapshots_metadata_without_html_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report.html").write_bytes(fixture.read_bytes())
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    monkeypatch.setattr(Path, "read_bytes", lambda _path: (_ for _ in ()).throw(AssertionError("panel preflight read HTML")))

    result = controller.source_v6_preflight({"root_path": str(reports), "database_path": "source-v6.duckdb"})

    assert result["total"] == 1
    assert result["snapshots"][0]["ordinal"] == 0
    assert result["snapshots"][0]["relative_path"] == "report.html"


def test_source_v6_panel_routes_all_import_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "performance"
    reports = tmp_path / "reports"
    reports.mkdir()
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        (reports / name).write_bytes((fixture_dir / name).read_bytes())
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "duckdb_import": {"workers": 2},
        "source_v6_import": {
            "write_batch_size": 7,
            "worker_chunk_size": 8,
            "max_in_flight_chunks": 2,
            "segment_writer_limit": 2,
        },
    }), encoding="utf-8")
    captured: dict[str, object] = {}
    original = panel_module.import_source_v6

    def wrapped(root: Path, database: Path, **kwargs: object) -> object:
        captured.update(kwargs)
        return original(root, database, **kwargs)

    monkeypatch.setattr(panel_module, "import_source_v6", wrapped)
    controller = PanelController(tmp_path, config)
    preflight = controller.source_v6_preflight({"root_path": str(reports), "database_path": "source-v6.duckdb"})

    result = controller.source_v6_start({"preflight_token": preflight["token"]})

    assert result["phase"] == "PUBLISHED"
    assert {name: captured[name] for name in ("workers", "batch_size", "worker_chunk_size", "max_in_flight_chunks", "segment_writer_limit")} == {
        "workers": 2,
        "batch_size": 7,
        "worker_chunk_size": 8,
        "max_in_flight_chunks": 2,
        "segment_writer_limit": 2,
    }


def test_source_v6_panel_clamps_default_writer_limit_for_partial_config(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"duckdb_import": {"workers": 2}, "source_v6_import": {"write_batch_size": 8}}), encoding="utf-8")
    controller = PanelController(tmp_path, config)
    workers, settings, limit = controller._source_v6_import_options()
    assert workers == 2 and settings.write_batch_size == 8 and limit == 2


def test_source_v6_panel_exposes_structured_worker_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = Path(__file__).parent / "fixtures" / "performance" / "source_v6_fixed_lot_overlap_a.html"
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report.html").write_bytes(fixture.read_bytes())
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    preflight = controller.source_v6_preflight({"root_path": str(reports), "database_path": "source-v6.duckdb"})
    error = SourceV6WorkerFailure(0, "report.html", "input disappeared", "read")
    monkeypatch.setattr(panel_module, "import_source_v6", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    result = controller.source_v6_start({"preflight_token": preflight["token"]})

    assert result["phase"] == "FAILED"
    assert result["worker_failure"] == {
        "ordinal": 0,
        "path": str((reports / "report.html").resolve()),
        "relative_path": "report.html",
        "preflight_size": preflight["snapshots"][0]["size"],
        "preflight_mtime_ns": preflight["snapshots"][0]["mtime_ns"],
        "reason": "input disappeared",
    }


def test_source_v6_panel_cancel_clears_preflight_state(tmp_path: Path) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "performance"
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report.html").write_bytes((fixture_dir / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller.source_v6_preflight({"root_path": str(reports), "database_path": "source-v6.duckdb"})
    cancelled = controller.source_v6_cancel()
    assert cancelled["phase"] == "CANCELLED"
    assert cancelled["cancel_requested"] is True


def test_source_v6_panel_start_rejects_held_shared_lock_without_target_mutation(tmp_path: Path) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "performance"
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "report.html").write_bytes((fixture_dir / "source_v6_fixed_lot_overlap_a.html").read_bytes())
    target = tmp_path / "source-v6.duckdb"
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    preflight = controller.source_v6_preflight({"root_path": str(reports), "database_path": str(target)})

    with source_v6_import_lock(target):
        result = controller.source_v6_start({"preflight_token": preflight["token"]})

    assert result["phase"] == "FAILED"
    assert "already being written" in result["error"]
    assert not target.exists()
    assert not list(tmp_path.glob("*.staging*"))


def test_source_v6_panel_merge_is_read_only_and_serialized(tmp_path: Path) -> None:
    from mrs3.source_v6 import normalize_source_v6
    from mrs3.source_v6_importer import source_v6_import_lock
    from mrs3.source_v6_merge import merge_source_v6
    from mrs3.source_v6_storage import create_v6_database, import_fragment, preflight_import

    fixture_dir = Path(__file__).parent / "fixtures" / "performance"
    sources = []
    for index, name in enumerate(("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html")):
        database = tmp_path / f"source-{index}.source-v6.duckdb"
        fragment = normalize_source_v6((fixture_dir / name).read_bytes(), source_name=name)
        create_v6_database(database, database_id=f"source-{index}")
        import_fragment(database, fragment, preflight_token=preflight_import(database, fragment))
        sources.append(database)
    target = tmp_path / "merged.source-v6.duckdb"
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    preflight = controller.source_v6_merge_preflight({"input_paths": [str(path) for path in sources], "target_path": str(target)})
    with source_v6_import_lock(target):
        result = controller.source_v6_merge_start({"preflight_token": preflight["token"]})
    assert result["phase"] == "FAILED"
    assert "already being written" in result["error"]
    assert not target.exists()

    result = controller.source_v6_merge({"input_paths": [str(path) for path in sources], "target_path": str(target)})
    assert result["phase"] == "MERGED"
    assert result["duplicate_count"] == 0


def test_source_v6_panel_dom_exposes_merge_only_lifecycle() -> None:
    from mrs3.panel import PANEL_HTML

    for control in ("sourceV6MergePreflight()", "sourceV6MergeStart()", "sourceV6MergeCancel()", "/api/source-v6/merge/preflight", "/api/source-v6/merge/start"):
        assert control in PANEL_HTML


def test_source_v6_panel_dom_has_truthful_workflow_controls() -> None:
    from mrs3.panel import PANEL_HTML

    for control in ("sourceV6Preflight()", "sourceV6Start()", "sourceV6Cancel()", "sourceV6Library()", "source_v6_progress", "source_v6_status"):
        assert control in PANEL_HTML
    assert "/api/source-v6/preflight" in PANEL_HTML
    assert "/api/source-v6/fresh/multiscope/start" in PANEL_HTML
    assert "/api/source-v6/fresh/multiscope/analysis/start" in PANEL_HTML
    assert 'id="source_v6_scope" multiple' in PANEL_HTML
    assert "/api/source-v6/cancel" in PANEL_HTML
    assert PANEL_HTML.count('id="source_v6_surface_path"') == 1
    assert '<label>Published surface<input id="source_v6_surface_path" type="text" readonly></label>' in PANEL_HTML
    assert '<input id="source_v6_surface_path" type="hidden">' not in PANEL_HTML


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

    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    monkeypatch.setattr(controller, "source_v6_start_fresh_analysis", lambda _payload: {"phase": "FAILED", "error": "analysis input is invalid"})
    monkeypatch.setattr(controller, "_workflow_default", lambda _name: tmp_path)

    assert controller.strategies_fresh_analyze({"surface_path": "surface.surface-v6.duckdb"}) == {
        "phase": "FAILED", "error": "Analysis failed. Check panel logs."
    }


def test_source_v6_panel_keeps_legacy_fragment_out_of_operational_surface(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "performance" / "source_v6_legacy_nonstitchable.html"
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / fixture.name).write_bytes(fixture.read_bytes())
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    preflight = controller.source_v6_preflight({"root_path": str(reports), "database_path": "source-v6.duckdb"})
    result = controller.source_v6_start({"preflight_token": preflight["token"]})
    assert result["phase"] == "FAILED"
    assert "stitchable" in result["error"]


def test_source_v6_panel_rejects_uncovered_selected_interval(tmp_path: Path) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "performance"
    reports = tmp_path / "reports"
    reports.mkdir()
    for name in ("source_v6_fixed_lot_overlap_a.html", "source_v6_fixed_lot_overlap_b.html"):
        (reports / name).write_bytes((fixture_dir / name).read_bytes())
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    preflight = controller.source_v6_preflight({"root_path": str(reports), "database_path": "source-v6.duckdb"})
    result = controller.source_v6_start({"preflight_token": preflight["token"], "scope_key": "ONUSDT|LONG|1h", "start_ms": 1767225600000, "end_ms": 1767398400000})
    assert result["phase"] == "FAILED"
    assert "READY" in result["error"]


def test_source_v6_analysis_panel_selects_published_surface_and_updates_analysis_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    surface = tmp_path / "source-v6-surface.duckdb"
    dates = tmp_path / "dates.csv"
    config = tmp_path / "config.json"
    surface.write_bytes(b"published")
    dates.write_text("dates", encoding="utf-8")
    config.write_text("{}", encoding="utf-8")

    def adapter(path: Path, **kwargs: object) -> object:
        calls.append(("adapter", (path, kwargs)))
        return object()

    def analysis(path: Path, _input: object, _config: object, **kwargs: object) -> dict[str, object]:
        calls.append(("analysis", (path, kwargs)))
        return {
            "state": "COMMITTED",
            "analysis_run_id": "run-v6",
            "metadata": {
                "source_surface_id": "surface-v6",
                "source_manifest_sha256": "m" * 64,
                "source_frozen_facts_sha256": "f" * 64,
                "algorithm_config_sha256": "c" * 64,
            },
        }

    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path,
        config,
        source_v6_adapter_func=adapter,
        source_v6_analysis_func=analysis,
        source_v6_listing_dates_loader=lambda _path: {"BTCUSDT": "2020-01-01"},
        analysis_config_loader=lambda _path: object(),
    )
    monkeypatch.setattr(panel_module, "read_surface_db", lambda _path: {
        "surface_id": "surface-v6", "manifest_sha256": "m" * 64,
        "frozen_facts_sha256": "f" * 64, "event_mode": "real_independent_events",
        "surface_schema_version": 6, "metric_schema_version": "source-v6-metrics-v1",
        "event_schema_version": "source-v6-events-v1", "readiness_schema_version": "source-v6-readiness-v1",
        "frozen_facts_digest_algorithm": "sha256-canonical-frozen-facts-v1",
    })
    controller.source_v6_library = lambda: ({
        "path": str(surface), "status": "VALID", "surface_id": "surface-v6",
        "manifest_sha256": "m" * 64, "frozen_facts_sha256": "f" * 64,
        "event_mode": "real_independent_events",
        "compatibility_versions": {
            "surface_schema_version": 6, "metric_schema_version": "source-v6-metrics-v1",
            "event_schema_version": "source-v6-events-v1", "readiness_schema_version": "source-v6-readiness-v1",
            "frozen_facts_digest_algorithm": "sha256-canonical-frozen-facts-v1",
        },
    },)

    result = controller.start_source_v6_analysis({
        "surface_path": str(surface), "selected_scope": "BTCUSDT|LONG|1h",
        "start_ms": 1_700_000_000_000, "end_ms": 1_700_003_600_000,
        "listing_dates_path": str(dates), "config_path": str(config),
        "algorithm_version": "v6-test",
    })

    assert calls[0][0] == "adapter"
    assert calls[0][1][1]["expected_surface_id"] == "surface-v6"
    assert "cancel_check" in calls[0][1][1]
    assert calls[1][0] == "analysis"
    assert calls[1][1][1]["listing_dates_sha256"]
    assert result["analysis"]["run_id"] == "run-v6"
    assert result["analysis"]["source"] == "SOURCE_V6"
    assert result["source_v6_analysis"]["phase"] == "COMMITTED"
    assert result["source_v6_analysis"]["work_units_completed"] == 3


def test_source_v6_analysis_panel_rejects_missing_config_and_invalid_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    surface = tmp_path / "source-v6-surface.duckdb"
    dates = tmp_path / "dates.csv"
    surface.write_bytes(b"published")
    dates.write_text("dates", encoding="utf-8")
    controller = PanelController(tmp_path, tmp_path / "config.json")
    monkeypatch.setattr(panel_module, "read_surface_db", lambda _path: {
        "surface_id": "surface-v6", "manifest_sha256": "m" * 64,
        "frozen_facts_sha256": "f" * 64, "event_mode": "real_independent_events",
        "surface_schema_version": 6, "metric_schema_version": "source-v6-metrics-v1",
        "event_schema_version": "source-v6-events-v1", "readiness_schema_version": "source-v6-readiness-v1",
        "frozen_facts_digest_algorithm": "sha256-canonical-frozen-facts-v1",
    })
    controller.source_v6_library = lambda: ({
        "path": str(surface), "status": "VALID", "surface_id": "surface-v6",
        "manifest_sha256": "m" * 64, "frozen_facts_sha256": "f" * 64,
        "event_mode": "real_independent_events",
        "compatibility_versions": {
            "surface_schema_version": 6, "metric_schema_version": "source-v6-metrics-v1",
            "event_schema_version": "source-v6-events-v1", "readiness_schema_version": "source-v6-readiness-v1",
            "frozen_facts_digest_algorithm": "sha256-canonical-frozen-facts-v1",
        },
    },)
    with pytest.raises(ValueError, match="UTC interval"):
        controller.start_source_v6_analysis({
            "surface_path": str(surface), "selected_scope": "BTCUSDT|LONG|1h",
            "start_ms": 2, "end_ms": 1, "listing_dates_path": str(dates),
            "config_path": str(tmp_path / "missing.json"), "algorithm_version": "v6-test",
        })
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    valid_interval = {
        "surface_path": str(surface), "selected_scope": "BTCUSDT|LONG|1h",
        "start_ms": 1, "end_ms": 2, "listing_dates_path": str(dates),
        "config_path": str(tmp_path / "missing.json"), "algorithm_version": "v6-test",
    }
    with pytest.raises(ValueError, match="analysis config"):
        controller.start_source_v6_analysis(valid_interval)
    with pytest.raises(ValueError, match="invalid analysis inputs"):
        controller.start_source_v6_analysis({**valid_interval, "config_path": str(config)})


def test_source_v6_analysis_panel_revalidates_direct_api_surface_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = tmp_path / "source-v6-surface.duckdb"
    surface.write_bytes(b"published")
    controller = PanelController(tmp_path, tmp_path / "config.json")
    compatibility = {
        "surface_schema_version": 6, "metric_schema_version": "source-v6-metrics-v1",
        "event_schema_version": "source-v6-events-v1", "readiness_schema_version": "source-v6-readiness-v1",
        "frozen_facts_digest_algorithm": "sha256-canonical-frozen-facts-v1",
    }
    current = {
        "surface_id": "surface-v6", "manifest_sha256": "m" * 64,
        "frozen_facts_sha256": "f" * 64, "event_mode": "real_independent_events", **compatibility,
    }
    monkeypatch.setattr(panel_module, "read_surface_db", lambda _path: dict(current))
    row = {
        "path": str(surface), "status": "VALID", "surface_id": "surface-v6",
        "manifest_sha256": "m" * 64, "frozen_facts_sha256": "f" * 64,
        "event_mode": "legacy_trades_proxy", "compatibility_versions": compatibility,
    }
    controller.source_v6_library = lambda: (row,)
    with pytest.raises(ValueError, match="unsupported event mode"):
        controller.start_source_v6_analysis({"surface_path": str(surface)})

    row["event_mode"] = "real_independent_events"
    row["compatibility_versions"] = {"surface_schema_version": 6}
    with pytest.raises(ValueError, match="unsupported compatibility"):
        controller.start_source_v6_analysis({"surface_path": str(surface)})

    row["compatibility_versions"] = compatibility
    row["manifest_sha256"] = "x" * 64
    with pytest.raises(ValueError, match="changed since library"):
        controller.start_source_v6_analysis({"surface_path": str(surface)})


def test_source_v6_analysis_panel_rejects_missing_run_id_and_recovers_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = tmp_path / "source-v6-surface.duckdb"
    dates = tmp_path / "dates.csv"
    config = tmp_path / "config.json"
    surface.write_bytes(b"published")
    dates.write_text("dates", encoding="utf-8")
    config.write_text("{}", encoding="utf-8")
    compatibility = {
        "surface_schema_version": 6, "metric_schema_version": "source-v6-metrics-v1",
        "event_schema_version": "source-v6-events-v1", "readiness_schema_version": "source-v6-readiness-v1",
        "frozen_facts_digest_algorithm": "sha256-canonical-frozen-facts-v1",
    }
    manifest = {
        "surface_id": "surface-v6", "manifest_sha256": "m" * 64,
        "frozen_facts_sha256": "f" * 64, "event_mode": "real_independent_events", **compatibility,
    }
    monkeypatch.setattr(panel_module, "read_surface_db", lambda _path: dict(manifest))
    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path, config,
        source_v6_adapter_func=lambda *_args, **_kwargs: object(),
        source_v6_analysis_func=lambda *_args, **_kwargs: {"state": "COMMITTED"},
        source_v6_listing_dates_loader=lambda _path: {"BTCUSDT": "2020-01-01"},
        analysis_config_loader=lambda _path: object(),
    )
    controller.source_v6_library = lambda: ({"path": str(surface), "status": "VALID", **manifest, "compatibility_versions": compatibility},)
    payload = {
        "surface_path": str(surface), "selected_scope": "BTCUSDT|LONG|1h",
        "start_ms": 1_700_000_000_000, "end_ms": 1_700_003_600_000,
        "listing_dates_path": str(dates), "config_path": str(config), "algorithm_version": "v6-test",
    }
    failed = controller.start_source_v6_analysis(payload)
    assert failed["source_v6_analysis"]["phase"] == "FAILED"
    assert "analysis_run_id" in failed["source_v6_analysis"]["error"]

    controller._source_v6_analysis_func = lambda *_args, **_kwargs: {
        "state": "COMMITTED", "analysis_run_id": "run-recovered", "metadata": {},
    }
    recovered = controller.start_source_v6_analysis(payload)
    assert recovered["source_v6_analysis"]["phase"] == "COMMITTED"
    assert recovered["analysis"]["run_id"] == "run-recovered"


def test_analysis_strategies_freezes_selected_scopes_and_allows_empty_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class Connection:
        def close(self) -> None:
            pass

    def strategy_func(
        connection: object, run_id: object, candidate_ids: object, selected_scopes: object,
        template_path: object, output_dir: object, config: object, criteria: object,
    ) -> object:
        calls.append((run_id, candidate_ids, selected_scopes, Path(str(template_path)).name, Path(str(output_dir)).name, criteria))
        return SimpleNamespace(strategies_path=tmp_path / "out" / "strategies", strategy_count=1)

    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path, tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        analysis_strategy_func=strategy_func,
        analysis_shortlist_func=lambda *_args: {"rows": ()},
        analysis_config_loader=lambda _path: object(),
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"}
    )
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")

    result = controller.start_analysis_strategies({
        "run_id": "R1",
        "selected_scopes": [
            {"symbol": "ETHUSDT", "side": "LONG", "timeframe": "15m"},
            {"symbol": "BTCUSDT", "side": "long", "timeframe": "1h"},
            {"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"},
        ],
        "template_path": str(template),
        "output_dir": str(tmp_path / "out"),
        "config_path": str(tmp_path / "config.json"),
        "criteria": [],
    })

    assert calls[0][1] == ()
    assert calls[0][2] == (("BTCUSDT", "LONG", "1h"), ("ETHUSDT", "LONG", "15m"))
    assert calls[0][3] == "template.json"
    assert calls[0][4] == "out"
    document = result["analysis_strategies"]
    assert document["phase"] == "COMMITTED"
    assert document["selected_scopes"] == [["BTCUSDT", "LONG", "1h"], ["ETHUSDT", "LONG", "15m"]]


def test_analysis_strategies_failed_lifecycle_exposes_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    def strategy_func(*_args: object, **_kwargs: object) -> object:
        raise ValueError("no BASE or READY candidates exist in the selected scopes")

    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path, tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        analysis_strategy_func=strategy_func,
        analysis_shortlist_func=lambda *_args: {"rows": ()},
        analysis_config_loader=lambda _path: object(),
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"}
    )
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")

    result = controller.start_analysis_strategies({
        "run_id": "R1",
        "selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}],
        "template_path": str(template),
        "output_dir": str(tmp_path / "out"),
        "config_path": str(tmp_path / "config.json"),
        "criteria": [],
    })

    document = result["analysis_strategies"]
    assert document["phase"] == "FAILED"
    assert document["running"] is False
    assert document["strategy_count"] == 0
    assert "no BASE or READY candidates" in document["error"]
    assert document["selected_scopes"] == [["BTCUSDT", "LONG", "1h"]]


def test_analysis_strategies_routes_committed_v6_run_without_legacy_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    surface = tmp_path / "surface.duckdb"
    surface.write_bytes(b"published surface")
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "mrs3.source_v6_surface.read_source_v6_analysis_run",
        lambda *_args: {
            "analysis_run_id": "V6RUN",
            "state": "COMMITTED",
            "metadata": {"state": "COMMITTED"},
            "facts": {
                "structures": [{
                    "candidate_id": "C_READY",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "timeframe": "1h",
                    "status": "READY_MRS3_STRUCTURE",
                }],
            },
        },
    )

    def v6_generator(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return SimpleNamespace(
            strategies_path=tmp_path / "generated",
            strategy_count=1,
        )

    monkeypatch.setattr("mrs3.panel.generate_v6_analysis_strategies", v6_generator)
    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        analysis_config_loader=lambda _path: object(),
    )
    result = controller.start_analysis_strategies({
        "run_id": "V6RUN",
        "source_v6_surface_path": str(surface),
        "selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}],
        "template_path": str(template),
        "output_dir": str(tmp_path / "out"),
        "config_path": str(tmp_path / "config.json"),
    })

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert Path(str(args[0])) == surface.resolve()
    assert args[1:4] == ("V6RUN", ("C_READY",), (("BTCUSDT", "LONG", "1h"),))
    assert kwargs == {}
    assert result["analysis_strategies"]["phase"] == "COMMITTED"


def test_analysis_shortlist_reads_ready_v6_structures_from_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = tmp_path / "surface.duckdb"
    surface.write_bytes(b"published surface")
    monkeypatch.setattr(
        "mrs3.source_v6_surface.read_source_v6_analysis_run",
        lambda *_args: {
            "analysis_run_id": "V6RUN",
            "facts": {"structures": [{
                "candidate_id": "C_READY", "symbol": "BTCUSDT", "side": "LONG",
                "timeframe": "1h", "order_count": 2, "status": "READY_MRS3_STRUCTURE",
            }]},
        },
    )
    controller = PanelController(tmp_path, tmp_path / "config.local.json")

    result = controller.analysis_shortlist({
        "run_id": "V6RUN",
        "source_v6_surface_path": str(surface),
        "selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}],
        "criteria": [],
    })

    assert result["ready_count"] == 1
    assert result["scopes"] == [{
        "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h",
        "base_1ord": 0, "order_2": 1, "order_3": 0, "order_4": 0,
        "ready": 1, "deferred": 0, "total": 1,
    }]


def test_panel_requires_matching_v6_confirmation_before_tester_run(tmp_path: Path) -> None:
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    manifest = {
        "generator_schema_version": "mrs3-ready-json-v6-v1",
        "source_surface_id": "SURFACE",
        "source_manifest_sha256": "m" * 64,
        "analysis_run_id": "RUN",
        "analysis_config_sha256": "c" * 64,
        "strategy_count": 1,
        "generation_manifest_sha256": "g" * 64,
    }
    (strategies / "strategy_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    base = {
        "config": str(tmp_path / "config.json"),
        "strategies": str(strategies),
        "output_csv": str(tmp_path / "results.csv"),
    }
    with pytest.raises(ValueError, match="confirmation"):
        controller._build_command("tester-run", base)
    confirmation = {**manifest, "confirmed": True}
    command, artifacts = controller._build_command("tester-run", {**base, "v6_confirmation": confirmation})
    assert "tester-run" in command and artifacts["output_csv"].name == "results.csv"


def test_analysis_strategies_rejects_malformed_selected_scopes(tmp_path: Path) -> None:

    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")
    base = {
        "run_id": "R1",
        "template_path": str(template),
        "output_dir": str(tmp_path / "out"),
        "config_path": str(tmp_path / "config.json"),
    }

    with pytest.raises(ValueError, match="side"):
        controller.start_analysis_strategies({**base, "selected_scopes": [{"symbol": "BTCUSDT", "timeframe": "1h"}]})
    with pytest.raises(ValueError, match="must be a list"):
        controller.start_analysis_strategies({**base, "selected_scopes": "BTCUSDT"})
    with pytest.raises(ValueError, match="must not be empty"):
        controller.start_analysis_strategies({**base, "selected_scopes": []})


def test_analysis_shortlist_includes_published_scopes_and_frozen_base_counts(
    tmp_path: Path,
) -> None:
    import duckdb
    from mrs3.analysis_storage import ensure_analysis_schema

    connection = duckdb.connect(":memory:")
    ensure_analysis_schema(connection)
    connection.execute("insert into surfaces(surface_id, build_mode, period_start_utc, period_end_utc, side, event_mode) values ('S1', 'DUCKDB_DIRECT', '2026-01-01', '2026-01-02', 'LONG', 'legacy_trades_proxy')")
    connection.execute("insert into analysis_runs(run_id, surface_id, algorithm_version, algorithm_config_json) values ('R1', 'S1', 'v1', '{}')")
    for point_id in ("BTCUSDT|LONG|1h|100|3|9", "ETHUSDT|LONG|15m|100|3|9"):
        symbol, side, timeframe, shift_bp, open_ma, close_ma = point_id.split("|")
        pair_key = f"{symbol}|{side}|{shift_bp}|{open_ma}|{close_ma}"
        connection.execute("insert into surface_pairs(surface_id, pair_key) values ('S1', ?)", [pair_key])
        connection.execute("insert into surface_timeframes(surface_id, pair_key, timeframe) values ('S1', ?, ?)", [pair_key, timeframe])
        connection.execute("insert into surface_points(surface_id, canonical_point_key, pair_key, timeframe, point_event_count, source_report_id, source_hash, provenance_state, metrics_json) values ('S1', ?, ?, ?, 7, 'report', ?, 'REPRODUCIBLE', '{}')", [point_id, pair_key, timeframe, "a" * 64])
    metrics = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "timeframe": "1h",
        "ready": True,
        "operational_facts_version": "cma_representatives_v1",
        "primary_close_ma": 9,
        "cma_representatives": [
            {
                "close_ma": 9,
                "point_id": "BTCUSDT|LONG|1h|100|3|9",
                "support": 1.0,
                "support_status": "PRIMARY_CLOSE",
                "continuity_status": "USABLE",
                "usable": True,
            }
        ],
        "base_1ord_point_id": "BTCUSDT|LONG|1h|100|3|9",
        "standalone_eligible_point_ids": ["BTCUSDT|LONG|1h|100|3|9"],
    }
    connection.execute("insert into plateaus(run_id, plateau_id, surface_id, metrics_json) values ('R1', 'P1', 'S1', ?)", [json.dumps(metrics)])
    connection.execute("insert into plateau_members(run_id, plateau_id, surface_id, canonical_point_key) values ('R1', 'P1', 'S1', 'BTCUSDT|LONG|1h|100|3|9')")

    controller = PanelController(
        tmp_path, tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: connection,
        analysis_shortlist_func=lambda *_args: {
            "run_id": "R1", "surface_id": "S1", "criteria": (), "input_count": 0,
            "ready_count": 0, "deferred_count": 0, "comparison_group_count": 0,
            "comparable_count": 0, "rows": (),
        },
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"}
    )
    document = controller.analysis_shortlist({"run_id": "R1"})

    scopes = {
        tuple((row["symbol"], row["side"], row["timeframe"])): row
        for row in document["scopes"]
    }
    assert set(scopes) == {("BTCUSDT", "LONG", "1h"), ("ETHUSDT", "LONG", "15m")}
    assert scopes[("BTCUSDT", "LONG", "1h")]["base_1ord"] == 1
    assert scopes[("ETHUSDT", "LONG", "15m")]["base_1ord"] == 0
    assert scopes[("BTCUSDT", "LONG", "1h")]["order_2"] == 0
    assert document["facets"]["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert document["facets"]["timeframes"] == ["1h", "15m"]
def test_analysis_shortlist_ui_includes_visible_1ord_column() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    header = html.split('id="shortlist_table_header"', 1)[1].split("</thead>", 1)[0]
    assert '<th scope="col">1ORD</th>' in header
    assert header.index('<th scope="col">1ORD</th>') < header.index('<th scope="col">2 orders</th>')
    render = html.split("function renderShortlist()", 1)[1]
    assert "item.base_1ord" in render


def test_analysis_shortlist_ui_builds_selected_scopes_from_visible_scopes() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    strategies = html.split("async function analysisStrategies()")[-1]
    assert "selected_scopes:shortlistScopes.map(item=>({symbol:item.symbol,side:item.side,timeframe:item.timeframe}))" in strategies
    assert "No shortlist scopes available" in strategies
    assert "shortlistScopePayload()" in strategies


def test_direct_coverage_review_ui_is_pair_side_tf_and_date_only() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    assert "`${row.pair}|${row.side}`" in html
    assert "meta.textContent=side" in html
    assert "item.timeframe" in html
    assert "function directDateOnly" in html
    assert "directDateOnly(row.interval_start_utc)" in html
    assert "directDateOnly(row.interval_end_utc)" in html


def test_direct_coverage_ui_keeps_preflight_activity_feedback_deferred() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    assert "Preparing coverage..." not in html
    assert "coverage_elapsed" not in html
    assert "coverageElapsed" not in html


def test_direct_coverage_ui_shows_check_in_progress_and_blocks_duplicates() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    check = html.split("async function directPreflight()", 1)[1]
    assert "let directCoverageChecking=false;" in html
    assert "if (directCoverageChecking) return;" in check
    assert "setDirectCoverageChecking(true)" in check
    assert "setDirectCoverageChecking(false)" in check
    assert "button.textContent=enabled?'Checking coverage...':'Check coverage'" in html
    assert "button.disabled=enabled" in html
    assert "target.setAttribute('aria-busy', String(enabled))" in html
    assert "if (direct && !directCoverageChecking)" in html
    assert check.index("setDirectCoverageChecking(true)") < check.index("duckdbRequest('/api/duckdb-direct/coverage'")
    assert check.index("duckdbRequest('/api/duckdb-direct/coverage'") < check.index("setDirectCoverageChecking(false)")


def test_direct_coverage_stale_check_clears_before_request_and_direct_start_prior_job_preserves_preview() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    check = html.split("async function directPreflight()", 1)[1]
    assert check.index("clearDirectCoverageState()") < check.index("duckdbRequest('/api/duckdb-direct/coverage'")
    assert check.index("setDirectStartEligible(true)") > check.index("duckdbRequest('/api/duckdb-direct/coverage'")
    clear = html.split("function clearDirectCoverageState()", 1)[1]
    assert "directPreflightToken=''" in clear
    assert "document.getElementById('coverageReview').hidden=true" in clear
    assert "setDirectStartEligible(false)" in clear
    assert "renderDirectArtifactLinks({})" in clear
    build = html.split("async function directBuild(parentSurfaceId='')", 1)[1]
    assert build.index("clearDirectExecutionState()") < build.index("duckdbRequest('/api/duckdb-direct/preflight'")
    assert "clearDirectCoverageState()" not in build
    assert "setDirectStartEligible(false);\nconst tabs" in html


def test_direct_coverage_artifact_links_use_existing_verified_route_only() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    assert "function renderDirectArtifactLinks" in html
    assert "href='/api/artifact?name='+encodeURIComponent(name)" in html
    assert "renderDirectArtifactLinks(result.artifacts)" in html
    assert "renderDirectArtifactLinks(direct.artifacts)" in html
    assert "Object.keys(direct.artifacts || {}).length" in html


def test_direct_coverage_ui_manual_fields_do_not_constrain_token_workflow() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    assert "manual UTC and Side fields do not constrain the coverage-token workflow" in html
    assert "from checked Pair + Side + TF rows" in html


def test_direct_coverage_artifact_links_route_serves_verified_inventory_and_rejects_unverified_side_audit(
    tmp_path: Path,
) -> None:
    scan = _fake_coverage_scan(tmp_path)
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: type("Connection", (), {"close": lambda self: None})(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    controller.duckdb_direct_coverage({"symbols": []})

    assert controller.artifact("coverage_inventory") == (
        "coverage_inventory.csv",
        scan.inventory_path.read_bytes(),
    )
    assert controller.artifact("surface_coverage_audit_long") is None

    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/artifact?name=coverage_inventory")
        verified = connection.getresponse()
        assert verified.status == 200
        assert verified.read() == scan.inventory_path.read_bytes()

        connection.request("GET", "/api/artifact?name=surface_coverage_audit_long")
        rejected = connection.getresponse()
        rejected.read()
        assert rejected.status == 404
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_failed_check_clears_previous_scan_and_artifact(tmp_path: Path) -> None:
    scan = _fake_coverage_scan(tmp_path)
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: type("Connection", (), {"close": lambda self: None})(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    controller.duckdb_direct_coverage({"symbols": []})
    assert controller.artifact("coverage_inventory") is not None

    with pytest.raises(ValueError, match="symbols must be"):
        controller.duckdb_direct_coverage({"symbols": 42})

    assert controller._direct_coverage_scan is None
    assert controller._direct_artifacts == {}
    assert controller.artifact("coverage_inventory") is None
    with pytest.raises(ValueError, match="stale coverage token"):
        controller.duckdb_direct_preflight(
            {
                "coverage_token": "coverage-token",
                "selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}],
            }
        )


def test_direct_coverage_check_rejects_running_job_before_clearing_state(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    scan = _fake_coverage_scan(tmp_path)
    job = _DirectJob(
        running=True,
        artifacts={"coverage_inventory": ("captured.csv", b"captured")},
    )
    controller._direct_job = job
    controller._direct_coverage_scan = scan
    controller._direct_artifacts["coverage_inventory"] = ("global.csv", b"global")

    with pytest.raises(RuntimeError, match="another direct build is already running"):
        controller.duckdb_direct_coverage({"symbols": []})

    assert controller._direct_job is job
    assert controller._direct_job.artifacts["coverage_inventory"] == ("captured.csv", b"captured")
    assert controller._direct_coverage_scan is scan
    assert controller._direct_artifacts["coverage_inventory"] == ("global.csv", b"global")


def test_direct_coverage_check_clears_completed_job_before_serving_new_inventory(
    tmp_path: Path,
) -> None:
    scan = _fake_coverage_scan(tmp_path)
    inventory_bytes = scan.inventory_path.read_bytes()
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: type("Connection", (), {"close": lambda self: None})(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    controller._direct_job = _DirectJob(
        running=False,
        artifacts={"coverage_inventory": ("stale.csv", b"stale")},
    )

    controller.duckdb_direct_coverage({"symbols": []})

    assert controller._direct_job is None
    assert controller.snapshot()["duckdb_direct"] is None
    assert controller.artifact("coverage_inventory") == ("coverage_inventory.csv", inventory_bytes)


def test_direct_coverage_completion_keeps_inventory_captured_at_job_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)
    captured_inventory = ("captured.csv", b"captured")
    mutable_inventory = ("mutable.csv", b"mutable")
    mutated = False

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        **_: object,
    ) -> tuple[object, ...]:
        nonlocal mutated
        controller._direct_artifacts["coverage_inventory"] = mutable_inventory
        mutated = True
        return tuple(
            SimpleNamespace(request=request, points=(), preflight=SimpleNamespace(audit_sha256=""))
            for request in requests
        )

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        **_: object,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=()) for surface in surfaces),
        )

    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    controller.duckdb_direct_coverage({"symbols": []})
    controller._direct_artifacts["coverage_inventory"] = captured_inventory
    _install_fake_stage_b(monkeypatch, controller)
    selected_payload = {"selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}]}
    token = controller.duckdb_direct_preflight({"coverage_token": "coverage-token", **selected_payload})["preflight_token"]
    controller.start_duckdb_direct(
        {
            "preflight_token": token,
            **selected_payload,
        }
    )

    assert mutated
    assert controller._direct_job is not None
    assert controller._direct_job.artifacts["coverage_inventory"] == captured_inventory


def test_failed_start_clears_completed_prior_job_before_validation(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller._direct_job = _DirectJob(
        running=False,
        artifacts={
            "surface_coverage_audit_long": ("surface_coverage_audit_LONG.csv", b"long,audit\n"),
        },
    )
    assert controller.snapshot()["duckdb_direct"]["artifacts"] == {
        "surface_coverage_audit_long": "surface_coverage_audit_LONG.csv"
    }

    with pytest.raises(ValueError, match="required field"):
        controller.start_duckdb_direct({})

    assert controller._direct_job is None
    assert controller.snapshot()["duckdb_direct"] is None
    assert controller.artifact("surface_coverage_audit_long") is None


def test_successful_direct_snapshot_feeds_direct_artifact_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Connection:
        def close(self) -> None:
            pass

    scan = _fake_coverage_scan(tmp_path)
    audit_data = b"long,audit\n"

    def prepare(
        _source: object,
        requests: tuple[object, ...],
        *,
        audit_root: object,
        coverage_scan: object,
        cancellation: object,
        progress_callback: object,
    ) -> tuple[object, ...]:
        prepared: list[object] = []
        for request in requests:
            side = request.side
            audit_sha = sha256(audit_data).hexdigest()
            filename = f"surface_coverage_audit_{side}.csv"
            path = Path(audit_root) / "surface_coverage" / audit_sha / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(audit_data)
            prepared.append(
                SimpleNamespace(
                    request=request,
                    points=(),
                    preflight=SimpleNamespace(
                        audit_sha256=audit_sha,
                        audit_bytes=audit_data,
                        audit_artifact_name=filename,
                    ),
                )
            )
        return tuple(prepared)

    def publish(
        _analysis: object,
        surfaces: tuple[object, ...],
        *,
        cancellation: object,
        progress_callback: object,
        parent_surface_id: str | None = None,
    ) -> DirectQueueResult:
        return DirectQueueResult(
            "PUBLISHED",
            tuple(SimpleNamespace(surface_id=f"surface-{surface.request.side}", points=()) for surface in surfaces),
        )

    monkeypatch.setattr("mrs3.panel.threading.Thread", _SynchronousThread)
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_scan_func=lambda *_args, **_kwargs: scan,
        direct_prepare_func=prepare,
        direct_publish_func=publish,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit"}
    )
    controller.duckdb_direct_coverage({"symbols": []})
    _install_fake_stage_b(monkeypatch, controller)
    selected_payload = {"selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}]}
    token = controller.duckdb_direct_preflight({"coverage_token": "coverage-token", **selected_payload})["preflight_token"]
    controller.start_duckdb_direct(
        {
            "preflight_token": token,
            **selected_payload,
        }
    )
    status = _wait_direct_finished(controller)

    assert status["artifacts"] == {
        "coverage_inventory": "coverage_inventory.csv",
        "surface_coverage_audit_long": "surface_coverage_audit_LONG.csv",
    }
    assert "renderDirectArtifactLinks(direct.artifacts)" in panel_module.PANEL_HTML


def test_analysis_schema_initialization_is_explicit_and_library_then_reads_only(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller.duckdb_import_settings({"analysis_duckdb_path": "analysis.duckdb"})
    assert controller.initialize_analysis() == {"schema_version": 4}
    assert controller.analysis_library({}) == []


def test_direct_build_requires_matching_latest_preflight_and_closes_distinct_connections(tmp_path: Path) -> None:
    connections: list[tuple[str, bool, object]] = []

    class Connection:
        def __init__(self) -> None: self.closed = False
        def close(self) -> None: self.closed = True

    def connect(path: str, *, read_only: bool) -> Connection:
        connection = Connection(); connections.append((path, read_only, connection)); return connection

    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}),
        ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",),
    )
    calls: list[str] = []
    def inspect(_: object, __: object) -> DirectPreflight: calls.append("preflight"); return preflight
    def build(_: object, __: object, ___: object, ____: object, progress: object) -> object:
        calls.append("build"); progress("PUBLISHED", materialized_points=1); return SimpleNamespace(surface_id="surface-1", points=(object(),))

    controller = PanelController(tmp_path, tmp_path / "config.local.json", direct_connection_factory=connect, direct_preflight_func=inspect, direct_build_func=build)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    request = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    flight = controller.duckdb_direct_preflight(request)
    with pytest.raises(ValueError, match="latest preflight token"):
        controller.start_duckdb_direct({**request, "preflight_token": "wrong"})
    with pytest.raises(ValueError, match="latest preflight token"):
        controller.start_duckdb_direct({**request, "side": "SHORT", "preflight_token": flight["token"]})
    with pytest.raises(ValueError, match="latest preflight token is required"):
        controller.start_duckdb_direct({**request, "preflight_token": flight["token"]})
    assert calls == ["preflight"]
    assert [(Path(path).name, read_only, connection.closed) for path, read_only, connection in connections] == [("source.duckdb", True, True)]


def test_direct_build_rejects_same_database_before_open_and_ui_has_controls(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller.duckdb_import_settings({"source_duckdb_path": "same.duckdb", "analysis_duckdb_path": "same.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    with pytest.raises(ValueError, match="must differ"):
        controller.duckdb_direct_preflight(payload)
    for control in ("direct_start", "direct_end", "direct_side", "direct_symbols", "direct_shifts"):
        assert f'id="{control}"' in __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML

    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    def fail_open(*_args: object, **_kwargs: object) -> object:
        raise OSError(str(tmp_path))
    controller._direct_connection_factory = fail_open
    with pytest.raises(ValueError, match="direct preflight failed") as error:
        controller.duckdb_direct_preflight(payload)
    assert str(tmp_path) not in str(error.value)


def test_direct_preflight_defaults_usable_symbols_and_marks_unavailable(tmp_path: Path) -> None:
    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {"ETHUSDT": ("1h",)},
        (CoverageIssue("ETHUSDT", "1h", "GRID_NOT_COVERED", "missing grid cells"),),
        MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,),
        (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",),
    )
    class Connection:
        def close(self) -> None: pass
    controller = PanelController(
        tmp_path, tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_preflight_func=lambda *_: preflight,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT", "ETHUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    document = controller.duckdb_direct_preflight(payload)
    assert document["selected_symbols"] == ["BTCUSDT"]
    assert document["unavailable_symbols"] == {"ETHUSDT": ["1h"]}
    with pytest.raises(ValueError, match="latest preflight token is required"):
        controller.start_duckdb_direct({**payload, "preflight_token": document["token"], "selected_symbols": []})
    with pytest.raises(ValueError, match="latest preflight token is required"):
        controller.start_duckdb_direct({**payload, "preflight_token": document["token"], "selected_symbols": ["ETHUSDT"]})


def test_direct_preflight_document_includes_coverage_review_rows(tmp_path: Path) -> None:
    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {},
        (),
        MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,),
        (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",),
        (
            CoverageReviewRow(
                symbol="BTCUSDT",
                side="LONG",
                timeframe="1h",
                selectable=False,
                interval_start_utc="2024-01-01T00:00:00+00:00",
                interval_end_utc="2024-01-01T04:00:00+00:00",
                gap_details=("missing: 2024-01-01 .. 2024-01-01",),
            ),
        ),
    )

    class Connection:
        def close(self) -> None:
            pass

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_preflight_func=lambda *_: preflight,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})

    document = controller.duckdb_direct_preflight(
        {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    )

    assert document["coverage_rows"] == [
        {
            "pair": "BTCUSDT",
            "side": "LONG",
            "timeframe": "1h",
            "selectable": False,
            "interval_start_utc": "2024-01-01T00:00:00+00:00",
            "interval_end_utc": "2024-01-01T04:00:00+00:00",
            "gap_details": ["missing: 2024-01-01 .. 2024-01-01"],
        }
    ]


def test_direct_coverage_scan_does_not_require_window(tmp_path: Path) -> None:
    rows = (
        CoverageReviewRow(
            symbol="BTCUSDT",
            side="LONG",
            timeframe="1h",
            selectable=False,
            interval_start_utc="2024-01-01T00:00:00+00:00",
            interval_end_utc="2024-01-01T04:00:00+00:00",
            gap_details=("missing: 2024-01-01 .. 2024-01-01",),
        ),
    )

    class Connection:
        def close(self) -> None:
            pass

    received: list[tuple[str, tuple[str, ...]]] = []

    def coverage(
        _connection: object, *, side: str, symbols: tuple[str, ...]
    ) -> tuple[CoverageReviewRow, ...]:
        received.append((side, symbols))
        return rows

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_coverage_func=coverage,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})

    document = controller.duckdb_direct_coverage({"side": "LONG", "symbols": []})

    assert received == [("LONG", ())]
    assert document["coverage_rows"][0]["pair"] == "BTCUSDT"
    assert document["coverage_rows"][0]["gap_details"] == ["missing: 2024-01-01 .. 2024-01-01"]


def test_direct_preflight_rejects_selected_scope_without_coverage_token(tmp_path: Path) -> None:
    preflight = DirectPreflight(
        {"BTCUSDT": ("1h",)}, {},
        (),
        MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,),
        (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",),
        (
            CoverageReviewRow(
                symbol="BTCUSDT",
                side="LONG",
                timeframe="1h",
                selectable=False,
                interval_start_utc="2024-01-01T00:00:00+00:00",
                interval_end_utc="2024-01-01T04:00:00+00:00",
                gap_details=("missing: 2024-01-01 .. 2024-01-01",),
            ),
        ),
    )

    class Connection:
        def close(self) -> None:
            pass

    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_preflight_func=lambda *_: preflight,
    )
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    selected_payload = {
        **payload,
        "selected_scopes": [{"symbol": "BTCUSDT", "timeframe": "1h"}],
    }
    with pytest.raises(ValueError, match="selected scopes require a valid coverage token"):
        controller.duckdb_direct_preflight(selected_payload)


def test_direct_build_reports_cancellation_without_leaking_paths(tmp_path: Path) -> None:
    base = DirectPreflight({"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",))
    connections: list[tuple[str, bool, object]] = []
    calls, built = 0, False
    def inspect(*_: object) -> DirectPreflight:
        nonlocal calls; calls += 1
        return base
    class Connection:
        def __init__(self) -> None: self.closed = False
        def close(self) -> None: self.closed = True
    def connect(path: str, *, read_only: bool) -> Connection:
        connection = Connection(); connections.append((path, read_only, connection)); return connection
    def build(*_args: object) -> object:
        nonlocal built; built = True
        raise AssertionError("legacy start must fail closed before any build")
    controller = PanelController(tmp_path, tmp_path / "config.local.json", direct_connection_factory=connect, direct_preflight_func=inspect, direct_build_func=build)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    token = controller.duckdb_direct_preflight(payload)["token"]
    with pytest.raises(ValueError, match="latest preflight token is required") as error:
        controller.start_duckdb_direct({**payload, "preflight_token": token})
    assert built is False
    assert calls == 1
    assert str(tmp_path) not in str(error.value)
    assert [(Path(path).name, read_only, connection.closed) for path, read_only, connection in connections] == [("source.duckdb", True, True)]


def test_direct_build_rejects_changed_source_snapshot_before_build(tmp_path: Path) -> None:
    base = DirectPreflight({"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",))
    changed = DirectPreflight(base.usable_timeframes, base.unavailable_symbols, base.coverage_issues, base.grid_contract, base.source_hashes, base.manifest, ("changed",))
    calls, built = 0, False
    def inspect(*_: object) -> DirectPreflight:
        nonlocal calls; calls += 1; return base if calls == 1 else changed
    class Connection:
        def close(self) -> None: pass
    def build(*_: object) -> object:
        nonlocal built; built = True; raise AssertionError("must not build stale source")
    controller = PanelController(tmp_path, tmp_path / "config.local.json", direct_connection_factory=lambda *_args, **_kwargs: Connection(), direct_preflight_func=inspect, direct_build_func=build)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    token = controller.duckdb_direct_preflight(payload)["token"]
    with pytest.raises(ValueError, match="latest preflight token is required"):
        controller.start_duckdb_direct({**payload, "preflight_token": token})
    assert built is False
    assert calls == 1


def test_direct_build_closes_source_if_analysis_open_fails(tmp_path: Path) -> None:
    base = DirectPreflight({"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",))
    opened: list[object] = []
    class Connection:
        def __init__(self) -> None: self.closed = False
        def close(self) -> None: self.closed = True
    def connect(_path: str, *, read_only: bool) -> Connection:
        if not read_only: raise OSError("analysis open failed")
        connection = Connection(); opened.append(connection); return connection
    controller = PanelController(tmp_path, tmp_path / "config.local.json", direct_connection_factory=connect, direct_preflight_func=lambda *_: base)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    token = controller.duckdb_direct_preflight(payload)["token"]
    with pytest.raises(ValueError, match="latest preflight token is required"):
        controller.start_duckdb_direct({**payload, "preflight_token": token})
    assert len(opened) == 1
    assert opened[0].closed is True


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


def test_duckdb_import_settings_preflight_start_cancel_and_evidence_gate(tmp_path: Path) -> None:
    calls: list[object] = []

    def preflight(request: object) -> ImportPreflight:
        calls.append(request)
        return ImportPreflight("token-1", 3, 5, "digest")

    def importer(request: object, progress: object) -> ImportJobResult:
        progress(ImportProgress("RUNNING", 3, 2, 1, 0, 1, 0, 0))
        return _import_result(tmp_path)

    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=preflight, import_func=importer)
    settings = controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "default_html_root": "html", "audit_root": "audit", "workers": 2, "transaction_batch_size": 10})
    assert settings["workers"] == 2
    assert controller.duckdb_import_preflight({"root_path": "html"})["token"] == "token-1"
    with pytest.raises(ValueError, match="preflight"):
        controller.start_duckdb_import({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token-1"})
    deadline = time.monotonic() + 1
    while controller.snapshot()["duckdb_import"]["running"] and time.monotonic() < deadline:
        time.sleep(.01)
    job = controller.snapshot()["duckdb_import"]
    assert job["counts"] == {"parsed": 2, "inserted": 1, "replaced": 0, "identical": 1, "ambiguous": 0, "quarantined": 0}
    assert job["final_state"] == "COMMITTED"
    assert job["safe_to_delete"] == "YES"
    assert all(str(tmp_path) not in value for value in json.dumps(job).splitlines())
    assert controller.cancel_duckdb_import()["running"] is False


def test_default_html_preflight_runs_in_background_and_exposes_progress(tmp_path: Path) -> None:
    html_root = tmp_path / "html"
    html_root.mkdir()
    (html_root / "a.html").write_bytes(b"a")
    (html_root / "nested.html").write_bytes(b"b")
    controller = PanelController(
        tmp_path,
        tmp_path / "config.local.json",
        preflight_func=panel_module.preflight_html_import,
    )
    controller.duckdb_import_settings(
        {"source_duckdb_path": "source.duckdb", "audit_root": "audit", "workers": 2}
    )

    initial = controller.duckdb_import_preflight({"root_path": "html"})
    assert initial["running"] is True or initial["phase"] == "READY"
    deadline = time.monotonic() + 2
    while controller.snapshot()["duckdb_import_preflight"]["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    status = controller.snapshot()["duckdb_import_preflight"]
    assert status["phase"] == "READY"
    assert status["discovered"] == status["snapshotted"] == 2
    assert status["processed_bytes"] == status["total_bytes"] == 2
    assert status["token"]


def test_duckdb_import_rejects_stale_and_parallel_jobs_and_tampered_evidence(tmp_path: Path) -> None:
    released = __import__("threading").Event()
    started = __import__("threading").Event()

    def preflight(_: object) -> ImportPreflight:
        return ImportPreflight("fresh", 1, 5, "digest")

    def importer(_: object, __: object) -> ImportJobResult:
        started.set(); released.wait(1)
        return _import_result(tmp_path, tampered=True)

    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=preflight, import_func=importer)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "default_html_root": "html", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    with pytest.raises(ValueError, match="preflight"):
        controller.start_duckdb_import({"root_path": "html", "preflight_token": "stale"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "fresh"})
    assert started.wait(1)
    with pytest.raises(RuntimeError, match="already running"):
        controller.start_duckdb_import({"root_path": "html", "preflight_token": "fresh"})
    assert controller.cancel_duckdb_import()["cancel_requested"] is True
    released.set()
    deadline = time.monotonic() + 1
    while controller.snapshot()["duckdb_import"]["running"] and time.monotonic() < deadline:
        time.sleep(.01)
    job = controller.snapshot()["duckdb_import"]
    assert job["safe_to_delete"] == "NO"
    assert job["artifacts"] == {}


def test_duckdb_preflight_rejects_running_import(tmp_path: Path) -> None:
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def preflight(_: object) -> ImportPreflight:
        return ImportPreflight("fresh", 1, 5, "digest")

    def importer(_: object, __: object) -> ImportJobResult:
        started.set(); release.wait(1)
        return _import_result(tmp_path)

    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=preflight, import_func=importer)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "fresh"})
    assert started.wait(1)
    with pytest.raises(RuntimeError, match="already running"):
        controller.duckdb_import_preflight({"root_path": "html"})
    release.set()


def test_sync_preflight_marker_blocks_import_for_entire_call(tmp_path: Path) -> None:
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def preflight(_: object) -> ImportPreflight:
        started.set(); release.wait(1)
        return ImportPreflight("fresh", 1, 5, "digest")

    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=preflight)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    worker = __import__("threading").Thread(target=lambda: controller.duckdb_import_preflight({"root_path": "html"}))
    worker.start(); assert started.wait(1)
    with pytest.raises(RuntimeError, match="preflight is still running"):
        controller.start_duckdb_import({"root_path": "html", "preflight_token": "fresh"})
    release.set(); worker.join(1)


def test_duckdb_import_migration_activates_only_valid_unchanged_target(tmp_path: Path) -> None:
    target = tmp_path / "migrated.duckdb"
    target.write_bytes(b"target")
    source = tmp_path / "source.duckdb"
    source.write_bytes(b"source")

    class Result:
        target_path = target
        target_database_sha256 = sha256(target.read_bytes()).hexdigest()
        validation = type("Validation", (), {"valid": True})()

    controller = PanelController(tmp_path, tmp_path / "config.local.json", migration_func=lambda *_: Result())
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb"})
    assert controller.migrate_duckdb_import({"target_path": "migrated.duckdb"})["source_duckdb_path"] == str(target.resolve())
    with pytest.raises(ValueError, match="different"):
        controller.migrate_duckdb_import({"target_path": "migrated.duckdb"})


def test_duckdb_migration_forwards_worker_and_batch_settings(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"; source.write_bytes(b"source")
    target = tmp_path / "target.duckdb"
    received: dict[str, object] = {}

    def migrate(origin: Path, destination: Path, *, workers: int, transaction_batch_size: int) -> object:
        received.update(origin=origin, workers=workers, transaction_batch_size=transaction_batch_size)
        destination.write_bytes(b"target")
        return type("Migration", (), {"validation": type("Validation", (), {"valid": True})(), "target_database_sha256": sha256(destination.read_bytes()).hexdigest()})()

    controller = PanelController(tmp_path, tmp_path / "config.local.json", migration_func=migrate)
    controller.duckdb_import_settings({"source_duckdb_path": str(source), "workers": 7, "transaction_batch_size": 13})
    controller.migrate_duckdb_import({"target_path": str(target)})

    assert received == {"origin": source.resolve(), "workers": 7, "transaction_batch_size": 13}


def test_duckdb_migration_hashes_target_without_read_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.duckdb"; source.write_bytes(b"source")
    target = tmp_path / "target.duckdb"
    def migrate(_: Path, destination: Path) -> object:
        destination.write_bytes(b"target")
        return type("Migration", (), {"validation": type("Validation", (), {"valid": True})(), "target_database_sha256": sha256(b"target").hexdigest()})()
    original = Path.read_bytes
    def reject(path: Path) -> bytes:
        if path == target: raise AssertionError("migrated target must be streamed")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", reject)
    controller = PanelController(tmp_path, tmp_path / "config.local.json", migration_func=migrate)
    controller.duckdb_import_settings({"source_duckdb_path": str(source)})
    controller.migrate_duckdb_import({"target_path": str(target)})


def test_http_duckdb_import_settings_and_preflight_are_dedicated_routes(tmp_path: Path) -> None:
    def migrate(_: Path, target: Path) -> object:
        target.write_bytes(b"migrated")
        return type("Migration", (), {"validation": type("Validation", (), {"valid": True})(), "target_database_sha256": sha256(target.read_bytes()).hexdigest()})()
    class Connection:
        def close(self) -> None: pass
    direct = DirectPreflight({"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",))
    controller = PanelController(
        tmp_path, tmp_path / "config.local.json",
        preflight_func=lambda _: ImportPreflight("token", 0, 5, "digest"), migration_func=migrate,
        direct_connection_factory=lambda *_args, **_kwargs: Connection(),
        direct_preflight_func=lambda *_: direct,
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        settings = {"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb", "audit_root": "audit", "default_html_root": "html"}
        connection.request("POST", "/api/duckdb-import/settings", json.dumps(settings).encode(), {"Content-Type": "application/json"})
        saved = connection.getresponse(); assert saved.status == 200; saved.read()
        connection.request("GET", "/api/duckdb-import/settings")
        loaded = connection.getresponse(); assert loaded.status == 200
        assert json.loads(loaded.read())["workers"] == 4
        connection.request("POST", "/api/duckdb-import/preflight", json.dumps({"root_path": "html"}).encode(), {"Content-Type": "application/json"})
        response = connection.getresponse(); assert response.status == 200
        assert json.loads(response.read())["token"] == "token"
        direct_payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
        connection.request("POST", "/api/duckdb-direct/preflight", json.dumps(direct_payload).encode(), {"Content-Type": "application/json"})
        direct_response = connection.getresponse(); assert direct_response.status == 200
        assert json.loads(direct_response.read())["selected_symbols"] == ["BTCUSDT"]
        connection.request("POST", "/api/duckdb-import/migrate", json.dumps({"target_path": "migrated.duckdb"}).encode(), {"Content-Type": "application/json"})
        migrated = connection.getresponse(); assert migrated.status == 200
        assert json.loads(migrated.read())["source_duckdb_path"] == str((tmp_path / "migrated.duckdb").resolve())
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_http_remote_path_check_is_a_dedicated_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    monkeypatch.setattr(controller, "remote_testing_check_paths", lambda: {"paths": {"source_db_root": True}})
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("POST", "/api/v2/testing/remote/check-paths", b"{}", {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"paths": {"source_db_root": True}}
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_http_ui_exposes_persistent_import_settings_and_migration_controls(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/legacy")
        response = connection.getresponse(); parser = _ImportUiParser()
        parser.feed(response.read().decode("utf-8"))
        assert response.status == 200
        assert {"import_source_duckdb", "import_analysis_duckdb", "import_default_html_root", "import_audit_root", "import_workers", "import_batch_size", "migration_target"} <= parser.input_ids
        assert {"direct_start", "direct_end", "direct_symbols"} <= parser.input_ids
        assert {"saveDuckdbSettings()", "migrateDuckdb()", "directPreflight()", "directBuild()", "directCancel()"} <= parser.actions
        panel_html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
        assert ".direct-unavailable" in panel_html and "row.className='direct-unavailable'" in panel_html
        assert "duckdb_import_preflight" in panel_html and "processed_bytes" in panel_html
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_completed_import_revalidates_evidence_before_status_and_download(tmp_path: Path) -> None:
    result = _import_result(tmp_path)
    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=lambda _: ImportPreflight("token", 1, 5, "digest"), import_func=lambda *_: result)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token"})
    assert _wait_import_finished(controller)["safe_to_delete"] == "YES"
    result.checklist_path.write_text("{}", encoding="utf-8")
    status = controller.snapshot()["duckdb_import"]
    assert status["final_state"] != "COMMITTED"
    assert status["safe_to_delete"] == "NO"
    assert status["artifacts"] == {}
    assert controller.artifact("import_checklist") is None


def test_artifact_response_serves_the_bytes_that_passed_evidence_validation(tmp_path: Path) -> None:
    result = _import_result(tmp_path)
    expected = result.checklist_path.read_bytes()
    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=lambda _: ImportPreflight("token", 1, 5, "digest"), import_func=lambda *_: result)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token"})
    assert _wait_import_finished(controller)["safe_to_delete"] == "YES"
    original_artifact = controller.artifact
    def mutate_after_validation(name: str) -> object:
        approved = original_artifact(name)
        result.checklist_path.write_bytes(b"tampered-after-validation")
        return approved
    controller.artifact = mutate_after_validation  # type: ignore[method-assign]
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/artifact?name=import_checklist")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == expected
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_generic_artifact_http_response_remains_path_streamed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "results.csv"; expected = b"header\nrow\n"; artifact.write_bytes(expected)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    controller._job = _Job("generic", "tester-run", (), {"output_csv": artifact}, {"output_csv": None}, status="SUCCEEDED")
    original_read_bytes = Path.read_bytes
    def reject_bulk_read(path: Path) -> bytes:
        if path == artifact:
            raise AssertionError("generic artifact must remain streamed")
        return original_read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", reject_bulk_read)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/api/artifact?name=output_csv")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == expected
    finally:
        connection.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_settings_change_during_migration_survives_source_activation(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"; source.write_bytes(b"source")
    target = tmp_path / "target.duckdb"
    started = __import__("threading").Event(); release = __import__("threading").Event()
    def migrate(_: Path, destination: Path) -> object:
        started.set(); assert release.wait(1)
        destination.write_bytes(b"target")
        return type("Migration", (), {"validation": type("Validation", (), {"valid": True})(), "target_database_sha256": sha256(destination.read_bytes()).hexdigest()})()
    controller = PanelController(tmp_path, tmp_path / "config.local.json", migration_func=migrate)
    controller.duckdb_import_settings({"source_duckdb_path": str(source), "workers": 4})
    migration = __import__("threading").Thread(target=lambda: controller.migrate_duckdb_import({"target_path": str(target)}))
    settings = __import__("threading").Thread(target=lambda: controller.duckdb_import_settings({"workers": 9}))
    migration.start(); assert started.wait(1); settings.start(); release.set()
    migration.join(2); settings.join(2)
    final = controller.duckdb_import_settings()
    assert final["source_duckdb_path"] == str(target.resolve())
    assert final["workers"] == 9


@pytest.mark.parametrize("result_error", [False, True])
def test_import_status_never_exposes_absolute_paths_from_errors(tmp_path: Path, result_error: bool) -> None:
    secret = tmp_path / "html" / "report.html"
    def importer(*_: object) -> ImportJobResult:
        if not result_error:
            raise RuntimeError(f"cannot open {secret}")
        result = _import_result(tmp_path, final_state="FAILED")
        return replace(result, error=f"cannot open {secret}")
    controller = PanelController(tmp_path, tmp_path / "config.local.json", preflight_func=lambda _: ImportPreflight("token", 1, 5, "digest"), import_func=importer)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "audit_root": "audit"})
    controller.duckdb_import_preflight({"root_path": "html"})
    controller.start_duckdb_import({"root_path": "html", "preflight_token": "token"})
    document = _wait_import_finished(controller)
    assert document["error"] is not None
    assert str(tmp_path) not in document["error"]


def test_controller_builds_shell_free_tester_command_and_captures_log(
    tmp_path: Path,
) -> None:
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )

    controller.start(
        "tester-run",
        {
            "config": "config.json",
            "strategies": "generated/strategies",
            "output_csv": "results/test.csv",
        },
    )
    snapshot = _wait_finished(controller)

    job = snapshot["job"]
    assert job["status"] == "SUCCEEDED"
    assert job["logs"] == ["started", "finished"]
    assert job["command"][1:4] == ["-m", "mrs3.cli", "tester-run"]
    assert job["command"][-2:] == [
        "--output-csv",
        str((tmp_path / "results/test.csv").resolve()),
    ]


def test_performance_dd5_job_preserves_last_progress_snapshot_on_failure(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_PerformanceProgressProcess)
    controller._job = _Job("performance", "performance-dd5", (), {}, {})

    controller._run_job("performance")

    job = controller.snapshot()["job"]
    assert job["status"] == "FAILED"
    assert job["logs"] == ["ordinary diagnostic"]
    assert job["performance_progress"] == {
        "stage": "READBACK_VERIFIED", "completed": 1, "total": 1,
        "quarantined": 0, "scheduled": 1, "prepared": 1, "imported": 1,
        "skipped": 0, "phase_seconds": {"PARSE_PREPARE": 0.1},
        "terminal_error": "ValueError",
    }


def test_controller_builds_source_csv_command_without_tester_config(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    controller.start("source-csv", {"config": "config.json", "input_csv": "a.csv;b.csv", "start": "2026-07-15T00:00:00Z", "end": "2026-08-06T00:00:00Z", "output_dir": "package"})
    job = _wait_finished(controller)["job"]

    assert job["command"][1:4] == ["-m", "mrs3.cli", "source-csv"]
    assert job["command"].count("--input-csv") == 2
    assert "--config" in job["command"]


def test_controller_builds_duckdb_command_with_optional_html_verification(
    tmp_path: Path,
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "source-duckdb",
        {
            "config": "config.json",
            "database": "source.duckdb",
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-08-06T00:00:00Z",
            "output_dir": "package",
            "verify_html_root": "html",
            "verification_sample_count": "4",
        },
    )

    assert command[command.index("--verify-html-root") + 1] == str((tmp_path / "html").resolve())
    assert command[command.index("--verification-sample-count") + 1] == "4"


@pytest.mark.parametrize("sample_count", ["", "two", "2", "6", 3.5, None])
def test_controller_rejects_invalid_duckdb_verification_sample_count_before_launch(
    tmp_path: Path, sample_count: object
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="verification_sample_count"):
        controller._build_command(
            "source-duckdb",
            {
                "config": "config.json",
                "database": "source.duckdb",
                "start": "2026-07-15T00:00:00Z",
                "end": "2026-08-06T00:00:00Z",
                "output_dir": "package",
                "verify_html_root": "html",
                "verification_sample_count": sample_count,
            },
        )


def test_controller_selects_verified_source_package_without_raw_csv(
    tmp_path: Path,
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "select",
        {
            "config": "config.json",
            "source_package": "verified-package",
            "dates": "dates.xlsx",
            "template": "template.json",
            "side": "LONG",
            "output_dir": "output",
        },
    )

    assert "--source-package" in command
    assert str((tmp_path / "verified-package").resolve()) in command
    assert "--input-csv" not in command


def test_controller_keeps_compatibility_raw_csv_selection(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    command, _ = controller._build_command(
        "select",
        {
            "config": "config.json",
            "input_csv": "input.csv",
            "dates": "dates.xlsx",
            "template": "template.json",
            "side": "LONG",
            "output_dir": "output",
        },
    )

    assert "--input-csv" in command
    assert "--source-package" not in command


@pytest.mark.parametrize(
    "source_payload",
    [{}, {"input_csv": "input.csv", "source_package": "package"}],
)
def test_controller_rejects_select_without_exactly_one_source(
    tmp_path: Path, source_payload: dict[str, str]
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="exactly one"):
        controller._build_command(
            "select",
            {
                "config": "config.json",
                "dates": "dates.xlsx",
                "template": "template.json",
                "side": "LONG",
                "output_dir": "output",
                **source_payload,
            },
        )


@pytest.mark.parametrize("invalid_source", [None, "   "])
def test_controller_rejects_null_or_blank_select_source(
    tmp_path: Path, invalid_source: str | None
) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)

    with pytest.raises(ValueError, match="exactly one"):
        controller._build_command(
            "select",
            {
                "config": "config.json",
                "input_csv": invalid_source,
                "dates": "dates.xlsx",
                "template": "template.json",
                "side": "LONG",
                "output_dir": "output",
            },
        )


def test_controller_rejects_parallel_jobs(tmp_path: Path) -> None:
    class WaitingProcess(_FakeProcess):
        def wait(self) -> int:
            time.sleep(0.2)
            return 0

    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=WaitingProcess,
    )
    payload = {"config": "config.json", "strategies": "strategies"}

    controller.start("tester-plan", payload)

    with pytest.raises(RuntimeError, match="already running"):
        controller.start("tester-plan", payload)


def test_controller_hides_artifacts_left_by_an_older_job(tmp_path: Path) -> None:
    output = tmp_path / "results/test.csv"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    state = output.with_name("test.state.json")
    state.write_text('{"state":"COMPLETED"}', encoding="utf-8")
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )

    controller.start(
        "tester-run",
        {
            "config": "config.json",
            "strategies": "strategies",
            "output_csv": "results/test.csv",
        },
    )
    snapshot = _wait_finished(controller)

    assert snapshot["job"]["workflow"] is None
    assert snapshot["job"]["artifacts"] == {"raw_log": "test.raw.log"}


def test_dashboard_reports_manifest_counts_but_never_claims_v2_selectable(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "package_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "package_version": 2,
                "event_mode": "real_independent_events",
                "window_start": "2026-07-15T00:00:00+00:00",
                "window_end": "2026-08-06T00:00:00+00:00",
                "report_count": 12,
                "coverage_accepted_reports": 10,
                "coverage_rejected_reports": 2,
                "point_count": 10,
                "included_cycles": 45,
                "exclusions": {"OPEN_BEFORE_WINDOW": 3, "CLOSE_ON_OR_AFTER_WINDOW": 4},
                "source_summary_status": "VERIFIED",
                "window_metrics_status": "DERIVED_FROM_VERIFIED_SOURCE",
            }
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json")
    controller._section_jobs["duckdb"] = _Job(
        job_id="complete-duckdb",
        action="source-duckdb",
        command=(),
        artifacts={"manifest": manifest},
        artifact_baseline={"manifest": None},
        status="SUCCEEDED",
    )

    dashboard = controller.snapshot()["dashboard"]["duckdb"]

    assert dashboard["available"] is True
    assert dashboard["state"] == "VERIFICATION_STATUSES_PRESENT"
    # This deliberately lacks source_summary_samples and the verification CSV.
    # The dashboard is not the package verifier and must never overrule it.
    assert dashboard["state"] != "SELECTABLE"
    assert dashboard["metrics"] == [
        {"label": "Отчёты", "value": 12},
        {"label": "Точки", "value": 10},
        {"label": "Покрытие: принято", "value": 10},
        {"label": "Покрытие: отклонено", "value": 2},
        {"label": "Включено (циклы)", "value": 45},
        {"label": "Исключено (циклы)", "value": 7},
    ]
    assert dashboard["details"] == [
        "real_independent_events · пакет v2",
        "Окно UTC: 2026-07-15T00:00:00+00:00 — 2026-08-06T00:00:00+00:00",
        "Source summary: VERIFIED",
        "Window metrics: DERIVED_FROM_VERIFIED_SOURCE",
    ]
    assert str(package) not in json.dumps(dashboard)


def test_dashboard_reports_candidate_tester_and_posttest_final_artifacts(
    tmp_path: Path,
) -> None:
    selected = tmp_path / "selected"
    selected.mkdir()
    (selected / "run_manifest.json").write_text(
        json.dumps(
            {
                "event_mode": "real_independent_events",
                "event_eligible_point_count": 11,
                "geometric_plateau_count": 4,
                "ready_plateau_count": 3,
                "ready_structure_count": 2,
                "ready_json_count": 5,
            }
        ),
        encoding="utf-8",
    )
    results = tmp_path / "results.csv"
    results.write_text(
        "strategy_name,total_pnl_pct,max_drawdown_pct\na,2.5,1.2\nb,3.0,2.0\n",
        encoding="utf-8",
    )
    posttest = tmp_path / "posttest"
    posttest.mkdir()
    (posttest / "posttest_manifest.json").write_text(
        json.dumps(
            {"raw_result_count": 2, "pareto_count": 1, "scaled_strategy_count": 1, "target_dd_pct": "5", "dd5_mode": "CALCULATION_ONLY"}
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json")
    controller._section_jobs = {
        "candidates": _Job("selected", "select", (), {"manifest": selected / "run_manifest.json"}, {"manifest": None}, status="SUCCEEDED"),
        "tester": _Job("tested", "tester-run", (), {"output_csv": results}, {"output_csv": None}, status="SUCCEEDED"),
        "posttest": _Job("dd5", "posttest", (), {"manifest": posttest / "posttest_manifest.json"}, {"manifest": None}, status="SUCCEEDED"),
    }

    dashboard = controller.snapshot()["dashboard"]

    assert dashboard["candidates"]["state"] == "READY_FOR_TEST"
    assert dashboard["candidates"]["metrics"][-1] == {"label": "JSON для теста", "value": 5}
    assert dashboard["tester"]["state"] == "COMPLETED"
    assert dashboard["tester"]["metrics"] == [
        {"label": "Результаты", "value": 2},
        {"label": "Лучший PnL, %", "value": "3"},
        {"label": "DD лучшего, %", "value": "2"},
        {"label": "Ошибки", "value": 0},
    ]
    assert dashboard["posttest"]["state"] == "CALCULATED"
    assert dashboard["posttest"]["metrics"][-1]["value"] == "5"
    assert dashboard["posttest"]["details"]


def test_dashboard_keeps_last_artifact_when_a_later_job_has_none(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "package_manifest.json"
    manifest.write_text(
        json.dumps(
            {"event_mode": "legacy_trades_proxy", "source_rows": 3, "accepted_rows": 2, "rejected_rows": 1}
        ),
        encoding="utf-8",
    )
    controller = PanelController(tmp_path, tmp_path / "config.json", process_factory=_FakeProcess)
    controller._section_jobs["csv"] = _Job(
        "old", "source-csv", (), {"manifest": manifest}, {"manifest": None}, status="SUCCEEDED"
    )

    controller.start("tester-plan", {"config": "config.json", "strategies": "strategies"})
    dashboard = _wait_finished(controller)["dashboard"]

    assert dashboard["csv"]["available"] is True
    assert dashboard["csv"]["metrics"][1] == {"label": "Точки", "value": 2}


def test_panel_rejects_non_loopback_bind(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.json")

    with pytest.raises(ValueError, match="loopback"):
        create_panel_server("0.0.0.0", 0, controller)


def test_http_panel_serves_ui_status_and_start_endpoint(tmp_path: Path) -> None:
    controller = PanelController(
        root=tmp_path,
        default_config=tmp_path / "config.json",
        process_factory=_FakeProcess,
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/legacy")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        assert response.status == 200
        assert "MRS3 Control Panel" in html
        assert "Каталог JSON-стратегий" in html
        assert "Legacy CSV source" in html
        assert html.count('<button role="tab"') == 5
        assert html.count('<section role="tabpanel"') == 5
        assert "Import" in html and "surface" in html and "analysis" in html
        assert "Test plan" in html and "DD5" in html
        assert "Анализатор портфелей" in html
        assert "Настройки" in html
        assert "legacy_trades_proxy" in html
        assert "real_independent_events" in html
        assert 'id="verify_html_root"' in html
        assert 'id="verification_sample_count"' in html
        assert "Совместимый CSV-вход (текущий путь)" in html
        assert "Симулятор сетов недоступен" in html
        assert "Рекомендации недоступны" in html
        assert 'data-runnable="true"' in html
        assert "document.querySelectorAll('[data-runnable]')" in html
        assert 'aria-live="polite"' in html
        assert "prefers-reduced-motion: reduce" in html
        assert "prefers-reduced-transparency: reduce" in html
        assert "prefers-contrast: more" in html
        assert "function activateTab" in html
        assert "function browse" in html
        assert "performance_progress" in html
        assert "function renderPerformanceProgress" in html
        assert "performanceProgress" in html
        assert "performanceTerminalError" in html
        assert "/api/browse" in html
        assert "CSV-файлы" in html

        body = json.dumps(
            {
                "action": "tester-plan",
                "config": "config.json",
                "strategies": "strategies",
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/start",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        started = connection.getresponse()
        document = json.loads(started.read().decode("utf-8"))
        assert started.status == 202
        assert document["job"]["action"] == "tester-plan"

        connection.request("GET", "/api/status")
        status = connection.getresponse()
        status_document = json.loads(status.read().decode("utf-8"))
        assert status.status == 200
        assert status_document["defaults"]["config"] == str(
            (tmp_path / "config.json").resolve()
        )

        connection.request(
            "POST",
            "/api/start",
            body=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        unsupported = connection.getresponse()
        unsupported.read()
        assert unsupported.status == 415
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_panel_browse_returns_only_explicit_native_selection(tmp_path: Path) -> None:
    selected = (tmp_path / "one.csv", tmp_path / "two.csv")
    calls: list[tuple[str, bool]] = []

    def chooser(kind: str, multiple: bool) -> tuple[Path, ...]:
        calls.append((kind, multiple))
        return selected

    controller = PanelController(
        tmp_path,
        tmp_path / "config.json",
        browse_factory=chooser,
    )
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        body = json.dumps({"kind": "csv", "multiple": True}).encode("utf-8")
        connection.request("POST", "/api/browse", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        document = json.loads(response.read().decode("utf-8"))
        assert response.status == 200
        assert calls == [("csv", True)]
        assert document == {"paths": [str(path.resolve()) for path in selected]}

        body = json.dumps({"kind": "unknown", "multiple": False}).encode("utf-8")
        connection.request("POST", "/api/browse", body=body, headers={"Content-Type": "application/json"})
        rejected = connection.getresponse()
        rejected.read()
        assert rejected.status == 400
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
