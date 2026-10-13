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
    target = root / "templates" / "tester" / "mrs3" / "config_tester.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPOSITORY / "templates" / "tester" / "mrs3" / "config_tester.json", target)


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
        lambda: controller.strategies_fresh_generate({"analysis_run_id": "a" * 64}),
        lambda: controller.strategies_fresh_generate_runs({"analysis_run_id": "a" * 64}),
        lambda: controller.start_analysis_strategies(
            {"run_id": "R1", "selected_scopes": [{"symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h"}]}
        ),
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


def test_legacy_tester_run_launches_only_protocol_v2_strategies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path, seed=False)
    use_protocol_v2_target(monkeypatch, check_launch=True)
    strategies = tmp_path / "legacy-strategies"
    strategies.mkdir()
    strategy = protocol_v2_template(exchange="Bybit", my_fix_balance=10000.0)
    strategy["basic"]["use_fix"] = False  # type: ignore[index]
    (strategies / "old.json").write_text(json.dumps(strategy), encoding="utf-8")

    bot_config = tmp_path / "bot-tester.json"
    monkeypatch.setattr(panel_module.RunnerConfig, "from_json", lambda _path: SimpleNamespace(tester_config=bot_config))
    request = {"config": str(tmp_path / "config.json"), "strategies": str(strategies), "output_csv": str(tmp_path / "out.csv")}

    with pytest.raises(EvaluationProtocolError, match="basic.use_fix"):
        controller._build_command("tester-run", request)
    strategy["basic"]["use_fix"] = True  # type: ignore[index]
    (strategies / "old.json").write_text(json.dumps(strategy), encoding="utf-8")
    # The bot tester config is checked too.
    bot_config.write_text(json.dumps({"InitialBalance": 1000, "LimitOrderVolumeCheck": False}), encoding="utf-8")
    with pytest.raises(EvaluationProtocolError, match="InitialBalance"):
        controller._build_command("tester-run", request)
    bot_config.write_text(json.dumps({"InitialBalance": 10000, "LimitOrderVolumeCheck": False}), encoding="utf-8")
    controller._build_command("tester-run", request)
    # A resumed run would reuse an older frozen config snapshot.
    (tmp_path / ".out.tester-config.snapshot").write_text("{}", encoding="utf-8")
    with pytest.raises(EvaluationProtocolError, match="snapshot"):
        controller._build_command("tester-run", request)
