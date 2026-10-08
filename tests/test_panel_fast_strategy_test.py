from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import time
from threading import Event
from types import SimpleNamespace

import pytest

import mrs3.panel as panel_module
import mrs3.panel_fast_strategy_test as fast_strategy_module
from mrs3.panel_fast_strategy_test import LocalFastStrategyTestService, LocalSingleModeStrategyTestService
from mrs3.panel_fast_strategy_test import FastStrategyTestError
from mrs3.panel_fast_strategy_test import _has_current_performance_v2_layout
from mrs3.panel_fast_strategy_test import _write_fast_tester_config
from mrs3.panel_fast_strategy_test import parse_initial_balance
from mrs3.locking import TesterTargetLock
from mrs3.panel import PanelController
from mrs3.performance_v2_html import parse_current_performance_v2_html
from mrs3.performance_v2_store import PerformanceV2Config
from mrs3.runner.config import RunnerConfig
from mrs3.runner.http import RowState
from mrs3.runner.monitor import BatchCompletion, StrategyCompletion


CURRENT_REPORT = Path(__file__).parent / "fixtures" / "performance" / "report_current_v2.html"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _generation(tmp_path: Path, count: int) -> tuple[Path, tuple[str, ...]]:
    root = tmp_path / "generation"
    source = root / "strategies"
    source.mkdir(parents=True)
    names = tuple(f"S{index}" for index in range(count))
    hashes = {}
    for name in names:
        strategy = {"name": name, "exchange": {"name": "Bybit"}, "basic": {"symbol": "BTCUSDT", "time_frame": "1h"}}
        payload = json.dumps(strategy, sort_keys=True, separators=(",", ":"))
        (source / f"{name}.json").write_text(payload, encoding="utf-8")
        hashes[f"{name}.json"] = sha256(payload.encode()).hexdigest()
    unsigned = {
        "format_version": 1,
        "analysis_run_id": "a" * 64,
        "event_mode": "real_independent_events",
        "strategy_count": count,
        "strategy_json_sha256": hashes,
        "candidate_identities": list(names),
        "candidate_identity_to_strategy_names": {name: [name] for name in names},
        "candidate_diagnostics": {
            name: {
                "order_count": 1,
                "orders": [{
                    "order_id": 1,
                    "plateau_id": f"P-{name}",
                    "plateau_point_count": 3,
                    "base_point_trades": 20,
                    "plateau_total_trades": 20,
                }],
            }
            for name in names
        },
    }
    unsigned["generation_manifest_sha256"] = sha256(_canonical(unsigned)).hexdigest()
    manifest = root / "strategy_manifest.json"
    manifest.write_text(json.dumps(unsigned), encoding="utf-8")
    return manifest, names


def _config(tmp_path: Path) -> RunnerConfig:
    bot = tmp_path / "bot"
    (bot / "config_tester.json").parent.mkdir(parents=True)
    (bot / "config_tester.json").write_text(json.dumps({
        "include_chart_balance": False,
        "report": {
            "include_chart_balance": False,
            "include_position_stats": True,
        },
        "MakerFee": 0.00001,
        "TakerFee": 0.00005,
        "SlippagePercent": 0,
        "FundingRate": 0,
        "FundingIntervalHours": 8,
    }), encoding="utf-8")
    return RunnerConfig(
        bot_root=bot,
        executable_path=bot / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=bot / "settings_strategy",
        report_dir=bot / "tester" / "report" / "my_test",
        wizard_result=bot / "tester" / "wizard_result.json",
        wizard_progress=bot / "tester" / "wizard_progress.json",
        tester_config=bot / "config_tester.json",
        inbox_root=tmp_path / "inbox",
        strategy_batch_size=2,
        max_parallel_submissions=2,
        max_strategy_attempts=4,
    )


