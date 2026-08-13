from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import mrs3.cli as cli
import pytest
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
    assert manifest["algorithm_version"] == "0.7-representative-v2"
    assert manifest["ready_json_count"] == 5


@pytest.mark.parametrize(
    "source_args",
    [
        [],
        ["--input-csv", "input.csv", "--source-package", "package"],
    ],
)
def test_select_cli_requires_exactly_one_input_source(
    tmp_path: Path, source_args: list[str]
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "select",
                *source_args,
                "--dates",
                str(paths["dates"]),
                "--template",
                str(paths["template"]),
                "--side",
                "LONG",
                "--config",
                str(paths["config"]),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )

    assert exc_info.value.code == 2


def test_select_cli_passes_source_package_without_raw_csv(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package = tmp_path / "package"
    called = {}

    def fake_selection(inputs, config):
        called["inputs"] = inputs
        return SimpleNamespace(manifest={"event_mode": "real_independent_events"})

    monkeypatch.setattr(cli, "run_selection", fake_selection)

    code = main(
        [
            "select",
            "--source-package",
            str(package),
            "--dates",
            str(paths["dates"]),
            "--template",
            str(paths["template"]),
            "--side",
            "LONG",
            "--config",
            str(paths["config"]),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )

    assert code == 0
    assert called["inputs"].csv_path is None
    assert called["inputs"].source_package_dir == package
    assert json.loads(capsys.readouterr().out)["event_mode"] == "real_independent_events"


def test_source_csv_cli_builds_legacy_package(tmp_path: Path, capsys) -> None:
    source = tmp_path / "input.csv"
    source.write_text("StartDate,EndDate,TotalTrades\n2026-07-15 00:00:00,2026-08-06 00:00:00,3\n", encoding="utf-8")
    output = tmp_path / "package"

    code = main(["source-csv", "--input-csv", str(source), "--start", "2026-07-15T00:00:00Z", "--end", "2026-08-06T00:00:00Z", "--output-dir", str(output), "--config", str(_write_runner_config(tmp_path))])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["event_mode"] == "legacy_trades_proxy"
    assert (output / "points.csv").is_file()


def test_source_duckdb_cli_dispatches_package_builder(tmp_path: Path, monkeypatch, capsys) -> None:
    called: dict[str, object] = {}

    def fake_builder(database, start, end, output, **kwargs):
        called.update(database=database, start=start, end=end, output=output, **kwargs)
        return SimpleNamespace(manifest={"event_mode": "real_independent_events"})

    monkeypatch.setattr(cli, "build_duckdb_package", fake_builder)
    database = tmp_path / "source.duckdb"
    output = tmp_path / "package"

    code = main(["source-duckdb", "--database", str(database), "--start", "2026-07-15T00:00:00Z", "--end", "2026-08-06T00:00:00Z", "--output-dir", str(output), "--config", str(_write_runner_config(tmp_path))])

    assert code == 0
    assert called["database"] == database
    assert called["verification_html_root"] is None
    assert called["verification_sample_count"] == 3
    assert json.loads(capsys.readouterr().out)["event_mode"] == "real_independent_events"


def test_source_duckdb_cli_passes_optional_html_verification_controls(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    called: dict[str, object] = {}

    def fake_builder(database, start, end, output, **kwargs):
        called.update(
            database=database,
            start=start,
            end=end,
            output=output,
            **kwargs,
        )
        return SimpleNamespace(manifest={"event_mode": "real_independent_events"})

    monkeypatch.setattr(cli, "build_duckdb_package", fake_builder)
    database = tmp_path / "source.duckdb"
    html_root = tmp_path / "html"
    output = tmp_path / "package"

    code = main(
        [
            "source-duckdb",
            "--database",
            str(database),
            "--start",
            "2026-07-15T00:00:00Z",
            "--end",
            "2026-08-06T00:00:00Z",
            "--output-dir",
            str(output),
            "--verify-html-root",
            str(html_root),
            "--verification-sample-count",
            "4",
            "--config",
            str(_write_runner_config(tmp_path)),
        ]
    )

    assert code == 0
    assert called["verification_html_root"] == html_root
    assert called["verification_sample_count"] == 4
    assert json.loads(capsys.readouterr().out)["event_mode"] == "real_independent_events"


@pytest.mark.parametrize("sample_count", ["2", "6"])
def test_source_duckdb_cli_rejects_out_of_range_verification_count_before_builder(
    tmp_path: Path, monkeypatch, sample_count: str
) -> None:
    called = False

    def fake_builder(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("builder must not run")

    monkeypatch.setattr(cli, "build_duckdb_package", fake_builder)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "source-duckdb",
                "--database",
                str(tmp_path / "source.duckdb"),
                "--start",
                "2026-07-15T00:00:00Z",
                "--end",
                "2026-08-06T00:00:00Z",
                "--output-dir",
                str(tmp_path / "package"),
                "--verification-sample-count",
                sample_count,
                "--config",
                str(_write_runner_config(tmp_path)),
            ]
        )

    assert exc_info.value.code == 2
    assert called is False


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

    def fake_posttest(
        results_value, audit_value, strategies_value, output_value, config, analysis_database=None
    ):
        called.update(
            results=results_value,
            audit=audit_value,
            strategies=strategies_value,
            output=output_value,
            analysis_database=analysis_database,
        )
        return SimpleNamespace(
            workbook=output_value / "posttest.xlsx",
            csv_directory=output_value / "posttest_csv",
            manifest=output_value / "posttest_manifest.json",
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
    assert summary == {
        "workbook": str(output / "posttest.xlsx"),
        "csv_directory": str(output / "posttest_csv"),
        "manifest": str(output / "posttest_manifest.json"),
    }


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
