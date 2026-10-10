"""Panel CSV Direct over HTTP -> unchanged shortlist -> READY JSON (no live tester)."""
from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import threading
import time

import duckdb
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.fresh_analysis_strategies import read_fresh_analysis_identity
from mrs3.panel import PanelController, create_panel_server
from tests.csv_direct_support import canonical_grid, snapshot, write_tester_zip
from tests.test_fresh_analysis_strategies import _template

ROUTE = "/api/v2/strategies/fresh/analyze-csv"


def make_controller(tmp_path: Path):
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    dates = tmp_path / "listing.csv"
    dates.write_text("ticker,launch\nXYZUSDT,2026-01-01\n", encoding="utf8")
    template = tmp_path / "strategy_template.json"
    template.write_text(json.dumps(_template()), encoding="utf8")
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "panel": {"default_root": "static", "path_defaults": {"analysis_db_root": str(analysis)}},
        "panel_workflow": {"listing_dates_path": str(dates), "algorithm_version": "0.7-canonical-phase1",
                           "strategy_templates": {"LONG": str(template)}},
    }), encoding="utf8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig())
    return controller, analysis


class Http:
    def __init__(self, controller):
        self.server = create_panel_server("127.0.0.1", 0, controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, route, payload):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=120)
        try:
            conn.request("POST", route, body=json.dumps(payload), headers={"Content-Type": "application/json"})
            res = conn.getresponse()
            return res.status, json.loads(res.read())
        finally:
            conn.close()

    def get(self, route):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        try:
            conn.request("GET", route)
            res = conn.getresponse()
            return res.status, res.read().decode("utf8")
        finally:
            conn.close()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def http(tmp_path):
    controller, analysis = make_controller(tmp_path)
    instance = Http(controller)
    try:
        yield instance, controller, analysis
    finally:
        instance.close()


@pytest.fixture
def bundle(tmp_path):
    return write_tester_zip(tmp_path / "canonical_144.zip", canonical_grid())


def _shortlist_options(analysis_id, ab=False):
    return {"analysis_run_id": analysis_id, "filter_version": "shortlist-v2",
            "pretest_ab_enabled": ab, "ladder_enabled": False, "pareto_enabled": False}


def test_http_csv_full_chain_to_original_ready_json(http, bundle):
    client, controller, analysis = http
    code, result = client.request(ROUTE, {"source_path": str(bundle)})
    assert code == 200 and result["phase"] == "COMMITTED", result
    assert (result["point_count"], result["scope_count"]) == (144, 1)
    db = Path(result["analysis_path"])
    assert db.parent == analysis and read_fresh_analysis_identity(db)["analysis_run_id"] == result["analysis_run_id"]
    with duckdb.connect(str(db), read_only=True) as connection:
        assert connection.execute("select count(*) from points").fetchone()[0] == 144
    options = _shortlist_options(result["analysis_run_id"])
    code, shortlist = client.request("/api/v2/strategies/fresh/shortlist", options)
    assert code == 200, shortlist
    assert shortlist["pretest_ab_available"] is False
    picked = [item["candidate_id"] for item in shortlist["items"] if item["filter_status"] == "READY_AFTER_FILTERS"]
    assert picked
    code, started = client.request("/api/v2/strategies/fresh/generate", {
        **options, "selection_token": shortlist["selection_token"],
        "candidate_ids": picked, "selected_scopes": [["XYZUSDT", "LONG", "30m"]],
    })
    assert code == 200, started
    for _ in range(400):
        code, text = client.get(f"/api/v2/strategies/fresh/generate/status?job_id={started['job_id']}")
        assert code == 200, text
        state = json.loads(text)
        if state["phase"] in {"COMMITTED", "FAILED"}:
            break
        time.sleep(0.05)
    assert state["phase"] == "COMMITTED", state
    assert state["strategy_count"] == len(picked)
    assert (Path(state["output_dir"]) / "strategy_manifest.json").is_file()
    catalog = controller.analysis_catalog()["analyses"]
    assert any(item["analysis_run_id"] == result["analysis_run_id"] for item in catalog)
    assert controller.strategies_fresh_open({"analysis_ref": db.name})["analysis_run_id"] == result["analysis_run_id"]
    assert not list(analysis.parent.rglob("*.surface-v6.duckdb")) and not list(analysis.parent.rglob("*.source-v6.duckdb"))


def test_enabled_ab_gate_is_refused(http, bundle):
    client, _controller, _analysis = http
    code, result = client.request(ROUTE, {"source_path": str(bundle)})
    assert code == 200, result
    code, response = client.request("/api/v2/strategies/fresh/shortlist", _shortlist_options(result["analysis_run_id"], ab=True))
    # The panel sanitizes the core PRETEST_AB_EVIDENCE_UNAVAILABLE refusal; the UI never sends it.
    assert code == 400, response


