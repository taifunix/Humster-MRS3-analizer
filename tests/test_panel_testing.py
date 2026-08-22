from __future__ import annotations

import json
from pathlib import Path

import pytest

from mrs3.panel_testing import (
    LocalTestingService,
    render_strategy,
    render_tester_config,
)
from mrs3.panel import PanelController
from mrs3.runner.config import RunnerConfig


def test_render_tester_config_updates_dates_and_symbols_from_long_template() -> None:
    template = '''{
      "StartDate": "2026-01-01T00:00:00",
      "EndDate": "2026-01-02T00:00:00",
      "parameter_mining": [
        {"name": "settings[*].mrs2.ma_long.len", "values": []},
        {"name": "settings[*].basic.symbol", "values": ["OLDUSDT",]},
      ],
      "report": {"enable_html_report": true}
    }'''

    rendered = json.loads(render_tester_config(template, ("CXUSDT", "BABAUSDT"), "2026-07-15", "2026-08-06"))

    assert rendered["StartDate"] == "2026-07-15T00:00:00"
    assert rendered["EndDate"] == "2026-08-06T00:00:00"
    assert rendered["parameter_mining"][1]["values"] == ["CXUSDT", "BABAUSDT"]
    assert rendered["parameter_mining"][0]["name"] == "settings[*].mrs2.ma_long.len"
    assert rendered["report"] == {"enable_html_report": True}


def test_render_strategy_keeps_one_named_strategy_and_sets_requested_side() -> None:
    template = json.dumps({"name": "AAOIUSDT", "basic": {"symbol": "AAOIUSDT", "use_long": False, "use_short": True}})

    filename, rendered = render_strategy(template, "CXUSDT", "LONG")

    assert filename == "AAOIUSDT.json"
    assert rendered["name"] == "AAOIUSDT"
    assert rendered["basic"] == {"symbol": "CXUSDT", "use_long": True, "use_short": False}


def _runner_config(tmp_path: Path) -> RunnerConfig:
    bot_root = tmp_path / "bot"
    (bot_root / "tester" / "report" / "my_test").mkdir(parents=True)
    (bot_root / "settings_strategy").mkdir()
    executable = bot_root / "hb_c.exe"
    executable.write_bytes(b"tester")
    return RunnerConfig(
        bot_root=bot_root,
        executable_path=executable,
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=bot_root / "settings_strategy",
        report_dir=bot_root / "tester" / "report" / "my_test",
        wizard_result=bot_root / "tester" / "wizard_result.json",
        wizard_progress=bot_root / "tester" / "wizard_progress.json",
        tester_config=bot_root / "tester" / "tester_config.json",
        inbox_root=tmp_path / "inbox",
    )