def _wait(service: LocalFastStrategyTestService, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = service.status(job_id)
        if status["state"] != "RUNNING":
            return status
        time.sleep(0.01)
    raise AssertionError("Fast TEST did not finish")


def test_runtime_directory_cleanup_keeps_root_and_retries_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "settings_strategy"
    runtime.mkdir()
    locked = runtime / "strategy.json"
    locked.write_text("{}", encoding="utf-8")
    real_unlink = Path.unlink
    attempts = 0

    def fail_once(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal attempts
        if path == locked and attempts == 0:
            attempts += 1
            error = PermissionError(13, "sharing violation", path)
            error.winerror = 32
            raise error
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    fast_strategy_module._clear_directory(runtime, expected=runtime)

    assert attempts == 1
    assert runtime.is_dir()
    assert list(runtime.iterdir()) == []


@pytest.mark.parametrize(("winerror", "timeout"), ((5, 30.0), (32, 0.0)))
def test_runtime_directory_cleanup_propagates_non_transient_or_expired_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winerror: int, timeout: float
) -> None:
    runtime = tmp_path / "settings_strategy"
    runtime.mkdir()
    locked = runtime / "strategy.json"
    locked.write_text("{}", encoding="utf-8")
    attempts = 0

    def always_fail(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        error = PermissionError(13, "access denied", path)
        error.winerror = winerror
        raise error

    monkeypatch.setattr(Path, "unlink", always_fail)
    monkeypatch.setattr(fast_strategy_module, "_WINDOWS_FILE_RELEASE_SECONDS", timeout)

    with pytest.raises(PermissionError):
        fast_strategy_module._clear_directory(runtime, expected=runtime)

    assert attempts == 1


def test_runtime_directory_cleanup_retries_locked_nested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "settings_strategy"
    nested = runtime / "nested"
    nested.mkdir(parents=True)
    (nested / "strategy.json").write_text("{}", encoding="utf-8")
    real_rmtree = fast_strategy_module.shutil.rmtree
    attempts = 0

    def fail_once(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal attempts
        if Path(path) == nested and attempts == 0:
            attempts += 1
            error = PermissionError(13, "sharing violation", path)
            error.winerror = 32
            raise error
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(fast_strategy_module.shutil, "rmtree", fail_once)

    fast_strategy_module._clear_directory(runtime, expected=runtime)

    assert attempts == 1
    assert runtime.is_dir()
    assert list(runtime.iterdir()) == []


def test_runtime_directory_cleanup_fails_if_deleted_entry_remains_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "settings_strategy"
    runtime.mkdir()
    (runtime / "strategy.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(fast_strategy_module, "_WINDOWS_FILE_RELEASE_SECONDS", 0.0)

    with pytest.raises(FastStrategyTestError, match="runtime directory could not be cleared"):
        fast_strategy_module._clear_directory(runtime, expected=runtime)


def test_fast_writer_starts_from_template_and_preserves_unrelated_keys(tmp_path: Path) -> None:
    config = _config(tmp_path)
    template = tmp_path / "config-template.json"
    template.write_text(json.dumps({
        "StartDate": "old",
        "EndDate": "old",
        "use_runs": True,
        "single_mode": False,
        "InitialBalance": 1000.0,
        "max_parallel_runs": 99,
        "include_chart_balance": False,
        "report": {"include_chart_balance": False, "include_position_stats": True},
        "unrelated": {"keep": [1, 2, 3]},
    }), encoding="utf-8")

    _write_fast_tester_config(
        config,
        "2026-08-01",
        "2026-08-31",
        single_mode=True,
        initial_balance=2500.5,
        template_path=template,
    )

    rendered = json.loads(config.tester_config.read_text(encoding="utf-8"))
    assert rendered["unrelated"] == {"keep": [1, 2, 3]}
    assert rendered["StartDate"] == "2026-08-01"
    assert rendered["EndDate"] == "2026-08-31"
    assert rendered["use_runs"] is False
    assert rendered["single_mode"] is True
    assert rendered["InitialBalance"] == 2500.5
    assert isinstance(rendered["InitialBalance"], float)
    assert rendered["max_parallel_runs"] == config.max_parallel_submissions
    assert rendered["include_chart_balance"] is True
    assert rendered["report"]["include_chart_balance"] is True
    assert rendered["report"]["include_position_stats"] is False
    assert rendered["report"]["include_trades_table"] is True


@pytest.mark.parametrize("value", ("", "abc", "1,5", "0", "-1", "NaN", True, 10 ** 1000))
def test_initial_balance_rejects_non_positive_or_non_finite_values(value: object) -> None:
    with pytest.raises(FastStrategyTestError, match="initial_balance"):
        parse_initial_balance(value)


def test_fast_writer_keeps_template_initial_balance_when_request_omits_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    template = tmp_path / "config-template.json"
    template.write_text(json.dumps({"InitialBalance": 1000.0, "report": {}}), encoding="utf-8")

    _write_fast_tester_config(config, "2026-08-01", "2026-08-31", template_path=template)

    rendered = json.loads(config.tester_config.read_text(encoding="utf-8"))
    assert rendered["InitialBalance"] == 1000.0
    assert isinstance(rendered["InitialBalance"], float)


def test_single_mode_retry_rerenders_original_initial_balance(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)

    class FailAfterRender(LocalSingleModeStrategyTestService):
        def _run_owned(self, job):
            _write_fast_tester_config(
                self.config, job.start_date, job.end_date, single_mode=True,
                initial_balance=job.initial_balance,
            )
            raise FastStrategyTestError("forced failure")

    service = FailAfterRender(config)
    source = service.start(
        manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31",
        initial_balance=2500.5, job_id="balance-source",
    )
    assert _wait(service, str(source["job_id"]))["state"] == "FAILED"
    config.tester_config.write_text(json.dumps({"InitialBalance": 1.0}), encoding="utf-8")

    retry = service.retry(str(source["job_id"]), job_id="balance-retry")

    assert _wait(service, str(retry["job_id"]))["state"] == "FAILED"
    assert json.loads(config.tester_config.read_text(encoding="utf-8"))["InitialBalance"] == 2500.5


@pytest.mark.parametrize("template_text", ["{", "[]"])
def test_fast_writer_fails_closed_for_invalid_template(tmp_path: Path, template_text: str) -> None:
    config = _config(tmp_path)
    before = config.tester_config.read_bytes()
    template = tmp_path / "invalid-template.json"
    template.write_text(template_text, encoding="utf-8")

    with pytest.raises(FastStrategyTestError, match="tester config"):
        _write_fast_tester_config(
            config,
            "2026-08-01",
            "2026-08-31",
            template_path=template,
        )

    assert config.tester_config.read_bytes() == before


def test_native_prevalidation_accepts_extended_current_action_layout(tmp_path: Path) -> None:
    source = CURRENT_REPORT.read_text(encoding="utf-8")
    for old, new in (
        (
            "<th>Timestamp</th><th>Symbol</th><th>Order ID</th><th>Action</th><th>Fee</th><th>PnL</th><th>Balance</th><th>Size</th><th>Post Size</th><th>Post Side</th>",
            "<th>Timestamp</th><th>Symbol</th><th>Order ID</th><th>Side</th><th>Action</th><th>Size</th><th>Price</th><th>Fee</th><th>Cost</th><th>PnL</th><th>Balance</th><th>Post Size</th><th>Post Side</th>",
        ),
        (
            "<td>2026-01-01T01:00:00Z</td><td>ONUSDT</td><td>1</td><td>opened</td><td>0.05</td><td>0</td><td>999.95</td><td>1</td><td>1</td><td>long</td>",
            "<td>2026-01-01T01:00:00Z</td><td>ONUSDT</td><td>1</td><td>buy</td><td>opened</td><td>1</td><td>1</td><td>0.05</td><td>1</td><td>0</td><td>999.95</td><td>1</td><td>long</td>",
        ),
        (
            "<td>2026-01-03T01:00:00+00:00</td><td>ONUSDT</td><td>1</td><td>closed</td><td>0.05</td><td>9.9</td><td>1009.9</td><td>1</td><td>0</td><td></td>",
            "<td>2026-01-03T01:00:00+00:00</td><td>ONUSDT</td><td>1</td><td>sell</td><td>closed</td><td>1</td><td>1</td><td>0.05</td><td>1</td><td>9.9</td><td>1009.9</td><td>0</td><td></td>",
        ),
    ):
        previous = source
        source = source.replace(old, new, 1)
        assert source != previous

    assert _has_current_performance_v2_layout(source)
    parsed = parse_current_performance_v2_html(
        source.encode(), PerformanceV2Config(tmp_path / "performance-v2")
    )
    assert parsed.actions[0].action == "opened"
    assert parsed.actions[0].size == Decimal("1")
    assert parsed.actions[0].fee == Decimal("0.05")


def test_native_prevalidation_rejects_extended_layout_missing_post_side() -> None:
    source = CURRENT_REPORT.read_text(encoding="utf-8")
    extended = source.replace(
        "<th>Action</th><th>Fee</th>",
        "<th>Action</th><th>Side</th><th>Price</th><th>Cost</th><th>Fee</th>",
        1,
    )
    assert extended != source
    source = extended.replace("<th>Post Side</th>", "", 1)
    assert source != extended

    assert not _has_current_performance_v2_layout(source)


def test_native_prevalidation_rejects_legacy_report_layout() -> None:
    legacy_report = CURRENT_REPORT.with_name("report_import.html")

    assert not _has_current_performance_v2_layout(legacy_report.read_text(encoding="utf-8"))


def test_native_reports_skips_unchanged_batch_baseline_before_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    prior = report_dir / "prior.html"
    prior.write_bytes(b"\xff")
    prior_stat = prior.stat()
    current = report_dir / "current.html"
    current.write_text(
        CURRENT_REPORT.read_text(encoding="utf-8").replace('"name":"MRS3 Current v2"', '"name":"S0"', 1),
        encoding="utf-8",
    )
    expected_settings = fast_strategy_module.extract_html_strategy_settings(current)
    assert expected_settings is not None
    reads: list[Path] = []
    original_read_text = Path.read_text

    def track_read(path: Path, *args: object, **kwargs: object) -> str:
        reads.append(path)
        if path == prior:
            raise AssertionError("unchanged prior report was read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track_read)
    found = LocalSingleModeStrategyTestService._native_reports(
        report_dir,
        {"S0"},
        expected_settings={"S0": expected_settings},
        start="2026-01-01",
        end="2026-01-09",
        baseline={prior.name: (prior_stat.st_mtime_ns, prior_stat.st_size)},
    )

    assert found == {"S0": current}
    assert prior not in reads
    assert reads.count(current) == 1


def test_single_mode_reload_uses_checkpoint_without_reading_prior_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, names = _generation(tmp_path, 3)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    config.strategy_dir.mkdir(parents=True)
    prior: dict[str, Path] = {}
    evidence: dict[str, list[int]] = {}
    for name in names[:2]:
        report = config.report_dir / f"{name}.html"
        report.write_bytes(b"authoritatively accepted")
        stat = report.stat()
        prior[name] = report
        evidence[name] = [stat.st_mtime_ns, stat.st_size]
    current = config.report_dir / f"{names[2]}.html"
    current.write_text(
        '<p>Test period: 2026-08-01 - 2026-08-31</p>'
        f'<pre>{{"name":"{names[2]}","basic":{{"symbol":"BTCUSDT","time_frame":"1h"}}}}</pre>',
        encoding="utf-8",
    )
    (config.report_dir / "tester_manifest.json").write_text(json.dumps({
        "job_id": "checkpointed-native",
        "mode": "SINGLE_MODE",
        "phase": "RUNNING",
        "generation_manifest_path": str(manifest),
        "expected_names": list(names),
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
        "attempt_counts": {name: 1 for name in names},
        "verified_reports": {name: report.name for name, report in prior.items()},
        "verified_report_evidence": evidence,
        "failed_names": [names[2]],
    }), encoding="utf-8")

    reads: list[Path] = []
    original_read_text = Path.read_text

    def track_read(path: Path, *args: object, **kwargs: object) -> str:
        if path.parent.resolve() == config.report_dir.resolve():
            reads.append(path)
            if path in prior.values():
                raise AssertionError("accepted prior report was read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", track_read)
    parsed: list[Path] = []
    original_extract = fast_strategy_module.extract_html_strategy_settings

    def track_extract(path: Path) -> dict[str, object] | None:
        parsed.append(path)
        return original_extract(path)

    monkeypatch.setattr(fast_strategy_module, "extract_html_strategy_settings", track_extract)
    monkeypatch.setattr(fast_strategy_module, "capture_run_snapshot_inbox", lambda *_args, **_kwargs: tmp_path / "inbox")

    service = LocalSingleModeStrategyTestService(config)
    recovered = service.retry("checkpointed-native", job_id="checkpointed-native-retry")

    assert recovered["state"] == "COMMITTED"
    assert recovered["evidence"]["verified_reports"] == {name: f"{name}.html" for name in names}
    assert all(report not in reads for report in prior.values())
    assert all(report not in parsed for report in prior.values())
    assert parsed == [current]


def test_single_mode_clears_reports_before_native_run_when_requested(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    (config.report_dir / "old.html").write_text("old", encoding="utf-8")
    events: list[str] = []
    observed: list[tuple[str, ...]] = []
    service = LocalSingleModeStrategyTestService(
        config,
        start_bot=lambda _config: None,
        stop_bot=lambda _config: events.append("stop"),
    )

    def after_cleanup(job: object) -> None:
        observed.append(tuple(path.name for path in config.report_dir.iterdir()))
        raise RuntimeError("test stop")

    service._run_native = after_cleanup
    started = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        clear_reports=True,
    )

    assert _wait(service, str(started["job_id"]))["state"] == "FAILED"
    assert events[0] == "stop"
    assert observed == [()]


def test_native_idle_waits_for_current_batch_result_files(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), poll_interval_seconds=0.001, batch_timeout_seconds=0.2, report_stability_polls=2)
    service = LocalSingleModeStrategyTestService(config)
    job = SimpleNamespace(cancel=Event(), phase="BOT_RUN", progress={}, verified_reports={})
    config.report_dir.mkdir(parents=True)
    (config.report_dir / "S0.html").write_text("placeholder", encoding="utf-8")
    config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
    config.wizard_result.write_text(json.dumps([
        {"runId": "", "strategies": ["OLD"], "stats": {}, "chartUrl": "/tester-report/my_test/OLD.html"},
        {"runId": "", "strategies": ["S0"], "stats": {}, "chartUrl": "/tester-report/my_test/S0.html"},
    ]), encoding="utf-8")
    polls = 0
    updates: list[dict[str, object]] = []
    service._emit = lambda current_job: updates.append(dict(current_job.progress))

    class Client:
        def tester_status(self) -> str:
            nonlocal polls
            polls += 1
            if polls == 3:
                (config.report_dir / "S1.html").write_text("placeholder", encoding="utf-8")
                config.wizard_result.write_text(json.dumps([
                    {"runId": "", "strategies": ["S0"], "stats": {}, "chartUrl": "/tester-report/my_test/S0.html"},
                    {"runId": "", "strategies": ["S1"], "stats": {}, "chartUrl": "/tester-report/my_test/S1.html"},
                ]), encoding="utf-8")
            elif polls == 4:
                config.wizard_result.write_text(json.dumps([
                    {"runId": "", "strategies": ["S1"], "stats": {}, "chartUrl": "/tester-report/my_test/S1.html"},
                ]), encoding="utf-8")
            return "idle"

    service._wait_for_native_idle(job, Client(), config, 1, 1, ("S0", "S1"))

    assert polls >= 3
    assert any(update.get("current") == 1 and update.get("active") == 1 for update in updates)
    assert updates[-1]["current"] == 2
    assert updates[-1]["active"] == 0
    assert [update["current"] for update in updates] == sorted(update["current"] for update in updates)


def test_native_idle_accepts_stable_batch_report_sequence_when_wizard_result_is_truncated(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), poll_interval_seconds=0.001, batch_timeout_seconds=0.2, report_stability_polls=2)
    service = LocalSingleModeStrategyTestService(config)
    config.report_dir.mkdir(parents=True)
    config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
    config.wizard_result.write_text("[]", encoding="utf-8")
    for index in range(1, 4):
        (config.report_dir / f"my_test_run_{index:03d}_of_003_previous_{index}.html").write_text("placeholder", encoding="utf-8")
    baseline = {
        path.name: (path.stat().st_mtime_ns, path.stat().st_size, sha256(path.read_bytes()).hexdigest())
        for path in config.report_dir.glob("*.html")
    }
    assert LocalSingleModeStrategyTestService._native_filename_evidence(
        config, 3, {name: values[:2] for name, values in baseline.items()}
    ) == ()
    for index in range(1, 4):
        (config.report_dir / f"my_test_run_{index:03d}_of_003_strategy_{index}.html").write_text("placeholder", encoding="utf-8")
    job = SimpleNamespace(cancel=Event(), phase="BOT_RUN", progress={}, verified_reports={}, report_baseline=baseline)
    updates: list[dict[str, object]] = []
    service._emit = lambda current_job: updates.append(dict(current_job.progress))

    class Client:
        def tester_status(self) -> str:
            return "completed"

    service._wait_for_native_idle(job, Client(), config, 1, 1, ("S0", "S1", "S2"), batch_baseline={
        name: values[:2] for name, values in baseline.items()
    })

    assert updates[-1]["current"] == 3
    assert updates[-1]["active"] == 0


def test_native_idle_continues_after_transient_status_failure(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), poll_interval_seconds=0.001, batch_timeout_seconds=0.2, stall_timeout_seconds=0.1, report_stability_polls=2)
    service = LocalSingleModeStrategyTestService(config)
    job = SimpleNamespace(cancel=Event(), phase="BOT_RUN", progress={}, verified_reports={})
    config.report_dir.mkdir(parents=True)
    (config.report_dir / "S0.html").write_text("placeholder", encoding="utf-8")
    config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
    config.wizard_result.write_text(json.dumps([
        {"runId": "", "strategies": ["S0"], "stats": {}, "chartUrl": "/tester-report/my_test/S0.html"},
    ]), encoding="utf-8")
    updates: list[dict[str, object]] = []
    service._emit = lambda current_job: updates.append(dict(current_job.progress))
    polls = 0

    class Client:
        def tester_status(self) -> str:
            nonlocal polls
            polls += 1
            if polls == 1:
                raise RuntimeError("temporary status failure")
            return "running"

    service._wait_for_native_idle(job, Client(), config, 1, 1, ("S0",))

    assert updates[0]["native_status"] == "unavailable"
    assert updates[-1]["active"] == 0


def test_native_idle_stall_timeout_is_independent_of_batch_timeout(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), poll_interval_seconds=0.001, batch_timeout_seconds=0.2, stall_timeout_seconds=0.01)
    service = LocalSingleModeStrategyTestService(config)
    job = SimpleNamespace(cancel=Event(), phase="BOT_RUN", progress={}, verified_reports={})

    class Client:
        def tester_status(self) -> str:
            return "running"

    with pytest.raises(TimeoutError, match="stalled"):
        service._wait_for_native_idle(job, Client(), config, 1, 1, ("S0",))


def test_native_progress_callback_failure_does_not_stop_live_tester(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = replace(
        _config(tmp_path), poll_interval_seconds=0.001, batch_timeout_seconds=1,
        stall_timeout_seconds=0.5, report_stability_polls=2,
    )
    active = False
    polls = 0
    callback_failed_while_active = []
    report: Path | None = None

    def start_bot(_: RunnerConfig) -> None:
        nonlocal active
        active = True

    def stop_bot(_: RunnerConfig) -> None:
        nonlocal active
        active = False

    class Client:
        def run_tester(self) -> None:
            nonlocal report
            config.report_dir.mkdir(parents=True, exist_ok=True)
            report = config.report_dir / "S0.html"
            report.write_text("native report", encoding="utf-8")

        def tester_status(self) -> str:
            nonlocal polls
            polls += 1
            return "running"

        def close(self) -> None:
            pass

    class Updates:
        failed = False
        fail_terminal = False

        def __call__(self, snapshot: dict[str, object]) -> None:
            if (
                not self.failed
                and snapshot.get("phase") == "BOT_RUN"
                and "native_status" in snapshot.get("progress", {})
            ):
                self.failed = True
                callback_failed_while_active.append(active)
                raise OSError("temporary progress journal failure")
            if self.fail_terminal and snapshot["state"] == "COMMITTED":
                raise OSError("terminal progress save failed")

    updates = Updates()
    service = LocalSingleModeStrategyTestService(
        config, start_bot=start_bot, stop_bot=stop_bot,
        client_factory=lambda _: Client(), on_update=updates,
    )
    monkeypatch.setattr(
        LocalSingleModeStrategyTestService,
        "_native_result_evidence",
        staticmethod(lambda _job, _config, expected: set(expected)),
    )

    def reports(report_dir: Path, expected: set[str], **_kwargs: object) -> dict[str, Path]:
        return {name: report_dir / f"{name}.html" for name in expected}

    service._native_reports = reports
    started = service.start(
        manifest, analysis_run_id="a" * 64, start_date="2026-08-01",
        end_date="2026-08-31", job_id="progress-callback-live",
    )
    status = _wait(service, str(started["job_id"]))

    assert updates.failed is True
    assert callback_failed_while_active == [True]
    assert polls >= 2
    assert status["state"] == "COMMITTED"
    assert status["progress_publication_error"] is None
    assert status["evidence"]["verified_reports"] == {"S0": "S0.html"}
    assert active is False
    assert report is not None and report.is_file()
    updates.fail_terminal = True
    with pytest.raises(OSError, match="terminal progress save failed"):
        service._emit(service._jobs[str(started["job_id"])])


def test_native_batch_failure_retains_already_created_valid_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, names = _generation(tmp_path, 2)
    config = _config(tmp_path)
    reports: list[Path] = []

    class Client:
        def run_tester(self) -> None:
            config.report_dir.mkdir(parents=True, exist_ok=True)
            source = CURRENT_REPORT.read_text(encoding="utf-8")
            valid = source.replace('"name":"MRS3 Current v2"', '"name":"S0"', 1).replace("ONUSDT", "BTCUSDT")
            invalid = source.replace('"name":"MRS3 Current v2"', '"name":"S1"', 1).replace("ONUSDT", "ETHUSDT")
            valid = valid.replace("2026-01-01 - 2026-01-09", "2026-08-01 - 2026-08-31")
            invalid = invalid.replace("2026-01-01 - 2026-01-09", "2026-08-01 - 2026-08-31")
            for name, content in (("S0", valid), ("S1", invalid)):
                path = config.report_dir / f"{name}.html"
                path.write_text(content, encoding="utf-8")
                reports.append(path)

        def close(self) -> None:
            pass

    service = LocalSingleModeStrategyTestService(
        config, start_bot=lambda _: None, stop_bot=lambda _: None,
        client_factory=lambda _: Client(),
    )

    def fail_after_one_report(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("native tester stopped making progress")

    monkeypatch.setattr(service, "_wait_for_native_idle", fail_after_one_report)
    started = service.start(
        manifest, analysis_run_id="a" * 64, start_date="2026-08-01",
        end_date="2026-08-31", job_id="partial-native-reports",
    )
    status = _wait(service, str(started["job_id"]))

    assert status["state"] == "FAILED"
    assert status["progress"]["current"] == 1
    assert status["progress"]["failed"] == 1
    assert status["evidence"]["verified_reports"] == {"S0": "S0.html"}
    assert status["evidence"]["failed_names"] == ["S1"]
    assert status["inbox_ready"] is False
    with pytest.raises(FastStrategyTestError, match="reports are incomplete"):
        service.capture_inbox(str(started["job_id"]))
    assert len(reports) == 2 and all(report.is_file() for report in reports)


def test_emit_uses_captured_live_snapshot_when_callback_mutates_job(tmp_path: Path) -> None:
    manifest_path, names = _generation(tmp_path, 1)
    manifest = fast_strategy_module.validate_strategy_manifest(manifest_path)
    config = _config(tmp_path)
    job = fast_strategy_module._Job(
        "snapshot-progress", manifest_path, manifest, names, names,
        "2026-01-01", "2026-01-09", config.report_dir, config.strategy_dir,
        single_mode=True,
    )
    job.phase = "BOT_RUN"
    service = LocalSingleModeStrategyTestService(config)
    service._on_update = lambda snapshot: (
        setattr(job, "state", "FAILED"),
        (_ for _ in ()).throw(OSError("transient progress save failure")),
    )

    service._emit(job)

    assert job.progress_publication_error == "transient progress save failure"
    assert service._snapshot(job)["progress_publication_error"] == "transient progress save failure"


def test_emit_publishes_then_clears_progress_publication_warning_in_panel_registry(tmp_path: Path) -> None:
    manifest_path, names = _generation(tmp_path, 1)
    manifest = fast_strategy_module.validate_strategy_manifest(manifest_path)
    config = _config(tmp_path)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    job_id = "progress-publication-warning"
    controller._panel_jobs.submit(
        "strategies.performance.v2.finalist-retest", {}, job_id,
        ("strategies.tester", "performance-v2-finalist-retest"), job_id=job_id,
    )
    controller._panel_jobs.transition(job_id, "RUNNING", phase="BOT_RUN")
    job = fast_strategy_module._Job(
        job_id, manifest_path, manifest, names, names,
        "2026-08-01", "2026-08-31", config.report_dir, config.strategy_dir,
        single_mode=True,
    )
    job.phase = "BOT_RUN"
    job.progress = {"current": 0, "total": 1}
    service = LocalSingleModeStrategyTestService(config)
    callback_failed = False

    def publish(snapshot: dict[str, object]) -> None:
        nonlocal callback_failed
        if not callback_failed:
            callback_failed = True
            raise OSError("temporary progress journal failure")
        controller._record_special_job(snapshot)

    service._on_update = publish
    service._emit(job)
    assert job.progress_publication_error == "temporary progress journal failure"

    service._emit(job)
    assert controller._panel_jobs.get(job_id)["progress"]["publication_error"] == "temporary progress journal failure"
    assert job.progress_publication_error is None

    service._emit(job)
    assert "publication_error" not in controller._panel_jobs.get(job_id)["progress"]


def test_emit_does_not_suppress_terminal_callback_failure_from_live_worker_snapshot(tmp_path: Path) -> None:
    manifest_path, names = _generation(tmp_path, 1)
    manifest = fast_strategy_module.validate_strategy_manifest(manifest_path)
    config = _config(tmp_path)
    job = fast_strategy_module._Job(
        "snapshot-terminal", manifest_path, manifest, names, names,
        "2026-01-01", "2026-01-09", config.report_dir, config.strategy_dir,
        single_mode=True,
    )
    release = Event()
    worker = fast_strategy_module.Thread(target=release.wait, daemon=True)
    worker.start()
    job.thread = worker
    job.state = job.phase = "COMMITTED"
    job.target_finalized = True
    service = LocalSingleModeStrategyTestService(config)

    def fail_terminal(_snapshot: dict[str, object]) -> None:
        job.state = "RUNNING"
        job.phase = "BOT_RUN"
        raise OSError("terminal persistence failed")

    service._on_update = fail_terminal
    try:
        with pytest.raises(OSError, match="terminal persistence failed"):
            service._emit(job)
    finally:
        release.set()
        worker.join(1)


def test_single_mode_primary_failure_survives_cleanup_failure_and_keeps_job_failed(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    stop_calls = 0

    def stop_bot(_: RunnerConfig) -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls > 2:
            raise RuntimeError("tester stop failed")

    service = LocalSingleModeStrategyTestService(
        config,
        start_bot=lambda _: None,
        stop_bot=stop_bot,
        client_factory=lambda _: SimpleNamespace(run_tester=lambda: None, close=lambda: None),
    )
    service._start_native_bot = lambda *_args: None

    def fail_native(_job: object, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("primary native error")

    service._wait_for_native_idle = fail_native
    started = service.start(
        manifest, analysis_run_id="a" * 64, start_date="2026-08-01",
        end_date="2026-08-31", job_id="primary-error-cleanup-failure",
    )
    worker = service._jobs[str(started["job_id"])].thread
    assert worker is not None
    worker.join(2)
    assert not worker.is_alive()

    status = service.status(str(started["job_id"]))
    assert status["state"] == "FAILED"
    assert status["phase"] == "FAILED"
    assert status["error"]["code"] == "SINGLE_MODE_TEST_FAILED"
    assert status["error"]["message"] == "primary native error"
    assert "tester stop failed" in status["error"]["cleanup_error"]
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()
    with pytest.raises(FastStrategyTestError, match="cleanup remains pending"):
        service.start(
            manifest, analysis_run_id="a" * 64, start_date="2026-08-01",
            end_date="2026-08-31", job_id="must-wait-for-cleanup",
        )


def test_cancelled_cleanup_keeps_already_recorded_primary_failure(tmp_path: Path) -> None:
    manifest_path, names = _generation(tmp_path, 1)
    manifest = fast_strategy_module.validate_strategy_manifest(manifest_path)
    config = _config(tmp_path)
    service = LocalSingleModeStrategyTestService(config, stop_bot=lambda _: None)
    job = fast_strategy_module._Job(
        "cancel-primary-cleanup", manifest_path, manifest, names, names,
        "2026-08-01", "2026-08-31", config.report_dir, config.strategy_dir,
        single_mode=True,
    )
    job.error = {"code": "SINGLE_MODE_TEST_FAILED", "message": "native failure"}
    job.cancel.set()
    service._jobs[job.job_id] = job

    def pending_cleanup(_job: object) -> None:
        raise fast_strategy_module._FastCleanupUnconfirmed("cleanup still pending")

    service._run_owned = pending_cleanup
    service._run(job)

    status = service.status(job.job_id)
    assert status["state"] == "CANCELLED"
    assert status["error"]["code"] == "SINGLE_MODE_TEST_FAILED"
    assert status["error"]["message"] == "native failure"
    assert status["error"]["cleanup_error"] == "cleanup still pending"
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()

    service._reconcile_pending_cleanup(job)
    assert not (config.bot_root / ".mrs3-tester-target.lock").exists()


def test_successful_native_batch_stop_failure_blocks_until_cleanup_reconciles(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path, 2)
    config = replace(_config(tmp_path), strategy_batch_size=1)
    report = config.report_dir / f"{names[0]}.html"
    stop_calls = 0
    allow_recovery = False

    def stop_bot(_: RunnerConfig) -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls >= 3 and not allow_recovery:
            raise RuntimeError("native batch stop failed")

    class Client:
        def run_tester(self) -> None:
            config.report_dir.mkdir(parents=True, exist_ok=True)
            report.write_text("verified native report", encoding="utf-8")

        def close(self) -> None:
            pass

    service = LocalSingleModeStrategyTestService(
        config, start_bot=lambda _: None, stop_bot=stop_bot,
        client_factory=lambda _: Client(),
    )
    service._start_native_bot = lambda *_args: None
    service._wait_for_native_idle = lambda *_args, **_kwargs: None
    service._native_reports = lambda _dir, expected, **_kwargs: {name: report for name in expected}

    started = service.start(
        manifest, analysis_run_id="a" * 64, start_date="2026-08-01",
        end_date="2026-08-31", job_id="successful-batch-stop-failure",
    )
    worker = service._jobs[str(started["job_id"])].thread
    assert worker is not None
    worker.join(2)
    assert not worker.is_alive()

    status = service.status(str(started["job_id"]))
    assert status["state"] == "FAILED"
    assert status["error"]["code"] == "RESTORE_OR_RELEASE_FAILED"
    assert "native batch stop failed" in status["error"]["cleanup_error"]
    assert status["progress"]["current"] == 1
    assert status["progress"]["failed"] == 0
    assert status["evidence"]["verified_reports"] == {names[0]: report.name}
    assert status["inbox_ready"] is False
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()
    with pytest.raises(FastStrategyTestError, match="cleanup remains pending"):
        service.start(
            manifest, analysis_run_id="a" * 64, start_date="2026-08-01",
            end_date="2026-08-31", job_id="blocked-by-stop-failure",
        )

    allow_recovery = True
    service.reconcile_pending_cleanup()
    assert not (config.bot_root / ".mrs3-tester-target.lock").exists()


def test_finalist_cancelling_checkpoint_write_error_is_reported_as_progress_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, names = _generation(tmp_path, 1)
    manifest = fast_strategy_module.validate_strategy_manifest(manifest_path)
    config = _config(tmp_path)
    controller = PanelController(tmp_path, tmp_path / "config.local.json")
    job_id = "cancelling-checkpoint-warning"
    controller._panel_jobs.submit(
        "strategies.performance.v2.finalist-retest", {}, "cancelling-checkpoint-warning",
        ("strategies.tester",), job_id=job_id,
    )
    controller._panel_jobs.transition(job_id, "RUNNING")

    def fail_checkpoint(_job_id: str, _progress: dict[str, object]) -> None:
        raise OSError("checkpoint disk write failed")

    monkeypatch.setattr(controller._panel_jobs, "_write_progress_checkpoint", fail_checkpoint)
    service = LocalSingleModeStrategyTestService(config)
    service._on_update = controller._record_special_job
    job = fast_strategy_module._Job(
        job_id, manifest_path, manifest, names, names,
        "2026-08-01", "2026-08-31", config.report_dir, config.strategy_dir,
        single_mode=True,
    )
    job.state = "RUNNING"
    job.phase = "CANCELLING"

    service._emit(job)

    assert job.progress_publication_error == "checkpoint disk write failed"
    assert controller._panel_jobs.get(job_id)["phase"] == "CANCELLING"


@pytest.mark.parametrize(
    "duplicate_header",
    ("Fee", "Side"),
)
def test_native_prevalidation_rejects_duplicate_action_headers(duplicate_header: str) -> None:
    source = CURRENT_REPORT.read_text(encoding="utf-8")
    if duplicate_header == "Fee":
        replacements = (
            ("<th>Fee</th>", "<th>Fee</th><th>Fee</th>"),
            ("<td>opened</td><td>0.05</td>", "<td>opened</td><td>0.05</td><td>0.05</td>"),
            ("<td>closed</td><td>0.05</td>", "<td>closed</td><td>0.05</td><td>0.05</td>"),
        )
    else:
        replacements = (
            ("<th>Action</th><th>Fee</th>", "<th>Action</th><th>Side</th><th>Side</th><th>Fee</th>"),
            ("<td>opened</td><td>0.05</td>", "<td>opened</td><td>buy</td><td>buy</td><td>0.05</td>"),
            ("<td>closed</td><td>0.05</td>", "<td>closed</td><td>sell</td><td>sell</td><td>0.05</td>"),
        )
    for old, new in replacements:
        updated = source.replace(old, new, 1)
        assert updated != source
        source = updated

    assert not _has_current_performance_v2_layout(source)


def test_fast_test_replaces_strategy_dir_for_each_chunk_and_clears_success(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path, 5)
    config = _config(tmp_path)
    observed: list[tuple[str, ...]] = []

    def start_bot(_: RunnerConfig) -> object:
        observed.append(tuple(sorted(path.stem for path in config.strategy_dir.glob("*.json"))))
        return object()

    def monitor(client: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        del client
        for name in expected:
            report = config.report_dir / f"{name}.html"
            report.write_text(f'<pre>{{"name":"{name}","basic":{{}}}}</pre>', encoding="utf-8")
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", config.report_dir / f"{name}.html", True, 1) for name in expected},
            polls=1,
            elapsed_seconds=0,
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=start_bot,
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    job = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-1")
    status = _wait(service, str(job["job_id"]))

    assert status["phase"] == "COMMITTED", status
    assert observed == [names[:2], names[2:4], names[4:]]
    assert not list(config.strategy_dir.glob("*.json")), status
    tester_config = json.loads(config.tester_config.read_text(encoding="utf-8"))
    assert tester_config["include_chart_balance"] is False
    assert tester_config["MakerFee"] == 0.00001
    assert "use_runs" not in tester_config
    assert "parameter_mining" not in tester_config
    assert tester_config["report"]["include_chart_balance"] is False
    assert tester_config["report"]["include_position_stats"] is True
    fast_manifest = json.loads((config.report_dir / "fast_test_manifest.json").read_text(encoding="utf-8"))
    assert fast_manifest["expected_names"] == list(names)
    assert fast_manifest["candidate_diagnostics"]["S0"]["orders"][0]["plateau_point_count"] == 3


def test_fast_test_retains_owner_when_tester_stop_is_unconfirmed(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: object(),
        stop_bot=lambda _: (_ for _ in ()).throw(RuntimeError("stop failed")),
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
    )

    job = service.start(
        manifest, analysis_run_id="a" * 64, start_date="2026-08-01",
        end_date="2026-08-31", job_id="fast-stop-failed",
    )

    assert _wait(service, str(job["job_id"]))["state"] == "FAILED"
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()


def test_single_mode_reconciles_pending_cancel_cleanup_before_relaunch(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    native_started = Event()
    stop_calls = 0

    def stop_bot(_: RunnerConfig) -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls == 2:
            raise RuntimeError("transient stop failure")

    service = LocalSingleModeStrategyTestService(config, stop_bot=stop_bot)

    def interrupted_native(job: object) -> None:
        native_started.set()
        while not job.cancel.is_set():
            time.sleep(0.001)
        raise fast_strategy_module._FastCancelled()

    service._run_native = interrupted_native
    first = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-pending-cleanup",
    )
    assert native_started.wait(1)
    service.cancel(str(first["job_id"]))

    worker = service._jobs[str(first["job_id"])].thread
    assert worker is not None
    worker.join(1)
    assert not worker.is_alive()
    assert service.status(str(first["job_id"]))["state"] == "CANCELLED"
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()

    service._run_native = lambda _job: (_ for _ in ()).throw(RuntimeError("second run"))
    second = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-after-reconcile",
    )
    assert _wait(service, str(second["job_id"]))["state"] == "FAILED"
    assert service.status(str(first["job_id"]))["state"] == "CANCELLED"
    assert not (config.bot_root / ".mrs3-tester-target.lock").exists()


def test_single_mode_pending_cleanup_failure_blocks_relaunch_without_releasing_lock(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    native_started = Event()
    stop_calls = 0

    def stop_bot(_: RunnerConfig) -> None:
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls >= 2:
            raise RuntimeError("persistent stop failure")

    service = LocalSingleModeStrategyTestService(config, stop_bot=stop_bot)

    def interrupted_native(job: object) -> None:
        native_started.set()
        while not job.cancel.is_set():
            time.sleep(0.001)
        raise fast_strategy_module._FastCancelled()

    service._run_native = interrupted_native
    first = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-blocked-cleanup",
    )
    assert native_started.wait(1)
    service.cancel(str(first["job_id"]))
    worker = service._jobs[str(first["job_id"])].thread
    assert worker is not None
    worker.join(1)
    assert not worker.is_alive()

    with pytest.raises(FastStrategyTestError, match="cleanup remains pending"):
        service.start(
            manifest,
            analysis_run_id="a" * 64,
            start_date="2026-08-01",
            end_date="2026-08-31",
            job_id="single-must-stay-blocked",
        )
    assert service.status(str(first["job_id"]))["state"] == "CANCELLED"
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()
    assert "single-must-stay-blocked" not in service._jobs


def test_single_mode_successful_cancellation_releases_target_as_before(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    native_started = Event()
    service = LocalSingleModeStrategyTestService(config, stop_bot=lambda _: None)

    def interrupted_native(job: object) -> None:
        native_started.set()
        while not job.cancel.is_set():
            time.sleep(0.001)
        raise fast_strategy_module._FastCancelled()

    service._run_native = interrupted_native
    first = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-clean-cancel",
    )
    assert native_started.wait(1)
    service.cancel(str(first["job_id"]))
    status = _wait(service, str(first["job_id"]))

    assert status["state"] == "CANCELLED"
    assert not (config.bot_root / ".mrs3-tester-target.lock").exists()


def test_single_mode_reconciles_release_failure_before_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    release_calls = 0
    real_release = TesterTargetLock.release

    def fail_once(owner: TesterTargetLock) -> None:
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise RuntimeError("transient release failure")
        real_release(owner)

    monkeypatch.setattr(TesterTargetLock, "release", fail_once)
    service = LocalSingleModeStrategyTestService(config, stop_bot=lambda _: None)

    def completed(job: object) -> None:
        job.state = job.phase = "FAILED"

    service._run_native = completed

    first = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-release-once",
    )
    worker = service._jobs[str(first["job_id"])].thread
    assert worker is not None
    worker.join(1)
    assert not worker.is_alive()
    assert service.status(str(first["job_id"]))["state"] == "FAILED"
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()

    second = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-release-relaunch",
    )

    assert _wait(service, str(second["job_id"]))["state"] == "FAILED"
    assert service.status(str(first["job_id"]))["state"] == "FAILED"
    assert release_calls == 3
    assert not (config.bot_root / ".mrs3-tester-target.lock").exists()


def test_single_mode_persistent_release_failure_blocks_relaunch_without_releasing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    release_calls = 0

    def always_fail(_owner: TesterTargetLock) -> None:
        nonlocal release_calls
        release_calls += 1
        raise RuntimeError("persistent release failure")

    monkeypatch.setattr(TesterTargetLock, "release", always_fail)
    service = LocalSingleModeStrategyTestService(config, stop_bot=lambda _: None)

    def completed(job: object) -> None:
        job.state = job.phase = "FAILED"

    service._run_native = completed
    first = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-release-blocked",
    )

    worker = service._jobs[str(first["job_id"])].thread
    assert worker is not None
    worker.join(1)
    assert not worker.is_alive()
    assert service.status(str(first["job_id"]))["state"] == "FAILED"
    with pytest.raises(FastStrategyTestError, match="cleanup remains pending"):
        service.start(
            manifest,
            analysis_run_id="a" * 64,
            start_date="2026-08-01",
            end_date="2026-08-31",
            job_id="single-release-must-stay-blocked",
        )
    assert release_calls == 4
    assert (config.bot_root / ".mrs3-tester-target.lock").is_file()
    assert "single-release-must-stay-blocked" not in service._jobs


def test_single_mode_retry_reconciles_pending_release_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    release_calls = 0
    real_release = TesterTargetLock.release

    def fail_once(owner: TesterTargetLock) -> None:
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise RuntimeError("transient release failure")
        real_release(owner)

    monkeypatch.setattr(TesterTargetLock, "release", fail_once)
    service = LocalSingleModeStrategyTestService(config, stop_bot=lambda _: None)

    def failed_run(_job: object) -> None:
        raise RuntimeError("run failed")

    service._run_native = failed_run
    first = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="single-retry-source",
    )
    worker = service._jobs[str(first["job_id"])].thread
    assert worker is not None
    worker.join(1)
    assert not worker.is_alive()
    assert service.status(str(first["job_id"]))["state"] == "FAILED"

    retry = service.retry(str(first["job_id"]), job_id="single-retry-after-cleanup")

    assert _wait(service, str(retry["job_id"]))["state"] == "FAILED"
    assert service.status(str(first["job_id"]))["state"] == "FAILED"
    assert release_calls == 3
    assert not (config.bot_root / ".mrs3-tester-target.lock").exists()


def test_fast_test_releases_owner_after_clean_ordinary_failure(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)

    class OrdinaryFailure(LocalFastStrategyTestService):
        def _run_owned(self, job: object) -> None:
            raise RuntimeError("ordinary tester failure")

    service = OrdinaryFailure(config)
    job = service.start(
        manifest,
        analysis_run_id="a" * 64,
        start_date="2026-08-01",
        end_date="2026-08-31",
        job_id="fast-clean-failure",
    )
    status = _wait(service, str(job["job_id"]))

    assert status["state"] == "FAILED"
    assert "ordinary tester failure" in json.dumps(status)
    with TesterTargetLock(config.bot_root):
        pass


def test_fast_test_captures_only_verified_reports_for_performance_import(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path, 2)
    config = _config(tmp_path)

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        for name in expected:
            (config.report_dir / f"{name}.html").write_text(
                f'<p>Test period: 2026-08-01 - 2026-08-31</p><pre>{{"name":"{name}","exchange":{{"name":"Bybit"}},"basic":{{"symbol":"BTCUSDT","time_frame":"1h"}}}}</pre>',
                encoding="utf-8",
            )
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", config.report_dir / f"{name}.html", True, 1) for name in expected},
            polls=1,
            elapsed_seconds=0,
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: object(),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-inbox")
    assert _wait(service, str(initial["job_id"]))["phase"] == "COMMITTED"
    (config.report_dir / "old.html").write_text("unused", encoding="utf-8")

    inbox = service.capture_inbox("fast-inbox")
    inbox_manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))

    assert inbox_manifest["run_mode"] == "FAST"
    assert [entry["strategy_name"] for entry in inbox_manifest["entries"]] == list(names)
    assert all(Path(entry["report_path"]).name != "old.html" for entry in inbox_manifest["entries"])