def test_target_must_stay_in_analysis_root_and_never_overwrites(http, bundle, tmp_path):
    client, _controller, analysis = http
    outside = tmp_path / "outside.analysis-v6.duckdb"
    code, response = client.request(ROUTE, {"source_path": str(bundle), "target_path": str(outside)})
    assert code == 400 and not outside.exists(), response
    target = analysis / "named.analysis-v6.duckdb"
    code, response = client.request(ROUTE, {"source_path": str(bundle), "target_path": str(target)})
    assert code == 200, response
    data = target.read_bytes()
    code, response = client.request(ROUTE, {"source_path": str(bundle), "target_path": str(target)})
    assert code == 400 and target.read_bytes() == data, response


@pytest.mark.parametrize("name,content", [("missing.zip", None), ("broken.zip", b"not-a-zip")])
def test_invalid_sources_create_no_database(http, tmp_path, name, content):
    client, _controller, analysis = http
    source = tmp_path / name
    if content is not None:
        source.write_bytes(content)
    code, response = client.request(ROUTE, {"source_path": str(source)})
    assert code == 400, response
    assert not list(analysis.glob("*.duckdb"))


def test_non_fixed_lot_run_is_refused(http, tmp_path):
    client, _controller, analysis = http
    source = write_tester_zip(tmp_path / "compounding.zip", canonical_grid(), snapshot(use_fix=False))
    code, response = client.request(ROUTE, {"source_path": str(source)})
    assert code == 400, response
    assert not list(analysis.glob("*.duckdb"))


def test_missing_listing_dates_returns_http_error(http, bundle):
    client, controller, analysis = http
    controller._workflow_default("listing_dates_path").unlink()
    code, response = client.request(ROUTE, {"source_path": str(bundle)})
    assert code == 400, response
    assert not list(analysis.glob("*.duckdb"))


def test_csv_source_symlink_is_not_accepted(http, bundle, tmp_path):
    client, _controller, analysis = http
    link = tmp_path / "external.zip"
    try:
        link.symlink_to(bundle)
    except OSError:
        pytest.skip("symlinks are not permitted for this user")
    code, response = client.request(ROUTE, {"source_path": str(link)})
    assert code == 400, response
    assert not list(analysis.glob("*.duckdb"))


def test_static_ui_contains_csv_source_endpoint_and_ab_unavailable_state(http):
    client, _controller, _analysis = http
    code, html = client.get("/")
    assert code == 200 and "analysis-csv-source" in html and "analysis-csv-start" in html
    assert 'id="shortlist-filter-pretest-ab-note" hidden' in html
    code, script = client.get("/panel-web/app.js")
    assert code == 200 and ROUTE in script
    assert "pretest_ab_available" in script


def test_rerunning_the_same_csv_reuses_its_analysis(http, bundle):
    client, _controller, analysis = http
    code, first = client.request(ROUTE, {"source_path": str(bundle)})
    assert code == 200, first
    code, second = client.request(ROUTE, {"source_path": str(bundle)})
    assert code == 200, second
    assert second["analysis_run_id"] == first["analysis_run_id"]
    assert second["analysis_path"] == first["analysis_path"]
    assert Path(first["analysis_path"]).name == f"csv-{first['analysis_run_id'][:24]}.analysis-v6.duckdb"
    assert len(list(analysis.glob("*.duckdb"))) == 1


def test_bare_target_name_is_placed_in_analysis_root(http, bundle):
    client, _controller, analysis = http
    code, result = client.request(ROUTE, {"source_path": str(bundle), "target_path": "named.analysis-v6.duckdb"})
    assert code == 200, result
    assert Path(result["analysis_path"]) == (analysis / "named.analysis-v6.duckdb").resolve()
    code, again = client.request(ROUTE, {"source_path": str(bundle), "target_path": "named.analysis-v6.duckdb"})
    assert code == 400 and "уже есть" in again["error"], again
    code, escape = client.request(ROUTE, {"source_path": str(bundle), "target_path": "../escape.analysis-v6.duckdb"})
    assert code == 400 and "внутри папки Analysis DB" in escape["error"], escape


def test_input_errors_are_specific_and_path_free(http, tmp_path):
    client, _controller, _analysis = http
    source = write_tester_zip(tmp_path / "compounding.zip", canonical_grid(), snapshot(use_fix=False))
    code, response = client.request(ROUTE, {"source_path": str(source)})
    assert code == 400 and "only fixed-lot runs" in response["error"], response
    assert str(tmp_path) not in response["error"]


@pytest.mark.parametrize("version", ["1", "true"])
def test_json_like_algorithm_version_is_refused(http, bundle, version):
    client, _controller, analysis = http
    code, response = client.request(ROUTE, {"source_path": str(bundle), "algorithm_version": version})
    assert code == 400 and "версия алгоритма" in response["error"], response
    assert not list(analysis.glob("*.duckdb"))
