"""Panel MRS3 flows under protocol v2 (protocol v2 §4.3, §7a)."""

from __future__ import annotations

import json
import shutil
import threading
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

import mrs3.panel as panel_module
from mrs3.evaluation_protocol import EvaluationProtocolError
from mrs3.panel import create_panel_server
from mrs3.performance_v2_finalist_retest import FinalistRetestError
from mrs3.performance_v2_store import initialize_performance_v2
from tests._protocol_v2 import REPOSITORY, protocol_v2_run_snapshot, protocol_v2_template, use_protocol_v2_target
from tests.test_panel_performance_v2_retest import _controller


def _copy_tester_template(root: Path) -> None:
    """The exchange template folders of the repository under a test project root."""
    target = root / "templates" / "exchanges"
    if not target.exists():
        shutil.copytree(REPOSITORY / "templates" / "exchanges", target)


def _protocol_v2_database(root: Path, exchange: str = "Bybit") -> None:
    (root / "config.performance.json").write_text(json.dumps({"unified_performance_v2": {
        "database_root": "performance-v2", "workers": 1, "evaluation_protocol": "v2", "exchange": exchange,
    }}), encoding="utf-8")
    database = root / "performance-v2" / "strategy_performance.duckdb"
    database.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database)) as connection:
        initialize_performance_v2(connection, evaluation_protocol="v2", exchange=exchange)


def test_a_v1_database_refuses_every_mrs3_flow_before_rendering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path)
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: tmp_path / "manifest.json")
    flows = (
        lambda: controller.strategies_performance_v2_retest_start({}),
        lambda: controller.strategies_performance_v2_finalist_retest_start({}),
        lambda: controller.strategies_tester_runs_start({}),
        lambda: controller.strategies_tester_start(
            {"analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31"}
        ),
    )
    for flow in flows:
        with pytest.raises(EvaluationProtocolError) as error:
            flow()
        assert error.value.code == "DATABASE_PROTOCOL_V1"


def test_http_refusal_carries_the_protocol_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    controller = _controller(tmp_path)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request(
            "POST", "/api/v2/jobs", json.dumps({"kind": "strategies.tester.runs", "request": {}}).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status == 409
    assert body["error"]["code"] == "DATABASE_PROTOCOL_V1"
    assert controller._panel_jobs.list() == []
    # The handler stops after the refusal: no second response, no server traceback.
    assert "Traceback" not in capsys.readouterr().err


def test_target_of_a_v2_database_uses_the_tester_base_and_exchange(tmp_path: Path) -> None:
    controller = _controller(tmp_path, seed=False)
    _copy_tester_template(tmp_path)
    _protocol_v2_database(tmp_path, "BingX")

    target = controller._evaluation_target()

    assert (str(target.base), target.exchange) == ("10000.0", "BingX")
    assert target.strategy_exchange == (("account", "bingx"),)
    assert dict(target.tester_fields)["TakerFee"] == 0.0003
    assert target.database == "performance-v2"
    shutil.rmtree(tmp_path / "templates" / "exchanges" / "BingX")
    with pytest.raises(EvaluationProtocolError) as error:
        controller._evaluation_target()
    assert error.value.code == "EXCHANGE_TEMPLATES_MISSING"


def test_tester_start_refuses_a_manifest_with_a_non_v2_strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path, seed=False)
    _copy_tester_template(tmp_path)
    use_protocol_v2_target(monkeypatch, check_launch=True)
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: tmp_path / "manifest.json")
    monkeypatch.setattr(panel_module, "validate_strategy_manifest", lambda _path: SimpleNamespace(strategy_source=strategies))
    started: list[str] = []
    monkeypatch.setattr(controller, "_start_tracked_panel_job", lambda kind, *_args, **_kwargs: started.append(kind) or {})
    request = {"analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31"}
    strategy = protocol_v2_template(exchange="Bybit", my_fix_balance=10000.0)

    (strategies / "good.json").write_text(json.dumps(strategy), encoding="utf-8")
    controller.strategies_tester_start(request)
    strategy["exchange"]["use_upnl"] = True  # type: ignore[index]
    (strategies / "old.json").write_text(json.dumps(strategy), encoding="utf-8")
    with pytest.raises(EvaluationProtocolError, match="exchange.use_upnl"):
        controller.strategies_tester_start(request)

    assert started == ["strategies.tester.start"]