def test_fast_test_captures_complete_manifest_after_service_restart(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path, 1)
    config = _config(tmp_path)

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        name = expected[0]
        report = config.report_dir / f"{name}.html"
        report.write_text(
            f'<p>Test period: 2026-08-01 - 2026-08-31</p><pre>{{"name":"{name}","exchange":{{"name":"Bybit"}},"basic":{{"symbol":"BTCUSDT","time_frame":"1h"}}}}</pre>',
            encoding="utf-8",
        )
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", report, True, 1)},
            polls=1,
            elapsed_seconds=0,
        )

    first = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: object(),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    initial = first.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-restart-inbox")
    assert _wait(first, str(initial["job_id"]))["phase"] == "COMMITTED"
    persisted_path = config.report_dir / "fast_test_manifest.json"
    persisted = None
    for _ in range(100):
        try:
            persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
            break
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
    assert persisted is not None
    persisted["phase"] = "RUNNING"
    persisted_path.write_text(json.dumps(persisted), encoding="utf-8")

    second = LocalFastStrategyTestService(config)
    inbox = second.capture_inbox("fast-restart-inbox")
    inbox_manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))

    assert inbox_manifest["expected_strategy_names"] == list(names)


def test_single_mode_captures_finished_reports_after_panel_restart(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / f"{names[0]}.html"
    report.write_text(
        CURRENT_REPORT.read_text(encoding="utf-8")
        .replace('"name":"MRS3 Current v2"', f'"name":"{names[0]}"', 1)
        .replace('"symbol":"ONUSDT"', '"symbol":"BTCUSDT"', 1),
        encoding="utf-8",
    )
    (config.report_dir / "tester_manifest.json").write_text(json.dumps({
        "job_id": "single-restart-inbox",
        "mode": "SINGLE_MODE",
        "phase": "RUNNING",
        "generation_manifest_path": str(manifest),
        "expected_names": list(names),
        "start_date": "2026-01-01",
        "end_date": "2026-01-09",
        "attempt_counts": {names[0]: 1},
        "verified_reports": {},
        "failed_names": [],
    }), encoding="utf-8")

    inbox = LocalSingleModeStrategyTestService(config).capture_inbox("single-restart-inbox")
    inbox_manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))

    assert inbox_manifest["expected_strategy_names"] == list(names)


