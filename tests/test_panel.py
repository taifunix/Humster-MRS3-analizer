from __future__ import annotations

from http.client import HTTPConnection
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import time
from hashlib import sha256
from types import MappingProxyType, SimpleNamespace

import pytest

from mrs3 import panel as panel_module
from mrs3.panel import PanelController, _Job, create_panel_server
from mrs3.duckdb_import import ImportJobResult, ImportPreflight, ImportProgress
from mrs3.duckdb_direct import CoverageIssue, DirectPreflight


class _FakeProcess:
    def __init__(self, command: list[str], **_: object) -> None:
        self.command = command
        self.pid = 12345
        self.stdout = io.StringIO("started\nfinished\n")
        self.returncode = 0

    def wait(self) -> int:
        return self.returncode


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
    with pytest.raises(ValueError, match="unknown parent surface"):
        controller.start_duckdb_direct({**payload, "preflight_token": token, "parent_surface_id": "missing"})
    assert opened == [("source.duckdb", True), ("analysis.duckdb", True)]
    controller.start_duckdb_direct({**payload, "preflight_token": token, "parent_surface_id": "surface-1"})
    assert _wait_direct_finished(controller)["surface_id"] == "surface-2"
    assert parents == ["surface-1"]


def test_analysis_library_ui_and_routes_are_exposed() -> None:
    html = __import__("mrs3.panel", fromlist=["PANEL_HTML"]).PANEL_HTML
    for marker in ("Analysis Library", "analysis_side", "analysis_symbol", "analysis_surface_id", "analysis_run_id", "analysis_dates", "analysis_config", "analysis_output", "analysis_unique", "analysis_economic", "analysis_event", "analysis_plateaus", "analysis_ready", "analysisStatus"):
        assert marker in html
    for endpoint in ("/api/analysis/initialize", "/api/analysis/library", "/api/analysis/rerun", "/api/analysis/compare", "/api/analysis/export"):
        assert endpoint in html or endpoint in __import__("mrs3.panel", fromlist=["_PanelHandler"])._PanelHandler.do_POST.__code__.co_consts


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
    controller.start_duckdb_direct({**request, "preflight_token": flight["token"]})
    result = _wait_direct_finished(controller)
    assert result["surface_id"] == "surface-1"
    assert calls == ["preflight", "preflight", "build"]
    assert [(Path(path).name, read_only, connection.closed) for path, read_only, connection in connections] == [("source.duckdb", True, True), ("source.duckdb", True, True), ("analysis.duckdb", False, True)]
    assert str(tmp_path) not in json.dumps(result)


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
    with pytest.raises(ValueError, match="at least one symbol"):
        controller.start_duckdb_direct({**payload, "preflight_token": document["token"], "selected_symbols": []})
    with pytest.raises(ValueError, match="unavailable"):
        controller.start_duckdb_direct({**payload, "preflight_token": document["token"], "selected_symbols": ["ETHUSDT"]})


def test_direct_build_reports_cancellation_without_leaking_paths(tmp_path: Path) -> None:
    started, release = __import__("threading").Event(), __import__("threading").Event()
    base = DirectPreflight({"BTCUSDT": ("1h",)}, {}, (), MappingProxyType({"kind": "OBSERVED_GRID_CONTRACT"}), ("a" * 64,), (("report-1", "a" * 64),), ("BTCUSDT|LONG|1h|100|3|9",))
    calls = 0
    def inspect(*_: object) -> DirectPreflight:
        nonlocal calls; calls += 1
        return base if calls < 2 else base
    class Connection:
        def close(self) -> None: pass
    def build(*args: object) -> object:
        cancellation, progress = args[-2], args[-1]
        started.set(); progress("MATERIALIZING", materialized_points=1); release.wait(1)
        if cancellation(): raise ValueError(f"cancelled at {tmp_path}")
        return SimpleNamespace(surface_id="surface-1", points=(object(),))
    controller = PanelController(tmp_path, tmp_path / "config.local.json", direct_connection_factory=lambda *_args, **_kwargs: Connection(), direct_preflight_func=inspect, direct_build_func=build)
    controller.duckdb_import_settings({"source_duckdb_path": "source.duckdb", "analysis_duckdb_path": "analysis.duckdb"})
    payload = {"start_utc": "2024-01-01T00:00:00Z", "end_utc": "2024-01-02T00:00:00Z", "side": "LONG", "symbols": ["BTCUSDT"], "shift_start_bp": 100, "shift_end_bp": 100, "shift_step_bp": 100}
    token = controller.duckdb_direct_preflight(payload)["token"]
    controller.start_duckdb_direct({**payload, "preflight_token": token})
    assert started.wait(1); assert controller.cancel_duckdb_direct()["cancel_requested"] is True
    release.set(); status = _wait_direct_finished(controller)
    assert status["publication_state"] == "CANCELLED"
    assert str(tmp_path) not in json.dumps(status)


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
    controller.start_duckdb_direct({**payload, "preflight_token": token})
    status = _wait_direct_finished(controller)
    assert status["publication_state"] == "FAILED"
    assert built is False


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
    controller.start_duckdb_direct({**payload, "preflight_token": token})
    assert _wait_direct_finished(controller)["publication_state"] == "FAILED"
    assert all(connection.closed for connection in opened)


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


def test_http_ui_exposes_persistent_import_settings_and_migration_controls(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True); thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request("GET", "/")
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
        connection.request("GET", "/")
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