def test_runs_start_refuses_a_non_v2_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path, seed=False)
    use_protocol_v2_target(monkeypatch, check_launch=True)
    runs = tmp_path / "bot" / "tester" / "runs"
    runs.mkdir(parents=True)
    monkeypatch.setattr(panel_module.RunnerConfig, "from_json", lambda _path: SimpleNamespace(bot_root=tmp_path / "bot"))
    snapshot = protocol_v2_run_snapshot()
    snapshot["settings"][0].update({"basic": {**snapshot["settings"][0]["basic"], "my_fix_balance": 10000.0}})  # type: ignore[index]
    snapshot["settings"][0]["exchange"]["name"] = "Bybit"  # type: ignore[index]
    (runs / "001_alpha.json").write_text(json.dumps(snapshot), encoding="utf-8")
    controller._require_protocol_v2_runs(controller._evaluation_target())

    snapshot["tester_config"]["LimitOrderVolumeCheck"] = True  # type: ignore[index]
    (runs / "001_alpha.json").write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(EvaluationProtocolError, match="LimitOrderVolumeCheck"):
        controller.strategies_tester_runs_start({})


def test_retests_reject_a_start_balance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path)
    use_protocol_v2_target(monkeypatch)

    with pytest.raises(ValueError, match="unsupported fields"):
        controller.strategies_performance_v2_retest_start({"initial_balance": 10000})
    with pytest.raises(FinalistRetestError) as error:
        controller.strategies_performance_v2_finalist_retest_start({"initial_balance": 10000})
    assert error.value.code == "INVALID_REQUEST"


def _failed_tester_job(controller, runtime: dict[str, object]) -> str:
    job_id = "source-tester"
    controller._panel_jobs.submit("strategies.tester.start", {}, f"panel:{job_id}", ("strategies.tester",), job_id=job_id)
    controller._panel_jobs.transition(job_id, "RUNNING")
    controller._panel_jobs.sync(job_id, {"state": "FAILED", "phase": "FAILED"}, runtime=runtime)
    return job_id


def test_retry_refuses_a_job_rendered_before_protocol_v2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path, seed=False)
    target = use_protocol_v2_target(monkeypatch)
    retried: list[str] = []
    monkeypatch.setattr(controller, "_start_tracked_panel_job", lambda kind, *_args, **_kwargs: retried.append(kind) or {})

    with pytest.raises(panel_module.PanelJobError, match="RETRY_PRE_PROTOCOL_V2"):
        controller.strategies_tester_retry({"job_id": _failed_tester_job(controller, {})})
    controller._panel_jobs = type(controller._panel_jobs)(tmp_path / "jobs2.json")
    controller.strategies_tester_retry({"job_id": _failed_tester_job(controller, controller._protocol_v2_runtime(target))})

    assert retried == ["strategies.tester.retry"]


def test_bingx_launches_use_the_bingx_templates(tmp_path: Path) -> None:
    from mrs3.evaluation_protocol import require_protocol_v2_tester_config

    controller = _controller(tmp_path, seed=False)
    _copy_tester_template(tmp_path)
    _protocol_v2_database(tmp_path, "BingX")
    target = controller._evaluation_target()
    folder = tmp_path / "templates" / "exchanges" / "BingX"

    assert controller._mrs3_tester_template(target) == folder / "mrs3-tester.json"
    assert controller._mrs3_strategy_templates(target) == {
        "LONG": str(folder / "mrs3-strategy.json"), "SHORT": str(folder / "mrs3-strategy.json"),
    }
    controller._require_protocol_v2_tester_template(target)
    bybit = json.loads((tmp_path / "templates" / "exchanges" / "Bybit" / "mrs3-tester.json").read_text(encoding="utf-8"))
    with pytest.raises(EvaluationProtocolError, match="MakerFee, TakerFee"):
        require_protocol_v2_tester_config(bybit, target)


def test_bybit_launches_use_the_bybit_templates(tmp_path: Path) -> None:
    controller = _controller(tmp_path, seed=False)
    _copy_tester_template(tmp_path)
    _protocol_v2_database(tmp_path, "Bybit")

    target = controller._evaluation_target()

    folder = tmp_path / "templates" / "exchanges" / "Bybit"
    assert controller._mrs3_tester_template(target) == folder / "mrs3-tester.json"
    assert controller._mrs3_strategy_templates(target)["LONG"] == str(folder / "mrs3-strategy.json")
    assert controller._protocol_v2_runtime(target)["evaluation_profile"] == target.profile_digest