def test_bulk_finalist_retest_restart_rebuilds_indexed_batch_and_manual_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_manifest, names = _generation(tmp_path / "Output", 2361)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    report_template = CURRENT_REPORT.read_text(encoding="utf-8")
    indexed_reports: dict[str, str] = {}
    for index, name in enumerate(names[:-1], 1):
        filename = f"my_test_run_{index:04d}_of_2361_original.html"
        (config.report_dir / filename).write_text(
            report_template
            .replace('"name":"MRS3 Current v2"', f'"name":"{name}"', 1)
            .replace('"symbol":"ONUSDT"', '"symbol":"BTCUSDT"', 1),
            encoding="utf-8",
        )
        indexed_reports[name] = filename
    manual_name = names[-1]
    manual_filename = "001_of_001.html"
    (config.report_dir / manual_filename).write_text(
        report_template
        .replace('"name":"MRS3 Current v2"', f'"name":"{manual_name}"', 1)
        .replace('"symbol":"ONUSDT"', '"symbol":"BTCUSDT"', 1),
        encoding="utf-8",
    )
    config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
    config.wizard_result.write_text(json.dumps([{
        "runId": "manual-single-run",
        "strategies": [manual_name],
        "chartUrl": f"/tester/report/{manual_filename}",
    }]), encoding="utf-8")
    job_id = "bulk-finalist-retest-restart"
    expected_names = tuple(sorted(names))
    (config.report_dir / "tester_manifest.json").write_text(json.dumps({
        "job_id": job_id,
        "mode": "SINGLE_MODE",
        "phase": "RUNNING",
        "generation_manifest_path": str(generation_manifest),
        "expected_names": list(expected_names),
        "start_date": "2026-01-01",
        "end_date": "2026-01-09",
        "attempt_counts": {name: 1 for name in names},
        "verified_reports": indexed_reports,
        "failed_names": [],
    }), encoding="utf-8")
    config_path = tmp_path / "config.local.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        panel_module.RunnerConfig, "from_json", staticmethod(lambda _path: config),
    )
    monkeypatch.setattr(
        LocalSingleModeStrategyTestService,
        "start",
        lambda *_args, **_kwargs: pytest.fail("recovery must not submit strategies"),
    )

    first = PanelController(tmp_path, config_path)
    first._panel_jobs.submit(
        "strategies.performance.v2.finalist-retest", {}, f"panel:{job_id}",
        ("strategies.tester",), job_id=job_id,
    )
    first._panel_jobs.transition(job_id, "RUNNING")
    frozen_cohort = [{"strategy_name": name} for name in names]
    first._panel_jobs.sync(
        job_id,
        {"state": "FAILED", "phase": "FAILED", "error": {"code": "INTERRUPTED"}},
        runtime={"bulk_retest": True, "cohort_members": frozen_cohort},
    )

    restarted = PanelController(tmp_path, config_path)
    assert isinstance(restarted._single_mode_strategy_test(), LocalSingleModeStrategyTestService)
    result = restarted.strategies_tester_verify_inbox(job_id)

    assert result["state"] == "COMMITTED"
    assert result["inbox_ready"] is True
    assert restarted._panel_jobs.runtime(job_id)["cohort_members"] == frozen_cohort
    inbox = Path(restarted._panel_jobs.runtime(job_id)["inbox_path"])
    inbox_manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert len(inbox_manifest["entries"]) == 2361
    assert {entry["strategy_name"] for entry in inbox_manifest["entries"]} == set(names)
    manual_entry = next(entry for entry in inbox_manifest["entries"] if entry["strategy_name"] == manual_name)
    assert manual_entry["report_path"] == manual_filename
    assert json.loads(config.wizard_result.read_text(encoding="utf-8"))[0]["chartUrl"].endswith(manual_filename)


