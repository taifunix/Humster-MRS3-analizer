from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

import mrs3.panel_performance_v2 as performance_v2
from mrs3.performance_v2_input import PerformanceV2InputError, read_performance_v2_inbox
from mrs3.performance_v2_store import PerformanceV2Config, load_performance_v2_config
from mrs3.panel_fast_strategy_test import LocalSingleModeStrategyTestService
from mrs3.panel_performance_v2 import _cleanup_performance_sources
from mrs3.panel_performance_v2 import _safe_cleanup_message
from mrs3.panel_performance_v2 import LocalPerformanceV2Service
from mrs3.panel_performance_v2 import PerformanceV2PanelRequest
from mrs3.runner.config import RunnerConfig
from mrs3.runner.http import RowState
from mrs3.runner.monitor import BatchCompletion, BatchRetryExhausted, StrategyCompletion
from mrs3.runner.inbox import capture_run_snapshot_inbox


def _strategy(name: str = "A") -> dict[str, object]:
    return {
        "name": name,
        "exchange": {"name": "Bybit"},
        "basic": {
            "symbol": "BTCUSDT",
            "time_frame": "1h",
            "use_long": True,
            "use_short": False,
        },
        "mrs3": {
            "ma_long": [{"id": 1, "len": 5, "multiplier": "0.99", "lot_x": "1"}],
            "ma_close_long": {"len": 3},
        },
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _config(tmp_path: Path) -> SimpleNamespace:
    bot = tmp_path / "bot"
    tester_config = bot / "config_tester.json"
    tester_config.parent.mkdir(parents=True)
    tester_config.write_text(json.dumps({
        "MakerFee": "0.00001",
        "TakerFee": "0.00005",
        "SlippagePercent": "0",
        "FundingRate": "0",
        "FundingIntervalHours": "8",
    }), encoding="utf-8")
    return SimpleNamespace(inbox_root=tmp_path / "inbox", tester_config=tester_config, bot_root=bot)


def _runner_config(tmp_path: Path) -> RunnerConfig:
    config = _config(tmp_path)
    return RunnerConfig(
        bot_root=config.bot_root,
        executable_path=config.bot_root / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=config.bot_root / "settings_strategy",
        report_dir=config.bot_root / "tester" / "report" / "my_test",
        wizard_result=config.bot_root / "tester" / "wizard_result.json",
        wizard_progress=config.bot_root / "tester" / "wizard_progress.json",
        tester_config=config.tester_config,
        inbox_root=config.inbox_root,
        strategy_batch_size=2,
        max_parallel_submissions=2,
        max_strategy_attempts=4,
    )


def _generation(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root = tmp_path / "generation"
    source = root / "strategies"
    source.mkdir(parents=True)
    name = "A"
    strategy = _strategy(name)
    strategy_bytes = _canonical(strategy)
    (source / f"{name}.json").write_bytes(strategy_bytes)
    unsigned = {
        "format_version": 1,
        "analysis_run_id": "a" * 64,
        "event_mode": "real_independent_events",
        "strategy_count": 1,
        "strategy_json_sha256": {f"{name}.json": sha256(strategy_bytes).hexdigest()},
        "candidate_identities": [name],
        "candidate_identity_to_strategy_names": {name: [name]},
        "candidate_diagnostics": {name: {"order_count": 1, "orders": [{
            "order_id": 1, "plateau_id": "P-A", "plateau_point_count": 3,
            "base_point_trades": 20, "plateau_total_trades": 20,
        }]}},
    }
    unsigned["generation_manifest_sha256"] = sha256(_canonical(unsigned)).hexdigest()
    manifest = root / "strategy_manifest.json"
    manifest.write_text(json.dumps(unsigned), encoding="utf-8")
    return manifest, (name,)


def test_single_mode_capture_keeps_sources_in_place_and_writes_metadata_only_inbox(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output = tmp_path / "Output" / "strategies"
    output.mkdir(parents=True)
    strategy = _strategy()
    strategy_path = output / "A.json"
    strategy_bytes = _canonical(strategy)
    strategy_path.write_bytes(strategy_bytes)
    report_root = config.bot_root / "tester" / "report" / "my_test"
    report_root.mkdir(parents=True)
    report_path = report_root / "A.html"
    report_path.write_text('<pre>{"name":"A","basic":{}}</pre>', encoding="utf-8")

    inbox = capture_run_snapshot_inbox(
        config,
        "single-1",
        {"A": strategy},
        {"A": report_path},
        strategy_paths={"A": strategy_path},
        tester_config_bytes=config.tester_config.read_bytes(),
        provenance={
            "analysis_run_id": "run-1",
            "generation_manifest_sha256": "a" * 64,
            "strategy_json_sha256": {"A.json": sha256(strategy_bytes).hexdigest()},
        },
        test_start="2026-08-01",
        test_end="2026-08-31",
        run_mode="SINGLE_MODE",
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert not (inbox / "strategies").exists()
    assert Path(entry["strategy_path"]).resolve() == strategy_path.resolve()
    assert Path(entry["report_path"]).resolve() == report_path.resolve()
    assert entry["source_strategy_sha256"] == sha256(strategy_bytes).hexdigest()
    assert manifest["run_mode"] == "SINGLE_MODE"
    assert strategy_path.read_bytes() == strategy_bytes


def test_v2_accepts_external_strategy_only_under_trusted_output_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output = tmp_path / "Output" / "strategies"
    output.mkdir(parents=True)
    strategy = _strategy()
    strategy_bytes = _canonical(strategy)
    strategy_path = output / "A.json"
    strategy_path.write_bytes(strategy_bytes)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    report_path = report_root / "A.html"
    report_path.write_bytes(b"report")
    inbox = config.inbox_root / "single-1"
    inbox.mkdir(parents=True)
    entry = {
        "strategy_name": "A",
        "strategy_version_id": sha256(strategy_bytes).hexdigest(),
        "strategy_path": str(strategy_path),
        "report_path": str(report_path),
        "source_strategy_sha256": sha256(strategy_bytes).hexdigest(),
        "source_report_sha256": sha256(report_path.read_bytes()).hexdigest(),
        "wizard_run_id": "wizard-1",
        "exchange_name": "Bybit",
    }
    manifest = {
        "schema_version": 1,
        "run_mode": "SINGLE_MODE",
        "expected_strategy_names": ["A"],
        "entries": [entry],
        "commission_contract": {
            "MakerFee": "0.00001",
            "TakerFee": "0.00005",
            "SlippagePercent": "0",
            "FundingRate": "0",
            "FundingIntervalHours": "8",
        },
        "commission_contract_id": "commission-1",
        "tester_config_sha256": "b" * 64,
        "v6_provenance": {
            "analysis_run_id": "run-1",
            "candidate_identity_to_strategy_names": {"candidate-1": ["A"]},
            "candidate_diagnostics": {
                "candidate-1": {
                    "order_count": 1,
                    "orders": [{
                        "order_id": 1,
                        "plateau_id": "P1",
                        "plateau_point_count": 3,
                        "base_point_trades": 20,
                        "plateau_total_trades": 20,
                    }],
                }
            },
        },
    }
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    v2 = PerformanceV2Config(tmp_path / "v2", strategy_root=output)

    prepared = read_performance_v2_inbox(inbox, report_root, config=v2)
    assert prepared.entries[0].strategy_path == strategy_path.resolve()

    manifest["entries"][0]["strategy_path"] = str(tmp_path / "outside.json")
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PerformanceV2InputError, match="trusted root"):
        read_performance_v2_inbox(inbox, report_root, config=v2)


def test_v2_rejects_external_strategy_for_direct_inbox(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output = tmp_path / "Output" / "strategies"
    output.mkdir(parents=True)
    strategy = _strategy()
    strategy_bytes = _canonical(strategy)
    strategy_path = output / "A.json"
    strategy_path.write_bytes(strategy_bytes)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    report_path = report_root / "A.html"
    report_path.write_bytes(b"report")
    inbox = config.inbox_root / "direct-1"
    inbox.mkdir(parents=True)
    entry = {
        "strategy_name": "A",
        "strategy_version_id": sha256(strategy_bytes).hexdigest(),
        "strategy_path": str(strategy_path),
        "report_path": str(report_path),
        "source_strategy_sha256": sha256(strategy_bytes).hexdigest(),
        "source_report_sha256": sha256(report_path.read_bytes()).hexdigest(),
        "wizard_run_id": "wizard-1",
        "exchange_name": "Bybit",
    }
    manifest = {
        "schema_version": 1,
        "run_mode": "FAST",
        "expected_strategy_names": ["A"],
        "entries": [entry],
        "commission_contract": {
            "MakerFee": "0.00001",
            "TakerFee": "0.00005",
            "SlippagePercent": "0",
            "FundingRate": "0",
            "FundingIntervalHours": "8",
        },
        "commission_contract_id": "commission-1",
        "tester_config_sha256": "b" * 64,
        "v6_provenance": {
            "analysis_run_id": "run-1",
            "candidate_identity_to_strategy_names": {"candidate-1": ["A"]},
            "candidate_diagnostics": {
                "candidate-1": {
                    "order_count": 1,
                    "orders": [{
                        "order_id": 1,
                        "plateau_id": "P1",
                        "plateau_point_count": 3,
                        "base_point_trades": 20,
                        "plateau_total_trades": 20,
                    }]
                }
            },
        },
    }
    (inbox / "inbox_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PerformanceV2InputError, match="trusted root"):
        read_performance_v2_inbox(
            inbox,
            report_root,
            config=PerformanceV2Config(tmp_path / "v2", strategy_root=output),
        )


def test_single_mode_auto_captures_metadata_inbox_and_marks_ready(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path)
    config = _runner_config(tmp_path)

    def monitor(_: object, expected: tuple[str, ...], *_args: object, **_kwargs: object) -> BatchCompletion:
        reports = {}
        for name in expected:
            report = config.report_dir / f"{name}.html"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(
                f'<p>Test period: 2026-08-01 - 2026-08-31</p><pre>{{"name":"{name}","basic":{{"symbol":"BTCUSDT","time_frame":"1h"}}}}</pre>',
                encoding="utf-8",
            )
            reports[name] = StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", report, True, 1)
        return BatchCompletion(strategies=reports, polls=1, elapsed_seconds=0)

    service = LocalSingleModeStrategyTestService(
        config,
        start_bot=lambda _: object(),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    started = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-ready",
    )
    for _ in range(100):
        status = service.status(str(started["job_id"]))
        if status["state"] != "RUNNING":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("single mode did not finish")

    assert status["state"] == "COMMITTED", status
    assert status["mode"] == "SINGLE_MODE"
    assert status["inbox_ready"] is True
    inbox = Path(status["inbox_path"])
    assert (inbox / "inbox_manifest.json").is_file()
    assert not (inbox / "strategies").exists()
    assert (config.report_dir / "A.html").is_file()


def test_single_mode_exhausted_report_retries_is_failed_not_partial_commit(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path)
    config = _runner_config(tmp_path)

    def monitor(_: object, expected: tuple[str, ...], *_args: object, **kwargs: object) -> BatchCompletion:
        assert kwargs["allow_partial"] is False
        raise BatchRetryExhausted("missing reports after max attempts: " + ", ".join(expected))

    service = LocalSingleModeStrategyTestService(
        config,
        start_bot=lambda _: object(),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    started = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-failed",
    )
    for _ in range(100):
        status = service.status(str(started["job_id"]))
        if status["state"] != "RUNNING":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("single mode did not finish")

    assert status["state"] == "FAILED", status
    assert status["phase"] == "FAILED"
    assert status["inbox_ready"] is False


def test_performance_cleanup_removes_only_exact_sources_and_stale_manifest(tmp_path: Path) -> None:
    report_root = tmp_path / "bot" / "tester" / "report" / "my_test"
    strategy_root = tmp_path / "Output" / "strategies"
    report_root.mkdir(parents=True)
    strategy_root.mkdir(parents=True)
    (report_root / "A.html").write_text("report", encoding="utf-8")
    (strategy_root / "A.json").write_text("strategy", encoding="utf-8")
    stale = strategy_root.parent / "strategy_manifest.json"
    stale.write_text("manifest", encoding="utf-8")
    unrelated = tmp_path / "Output" / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    _cleanup_performance_sources(report_root, strategy_root)

    assert list(report_root.iterdir()) == []
    assert list(strategy_root.iterdir()) == []
    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_performance_cleanup_rejects_non_exact_report_root(tmp_path: Path) -> None:
    report_root = tmp_path / "reports"
    strategy_root = tmp_path / "Output" / "strategies"
    report_root.mkdir(parents=True)
    strategy_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="exact path"):
        _cleanup_performance_sources(report_root, strategy_root)


def test_v2_config_owns_output_strategy_root_server_side(tmp_path: Path) -> None:
    config_path = tmp_path / "config.performance.json"
    config_path.write_text(json.dumps({
        "unified_performance_v2": {"database_root": "data/performance-v2"}
    }), encoding="utf-8")
    config = load_performance_v2_config(config_path)
    assert config.strategy_root == (tmp_path / "Output" / "strategies").resolve()


def test_cleanup_failure_preserves_committed_v2_result_and_hides_local_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_panel_performance_v2 import _request

    request, _ = _request(tmp_path)
    request = replace(request, strategy_root=tmp_path / "Output" / "strategies")

    def fail_cleanup(*_args: object) -> None:
        raise RuntimeError(f"cannot remove {tmp_path}\\Output\\strategies")

    monkeypatch.setattr("mrs3.panel_performance_v2._cleanup_performance_sources", fail_cleanup)
    result = LocalPerformanceV2Service().run(request)

    assert result.status == "COMMITTED"
    assert result.cleanup_warning == {
        "code": "CLEANUP_FAILED",
        "message": "cannot remove <path>",
    }


def test_cleanup_warning_hides_windows_paths_with_spaces() -> None:
    assert _safe_cleanup_message(
        RuntimeError(r"cannot remove C:\Users\Alice Example\Output\strategies")
    ) == "cannot remove <path>"


def test_cleanup_refuses_reparse_child_before_recursive_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report_root = tmp_path / "bot" / "tester" / "report" / "my_test"
    child = report_root / "junction-like-child"
    child.mkdir(parents=True)
    (child / "keep.txt").write_text("keep", encoding="utf-8")
    real_is_reparse = performance_v2._is_reparse
    monkeypatch.setattr(performance_v2, "_is_reparse", lambda path: path == child or real_is_reparse(path))

    with pytest.raises(ValueError, match="symlink or reparse"):
        performance_v2._cleanup_exact_directory(report_root, "tester", "report", "my_test")

    assert (child / "keep.txt").is_file()
