from __future__ import annotations

from http.client import HTTPConnection
import json
from hashlib import sha256
from pathlib import Path
import threading
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from mrs3.config import AlgorithmConfig
from mrs3.panel import PanelController, create_panel_server
from mrs3.panel_jobs import PanelJobError
from mrs3.panel_tester_runs import LocalRunsBatchService


def test_panel_exposes_run_file_generation_action() -> None:
    panel_source = Path("src/mrs3/panel.py").read_text(encoding="utf-8")
    panel_html = Path("src/mrs3/panel_web/index.html").read_text(encoding="utf-8")
    web_source = Path("src/mrs3/panel_web/app.js").read_text(encoding="utf-8")

    assert 'id="shortlist-generate-runs"' in panel_html
    assert "/api/v2/strategies/fresh/runs" in panel_source
    assert "#shortlist-generate-runs" in web_source
    assert 'id="tester-start-runs"' in panel_html
    assert "strategies.tester.runs" in web_source
    assert "Проверить и запустить стратегии" in panel_html


def test_generation_validation_does_not_poison_the_next_request(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths["a" * 64] = tmp_path / "run.analysis-v6.duckdb"

    with pytest.raises(ValueError, match="Phase 2 filters must be booleans"):
        controller.strategies_fresh_generate({
            "analysis_run_id": "a" * 64,
            "candidate_ids": ["candidate"],
            "selected_scopes": [["BTCUSDT", "LONG", "1h"]],
            "filters": {"source_pnl": "true"},
        })

    assert controller._fresh_generation_job is None


def test_generation_thread_start_failure_does_not_poison_the_next_request(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths["a" * 64] = tmp_path / "run.analysis-v6.duckdb"

    class FailingThread:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread capacity unavailable")

    monkeypatch.setattr("mrs3.panel.threading.Thread", FailingThread)

    with pytest.raises(RuntimeError, match="thread capacity unavailable"):
        controller.strategies_fresh_generate({
            "analysis_run_id": "a" * 64,
            "candidate_ids": ["candidate"],
            "selected_scopes": [["BTCUSDT", "LONG", "1h"]],
            "filters": {},
        })

    assert controller._fresh_generation_job is None


def test_http_generation_start_failure_reports_actionable_reason(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())

    def fail_start(_payload):
        raise RuntimeError("thread capacity unavailable")

    monkeypatch.setattr(controller, "strategies_fresh_generate", fail_start)
    server = create_panel_server("127.0.0.1", 0, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        payload = json.dumps({}).encode("utf-8")
        connection.request("POST", "/api/v2/strategies/fresh/generate", payload, {"Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert response.status == 409
    assert body == {"error": "thread capacity unavailable"}


def test_run_files_uses_filtered_ready_candidates(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"; config.write_text("{}", encoding="utf-8")
    template = tmp_path / "Input" / "run_snapshot_2.json"; template.parent.mkdir()
    template.write_text(json.dumps({
        "settings": [{"name": "template", "basic": {"strategy": "mrs3", "symbol": "OLD", "time_frame": "5m", "use_long": True, "use_short": False}, "mrs3": {
            "ma_long": [{"id": 1, "len": 1, "multiplier": 1.0, "lot_x": 0.0}], "ma_short": [],
            "ma_close_long": {"len": 1, "multiplier": 1.0}, "ma_close_short": {"len": 1, "multiplier": 1.0},
        }}], "tester_config": {},
    }), encoding="utf-8")
    tester_config = tmp_path / "bot" / "tester" / "config_tester.json"; tester_config.parent.mkdir(parents=True)
    tester_config.write_text('{"use_runs": false}', encoding="utf-8")
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(
        bot_root=tmp_path / "bot", tester_config=tester_config, max_parallel_submissions=7,
    ))
    rows = tuple({
        "candidate_id": f"C{index}", "structure_id": f"S{index}", "symbol": "BTCUSDT", "side": "LONG",
        "timeframe": "1h", "order_count": 1, "common_close_ma": 7, "filter_status": "READY_AFTER_FILTERS",
        "orders": ({"point_id": f"P{index}", "plateau_id": "PLAT", "open_ma": 5, "shift_bp": 100, "close_support": 1.0, "source_pnl_pct": 10},),
    } for index in range(6))
    monkeypatch.setattr("mrs3.panel.filter_fresh_analysis_candidates", lambda *_args: SimpleNamespace(rows=rows))
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths["a" * 64] = tmp_path / "run.analysis-v6.duckdb"

    result = controller.strategies_fresh_generate_runs({"analysis_run_id": "a" * 64, "filters": {}, "selected_scopes": [["BTCUSDT", "LONG", "1h"]], "start_date": "2026-08-01", "end_date": "2026-08-18"})

    assert result["run_count"] == 6
    assert len(list((tmp_path / "bot" / "tester" / "runs").glob("*.json"))) == 6
    assert json.loads(tester_config.read_text(encoding="utf-8"))["use_runs"] is True


def test_fresh_generation_uses_config_workflow_defaults_not_browser_paths(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "panel_workflow": {
            "listing_dates_path": "input/dates.xlsx",
            "strategy_templates": {"LONG": "input/long.json", "SHORT": "input/short.json"},
        },
    }), encoding="utf-8")
    captured: dict[str, object] = {}

    def generate(*args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(
            run_id="a" * 64, surface_id="surface", strategy_count=1,
            manifest_path=tmp_path / "output" / "strategy_manifest.json",
        )

    monkeypatch.setattr("mrs3.panel.generate_fresh_analysis_strategies", generate)
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths["a" * 64] = tmp_path / "run.analysis-v6.duckdb"
    controller._fresh_analysis_surfaces["a" * 64] = tmp_path / "surface.surface-v6.duckdb"
    started = controller.strategies_fresh_generate({
        "analysis_path": "run.analysis-v6.duckdb", "analysis_run_id": "a" * 64,
        "candidate_ids": ["candidate"], "selected_scopes": [["BTCUSDT", "LONG", "1h"]],
    })

    deadline = monotonic() + 1
    result = controller.strategies_fresh_generation_status(str(started["job_id"]))
    while result["running"] and monotonic() < deadline:
        sleep(0.01)
        result = controller.strategies_fresh_generation_status(str(started["job_id"]))

    args = captured["args"]
    assert Path(args[4]).name == "long.json"
    assert Path(args[5]) == tmp_path / "Output"
    assert result["phase"] == "COMMITTED"
    assert result["strategy_count"] == 1


def test_fresh_batch_is_recovered_from_output_after_panel_restart(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "Output" / "strategy_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("mrs3.panel.validate_strategy_manifest", lambda path: SimpleNamespace(
        manifest_path=Path(path), analysis_run_id="a" * 64,
        provenance={"strategy_json_sha256": {"S0.json": "0" * 64}},
    ))

    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())

    assert controller.strategies_fresh_batch() == {
        "phase": "COMMITTED", "analysis_run_id": "a" * 64, "strategy_count": 1,
    }


def test_generation_status_keeps_safe_generation_error(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({
        "panel_workflow": {
            "strategy_templates": {"LONG": "input/long.json", "SHORT": "input/short.json"},
        },
    }), encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths["a" * 64] = tmp_path / "run.analysis-v6.duckdb"
    monkeypatch.setattr("mrs3.panel.generate_fresh_analysis_strategies", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("panel workflow default is unavailable")))

    started = controller.strategies_fresh_generate({
        "analysis_run_id": "a" * 64, "candidate_ids": ["candidate"],
        "selected_scopes": [["BTCUSDT", "LONG", "1h"]],
    })
    deadline = monotonic() + 1
    result = controller.strategies_fresh_generation_status(str(started["job_id"]))
    while result["running"] and monotonic() < deadline:
        sleep(0.01)
        result = controller.strategies_fresh_generation_status(str(started["job_id"]))

    assert result["phase"] == "FAILED"
    assert result["error"] == "READY JSON generation failed: panel workflow default is unavailable"


def test_generation_status_redacts_path_from_permission_error(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"panel_workflow": {"strategy_templates": {"LONG": "input/long.json"}}}), encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths["a" * 64] = tmp_path / "run.analysis-v6.duckdb"
    monkeypatch.setattr(
        "mrs3.panel.generate_fresh_analysis_strategies",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(13, "Access is denied", tmp_path / "Output" / "strategies")),
    )

    started = controller.strategies_fresh_generate({
        "analysis_run_id": "a" * 64, "candidate_ids": ["candidate"],
        "selected_scopes": [["BTCUSDT", "LONG", "1h"]],
    })
    deadline = monotonic() + 1
    result = controller.strategies_fresh_generation_status(str(started["job_id"]))
    while result["running"] and monotonic() < deadline:
        sleep(0.01)
        result = controller.strategies_fresh_generation_status(str(started["job_id"]))

    assert result["error"] == "READY JSON generation failed: permission denied while publishing strategy files"
    assert str(tmp_path) not in result["error"]


