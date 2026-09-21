from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess

from openpyxl import Workbook
import pandas as pd
import pytest

from mrs3.panel_testing import (
    LocalTestingService,
    expected_screener_runs,
    render_strategy,
    render_tester_config,
)
from mrs3.locking import TesterTargetBusyError, TesterTargetLock
from mrs3.panel import PanelController, PanelTestingError
from mrs3.runner.config import RunnerConfig
from mrs3.screener.registry import ScreeningRow, write_screening_results
from mrs3.screener.render import render_screener_tester_config


def test_canonical_strategy_templates_are_tracked_by_mode() -> None:
    root = Path(__file__).parents[1]
    expected = {
        "templates/strategies/source-v6-mrs2/long.json": ("mrs2", True, False),
        "templates/strategies/source-v6-mrs2/short.json": ("mrs2", False, True),
        "templates/strategies/retest-mrs3/base.json": ("mrs3", True, False),
    }

    for relative, contract in expected.items():
        document = json.loads((root / relative).read_text(encoding="utf-8"))
        assert (
            document["basic"]["strategy"],
            document["basic"]["use_long"],
            document["basic"]["use_short"],
        ) == contract


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


def test_canonical_mrs2_templates_are_strict_and_render_worker_count() -> None:
    root = Path(__file__).parents[1]
    for name in ("config_tester_long.json", "config_tester_short.json"):
        path = root / "templates" / "tester" / "mrs2" / name
        assert path.is_file()
        template = path.read_text(encoding="utf-8")
        original = json.loads(template)
        assert original["parameter_mining"]
        rendered = json.loads(
            render_tester_config(
                template,
                ("CXUSDT",),
                "2026-07-15",
                "2026-08-06",
                max_parallel_runs=7,
            )
        )
        assert rendered["max_parallel_runs"] == 7
        target = next(item for item in original["parameter_mining"] if item["name"] == "settings[*].basic.symbol")
        rendered_target = next(item for item in rendered["parameter_mining"] if item["name"] == "settings[*].basic.symbol")
        assert rendered_target["values"] == ["CXUSDT"]
        for entry in original["parameter_mining"]:
            if entry["name"] != target["name"]:
                assert next(item for item in rendered["parameter_mining"] if item["name"] == entry["name"]) == entry

        if name == "config_tester_short.json":
            multiplier = next(item for item in original["parameter_mining"] if item["name"].endswith("mrs2.ma_short.multiplier"))
            assert multiplier["start"] == 1.0 and multiplier["end"] == 19.0
            assert multiplier["values"] == [
                "1,003", "1,004", "1,005", "1,006", "1,007", "1,009", "1,011",
                "1,014", "1,017", "1,02", "1,023", "1,027", "1,031", "1,035",
                "1,039", "1,043", "1,047", "1,051", "1,055",
            ]

    mrs3 = root / "templates" / "tester" / "mrs3" / "config_tester.json"
    document = json.loads(mrs3.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert document["parameter_mining"] == []
    assert document["report"]["include_chart_balance"] is True


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


def test_local_testing_prepare_uses_canonical_template_and_configured_workers(tmp_path: Path) -> None:
    config = replace(_runner_config(tmp_path), max_parallel_submissions=7)
    prepared = LocalTestingService(config, Path(__file__).parents[1]).prepare(
        side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06",
        output_dir=config.inbox_root / "panel-testing",
    )

    rendered = json.loads(prepared.tester_config.read_text(encoding="utf-8"))
    assert rendered["max_parallel_runs"] == 7
    assert "parameter_mining" in rendered


def test_local_testing_prepare_accepts_screener_template_and_renderer_override(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    service = LocalTestingService(config, Path(__file__).parents[1])

    prepared = service.prepare(
        side="LONG",
        symbols=("CXUSDT", "BABAUSDT"),
        start="2026-08-01",
        end="2026-09-18",
        output_dir=config.inbox_root / "panel-testing",
        template_override=(
            "templates/tester/mrs2/config_tester_long_screen.json",
            render_screener_tester_config,
        ),
    )

    rendered = json.loads(prepared.tester_config.read_text(encoding="utf-8"))
    mining = rendered["parameter_mining"]
    total_combos = 1
    for entry in mining:
        total_combos *= len(entry["values"])
    assert total_combos == 304 * 2
    symbol_entry = next(e for e in mining if e["name"] == "settings[*].basic.symbol")
    assert symbol_entry["end"] == 2.0
    # The strategy side is unaffected by the config-template override — it
    # still comes from _TEMPLATES[side][1], the same as RUNNER 01.
    files = tuple(prepared.strategy_source.glob("*.json"))
    strategy = json.loads(files[0].read_text(encoding="utf-8"))
    assert strategy["basic"]["use_long"] is True


def test_local_testing_prepare_rejects_config_template_path_escaping_repo_root(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    service = LocalTestingService(config, Path(__file__).parents[1])

    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        service.prepare(
            side="LONG",
            symbols=("CXUSDT",),
            start="2026-07-15",
            end="2026-08-06",
            output_dir=config.inbox_root / "panel-testing",
            template_override=("../../../../etc/passwd", render_tester_config),
        )


def test_expected_screener_runs_rejects_config_template_path_escaping_repo_root(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).parents[1]
    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        expected_screener_runs(repo_root, "../../../../etc/passwd", ("AUSDT",))


def test_local_testing_prepare_default_kwargs_render_exactly_like_runner01(
    tmp_path: Path,
) -> None:
    # Regression: omitting template_override, or explicitly passing None,
    # must render byte-for-byte the same as before this kwarg existed, and
    # the same as explicitly naming RUNNER 01's own canonical LONG template.
    config = _runner_config(tmp_path)
    service = LocalTestingService(config, Path(__file__).parents[1])

    prepared_omitted = service.prepare(
        side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06",
        output_dir=config.inbox_root / "panel-testing",
    )
    rendered_omitted = prepared_omitted.tester_config.read_text(encoding="utf-8")

    prepared_none = service.prepare(
        side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06",
        output_dir=config.inbox_root / "panel-testing",
        template_override=None,
    )
    rendered_none = prepared_none.tester_config.read_text(encoding="utf-8")

    prepared_explicit = service.prepare(
        side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06",
        output_dir=config.inbox_root / "panel-testing",
        template_override=("templates/tester/mrs2/config_tester_long.json", render_tester_config),
    )
    rendered_explicit = prepared_explicit.tester_config.read_text(encoding="utf-8")

    assert rendered_omitted == rendered_none == rendered_explicit


def test_expected_screener_runs_multiplies_static_combos_by_symbol_count() -> None:
    repo_root = Path(__file__).parents[1]
    total = expected_screener_runs(
        repo_root,
        "templates/tester/mrs2/config_tester_long_screen.json",
        ("AUSDT", "BUSDT", "CUSDT"),
    )
    assert total == 304 * 3


def test_expected_screener_runs_rejects_empty_symbols() -> None:
    repo_root = Path(__file__).parents[1]
    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        expected_screener_runs(
            repo_root, "templates/tester/mrs2/config_tester_long_screen.json", ()
        )


def test_expected_screener_runs_rejects_missing_symbol_entry(tmp_path: Path) -> None:
    template = tmp_path / "screen.json"
    template.write_text(
        json.dumps(
            {
                "parameter_mining": [
                    {"name": "settings[*].mrs2.ma_long.len", "values": ["4"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        expected_screener_runs(tmp_path, "screen.json", ("AUSDT",))


def test_expected_screener_runs_rejects_duplicate_symbol_entries(tmp_path: Path) -> None:
    template = tmp_path / "screen.json"
    template.write_text(
        json.dumps(
            {
                "parameter_mining": [
                    {"name": "settings[*].basic.symbol", "values": ["AUSDT"]},
                    {"name": "settings[*].basic.symbol", "values": ["BUSDT"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        expected_screener_runs(tmp_path, "screen.json", ("AUSDT",))


def test_expected_screener_runs_rejects_symbol_without_usdt_suffix() -> None:
    repo_root = Path(__file__).parents[1]
    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        expected_screener_runs(
            repo_root, "templates/tester/mrs2/config_tester_long_screen.json", ("BTC",)
        )


def test_expected_screener_runs_wraps_missing_template_file(tmp_path: Path) -> None:
    with pytest.raises(PanelTestingError, match="invalid tester configuration"):
        expected_screener_runs(tmp_path, "missing_screen.json", ("AUSDT",))


def test_expected_screener_runs_short_template() -> None:
    repo_root = Path(__file__).parents[1]
    total = expected_screener_runs(
        repo_root,
        "templates/tester/mrs2/config_tester_short_screen.json",
        ("AUSDT",),
    )
    assert total == 304


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
        stop_bot=lambda _config: None,
    )

    filled = service.fill(
        side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06"
    )

    assert calls and calls[0][1:] == (("AAOIUSDT",), True)
    assert existing_report.read_text(encoding="utf-8") == "keep"
    assert json.loads(config.tester_config.read_text(encoding="utf-8"))["StartDate"] == "2026-07-15T00:00:00"
    assert filled["strategy_name"] == "AAOIUSDT"
    assert filled["reports_cleared"] is False
    with pytest.raises(TesterTargetBusyError):
        TesterTargetLock(config.bot_root).acquire()
    service.stop()
    assert not config.tester_config.exists()


def test_local_testing_fill_reclaims_dead_same_machine_lock_from_previous_boot(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    stale = TesterTargetLock(config.bot_root).acquire()
    owner = json.loads(stale.path.read_text(encoding="utf-8"))
    owner.update({
        "pid": 999999,
        "process_start_identity": "1.0",
        "boot_identity": "previous-boot",
        "container_identity": "previous-native-runtime",
    })
    stale.path.write_text(json.dumps(owner), encoding="utf-8")
    stale.owner = None
    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        stop_bot=lambda _config: None,
    )

    filled = service.fill(
        side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06"
    )

    assert filled["strategy_name"] == "AAOIUSDT"
    current = json.loads(stale.path.read_text(encoding="utf-8"))
    assert current["acquisition_token"] != owner["acquisition_token"]
    service.stop()


def test_local_testing_fill_optionally_clears_report_contents_after_stopping_tester(
    tmp_path: Path,
) -> None:
    config = _runner_config(tmp_path)
    old_file = config.report_dir / "old.html"
    old_file.write_text("old", encoding="utf-8")
    old_directory = config.report_dir / "old-run"
    old_directory.mkdir()
    (old_directory / "details.json").write_text("old", encoding="utf-8")
    calls: list[str] = []
    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        stop_bot=lambda _config: calls.append("stop"),
    )

    service.fill(
        side="LONG",
        symbols=("CXUSDT",),
        start="2026-07-15",
        end="2026-08-06",
        delete_old_reports=True,
    )

    assert calls == ["stop"]
    assert config.report_dir.is_dir()
    assert tuple(config.report_dir.iterdir()) == ()


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

    class TesterClient:
        def run_tester(self) -> None:
            return None

        def tester_status(self) -> str:
            return "Running"

        def close(self) -> None:
            return None

    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        start_bot=lambda _config: calls.append("start") or object(),
        stop_bot=lambda _config: calls.append("stop") or object(),
        client_factory=lambda _config: TesterClient(),
        sleep=lambda _seconds: None,
    )

    assert service.start() == {"state": "STARTED", "tester_status": "RUNNING"}
    assert service.stop() == {"state": "STOPPED"}
    assert calls == ["start", "stop"]


def test_local_testing_start_waits_then_uses_files_tab_run_endpoint(tmp_path: Path) -> None:
    config = replace(_runner_config(tmp_path), request_timeout_seconds=10)
    calls: list[object] = []

    class TesterClient:
        def run_tester(self) -> None:
            calls.append("files-run")

        def tester_status(self) -> str:
            calls.append("status")
            return "<span>Running</span>"

        def close(self) -> None:
            calls.append("close")

    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        start_bot=lambda _config: calls.append("bot-start") or object(),
        client_factory=lambda _config: TesterClient(),
        sleep=lambda seconds: calls.append(("sleep", seconds)),
    )

    assert service.start() == {"state": "STARTED", "tester_status": "RUNNING"}
    assert calls == ["bot-start", ("sleep", 10), "files-run", "status", "close"]


def test_local_testing_start_stops_bot_when_files_tab_run_request_fails(tmp_path: Path) -> None:
    config = replace(_runner_config(tmp_path), request_timeout_seconds=10)
    calls: list[object] = []

    class TesterClient:
        def run_tester(self) -> None:
            calls.append("files-run")
            raise RuntimeError("run request failed")

        def close(self) -> None:
            calls.append("close")

    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        start_bot=lambda _config: calls.append("bot-start") or object(),
        stop_bot=lambda _config: calls.append("bot-stop") or object(),
        client_factory=lambda _config: TesterClient(),
        sleep=lambda seconds: calls.append(("sleep", seconds)),
    )

    with pytest.raises(RuntimeError, match="run request failed"):
        service.start()

    assert calls == ["bot-start", ("sleep", 10), "files-run", "bot-stop", "close"]
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()


def test_local_testing_start_redacts_unknown_tester_status(tmp_path: Path) -> None:
    calls: list[str] = []

    class TesterClient:
        def run_tester(self) -> None:
            return None

        def tester_status(self) -> str:
            return "<span>D:\\private\\tester-token</span>"

        def close(self) -> None:
            calls.append("close")

    service = LocalTestingService(
        _runner_config(tmp_path),
        Path(__file__).parents[1],
        start_bot=lambda _config: object(),
        client_factory=lambda _config: TesterClient(),
        sleep=lambda _seconds: None,
    )

    result = service.start()

    assert result == {"state": "STARTED", "tester_status": "UNKNOWN"}
    assert "private" not in str(result)
    assert calls == ["close"]


def test_local_testing_start_ignores_client_close_error_after_files_tab_run(tmp_path: Path) -> None:
    calls: list[str] = []

    class TesterClient:
        def run_tester(self) -> None:
            return None

        def tester_status(self) -> str:
            return "Running"

        def close(self) -> None:
            calls.append("close")
            raise RuntimeError("close failed")

    config = _runner_config(tmp_path)
    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        start_bot=lambda _config: object(),
        client_factory=lambda _config: TesterClient(),
        sleep=lambda _seconds: None,
    )

    assert service.start() == {"state": "STARTED", "tester_status": "RUNNING"}
    assert calls == ["close"]
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()


def test_local_testing_keeps_owner_when_process_state_is_unconfirmed(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        start_bot=lambda _config: (_ for _ in ()).throw(RuntimeError("start failed")),
    )

    with pytest.raises(RuntimeError, match="start failed"):
        service.start()

    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()


def test_local_testing_does_not_restore_while_stop_is_unconfirmed(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        stop_bot=lambda _config: None,
    )
    service.fill(side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06")
    service._stop_bot = lambda _config: (_ for _ in ()).throw(RuntimeError("stop failed"))

    with pytest.raises(RuntimeError, match="stop failed"):
        service.stop()

    assert config.tester_config.is_file()
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()


def test_local_fill_does_not_mutate_when_initial_stop_is_unconfirmed(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    existing_report = config.report_dir / "keep.html"
    existing_report.write_text("keep", encoding="utf-8")
    service = LocalTestingService(
        config,
        Path(__file__).parents[1],
        stop_bot=lambda _config: (_ for _ in ()).throw(RuntimeError("stop failed")),
    )

    with pytest.raises(RuntimeError, match="stop failed"):
        service.fill(side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06")

    assert not config.tester_config.exists()
    assert not tuple(config.strategy_dir.glob("*.json"))
    assert existing_report.read_text(encoding="utf-8") == "keep"
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()


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


def test_panel_controller_reports_when_local_tester_files_are_already_prepared(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    document = {"tester_runner": {
        "bot_root": str(config.bot_root), "executable": "hb_c.exe", "base_url": config.base_url, "port": config.port,
        "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test", "wizard_result": "tester/wizard_result.json",
        "wizard_progress": "tester/wizard_progress.json", "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
    }}
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)
    request = {"symbols": "CXUSDT", "side": "LONG", "start": "2026-07-15", "end": "2026-08-06"}

    controller.local_testing_fill(request)

    with pytest.raises(PanelTestingError, match="TESTER_FILES_PREPARED"):
        controller.local_testing_fill(request)


def test_panel_controller_reports_when_another_local_tester_owner_holds_the_lock(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    document = {"tester_runner": {
        "bot_root": str(config.bot_root), "executable": "hb_c.exe", "base_url": config.base_url, "port": config.port,
        "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test", "wizard_result": "tester/wizard_result.json",
        "wizard_progress": "tester/wizard_progress.json", "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
    }}
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    external_owner = TesterTargetLock(config.bot_root).acquire()
    request = {"symbols": "CXUSDT", "side": "LONG", "start": "2026-07-15", "end": "2026-08-06"}

    try:
        with pytest.raises(PanelTestingError, match="TESTER_FILES_PREPARED"):
            PanelController(tmp_path, config_path).local_testing_fill(request)
    finally:
        external_owner.release()


def test_panel_controller_rejects_non_boolean_report_cleanup_request(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    document = {"tester_runner": {
        "bot_root": str(config.bot_root), "executable": "hb_c.exe", "base_url": config.base_url, "port": config.port,
        "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test", "wizard_result": "tester/wizard_result.json",
        "wizard_progress": "tester/wizard_progress.json", "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
    }}
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(Exception, match="invalid testing request"):
        PanelController(tmp_path, config_path).local_testing_fill({
            "symbols": "CXUSDT", "side": "LONG", "start": "2026-07-15", "end": "2026-08-06",
            "delete_old_reports": "yes",
        })


def _tester_runner_document(config: RunnerConfig) -> dict[str, object]:
    return {"tester_runner": {
        "bot_root": str(config.bot_root), "executable": "hb_c.exe", "base_url": config.base_url, "port": config.port,
        "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test", "wizard_result": "tester/wizard_result.json",
        "wizard_progress": "tester/wizard_progress.json", "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
    }}


def test_local_screener_status_matches_local_testing_status(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    assert controller.local_screener_status() == controller.local_testing_status()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AUSDT, BUSDT", ("AUSDT", "BUSDT")),
        ("AUSDT BUSDT\nCUSDT", ("AUSDT", "BUSDT", "CUSDT")),
        (["AUSDT", "BUSDT"], ("AUSDT", "BUSDT")),
    ],
)
def test_local_screener_request_splits_symbols(raw: object, expected: tuple[str, ...]) -> None:
    request = PanelController._local_screener_request(
        {"symbols": raw, "side": "LONG", "start": "2026-08-01", "end": "2026-09-18"}
    )
    assert request["symbols"] == expected


def test_local_screener_request_rejects_empty_symbols() -> None:
    with pytest.raises(PanelTestingError, match="invalid testing request"):
        PanelController._local_screener_request({"symbols": "   ", "side": "LONG"})


def test_panel_controller_fills_screener_long_uses_screening_grid(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    result = controller.local_screener_fill({
        "symbols": "AUSDT\nBUSDT", "side": "long", "start": "2026-08-01", "end": "2026-09-18",
    })

    assert result["side"] == "LONG"
    assert result["expected_runs"] == 304 * 2
    rendered = json.loads(config.tester_config.read_text(encoding="utf-8"))
    mining = rendered["parameter_mining"]
    total = 1
    for entry in mining:
        total *= len(entry["values"])
    assert total == 304 * 2
    symbol_entry = next(e for e in mining if e["name"] == "settings[*].basic.symbol")
    assert symbol_entry["end"] == 2.0


def test_panel_controller_screener_fill_succeeds_when_expected_runs_preview_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # expected_screener_runs re-reads/re-parses the template after fill has
    # already staged it; a failure there must not undo an otherwise-successful
    # fill (see the "Files are already staged" comment in local_screener_fill).
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    def _boom(*args: object, **kwargs: object) -> int:
        raise PanelTestingError("invalid tester configuration")

    monkeypatch.setattr("mrs3.panel.expected_screener_runs", _boom)

    result = controller.local_screener_fill({
        "symbols": "AUSDT", "side": "long", "start": "2026-08-01", "end": "2026-09-18",
    })

    assert "expected_runs" not in result
    assert config.tester_config.exists()


def test_panel_controller_fills_screener_short_uses_short_template(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    result = controller.local_screener_fill({
        "symbols": "AUSDT", "side": "SHORT", "start": "2026-08-01", "end": "2026-09-18",
    })

    assert result["side"] == "SHORT"
    rendered = json.loads(config.tester_config.read_text(encoding="utf-8"))
    assert any(
        entry["name"] == "settings[*].mrs2.ma_short.multiplier" for entry in rendered["parameter_mining"]
    )
    strategy_files = tuple(config.strategy_dir.glob("*.json"))
    strategy = json.loads(strategy_files[0].read_text(encoding="utf-8"))
    assert strategy["basic"]["use_short"] is True


def test_panel_controller_screener_fill_rejects_symbol_without_usdt_suffix(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    with pytest.raises(PanelTestingError, match="USDT"):
        controller.local_screener_fill({
            "symbols": "CXBTC", "side": "LONG", "start": "2026-08-01", "end": "2026-09-18",
        })


def test_panel_controller_screener_and_runner01_share_the_tester_lock(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    controller.local_screener_fill({
        "symbols": "AUSDT", "side": "LONG", "start": "2026-08-01", "end": "2026-09-18",
    })

    with pytest.raises(PanelTestingError, match="TESTER_FILES_PREPARED"):
        controller.local_testing_fill({
            "symbols": "CXUSDT", "side": "LONG", "start": "2026-07-15", "end": "2026-08-06",
        })


def _write_screener_evaluation_fixture(tmp_path: Path, config: RunnerConfig) -> Path:
    dates_path = tmp_path / "dates.xlsx"
    pd.DataFrame([["AUSDT", "2020-01-01"]]).to_excel(dates_path, index=False, header=False)

    row = {
        "StartDate": "2026-08-01 00:00:00",
        "EndDate": "2026-08-31 00:00:00",
        "TotalPnLPercent": 30,
        "MaxDrawdownPercent": 5,
        "TotalTrades": 20,
        "WinRate": 80,
        "settings[*].basic.symbol": "AUSDT",
        "settings[*].basic.time_frame": "1h",
        "settings[*].mrs2.ma_close_long.len": "2",
        "settings[*].mrs2.ma_long.len": "4",
        "settings[*].mrs2.ma_long.multiplier": "0.99",
    }
    pd.DataFrame([row]).to_csv(
        config.report_dir / "reports_history.csv", index=False, encoding="utf-8-sig"
    )

    document = _tester_runner_document(config)
    document["panel_workflow"] = {"listing_dates_path": str(dates_path)}
    document["screener"] = {
        "expected_combos_per_pair": 1,
        "go_min_good": 1,
        "stop_best_pnl30": 1,
        "good_pnl30": 1,
        "big_min_good": 1,
        "big_shift_bp": 1,
    }
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    return config_path


def test_local_screener_evaluate_returns_serialized_verdicts(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = _write_screener_evaluation_fixture(tmp_path, config)
    controller = PanelController(tmp_path, config_path)

    result = controller.local_screener_evaluate()

    assert result["big_shift_bp"] == 1
    assert result["verdicts"] == [
        {
            "symbol": "AUSDT",
            "side": "LONG",
            "verdict": "GO",
            "big_shift": True,
            "n_reports": 1,
            "n_unique_combos": 1,
            "n_good": 1,
            "n_good_big_shift": 1,
            "best_pnl30": "30",
            "best_timeframe": "1h",
            "best_shift_bp": 100,
            "best_close_len": "2",
            "best_dd_pct": "5",
            "effective_days": "30",
            "window_start": "2026-08-01 00:00:00",
            "window_end": "2026-08-31 00:00:00",
        }
    ]
    json.dumps(result)  # must be JSON-serializable end to end


def test_local_screener_export_returns_downloadable_csv(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = _write_screener_evaluation_fixture(tmp_path, config)
    controller = PanelController(tmp_path, config_path)

    filename, data = controller.local_screener_export()

    assert filename == "screener_verdicts.csv"
    text = data.decode("utf-8-sig")
    assert "AUSDT" in text
    assert "GO" in text


def _build_liquidity_registry(tmp_path: Path, symbols: list[str]) -> Path:
    workbook = Workbook()
    pairs = workbook.active
    pairs.title = "Пары"
    pairs.append(["Пара", "Дата листинга на Bybit (UTC)"])
    for symbol in symbols:
        pairs.append([symbol, "2020-01-01"])
    path = tmp_path / "registry.xlsx"
    workbook.save(path)
    return path


def _config_with_registry(config: RunnerConfig, registry_path: Path) -> dict[str, object]:
    document = _tester_runner_document(config)
    document["screener"] = {"liquidity_registry_path": str(registry_path)}
    return document


def test_local_screener_registry_pairs_returns_unscreened_symbols(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    registry_path = _build_liquidity_registry(tmp_path, ["AUSDT", "BUSDT"])
    write_screening_results(
        registry_path,
        (
            ScreeningRow(
                symbol="AUSDT", side="LONG", verdict="GO", big_shift=True,
                n_good=9, n_good_big_shift=9,
                window_start="2026-08-01", window_end="2026-09-18",
            ),
        ),
    )
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_config_with_registry(config, registry_path)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    assert controller.local_screener_registry_pairs("long") == {"symbols": ["BUSDT"]}
    assert controller.local_screener_registry_pairs("SHORT") == {"symbols": ["AUSDT", "BUSDT"]}


def test_local_screener_registry_pairs_rejects_invalid_side(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    registry_path = _build_liquidity_registry(tmp_path, ["AUSDT"])
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_config_with_registry(config, registry_path)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    with pytest.raises(PanelTestingError, match="invalid testing request"):
        controller.local_screener_registry_pairs("BOTH")


def test_local_screener_registry_pairs_requires_configured_registry(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    with pytest.raises(PanelTestingError, match="liquidity registry is not configured"):
        controller.local_screener_registry_pairs("LONG")


def test_local_screener_evaluate_redacts_local_paths_from_error_messages(tmp_path: Path) -> None:
    # No reports_history*.csv written -> evaluate.py's error embeds report_dir,
    # a local bot-root path; the panel must never leak that to the client.
    config = _runner_config(tmp_path)
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(_tester_runner_document(config)), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    with pytest.raises(PanelTestingError) as excinfo:
        controller.local_screener_evaluate()

    message = str(excinfo.value)
    assert str(config.bot_root) not in message
    assert str(tmp_path) not in message


def test_local_screener_evaluate_preserves_screener_config_error_message(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    document = _tester_runner_document(config)
    document["screener"] = {"go_min_good": 0}  # ScreenerConfig requires a positive integer.
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    controller = PanelController(tmp_path, config_path)

    with pytest.raises(PanelTestingError, match="go_min_good"):
        controller.local_screener_evaluate()


def test_runner_config_rejects_report_directory_link_before_report_cleanup(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    alias = config.report_dir.parent / "report-alias"
    try:
        alias.symlink_to(config.report_dir, target_is_directory=True)
    except OSError:
        created = subprocess.run(
            ("cmd", "/c", "mklink", "/J", str(alias), str(config.report_dir)),
            capture_output=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("symbolic links and junctions are unavailable in this test environment")
    document = {"tester_runner": {
        "bot_root": str(config.bot_root), "executable": "hb_c.exe", "base_url": config.base_url, "port": config.port,
        "strategy_dir": "settings_strategy", "report_dir": "tester/report/report-alias", "wizard_result": "tester/wizard_result.json",
        "wizard_progress": "tester/wizard_progress.json", "tester_config": "tester/tester_config.json", "inbox_root": str(config.inbox_root),
    }}
    config_path = tmp_path / "config.local.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(Exception, match="report_dir must not contain links"):
        RunnerConfig.from_json(config_path)


def test_local_testing_rejects_direct_linked_report_directory_before_cleanup(tmp_path: Path) -> None:
    config = _runner_config(tmp_path)
    old_report = config.report_dir / "keep.html"
    old_report.write_text("keep", encoding="utf-8")
    alias = config.report_dir.parent / "report-alias"
    created = subprocess.run(
        ("cmd", "/c", "mklink", "/J", str(alias), str(config.report_dir)),
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("junctions are unavailable in this test environment")
    service = LocalTestingService(
        replace(config, report_dir=alias),
        Path(__file__).parents[1],
        stop_bot=lambda _config: None,
    )

    with pytest.raises(Exception, match="report_dir must not contain links"):
        service.fill(
            side="LONG", symbols=("CXUSDT",), start="2026-07-15", end="2026-08-06",
            delete_old_reports=True,
        )

    assert old_report.read_text(encoding="utf-8") == "keep"