def test_finalist_import_click_captures_persisted_html_then_starts_replace_after_panel_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation_manifest, names = _generation(tmp_path / "Output", 1)
    name = names[0]
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / f"{name}.html"
    report.write_text(
        CURRENT_REPORT.read_text(encoding="utf-8")
        .replace('"name":"MRS3 Current v2"', f'"name":"{name}"', 1)
        .replace('"symbol":"ONUSDT"', '"symbol":"BTCUSDT"', 1),
        encoding="utf-8",
    )
    tester_manifest = {
        "job_id": "finalist-import-after-restart",
        "mode": "SINGLE_MODE",
        "phase": "COMMITTED",
        "generation_manifest_path": str(generation_manifest),
        "expected_names": list(names),
        "start_date": "2026-01-01",
        "end_date": "2026-01-09",
        "attempt_counts": {name: 1},
        "verified_reports": {name: report.name},
        "failed_names": [],
    }
    (config.report_dir / "tester_manifest.json").write_text(
        json.dumps(tester_manifest), encoding="utf-8",
    )
    config_path = tmp_path / "config.local.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        panel_module.RunnerConfig, "from_json", staticmethod(lambda _path: config),
    )

    first_panel = PanelController(tmp_path, config_path)
    tester_job_id = str(tester_manifest["job_id"])
    first_panel._panel_jobs.submit(
        "strategies.performance.v2.finalist-retest", {}, "persist-finalist-job",
        ("strategies.tester",), job_id=tester_job_id,
    )
    first_panel._panel_jobs.transition(tester_job_id, "RUNNING", phase="BOT_RUN")
    first_panel._panel_jobs.sync(
        tester_job_id,
        {"state": "COMMITTED", "phase": "COMMITTED", "progress": {"current": 1, "total": 1}},
        runtime={
            "bulk_retest": True,
            "scope": "FINALIST",
            "test_start": "2026-01-01",
            "test_end": "2026-01-09",
            "cohort_members": [{"strategy_id": 7, "strategy_name": name, "result_id": 17}],
        },
    )
    controller = PanelController(tmp_path, config_path)
    tester_service = LocalSingleModeStrategyTestService(config)
    monkeypatch.setattr(controller, "_single_mode_strategy_test", lambda: tester_service)
    queued: list[dict[str, object]] = []

    def start_import(
        payload: dict[str, object], *, _internal: bool = False, job_id: str | None = None,
    ) -> dict[str, object]:
        assert _internal is True
        assert job_id is not None
        assert payload["tester_job_id"] == tester_job_id
        assert payload["mode"] == "REPLACE"
        assert payload["replacement_strategy_ids"] == {name: 7}
        assert payload["_expected_current_result_ids"] == {name: 17}
        inbox = controller._tester_inbox(tester_job_id)
        inbox_manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
        assert inbox_manifest["expected_strategy_names"] == list(names)
        assert inbox_manifest["source_mode"] == "metadata_only"
        child = controller._panel_jobs.submit(
            "strategies.performance.v2.import", {}, "persist-import-child",
            ("performance-v2-db",), job_id=job_id,
        )
        controller._panel_jobs.transition(child["job_id"], "RUNNING")
        queued.append(payload)
        return {"job_id": child["job_id"]}

    monkeypatch.setattr(controller, "strategies_performance_v2_import", start_import)

    result = controller.strategies_performance_v2_finalist_retest_import({"tester_job_id": tester_job_id})

    assert result["job_id"] == controller._panel_jobs.runtime(tester_job_id)["bulk_import_job_id"]
    assert len(queued) == 1
    assert controller._panel_jobs.get(tester_job_id)["inbox_ready"] is True
    assert controller._panel_jobs.runtime(tester_job_id)["inbox_path"] == str((config.inbox_root / tester_job_id).resolve())