def test_performance_cleanup_requires_boolean_confirmation(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())

    try:
        controller.strategies_performance_dd5({"tester_job_id": "job", "delete_html": "false"})
    except ValueError as error:
        assert "delete_html" in str(error)
    else:
        raise AssertionError("string confirmation must be rejected")


def test_tester_job_uses_the_ui_recovery_kind(tmp_path: Path, monkeypatch) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json", analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    captured: dict[str, object] = {}
    monkeypatch.setattr(controller, "_fresh_strategy_manifest", lambda _analysis_id: tmp_path / "strategy_manifest.json")
    monkeypatch.setattr(controller, "_start_tracked_panel_job", lambda kind, *_args: captured.setdefault("kind", kind))

    controller.strategies_tester_start({"analysis_run_id": "a" * 64, "start_date": "2026-01-01", "end_date": "2026-01-31"})

    assert captured["kind"] == "strategies.tester.start"


def test_committed_tester_inbox_readiness_survives_panel_reload(tmp_path: Path) -> None:
    controller = PanelController(tmp_path, tmp_path / "config.local.json", analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    job = controller._panel_jobs.submit("strategies.tester.runs", {}, "tester", ("strategies.tester",))
    controller._panel_jobs.transition(job["job_id"], "RUNNING")

    controller._record_special_job({"job_id": job["job_id"], "state": "COMMITTED", "inbox_path": str(tmp_path / "inbox")})

    assert controller._panel_jobs.get(job["job_id"])["inbox_ready"] is True


def test_existing_committed_tester_inbox_is_marked_ready_on_panel_reload(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=inbox_root))
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    job = controller._panel_jobs.submit("strategies.tester.start", {}, "tester", job_id="batch-1")
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    inbox = inbox_root / job["job_id"]
    controller._panel_jobs.transition(job["job_id"], "FAILED")
    controller._panel_jobs.recover_committed(job["job_id"], runtime={"inbox_path": str(inbox)})
    monkeypatch.setattr(controller, "_validate_performance_inbox", lambda _inbox: None)

    controller._reconcile_interrupted_tester_jobs()

    assert controller._panel_jobs.get(job["job_id"])["inbox_ready"] is True


