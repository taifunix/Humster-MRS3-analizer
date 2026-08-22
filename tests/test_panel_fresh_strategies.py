from __future__ import annotations

import json
from pathlib import Path
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
    result = controller.strategies_fresh_generate({
        "analysis_path": "run.analysis-v6.duckdb", "analysis_run_id": "a" * 64,
        "candidate_ids": ["candidate"], "selected_scopes": [["BTCUSDT", "LONG", "1h"]],
    })

    args = captured["args"]
    assert Path(args[4]).name == "long.json"
    assert result["strategy_count"] == 1


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