@pytest.mark.parametrize(
    "manifest_change",
    (
        {"failed_names": ["S0"]},
        {"phase": "FAILED"},
        {"expected_names": ["../S0"]},
    ),
)
def test_single_mode_restart_rejects_unfinished_or_untrusted_manifest(
    tmp_path: Path, manifest_change: dict[str, object]
) -> None:
    manifest, names = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / f"{names[0]}.html"
    report.write_text(
        CURRENT_REPORT.read_text(encoding="utf-8")
        .replace('"name":"MRS3 Current v2"', f'"name":"{names[0]}"', 1)
        .replace('"symbol":"ONUSDT"', '"symbol":"BTCUSDT"', 1),
        encoding="utf-8",
    )
    saved = {
        "job_id": "single-restart-rejected",
        "mode": "SINGLE_MODE",
        "phase": "RUNNING",
        "generation_manifest_path": str(manifest),
        "expected_names": list(names),
        "start_date": "2026-01-01",
        "end_date": "2026-01-09",
        "attempt_counts": {names[0]: 1},
        "verified_reports": {},
        "failed_names": [],
        **manifest_change,
    }
    (config.report_dir / "tester_manifest.json").write_text(json.dumps(saved), encoding="utf-8")

    with pytest.raises(FastStrategyTestError, match="no completed reports"):
        LocalSingleModeStrategyTestService(config).capture_inbox("single-restart-rejected")

    assert not (config.inbox_root / "single-restart-rejected").exists()