def test_runs_tester_rejects_an_empty_runs_directory(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    bot_root = tmp_path / "bot"
    (bot_root / "tester" / "runs").mkdir(parents=True)
    (bot_root / "run_tester.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(bot_root=bot_root))
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())

    try:
        controller.strategies_tester_runs_start({})
    except ValueError as error:
        assert str(error) == "RUNS_EMPTY"
    else:
        raise AssertionError("empty runs directory must be rejected")


def test_runs_tester_clears_only_its_report_directory_and_counts_html(tmp_path: Path) -> None:
    root = tmp_path / "bot"
    runs = root / "tester" / "runs"; runs.mkdir(parents=True)
    entries = []
    for index, name in enumerate(("one", "two"), start=1):
        filename = f"{index:03d}.json"; snapshot = {"settings": [{"name": name, "exchange": {"name": "Bybit"}}]}
        path = runs / filename; path.write_text(json.dumps(snapshot), encoding="utf-8")
        entries.append({"filename": filename, "strategy_name": name, "snapshot_sha256": sha256(path.read_bytes()).hexdigest(), "strategy_sha256": sha256(json.dumps(snapshot["settings"][0], sort_keys=True, separators=(",", ":")).encode()).hexdigest()})
    manifest = {"schema_version": 1, "analysis_run_id": "a" * 64, "test_start": "2026-08-01", "test_end": "2026-08-18", "entries": entries}
    manifest["generation_manifest_sha256"] = sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (root / "tester" / "runs_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    tester_config = root / "tester" / "config_tester.json"; tester_config.write_text("{}", encoding="utf-8")
    (root / "run_tester.bat").write_text("@echo off\n", encoding="utf-8")
    report = root / "tester" / "report" / "my_test_runs"; report.mkdir(parents=True)
    (report / "old.html").write_text("old", encoding="utf-8")

    class Process:
        def poll(self): return 0
        def terminate(self): raise AssertionError("completed process must not be terminated")

    def launch(_root):
        for name in ("one", "two"):
            (report / f"{name}.html").write_text(f'<pre>{{"name":"{name}","basic":{{"symbol":"ONUSDT"}},"exchange":{{"name":"Bybit"}}}}</pre>', encoding="utf-8")
        return Process()

    inbox = tmp_path / "inbox"; inbox.mkdir()
    service = LocalRunsBatchService(SimpleNamespace(bot_root=root, tester_config=tester_config), launcher=launch, capture_inbox=lambda *_args, **_kwargs: inbox)
    service.start("runs-job")
    deadline = monotonic() + 3
    while service.status("runs-job")["state"] == "RUNNING" and monotonic() < deadline:
        sleep(0.01)
    result = service.status("runs-job")
    assert result["state"] == "COMMITTED"
    assert result["inbox_path"] == str(inbox)
    assert result["progress"] == {"current": 2, "total": 2, "unit": "reports"}
    assert not (report / "old.html").exists()


def test_runs_and_regular_tester_share_the_panel_job_resource(tmp_path: Path) -> None:
    config = tmp_path / "config.local.json"; config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    job = controller._panel_jobs.submit("strategies.tester.start", {}, "regular", ("strategies.tester",))
    controller._panel_jobs.transition(job["job_id"], "RUNNING")

    try:
        controller.strategies_tester_runs_start({})
    except PanelJobError as error:
        assert error.code == "RESOURCE_BUSY"
    else:
        raise AssertionError("RUNS must not overlap the ordinary tester batch")


def test_completed_tester_batch_is_recovered_after_panel_restart(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=inbox_root))
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    job = controller._panel_jobs.submit("strategies.tester.start", {"analysis_run_id": "a" * 64}, "tester", job_id="batch-1")
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    inbox = inbox_root / job["job_id"]
    inbox.mkdir(parents=True)
    (inbox / "inbox_manifest.json").write_text(json.dumps({
        "schema_version": 1, "entries": [{"strategy_name": "one"}], "v6_provenance": {"analysis_run_id": "a" * 64},
    }), encoding="utf-8")
    (inbox_root / f"{job['job_id']}.state.json").write_text(json.dumps({
        "state": "COMPLETED", "expected_names": ["one"], "inbox_path": str(inbox),
    }), encoding="utf-8")

    restarted = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())

    assert restarted.strategies_tester_status(job["job_id"])["state"] == "FAILED"


