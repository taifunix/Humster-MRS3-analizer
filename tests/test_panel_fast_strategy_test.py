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

import mrs3.panel_fast_strategy_test as fast_strategy_module
from mrs3.panel_fast_strategy_test import LocalFastStrategyTestService, LocalSingleModeStrategyTestService
from mrs3.panel_fast_strategy_test import FastStrategyTestError
from mrs3.panel_fast_strategy_test import _has_current_performance_v2_layout
from mrs3.panel_fast_strategy_test import _write_fast_tester_config
from mrs3.locking import TesterTargetLock
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


def test_fast_writer_starts_from_template_and_preserves_unrelated_keys(tmp_path: Path) -> None:
    config = _config(tmp_path)
    template = tmp_path / "config-template.json"
    template.write_text(json.dumps({
        "StartDate": "old",
        "EndDate": "old",
        "use_runs": True,
        "single_mode": False,
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
        template_path=template,
    )

    rendered = json.loads(config.tester_config.read_text(encoding="utf-8"))
    assert rendered["unrelated"] == {"keep": [1, 2, 3]}
    assert rendered["StartDate"] == "2026-08-01"
    assert rendered["EndDate"] == "2026-08-31"
    assert rendered["use_runs"] is False
    assert rendered["single_mode"] is True
    assert rendered["max_parallel_runs"] == config.max_parallel_submissions
    assert rendered["include_chart_balance"] is True
    assert rendered["report"]["include_chart_balance"] is True
    assert rendered["report"]["include_position_stats"] is False
    assert rendered["report"]["include_trades_table"] is True


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


def test_single_mode_retry_with_all_reports_creates_and_publishes_inbox(tmp_path: Path) -> None:
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
    assert status["inbox_ready"] is True
    assert Path(str(status["inbox_path"]), "inbox_manifest.json").is_file()
    assert updates[-1]["progress"]["current"] == 2
    assert updates[-1]["inbox_ready"] is True

    reloaded = LocalSingleModeStrategyTestService(config)
    inbox = reloaded.capture_inbox("single-retry")
    reloaded.mark_inbox_ready("single-retry", inbox)
    restored = reloaded.status("single-retry")
    assert restored["progress"]["current"] == 2
    assert restored["progress"]["total"] == 2


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