def test_single_mode_restart_rejects_partial_reports(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path, 2)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    (config.report_dir / f"{names[0]}.html").write_text(
        CURRENT_REPORT.read_text(encoding="utf-8")
        .replace('"name":"MRS3 Current v2"', f'"name":"{names[0]}"', 1)
        .replace('"symbol":"ONUSDT"', '"symbol":"BTCUSDT"', 1),
        encoding="utf-8",
    )
    (config.report_dir / "tester_manifest.json").write_text(json.dumps({
        "job_id": "single-restart-partial",
        "mode": "SINGLE_MODE",
        "phase": "RUNNING",
        "generation_manifest_path": str(manifest),
        "expected_names": list(names),
        "start_date": "2026-01-01",
        "end_date": "2026-01-09",
        "attempt_counts": {name: 1 for name in names},
        "verified_reports": {},
        "failed_names": [],
    }), encoding="utf-8")

    with pytest.raises(FastStrategyTestError, match="no completed reports"):
        LocalSingleModeStrategyTestService(config).capture_inbox("single-restart-partial")

    assert not (config.inbox_root / "single-restart-partial").exists()


def test_fast_test_rejects_malformed_plateau_diagnostics(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["candidate_diagnostics"]["S0"]["orders"] = []
    unsigned = dict(document)
    unsigned.pop("generation_manifest_sha256")
    document["generation_manifest_sha256"] = sha256(_canonical(unsigned)).hexdigest()
    manifest.write_text(json.dumps(document), encoding="utf-8")

    try:
        LocalFastStrategyTestService(_config(tmp_path)).start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31")
    except FastStrategyTestError as error:
        assert "malformed plateau diagnostics" in str(error)
    else:
        raise AssertionError("malformed diagnostics must be rejected")


def test_fast_test_continues_after_failure_and_leaves_only_failed_json(tmp_path: Path) -> None:
    manifest, names = _generation(tmp_path, 4)
    config = _config(tmp_path)

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        successful = tuple(name for name in expected if name != "S1")
        for name in successful:
            (config.report_dir / f"{name}.html").write_text(f'<pre>{{"name":"{name}","basic":{{}}}}</pre>', encoding="utf-8")
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", config.report_dir / f"{name}.html" if name in successful else None, name in successful, 4 if name == "S1" else 1) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=("S1",) if "S1" in expected else (),
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: object(),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    job = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-2")
    status = _wait(service, str(job["job_id"]))

    assert status["phase"] == "PARTIAL", status
    assert status["progress"]["current"] == 3
    assert status["evidence"]["failed_names"] == ["S1"]
    assert not list(config.strategy_dir.glob("*.json"))


def test_fast_retry_accepts_matching_manual_report_without_starting_bot(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 2)
    config = _config(tmp_path)
    starts: list[int] = []

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        successful = tuple(name for name in expected if name != "S1")
        for name in successful:
            (config.report_dir / f"{name}.html").write_text(f'<pre>{{"name":"{name}","basic":{{}}}}</pre>', encoding="utf-8")
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", config.report_dir / f"{name}.html" if name in successful else None, name in successful, 4 if name == "S1" else 1) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=("S1",) if "S1" in expected else (),
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: starts.append(1),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-retry-source")
    assert _wait(service, str(initial["job_id"]))["phase"] == "PARTIAL"
    (config.report_dir / "S1.html").write_text('<p>Test period: 2026-08-01 - 2026-08-31</p><pre>{"name":"S1","basic":{"symbol":"BTCUSDT","time_frame":"1h"}}</pre>', encoding="utf-8")

    recovered = service.retry(str(initial["job_id"]), job_id="fast-retry-1")
    status = _wait(service, str(recovered["job_id"]))

    assert status["phase"] == "COMMITTED", status
    assert status["progress"]["current"] == 2
    assert status["evidence"]["failed_names"] == []
    assert starts == [1]
    assert not list(config.strategy_dir.glob("*.json"))


def test_single_mode_retry_with_all_reports_waits_for_explicit_inbox_verification(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 2)
    config = _config(tmp_path)
    updates: list[dict[str, object]] = []

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        successful = tuple(name for name in expected if name != "S1")
        for name in successful:
            (config.report_dir / f"{name}.html").write_text(
                f'<pre>{{"name":"{name}","basic":{{}}}}</pre>', encoding="utf-8"
            )
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", config.report_dir / f"{name}.html" if name in successful else None, name in successful, 1) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=("S1",),
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: None,
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
        on_update=updates.append,
    )
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="single-source")
    assert _wait(service, str(initial["job_id"]))["phase"] == "PARTIAL"
    service.single_mode = True
    service._jobs["single-source"].single_mode = True
    (config.report_dir / "S1.html").write_text(
        '<p>Test period: 2026-08-01 - 2026-08-31</p><pre>{"name":"S1","basic":{"symbol":"BTCUSDT","time_frame":"1h"}}</pre>',
        encoding="utf-8",
    )
    updates.clear()

    status = service.retry("single-source", job_id="single-retry")

    assert status["state"] == "COMMITTED"
    assert status["inbox_ready"] is False
    assert "inbox_path" not in status
    assert not (config.inbox_root / "single-retry").exists()
    assert updates[-1]["progress"]["current"] == 2

    inbox = service.capture_inbox("single-retry")
    service.mark_inbox_ready("single-retry", inbox)
    restored = service.status("single-retry")
    assert Path(str(restored["inbox_path"]), "inbox_manifest.json").is_file()
    assert restored["progress"]["current"] == 2
    assert restored["progress"]["total"] == 2


def test_single_mode_capture_uses_the_saved_tester_config_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest_path, names = _generation(tmp_path, 1)
    config = _config(tmp_path)
    service = LocalSingleModeStrategyTestService(config)
    manifest = fast_strategy_module.validate_strategy_manifest(manifest_path)
    job = fast_strategy_module._Job(
        "saved-config", manifest_path, manifest, names, names, "2026-08-01", "2026-08-31",
        config.report_dir, config.strategy_dir, single_mode=True,
        verified_reports={names[0]: f"{names[0]}.html"}, tester_config_bytes=b'{"saved":true}',
    )
    service._jobs[job.job_id] = job
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        fast_strategy_module,
        "capture_run_snapshot_inbox",
        lambda *_args, **kwargs: captured.update(kwargs) or config.inbox_root / job.job_id,
    )
    config.tester_config.write_text('{"current":true}', encoding="utf-8")

    service.capture_inbox(job.job_id)

    assert captured["tester_config_bytes"] == b'{"saved":true}'


def test_fast_retry_indexes_only_unverified_reports_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest, names = _generation(tmp_path, 3)
    config = _config(tmp_path)
    starts: list[int] = []

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        successful = tuple(name for name in expected if name == "S0")
        for name in successful:
            (config.report_dir / f"{name}.html").write_text(
                f'<pre>{{"name":"{name}","basic":{{}}}}</pre>', encoding="utf-8"
            )
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", config.report_dir / f"{name}.html" if name in successful else None, name in successful, 1) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=tuple(name for name in expected if name != "S0"),
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: starts.append(1),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-index-source")
    assert _wait(service, str(initial["job_id"]))["phase"] == "PARTIAL"
    (config.report_dir / "S1.html").write_text(
        '<p>Test period: 2026-08-01 - 2026-08-31</p><pre>{"name":"S1","basic":{"symbol":"BTCUSDT","time_frame":"1h"}}</pre>',
        encoding="utf-8",
    )
    calls = 0
    original_extract = fast_strategy_module.extract_html_strategy_settings

    def count_extract(path: Path) -> dict[str, object] | None:
        nonlocal calls
        calls += 1
        return original_extract(path)

    monkeypatch.setattr(fast_strategy_module, "extract_html_strategy_settings", count_extract)
    recovered = service.retry(str(initial["job_id"]), job_id="fast-index-retry")
    status = _wait(service, str(recovered["job_id"]))

    assert status["phase"] == "PARTIAL"
    assert status["evidence"]["verified_reports"] == {"S0": "S0.html", "S1": "S1.html"}
    assert calls == 1