def test_verified_tester_batch_finishes_inbox_capture_after_restart(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    inbox_root = tmp_path / "inbox"
    monkeypatch.setattr("mrs3.panel.RunnerConfig.from_json", lambda _path: SimpleNamespace(inbox_root=inbox_root))
    monkeypatch.setattr("mrs3.panel.plan_batch", lambda *_args, **_kwargs: SimpleNamespace(resume_remaining_names=()))
    inbox = inbox_root / "batch-1"
    monkeypatch.setattr("mrs3.panel.run_batch", lambda *_args, **_kwargs: SimpleNamespace(inbox_path=inbox))
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    job = controller._panel_jobs.submit("strategies.tester.start", {"analysis_run_id": "a" * 64}, "tester", job_id="batch-1")
    controller._panel_jobs.transition(job["job_id"], "RUNNING")
    inbox_root.mkdir()
    incomplete_inbox = inbox_root / "batch-1"
    incomplete_inbox.mkdir()
    (incomplete_inbox / "partial.html").write_text("partial", encoding="utf-8")
    (inbox_root / "batch-1.state.json").write_text(json.dumps({
        "state": "STOPPED_FOR_CLEANUP", "strategy_source": str(tmp_path / "strategies"),
        "output_csv": str(inbox_root / "batch-1.csv"), "v6_provenance": {"analysis_run_id": "a" * 64},
    }), encoding="utf-8")

    restarted = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    deadline = monotonic() + 1
    while restarted._panel_jobs.get(job["job_id"])["state"] != "COMMITTED" and monotonic() < deadline:
        sleep(0.01)

    assert restarted._panel_jobs.get(job["job_id"])["state"] == "COMMITTED"
    assert not (incomplete_inbox / "partial.html").exists()
