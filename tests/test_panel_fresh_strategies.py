from __future__ import annotations

import json
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace

from mrs3.config import AlgorithmConfig
from mrs3.panel import PanelController


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