@pytest.mark.parametrize("change", ({"profile_digest": "p" * 64}, {"database": "data/other"}))
def test_retry_refuses_a_job_of_another_database_or_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object],
) -> None:
    from dataclasses import replace

    controller = _controller(tmp_path, seed=False)
    target = use_protocol_v2_target(monkeypatch)
    monkeypatch.setattr(controller, "_start_tracked_panel_job", lambda kind, *_args, **_kwargs: {})

    with pytest.raises(panel_module.PanelJobError, match="RETRY_PRE_PROTOCOL_V2"):
        controller.strategies_tester_retry(
            {"job_id": _failed_tester_job(controller, controller._protocol_v2_runtime(replace(target, **change)))},
        )


def test_listing_dates_use_the_sheet_of_the_exchange(tmp_path: Path) -> None:
    controller = _controller(tmp_path, seed=False)
    _copy_tester_template(tmp_path)
    _protocol_v2_database(tmp_path, "BingX")
    workbook = (tmp_path / "input" / "dates.xlsx").resolve()

    assert controller._listing_dates_source() == (workbook, "Пары_BingX")
    assert controller._retest_listing_context() == (workbook, Path("input/dates.xlsx"), "Пары_BingX")


def test_tester_start_hands_the_exchange_template_to_its_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    controller = _controller(tmp_path, seed=False)
    _copy_tester_template(tmp_path)
    bingx = tmp_path / "templates" / "exchanges" / "BingX" / "mrs3-tester.json"
    target = replace(use_protocol_v2_target(monkeypatch, "BingX"), tester_template=bingx)
    monkeypatch.setattr(panel_module.PanelController, "_evaluation_target", lambda _self, _exchange=None: target)
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: tmp_path / "manifest.json")
    started: dict[str, object] = {}
    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: SimpleNamespace(
        start=lambda *_args, **kwargs: started.update(kwargs) or {}, reconcile_pending_cleanup=lambda: None,
    ))
    monkeypatch.setattr(controller, "_start_tracked_panel_job", lambda _kind, _request, _resources, submit, **_kwargs: submit("job"))

    controller.strategies_tester_start({"analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31"})

    assert started["tester_config_template"] == bingx


def test_import_refuses_a_job_rendered_for_another_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    controller = _controller(tmp_path, seed=False)
    target = use_protocol_v2_target(monkeypatch)
    job_id = "committed-tester"
    controller._panel_jobs.submit("strategies.tester.start", {}, f"panel:{job_id}", ("strategies.tester",), job_id=job_id)
    controller._panel_jobs.transition(job_id, "RUNNING")
    controller._panel_jobs.sync(
        job_id, {"state": "COMMITTED", "phase": "COMMITTED", "inbox_ready": True},
        runtime={
            **controller._protocol_v2_runtime(replace(target, database="data/other")),
            "performance_v2_import_verified": True,
        },
    )

    with pytest.raises(EvaluationProtocolError) as error:
        controller.strategies_performance_v2_import({"tester_job_id": job_id})
    assert error.value.code == "IMPORT_TARGET_CHANGED"
    # The refusal comes before any state change: the verified inbox stays importable.
    assert controller._panel_jobs.runtime(job_id)["performance_v2_import_verified"] is True


def test_import_checks_every_member_of_a_report_collection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    controller = _controller(tmp_path, seed=False)
    target = use_protocol_v2_target(monkeypatch)
    jobs = controller._panel_jobs
    for job_id, runtime in (
        ("member-current", controller._protocol_v2_runtime(target)),
        ("member-other", controller._protocol_v2_runtime(replace(target, profile_digest="p" * 64))),
    ):
        jobs.submit("strategies.tester.start", {}, f"panel:{job_id}", ("strategies.tester",), job_id=job_id)
        jobs.transition(job_id, "RUNNING")
        jobs.sync(job_id, {"state": "COMMITTED", "phase": "COMMITTED"}, runtime=runtime)
    members = [{"tester_job_id": "member-current", "superseded": False}, {"tester_job_id": "member-other", "superseded": False}]

    controller._require_import_target("member-current", False)
    jobs.submit("strategies.tester.collection", {}, "panel:collection", (), job_id="collection")
    jobs.transition("collection", "RUNNING")
    jobs.sync("collection", {"state": "COMMITTED"}, runtime={"members": members})
    with pytest.raises(EvaluationProtocolError, match="IMPORT_TARGET_CHANGED"):
        controller._require_import_target("collection", True)
    members[1]["superseded"] = True
    jobs.sync("collection", {"state": "COMMITTED"}, runtime={"members": members})
    controller._require_import_target("collection", True)
