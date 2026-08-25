from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

from mrs3.config import AlgorithmConfig
from mrs3.panel import PanelController


def test_panel_exposes_run_file_generation_action() -> None:
    panel_source = Path("src/mrs3/panel.py").read_text(encoding="utf-8")
    panel_html = Path("src/mrs3/panel_web/index.html").read_text(encoding="utf-8")
    web_source = Path("src/mrs3/panel_web/app.js").read_text(encoding="utf-8")

    assert 'id="shortlist-generate-runs"' in panel_html
    assert "/api/v2/strategies/fresh/runs" in panel_source
    assert "#shortlist-generate-runs" in web_source


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

    assert result["run_count"] == 5
    assert len(list((tmp_path / "bot" / "tester" / "runs").glob("*.json"))) == 5
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
            analysis_run_id="a" * 64, surface_id="surface", strategy_count=1,
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


def test_generation_status_fails_after_generation_error(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.local.json"
    config.write_text("{}", encoding="utf-8")
    controller = PanelController(tmp_path, config, analysis_config_loader=lambda _: AlgorithmConfig.defaults())
    controller._fresh_analysis_paths["a" * 64] = tmp_path / "run.analysis-v6.duckdb"
    monkeypatch.setattr("mrs3.panel.generate_fresh_analysis_strategies", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError()))

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