def test_fast_retry_recovers_partial_manifest_after_service_restart(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 2)
    config = _config(tmp_path)

    def failed_monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", None, False, 4) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=expected,
        )

    first = LocalFastStrategyTestService(config, start_bot=lambda _: None, stop_bot=lambda _: None, client_factory=lambda _: object(), wait_for_exact_batch=lambda *_args, **_kwargs: (), monitor=failed_monitor)
    initial = first.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-restart-source")
    assert _wait(first, str(initial["job_id"]))["phase"] == "PARTIAL"

    def recovered_monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        reports = {}
        for name in expected:
            report = config.report_dir / f"{name}.html"
            report.write_text(f'<p>Test period: 2026-08-01 - 2026-08-31</p><pre>{{"name":"{name}","basic":{{"symbol":"BTCUSDT","time_frame":"1h"}}}}</pre>', encoding="utf-8")
            reports[name] = StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", report, True, 5)
        return BatchCompletion(
            strategies=reports,
            polls=1,
            elapsed_seconds=0,
        )

    second = LocalFastStrategyTestService(config, start_bot=lambda _: None, stop_bot=lambda _: None, client_factory=lambda _: object(), wait_for_exact_batch=lambda *_args, **_kwargs: (), monitor=recovered_monitor)
    retry = second.retry("fast-restart-source", job_id="fast-restart-retry")
    assert _wait(second, str(retry["job_id"]))["phase"] == "COMMITTED"


def test_single_mode_retry_recovers_interrupted_running_manifest(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    config.strategy_dir.mkdir(parents=True)
    (config.report_dir / "tester_manifest.json").write_text(json.dumps({
        "job_id": "interrupted-native",
        "mode": "SINGLE_MODE",
        "phase": "RUNNING",
        "generation_manifest_path": str(manifest),
        "expected_names": ["S0"],
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
        "attempt_counts": {"S0": 1},
        "verified_reports": {},
        "failed_names": ["S0"],
    }), encoding="utf-8")

    service = LocalSingleModeStrategyTestService(config)
    loaded = service._load_persisted_job("interrupted-native")

    assert loaded is not None
    assert loaded.state == "FAILED"
    assert loaded.phase == "FAILED"
    assert loaded.initial_balance is None


def test_fast_terminal_manifest_is_written_before_terminal_state(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 1)
    config = _config(tmp_path)
    observed: list[tuple[str, str]] = []

    def failed_monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", None, False, 4) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=expected,
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: None,
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=failed_monitor,
    )
    write_manifest = service._write_manifest

    def record_manifest(job: object) -> None:
        observed.append((job.phase, job.state))
        write_manifest(job)

    service._write_manifest = record_manifest
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-order")

    assert _wait(service, str(initial["job_id"]))["phase"] == "PARTIAL"
    assert observed[-1] == ("PARTIAL", "RUNNING")


def test_fast_retry_rejects_manual_report_without_unambiguous_period(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 2)
    config = _config(tmp_path)
    starts: list[int] = []

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", None, False, 4) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=expected,
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: starts.append(1),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-period-source")
    assert _wait(service, str(initial["job_id"]))["phase"] == "PARTIAL"
    (config.report_dir / "S1.html").write_text('<p>Report range 2026-08-01 - 2026-08-31</p><p>Test period 2026-07-01 - 2026-07-31</p><pre>{"name":"S1","basic":{"symbol":"BTCUSDT","time_frame":"1h"}}</pre>', encoding="utf-8")

    recovered = service.retry(str(initial["job_id"]), job_id="fast-period-retry")
    status = _wait(service, str(recovered["job_id"]))

    assert status["phase"] == "PARTIAL", status
    assert status["evidence"]["failed_names"] == ["S0", "S1"]
    assert starts == [1, 1]


def test_fast_retry_grants_exactly_one_attempt_to_remaining_strategy(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 2)
    config = _config(tmp_path)
    starts: list[int] = []
    monitor_calls = 0

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        nonlocal monitor_calls
        monitor_calls += 1
        received_config = _args[2]
        assert received_config.max_strategy_attempts == (4 if monitor_calls == 1 else 2)
        if monitor_calls == 1:
            (config.report_dir / "S0.html").write_text('<pre>{"name":"S0","basic":{}}</pre>', encoding="utf-8")
            return BatchCompletion(
                strategies={
                    "S0": StrategyCompletion("S0", RowState.RESULT, (), "run-S0", config.report_dir / "S0.html", True, 1),
                    "S1": StrategyCompletion("S1", RowState.RESULT, (), "run-S1", None, False, 1),
                },
                polls=1,
                elapsed_seconds=0,
                failed_names=("S1",),
            )
        (config.report_dir / "S1.html").write_text('<pre>{"name":"S1","basic":{}}</pre>', encoding="utf-8")
        return BatchCompletion(
            strategies={"S1": StrategyCompletion("S1", RowState.RESULT, (), "run-S1-retry", config.report_dir / "S1.html", True, 2)},
            polls=1,
            elapsed_seconds=0,
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: starts.append(1),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-attempt-source")
    assert _wait(service, str(initial["job_id"]))["phase"] == "PARTIAL"

    recovered = service.retry(str(initial["job_id"]), job_id="fast-attempt-retry")
    status = _wait(service, str(recovered["job_id"]))

    assert status["phase"] == "COMMITTED", status
    assert status["progress"]["current"] == 2
    assert status["evidence"]["failed_names"] == []
    assert starts == [1, 1]


def test_fast_restart_does_not_trust_replaced_verified_report(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 2)
    config = _config(tmp_path)

    def failed_monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}", None, False, 4) for name in expected},
            polls=1,
            elapsed_seconds=0,
            failed_names=expected,
        )

    first = LocalFastStrategyTestService(config, start_bot=lambda _: None, stop_bot=lambda _: None, client_factory=lambda _: object(), wait_for_exact_batch=lambda *_args, **_kwargs: (), monitor=failed_monitor)
    initial = first.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-replaced-source")
    assert _wait(first, str(initial["job_id"]))["phase"] == "PARTIAL"
    report = config.report_dir / "S0.html"
    report.write_text('<p>Report range 2026-08-01 - 2026-08-31</p><pre>{"name":"S0","basic":{"symbol":"WRONG","time_frame":"1h"}}</pre>', encoding="utf-8")
    persisted_path = config.report_dir / "fast_test_manifest.json"
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    persisted["verified_reports"] = {"S0": "S0.html"}
    persisted["failed_names"] = ["S1"]
    persisted_path.write_text(json.dumps(persisted), encoding="utf-8")

    second = LocalFastStrategyTestService(config)
    loaded = second._load_persisted_job("fast-replaced-source")
    assert loaded is not None
    assert "S0" not in loaded.verified_reports
    assert "S0" in loaded.run_names


def test_fast_retry_uses_one_extra_attempt_for_each_previous_attempt_count(tmp_path: Path) -> None:
    manifest, _ = _generation(tmp_path, 3)
    config = _config(tmp_path)
    starts: list[int] = []
    limits: list[int] = []
    monitor_calls = 0

    def monitor(_: object, expected: tuple[str, ...], *_args, **_kwargs) -> BatchCompletion:
        nonlocal monitor_calls
        monitor_calls += 1
        limits.append(_args[2].max_strategy_attempts)
        if monitor_calls <= 2:
            if monitor_calls == 2:
                return BatchCompletion(
                    strategies={"S2": StrategyCompletion("S2", RowState.RESULT, (), "run-S2", None, False, 3)},
                    polls=1,
                    elapsed_seconds=0,
                    failed_names=("S2",),
                )
            report = config.report_dir / "S0.html"
            report.write_text('<pre>{"name":"S0","basic":{}}</pre>', encoding="utf-8")
            return BatchCompletion(
                strategies={
                    "S0": StrategyCompletion("S0", RowState.RESULT, (), "run-S0", report, True, 1),
                    "S1": StrategyCompletion("S1", RowState.RESULT, (), "run-S1", None, False, 1),
                    "S2": StrategyCompletion("S2", RowState.RESULT, (), "run-S2", None, False, 3),
                },
                polls=1,
                elapsed_seconds=0,
                failed_names=("S1", "S2"),
            )
        name = expected[0]
        report = config.report_dir / f"{name}.html"
        report.write_text(f'<pre>{{"name":"{name}","basic":{{}}}}</pre>', encoding="utf-8")
        return BatchCompletion(
            strategies={name: StrategyCompletion(name, RowState.RESULT, (), f"run-{name}-retry", report, True, limits[-1])},
            polls=1,
            elapsed_seconds=0,
        )

    service = LocalFastStrategyTestService(
        config,
        start_bot=lambda _: starts.append(1),
        stop_bot=lambda _: None,
        client_factory=lambda _: object(),
        wait_for_exact_batch=lambda *_args, **_kwargs: (),
        monitor=monitor,
    )
    initial = service.start(manifest, analysis_run_id="a" * 64, start_date="2026-08-01", end_date="2026-08-31", job_id="fast-varied-source")
    assert _wait(service, str(initial["job_id"]))["phase"] == "PARTIAL"

    recovered = service.retry(str(initial["job_id"]), job_id="fast-varied-retry")
    status = _wait(service, str(recovered["job_id"]))

    assert status["phase"] == "COMMITTED", status
    assert limits == [4, 4, 2, 4]
    assert starts == [1, 1, 1]
