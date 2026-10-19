"""Panel settings: list, switch and create PerformanceDBs (P7 spec §5)."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import duckdb
import pytest

from mrs3.exchange_profiles import PerformanceDatabaseError
from mrs3.panel import create_panel_server
from mrs3.performance_v2_store import database_identity, load_performance_v2_config
from tests.test_panel_performance_v2_retest import _controller

def _setup(tmp_path: Path, *, templates: bool = True):
    from tests.test_panel_protocol_v2 import _copy_tester_template

    controller = _controller(tmp_path, seed=False)
    (tmp_path / "config.performance.json").write_text(json.dumps({"unified_performance_v2": {
        "database_root": "data/performance-v2", "workers": 1,
    }}), encoding="utf-8")
    if templates:
        _copy_tester_template(tmp_path)
    return controller, controller.default_config.with_name("config.local.json")


def _performance_config(tmp_path: Path):
    return load_performance_v2_config(tmp_path / "config.performance.json", v1_database_root=tmp_path / "legacy")


def test_without_entries_only_the_default_database_is_listed(tmp_path: Path) -> None:
    controller, _local = _setup(tmp_path, templates=False)

    result = controller.performance_databases_get()

    assert result["active"] is None and result["busy"] is False
    assert result["exchanges"] == []
    assert [(row["name"], row["database_root"], row["active"]) for row in result["items"]] == [
        (None, "data/performance-v2", True),
    ]


def test_create_writes_an_empty_v2_database_with_markers_and_keeps_the_config(tmp_path: Path) -> None:
    controller, local = _setup(tmp_path)
    before = json.loads(local.read_text(encoding="utf-8"))

    result = controller.performance_databases_create(
        {"name": "bingx-v2", "database_root": "data/performance-v2-bingx", "exchange": "BingX"},
    )

    database = tmp_path / "data" / "performance-v2-bingx" / "strategy_performance.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        assert database_identity(connection) == ("v2", "BingX")
    after = json.loads(local.read_text(encoding="utf-8"))
    assert {key: value for key, value in after.items() if key != "performance_databases"} == before
    assert after["performance_databases"] == {"active": None, "items": {"bingx-v2": {
        "database_root": "data/performance-v2-bingx", "evaluation_protocol": "v2", "exchange": "BingX",
    }}}
    row = next(row for row in result["items"] if row["name"] == "bingx-v2")
    assert row["active"] is False
    assert row["markers"]["evaluation_protocol"] == "v2" and row["markers"]["exchange"] == "BingX"
    # Creating does not switch: the default database stays active.
    assert _performance_config(tmp_path).exchange is None


@pytest.mark.parametrize("payload, code", [
    ({"name": "x", "database_root": "data/x", "exchange": "OKX"}, "EXCHANGE_TEMPLATES_MISSING"),
    ({"name": "x", "database_root": "results/x", "exchange": "BingX"}, "INVALID_REQUEST"),
    ({"name": "x", "database_root": "data/../x", "exchange": "BingX"}, "INVALID_REQUEST"),
    ({"name": "x", "database_root": "data", "exchange": "BingX"}, "INVALID_REQUEST"),
    ({"name": "bad name", "database_root": "data/x", "exchange": "BingX"}, "INVALID_REQUEST"),
    ({"name": "x", "database_root": "data/performance-v2/inner", "exchange": "BingX"}, "PERFORMANCE_DATABASES_INVALID"),
    ({"name": "x", "database_root": "data/x"}, "INVALID_REQUEST"),
])
def test_create_refuses_an_invalid_request(tmp_path: Path, payload: dict[str, object], code: str) -> None:
    controller, local = _setup(tmp_path)
    before = local.read_bytes()

    with pytest.raises(PerformanceDatabaseError) as error:
        controller.performance_databases_create(payload)

    assert error.value.code == code
    assert local.read_bytes() == before
    assert not (tmp_path / "data" / "x").exists()


def test_create_refuses_a_used_folder_and_a_taken_name(tmp_path: Path) -> None:
    controller, _local = _setup(tmp_path)
    (tmp_path / "data" / "used").mkdir(parents=True)
    (tmp_path / "data" / "used" / "file.txt").write_text("x", encoding="utf-8")

    with pytest.raises(PerformanceDatabaseError, match="new or empty folder"):
        controller.performance_databases_create({"name": "a", "database_root": "data/used", "exchange": "BingX"})
    controller.performance_databases_create({"name": "a", "database_root": "data/a", "exchange": "BingX"})
    for taken in ("a", "A"):
        with pytest.raises(PerformanceDatabaseError) as error:
            controller.performance_databases_create({"name": taken, "database_root": "data/b", "exchange": "BingX"})
        assert error.value.code == "PERFORMANCE_DATABASE_EXISTS"


def test_create_never_writes_into_the_default_database_folder(tmp_path: Path) -> None:
    controller, local = _setup(tmp_path)
    before = local.read_bytes()

    with pytest.raises(PerformanceDatabaseError, match="default database folder"):
        controller.performance_databases_create(
            {"name": "x", "database_root": "data/performance-v2", "exchange": "BingX"},
        )
    assert local.read_bytes() == before
    assert not (tmp_path / "data" / "performance-v2").exists()


def test_a_failed_config_write_removes_the_new_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mrs3.panel as panel_module

    controller, local = _setup(tmp_path)
    before = local.read_bytes()

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(panel_module, "save_performance_databases", fail)
    with pytest.raises(PerformanceDatabaseError) as error:
        controller.performance_databases_create({"name": "x", "database_root": "data/x", "exchange": "BingX"})

    assert error.value.code == "PERFORMANCE_DATABASE_CREATE_FAILED"
    assert not (tmp_path / "data" / "x").exists()
    assert local.read_bytes() == before
    # A folder that existed empty is left empty again, so a retry is accepted.
    (tmp_path / "data" / "empty").mkdir(parents=True)
    with pytest.raises(PerformanceDatabaseError):
        controller.performance_databases_create({"name": "y", "database_root": "data/empty", "exchange": "BingX"})
    assert list((tmp_path / "data" / "empty").iterdir()) == []


def test_the_list_skips_markers_while_a_writer_works(tmp_path: Path) -> None:
    controller, _local = _setup(tmp_path)
    controller.performance_databases_create({"name": "x", "database_root": "data/x", "exchange": "BingX"})
    result: dict[str, object] = {}

    def read_from_another_thread() -> None:
        result.update(controller.performance_databases_get())

    with controller._performance_v2_writer_lock:
        reader = threading.Thread(target=read_from_another_thread)
        reader.start()
        reader.join(timeout=10)

    row = next(row for row in result["items"] if row["name"] == "x")  # type: ignore[union-attr]
    assert row["markers"] is None and row["size_bytes"]
    # An unfinished job may be opening a database: markers are not read either.
    controller._panel_jobs.submit("strategies.tester.start", {}, "panel:busy", ("strategies.tester",), job_id="busy")
    listed = controller.performance_databases_get()
    assert listed["busy"] is True
    assert all(row["markers"] is None for row in listed["items"])


def test_activate_switches_the_database_and_back(tmp_path: Path) -> None:
    controller, _local = _setup(tmp_path)
    controller.performance_databases_create({"name": "bingx-v2", "database_root": "data/bingx", "exchange": "BingX"})

    result = controller.performance_databases_activate({"name": "bingx-v2"})

    assert result["active"] == "bingx-v2"
    config = _performance_config(tmp_path)
    assert (config.database_root, config.evaluation_protocol, config.exchange) == (
        (tmp_path / "data" / "bingx").resolve(), "v2", "BingX",
    )
    assert controller._evaluation_target().exchange == "BingX"
    controller.performance_databases_activate({"name": None})
    assert _performance_config(tmp_path).exchange is None
    with pytest.raises(PerformanceDatabaseError) as error:
        controller.performance_databases_activate({"name": "missing"})
    assert error.value.code == "PERFORMANCE_DATABASE_UNKNOWN"


def test_activate_is_refused_while_a_job_runs_or_a_collection_is_open(tmp_path: Path) -> None:
    controller, local = _setup(tmp_path)
    controller.performance_databases_create({"name": "bingx-v2", "database_root": "data/bingx", "exchange": "BingX"})
    jobs = controller._panel_jobs
    jobs.submit("strategies.tester.start", {}, "panel:running", ("strategies.tester",), job_id="running")
    jobs.transition("running", "RUNNING")
    before = local.read_bytes()

    with pytest.raises(PerformanceDatabaseError) as error:
        controller.performance_databases_activate({"name": "bingx-v2"})
    assert error.value.code == "RESOURCE_BUSY"
    assert local.read_bytes() == before

    jobs.sync("running", {"state": "COMMITTED", "phase": "COMMITTED"})
    jobs.submit("strategies.tester.collection", {}, "panel:collection", (), job_id="collection")
    jobs.transition("collection", "RUNNING")
    jobs.sync("collection", {"state": "COMMITTED"}, runtime={
        "collection_state": "OPEN", "members": [{"tester_job_id": "running", "superseded": False}],
    })
    with pytest.raises(PerformanceDatabaseError, match="RESOURCE_BUSY"):
        controller.performance_databases_activate({"name": "bingx-v2"})
    jobs.sync("collection", {"state": "COMMITTED"}, runtime={"collection_state": "CLEARED", "members": []})
    assert controller.performance_databases_activate({"name": "bingx-v2"})["active"] == "bingx-v2"


def _request(server, method: str, path: str, body: dict[str, object] | None = None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        connection.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def test_http_routes_list_create_and_refuse_with_codes(tmp_path: Path) -> None:
    controller, _local = _setup(tmp_path)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, listed = _request(server, "GET", "/api/v2/settings/performance-databases")
        assert status == 200 and listed["exchanges"] == ["BingX", "Bybit"]
        status, created = _request(server, "POST", "/api/v2/settings/performance-databases/create", {
            "name": "bingx-v2", "database_root": "data/bingx", "exchange": "BingX",
        })
        assert status == 200 and [row["name"] for row in created["items"]] == [None, "bingx-v2"]
        status, refused = _request(server, "POST", "/api/v2/settings/performance-databases/activate", {"name": "nope"})
        assert status == 400 and refused["error"]["code"] == "PERFORMANCE_DATABASE_UNKNOWN"
        controller._panel_jobs.submit("strategies.tester.start", {}, "panel:busy", ("strategies.tester",), job_id="busy")
        status, busy = _request(server, "POST", "/api/v2/settings/performance-databases/activate", {"name": "bingx-v2"})
        assert status == 409 and busy["error"]["code"] == "RESOURCE_BUSY"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_settings_screen_has_the_performance_database_card() -> None:
    web = Path(__file__).resolve().parents[1] / "src" / "mrs3" / "panel_web"
    html = (web / "index.html").read_text(encoding="utf-8")
    script = (web / "app.js").read_text(encoding="utf-8")

    for element in ("performance-databases-card", "performance-databases-rows", "performance-database-name",
                    "performance-database-root", "performance-database-exchange", "performance-database-create"):
        assert f'id="{element}"' in html
    assert "'/api/v2/settings/performance-databases'" in script
    assert "loadPerformanceDatabases();" in script
    # Rows are built with textContent, never as HTML from server values.
    block = script[script.index("function renderPerformanceDatabases"):script.index("async function loadPerformanceDatabases")]
    assert "innerHTML" not in block


def test_create_stores_the_exchange_as_its_template_folder_names_it(tmp_path: Path) -> None:
    controller, local = _setup(tmp_path)

    controller.performance_databases_create({"name": "x", "database_root": "data/x", "exchange": "bingx"})

    assert json.loads(local.read_text(encoding="utf-8"))["performance_databases"]["items"]["x"]["exchange"] == "BingX"


def test_a_database_created_in_the_settings_takes_both_exchanges(tmp_path: Path) -> None:
    # ADR-0083 (user, 2026-10-19): a new database from «Настройки» holds Bybit and BingX strategies together
    from dataclasses import replace

    from mrs3.pair_registry.records import import_tick_tests
    from mrs3.performance_v2_import import PerformanceV2ImportRequest, import_performance_v2
    from tests.test_pair_registry_records import NOW, _registry
    from tests.test_performance_v2_import import _report_2026_10, _request, _strategy_exchange, _write_named_report

    controller, _local = _setup(tmp_path)
    controller.performance_databases_create(
        {"name": "both", "database_root": "data/performance-both", "exchange": "Bybit"})
    root = tmp_path / "data" / "performance-both"
    request, _ = _request(tmp_path / "import", names=("alpha", "BINGX_alpha"), initialize_db=False)
    config = replace(request.config, database_root=root, evaluation_protocol="v2", exchange="Bybit")
    request = PerformanceV2ImportRequest(request.inbox, request.report_root, config,
                                         listing_dates_path=request.listing_dates_path)
    _strategy_exchange(request, "BINGX_alpha", "BingX")                     # the BingX strategy JSON names BingX
    _write_named_report(request, "alpha", _report_2026_10(protocol_v2=True))
    _write_named_report(request, "BINGX_alpha", _report_2026_10(protocol_v2=True, exchange="BingX"))

    result = import_performance_v2(request)

    assert (result.imported_count, result.rejected_count) == (2, 0)
    database = root / "strategy_performance.duckdb"
    with duckdb.connect(str(database), read_only=True) as connection:
        assert database_identity(connection) == ("v2", "Bybit")                 # the home exchange only
        assert connection.execute("select r.exchange from strategies s join strategy_results r "
                                  "on r.result_id = s.current_result_id order by 1").fetchall() == [
            ("BingX",), ("Bybit",)]
    registry = _registry(tmp_path / "registry")
    assert import_tick_tests(registry, database, "bybit", now_ms=NOW) == 1
    assert import_tick_tests(registry, database, "bingx", now_ms=NOW) == 1