def test_local_testing_status_is_read_only_redacted_and_reports_disk(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    service = LocalTestingService(config, Path(__file__).parents[1])

    status = service.status()

    assert status == {
        "preflight_ok": True,
        "bot": {"exists": True, "executable": True},
        "report": {"exists": True},
        "strategy": {"exists": True},
        "disk_free_bytes": status["disk_free_bytes"],
    }
    assert isinstance(status["disk_free_bytes"], int)
    assert str(config.bot_root) not in json.dumps(status)
    assert not config.tester_config.exists()


def test_local_testing_prepare_stages_selected_side_without_touching_bot(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    bot_before = {
        path.relative_to(config.bot_root): path.read_bytes()
        for path in config.bot_root.rglob("*")
        if path.is_file()
    }
    service = LocalTestingService(config, Path(__file__).parents[1])

    prepared = service.prepare(
        side="short",
        symbols=("CXUSDT", "BABAUSDT"),
        start="2026-07-15",
        end="2026-08-06",
        output_dir=config.inbox_root / "panel-testing",
    )

    rendered_config = json.loads(prepared.tester_config.read_text(encoding="utf-8"))
    assert rendered_config["StartDate"] == "2026-07-15T00:00:00"
    assert rendered_config["EndDate"] == "2026-08-06T00:00:00"
    symbols = next(
        item["values"]
        for item in rendered_config["parameter_mining"]
        if item["name"] == "settings[*].basic.symbol"
    )
    assert symbols == ["CXUSDT", "BABAUSDT"]
    files = tuple(prepared.strategy_source.glob("*.json"))
    assert len(files) == 1
    strategy = json.loads(files[0].read_text(encoding="utf-8"))
    assert files[0].stem == strategy["name"]
    assert strategy["basic"]["symbol"] == "CXUSDT"
    assert strategy["basic"]["use_long"] is False
    assert strategy["basic"]["use_short"] is True
    assert {
        path.relative_to(config.bot_root): path.read_bytes()
        for path in config.bot_root.rglob("*")
        if path.is_file()
    } == bot_before
    assert not config.tester_config.exists()


def test_local_testing_prepare_uses_isolated_directory_without_deleting_workspace_files(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    workspace = config.inbox_root / "panel-testing"
    old_strategy = workspace / "strategies" / "keep.json"
    old_strategy.parent.mkdir(parents=True)
    old_strategy.write_text('{"name":"keep"}', encoding="utf-8")
    service = LocalTestingService(config, Path(__file__).parents[1])

    prepared = service.prepare(
        side="LONG",
        symbols=("CXUSDT",),
        start="2026-07-15",
        end="2026-08-06",
        output_dir=workspace,
    )

    assert old_strategy.exists()
    assert prepared.strategy_source != old_strategy.parent
    assert str(tmp_path) not in json.dumps(prepared.as_dict())


def test_local_testing_prepare_rejects_arbitrary_staging_directory(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    service = LocalTestingService(config, Path(__file__).parents[1])

    with pytest.raises(Exception, match="staging output"):
        service.prepare(
            side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06",
            output_dir=tmp_path / "user-owned-directory",
        )


def test_local_testing_fill_installs_exactly_one_strategy_and_config_without_clearing_reports(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    existing_report = config.report_dir / "keep.html"
    existing_report.write_text("keep", encoding="utf-8")
    calls: list[tuple[Path, tuple[str, ...], bool]] = []

    def install(_config: RunnerConfig, source: Path, *, selected_names, preserve_raw_artifacts):
        calls.append((source, selected_names, preserve_raw_artifacts))

    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        install_batch=install,
    )

    filled = service.fill(
        side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06"
    )

    assert calls and calls[0][1:] == (("AAOIUSDT",), True)
    assert existing_report.read_text(encoding="utf-8") == "keep"
    assert json.loads(config.tester_config.read_text(encoding="utf-8"))["StartDate"] == "2026-07-15T00:00:00"
    assert filled["strategy_name"] == "AAOIUSDT"


def test_local_testing_fill_replaces_all_root_strategy_json_with_exactly_one_rendered_file(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    (config.strategy_dir / "old-one.json").write_text('{"name":"old-one"}', encoding="utf-8")
    (config.strategy_dir / "old-two.json").write_text('{"name":"old-two"}', encoding="utf-8")
    service = LocalTestingService(config, Path(__file__).parents[1])

    service.fill(side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06")

    installed = tuple(config.strategy_dir.glob("*.json"))
    assert [path.name for path in installed] == ["AAOIUSDT.json"]
    assert json.loads(installed[0].read_text(encoding="utf-8"))["basic"]["symbol"] == "CXUSDT"


def test_local_testing_start_and_stop_delegate_only_after_preflight(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    calls: list[str] = []
    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        start_bot=lambda _config: calls.append("start") or object(),
        stop_bot=lambda _config: calls.append("stop") or object(),
    )

    assert service.start() == {"state": "STARTED"}
    assert service.stop() == {"state": "STOPPED"}
    assert calls == ["start", "stop"]


def test_panel_controller_exposes_local_testing_preflight_without_paths(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    document = {
        "tester_runner": {
            "bot_root": str(config.bot_root), "executable": "hb_c.exe",
            "base_url": config.base_url, "port": config.port,
            "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test",
            "wizard_result": "tester/wizard_result.json", "wizard_progress": "tester/wizard_progress.json",
            "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
        }
    }
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")

    status = PanelController(tmp_path, config_path).local_testing_status()

    assert status["preflight_ok"] is True
    assert str(config.bot_root) not in json.dumps(status)


def test_panel_controller_fills_one_local_strategy_and_tester_config(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    document = {"tester_runner": {
        "bot_root": str(config.bot_root), "executable": "hb_c.exe", "base_url": config.base_url, "port": config.port,
        "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test", "wizard_result": "tester/wizard_result.json",
        "wizard_progress": "tester/wizard_progress.json", "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
    }}
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")

    prepared = PanelController(tmp_path, config_path).local_testing_fill({
        "symbols": "CXUSDT, BABAUSDT", "side": "SHORT", "start": "2026-07-15", "end": "2026-08-06",
    })

    assert prepared["side"] == "SHORT"
    assert prepared["symbols"] == ["CXUSDT", "BABAUSDT"]
    assert str(config.bot_root) not in json.dumps(prepared)
    assert config.tester_config.is_file()
    assert tuple(config.strategy_dir.glob("*.json"))
