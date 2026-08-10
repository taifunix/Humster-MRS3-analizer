from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import mrs3.cli as cli
from mrs3.cli import main
from tests.factories import write_selection_inputs


def test_select_cli_writes_manifest_and_returns_zero(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    output = tmp_path / "output"

    code = main(
        [
            "select",
            "--input-csv",
            str(paths["csv"]),
            "--dates",
            str(paths["dates"]),
            "--template",
            str(paths["template"]),
            "--side",
            "LONG",
            "--config",
            str(paths["config"]),
            "--output-dir",
            str(output),
        ]
    )

    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert code == 0
    assert manifest["algorithm_version"] == "0.6"
    assert manifest["ready_json_count"] == 5


def _write_runner_config(tmp_path: Path) -> Path:
    bot = tmp_path / "hb"
    bot.mkdir(parents=True)
    (bot / "hb_c.exe").write_bytes(b"test executable")
    path = tmp_path / "runner.json"
    path.write_text(
        json.dumps(
            {
                "tester_runner": {
                    "bot_root": str(bot),
                    "executable": "hb_c.exe",
                    "base_url": "http://127.0.0.1:8087",
                    "port": 8087,
                    "strategy_dir": "settings_strategy",
                    "report_dir": "tester/report/my_test",
                    "wizard_result": "tester/wizard_result.json",
                    "wizard_progress": "tester/wizard_progress.json",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_tester_plan_cli_is_read_only_and_returns_batch(tmp_path: Path, capsys) -> None:
    config = _write_runner_config(tmp_path)
    source = tmp_path / "generated"
    source.mkdir()
    (source / "A.json").write_text(json.dumps({"name": "A"}), encoding="utf-8")

    code = main(
        ["tester-plan", "--config", str(config), "--strategies", str(source)]
    )

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["expected_count"] == 1
    assert output["expected_names"] == ["A"]
    assert sorted(path.name for path in (tmp_path / "hb").iterdir()) == ["hb_c.exe"]


def test_tester_run_cli_dispatches_workflow(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config = _write_runner_config(tmp_path)
    source = tmp_path / "generated"
    source.mkdir()
    (source / "A.json").write_text(json.dumps({"name": "A"}), encoding="utf-8")
    output = tmp_path / "results.csv"
    called: dict[str, Path] = {}

    def fake_run(config_value, source_value, output_value):
        called["source"] = source_value
        called["output"] = output_value
        return SimpleNamespace(
            output_csv=output_value.resolve(),
            state_file=output_value.with_name("results.state.json").resolve(),
            progress_file=output_value.with_name("results.progress.json").resolve(),
            result_rows=1,
            events=("COMPLETED",),
        )

    monkeypatch.setattr(cli, "run_batch", fake_run)

    code = main(
        [
            "tester-run",
            "--config",
            str(config),
            "--strategies",
            str(source),
            "--output-csv",
            str(output),
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert called == {"source": source, "output": output}
    assert summary["result_rows"] == 1
    assert summary["progress_file"].endswith("results.progress.json")


def test_posttest_cli_dispatches_dd5_comparison(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    results = tmp_path / "results.csv"
    audit = tmp_path / "audit.xlsx"
    strategies = tmp_path / "strategies"
    output = tmp_path / "posttest"
    called: dict[str, Path] = {}

    def fake_posttest(results_value, audit_value, strategies_value, output_value, config):
        called.update(
            results=results_value,
            audit=audit_value,
            strategies=strategies_value,
            output=output_value,
        )
        return SimpleNamespace(
            workbook=output_value / "posttest.xlsx",
            csv_directory=output_value / "posttest_csv",
            scaled_strategies_dir=output_value / "scaled_strategies",
            manifest=output_value / "posttest_manifest.json",
            scaled_count=3,
        )

    monkeypatch.setattr(cli, "run_posttest", fake_posttest)

    code = main(
        [
            "posttest",
            "--results-csv",
            str(results),
            "--audit-xlsx",
            str(audit),
            "--strategies-dir",
            str(strategies),
            "--config",
            str(paths["config"]),
            "--output-dir",
            str(output),
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert called["results"] == results
    assert summary["scaled_count"] == 3


def test_panel_cli_starts_loopback_server(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    called: dict[str, object] = {}

    def fake_panel(host, port, default_config, *, open_browser):
        called.update(
            host=host,
            port=port,
            config=default_config,
            open_browser=open_browser,
        )

    monkeypatch.setattr(cli, "serve_panel", fake_panel)

    code = main(
        [
            "panel",
            "--host",
            "127.0.0.1",
            "--port",
            "9876",
            "--config",
            str(config),
            "--no-browser",
        ]
    )

    assert code == 0
    assert called == {
        "host": "127.0.0.1",
        "port": 9876,
        "config": config,
        "open_browser": False,
    }
