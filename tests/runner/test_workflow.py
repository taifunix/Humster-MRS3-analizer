from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
import os
from pathlib import Path

import httpx
import pandas as pd
import pytest

import mrs3.runner.workflow as runner_workflow
from mrs3.runner.config import RunnerConfig
from mrs3.runner.files import BatchPreparationError
from mrs3.runner.http import RowState, StrategyRow
from mrs3.runner.monitor import BatchCompletion, BatchHtmlCollision
from mrs3.runner.results import ResultMismatchError, WizardResult
from mrs3.runner.workflow import BatchPlan, WorkflowDependencies, plan_batch, run_batch


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _config(tmp_path: Path) -> RunnerConfig:
    bot = (tmp_path / "hb").resolve()
    bot.mkdir(parents=True)
    (bot / "hb_c.exe").write_bytes(b"test executable")
    tester = bot / "tester"
    tester.mkdir()
    tester_config = tester / "tester_config.json"
    tester_config.write_text(
        json.dumps(
            {"tester_config": {"MakerFee": "0.0002", "TakerFee": "0.0004", "SlippagePercent": "0.01", "FundingRate": "0.0001", "FundingIntervalHours": "8"}}
        ),
        encoding="utf-8",
    )
    return RunnerConfig(
        bot_root=bot,
        executable_path=(bot / "hb_c.exe").resolve(),
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=(bot / "settings_strategy").resolve(),
        report_dir=(bot / "tester/report/my_test").resolve(),
        wizard_result=(bot / "tester/wizard_result.json").resolve(),
        wizard_progress=(bot / "tester/wizard_progress.json").resolve(),
        tester_config=tester_config,
        inbox_root=tmp_path / "inbox",
        poll_interval_seconds=0.001,
        startup_timeout_seconds=0.1,
        batch_timeout_seconds=0.2,
        stall_timeout_seconds=0.2,
        report_stability_polls=2,
        metric_tolerance=Decimal("0.01"),
    )


def _strategy(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps({"name": name, "exchange": {"name": "Bybit"}, "settings": []}), encoding="utf-8"
    )


class BatchClient:
    def __init__(self, config: RunnerConfig, mismatch: bool = False) -> None:
        self.config = config
        self.mismatch = mismatch
        self.launched = False
        self.closed = False

    def list_strategies(self) -> tuple[StrategyRow, ...]:
        state = RowState.RESULT if self.launched else RowState.TEST
        run_id = "abc123" if self.launched else None
        return (
            StrategyRow("MEXC-futures", "ADMSTOCK_USDT", "mrs2", "2h", "A", state, None, run_id),
        )

    def launch_strategy(self, name: str) -> None:
        assert name == "A"
        self.launched = True
        self.config.report_dir.mkdir(parents=True, exist_ok=True)
        total_pnl = -6000 if self.mismatch else -6825.6651944
        html = f"""<html><body>
<pre>{{"name":"{name}","basic":{{"symbol":"ADMSTOCK_USDT","time_frame":"2h","strategy":"mrs2"}}}}</pre>
<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>
<tr><td>Initial balance</td><td>1000</td></tr><tr><td>Final balance</td><td>-5825.6651944</td></tr>
<tr><td>Total PnL</td><td>-6825.6651944</td></tr><tr><td>Total PnL, %</td><td>-682.56651944</td></tr>
<tr><td>Total Trades</td><td>2</td></tr><tr><td>Win Rate, %</td><td>50</td></tr>
<tr><td>Win Trades</td><td>1</td></tr><tr><td>Los Trades</td><td>1</td></tr>
<tr><td>Max Drawdown</td><td>6891</td></tr><tr><td>Max Drawdown, %</td><td>646.0958922978</td></tr>
<tr><td>Total fees</td><td>9.6289444</td></tr></tbody></table>
<table><thead><tr><th>Timestamp</th><th>Symbol</th><th>Action</th><th>PnL</th></tr></thead><tbody>
<tr><td>2026-07-01T00:00:00Z</td><td>ADMSTOCK_USDT</td><td>Opened</td><td>0</td></tr>
<tr><td>2026-07-02T00:00:00Z</td><td>ADMSTOCK_USDT</td><td>Closed</td><td>-6825.6651944</td></tr>
</tbody></table></body></html>"""
        (self.config.report_dir / "A.html").write_text(html, encoding="utf-8")
        self.config.wizard_result.write_text(
            json.dumps(
                [
                    {
                        "runId": "abc123",
                        "timestamp": "2026-08-08T20:47:06Z",
                        "strategies": ["A"],
                        "stats": {
                            "InitialBalance": 1000,
                            "FinalBalance": -5825.6651944,
                            "TotalPnL": total_pnl,
                            "TotalPnLPercent": -682.56651944,
                            "TotalTrades": 2,
                            "WinRate": 50,
                            "MaxDrawdown": 6891,
                            "MaxDrawdownPercent": 646.0958922978,
                            "TotalFees": 9.6289444,
                        },
                        "chartUrl": "/tester-report/my_test/A.html",
                        "period": "2026-07-01 .. 2026-08-02",
                        "elapsed": "00:00:03",
                    }
                ]
            ),
            encoding="utf-8",
        )

    def close(self) -> None:
        self.closed = True


def test_dry_run_reports_actions_without_mutating_bot_tree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "B")
    _strategy(source, "A")

    before = {
        path.relative_to(config.bot_root): path.read_bytes()
        for path in config.bot_root.rglob("*")
        if path.is_file()
    }

    plan = plan_batch(config, source)

    after = {
        path.relative_to(config.bot_root): path.read_bytes()
        for path in config.bot_root.rglob("*")
        if path.is_file()
    }

    assert plan.expected_names == ("A", "B")
    assert after == before
    assert "POST /htmx/system/shutdown" in plan.actions[0]
    assert all("/htmx/tester/run" not in action for action in plan.actions)


def test_plan_lists_only_root_level_json_to_replace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _strategy(config.strategy_dir, "OLD")
    _strategy(config.strategy_dir / "Bybit", "PROTECTED")
    source = tmp_path / "generated"
    _strategy(source, "NEW")

    plan = plan_batch(config, source)

    assert plan.root_json_to_replace == ("OLD.json",)
    assert "1 root-level strategy JSON" in plan.actions[2]
    assert "Bybit" not in "\n".join(plan.actions)


def test_batch_visibility_ignores_protected_extra_strategies(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class Client:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (
                StrategyRow("Bybit", "AAOIUSDT", "mrs2", "1h", "PROTECTED", RowState.TEST, None, None),
                StrategyRow("Bybit", "ONUSDT", "mrs3", "3h", "A", RowState.TEST, None, None),
            )

    rows = runner_workflow._wait_for_exact_batch(Client(), ("A",), config)

    assert tuple(row.name for row in rows) == ("A",)


def test_resume_visibility_accepts_tester_result_row_for_retest(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class Client:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (
                StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", "A", RowState.RESULT, None, "old"),
            )

    rows = runner_workflow._wait_for_exact_batch(
        Client(), ("A",), config, allow_result_rows=True
    )


def _wizard_results(names: tuple[str, ...]) -> tuple[WizardResult, ...]:
    return tuple(
        WizardResult(
            "run", "now", (name,), {}, "/tester-report/my_test/x.html", "x.html", "period", "0"
        )
        for name in names
    )

    assert rows[0].state is RowState.RESULT


def test_merge_wizard_results_keeps_saved_entries_when_tester_rewrites_log() -> None:
    saved = WizardResult("saved", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", "")
    current = WizardResult("current", "", ("B",), {}, "/tester-report/my_test/B.html", "B.html", "", "")

    merged = runner_workflow._merge_wizard_results((saved,), (current,))

    assert {result.strategy_names[0] for result in merged} == {"A", "B"}


def test_reconciliation_waits_for_transient_tester_log_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    plan = plan_batch(config, source)
    result = WizardResult("run", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", "")
    calls = 0

    monkeypatch.setattr(runner_workflow, "load_wizard_results", lambda _: (result,))
    def reconcile(*_: object) -> pd.DataFrame:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ResultMismatchError("tester is still updating wizard_result.json")
        return pd.DataFrame([{"strategy_name": "A"}])
    monkeypatch.setattr(runner_workflow, "reconcile_results", reconcile)

    frame = runner_workflow._wait_for_stable_reconciliation(plan, (), config)

    assert len(frame) == 1
    assert calls == config.report_stability_polls + 1


def test_all_reusable_resume_commits_without_starting_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    output = tmp_path / "results.csv"
    plan = runner_workflow.BatchPlan(
        source, ("A",), ("A.json",), (), (), (), ("A",)
    )
    monkeypatch.setattr(runner_workflow, "plan_batch", lambda *_args, **_kwargs: plan)
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_: pd.DataFrame([{"strategy_name": "A"}]),
    )
    stops: list[str] = []
    result = run_batch(
        config,
        source,
        output,
        dependencies=WorkflowDependencies(
            stop=lambda _: stops.append("stop"),
            start=lambda _: pytest.fail("bot must not be started"),
        ),
    )

    assert stops == ["stop"]

    assert result.result_rows == 1
    assert output.exists()
    assert "CSV_COMMITTED" in result.events


def test_run_rejects_output_inside_bot_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")

    with pytest.raises(ValueError, match="outside bot_root"):
        run_batch(config, source, config.bot_root / "results.csv")


def test_nested_strategy_source_fails_before_bot_stop(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = config.strategy_dir / "Bybit"
    _strategy(source, "A")
    stop_calls: list[str] = []
    dependencies = WorkflowDependencies(
        stop=lambda _: stop_calls.append("stop"),
        start=lambda _: pytest.fail("bot must not start"),
        client_factory=lambda _: pytest.fail("client must not be created"),
    )

    with pytest.raises(BatchPreparationError, match="inside strategy_dir"):
        run_batch(config, source, tmp_path / "results.csv", dependencies=dependencies)

    assert stop_calls == []


def test_missing_executable_fails_before_stop_or_file_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.executable_path.unlink()
    existing = config.report_dir / "keep.html"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep", encoding="utf-8")
    source = tmp_path / "generated"
    _strategy(source, "A")
    stop_calls: list[str] = []
    dependencies = WorkflowDependencies(
        stop=lambda _: stop_calls.append("stop"),
        start=lambda _: object(),
        client_factory=lambda _: pytest.fail("client must not be created"),
    )

    with pytest.raises(FileNotFoundError, match="hb_c.exe"):
        run_batch(
            config,
            source,
            tmp_path / "results.csv",
            dependencies=dependencies,
        )

    assert stop_calls == []
    assert existing.read_text(encoding="utf-8") == "keep"


def test_successful_batch_commits_csv_before_raw_cleanup(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    client = BatchClient(config)
    stop_observations: list[bool] = []

    def observe_stop(config: RunnerConfig) -> object:
        stop_observations.append(output.exists())
        return object()

    output = tmp_path / "out" / "results.csv"
    dependencies = WorkflowDependencies(
        stop=observe_stop,
        start=lambda config: object(),
        client_factory=lambda config: client,
    )

    result = run_batch(config, source, output, dependencies=dependencies)

    assert result.events.index("CSV_COMMITTED") < result.events.index(
        "RAW_ARTIFACTS_REMOVED"
    )
    assert output.exists()
    assert pd.read_csv(output).loc[0, "strategy_name"] == "A"
    assert not config.report_dir.exists()
    assert not config.wizard_result.exists()
    assert client.closed
    assert stop_observations == [False, False, True]
    assert result.events.index("CSV_COMMITTED") < result.events.index(
        "STOPPED_FOR_CLEANUP"
    ) < result.events.index("RAW_ARTIFACTS_REMOVED")
    progress = json.loads(result.progress_file.read_text(encoding="utf-8"))
    assert progress["expected_count"] == 1
    assert progress["completed_count"] == 1
    assert progress["result_count"] == 1


def test_batch_keeps_shared_timeframe_in_parallel_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    _strategy(source, "B")
    output = tmp_path / "out" / "results.csv"
    monitor_kwargs: dict[str, object] = {}

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return tuple(
                StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", name, RowState.TEST)
                for name in ("A", "B")
            )

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    def monitor(*_: object, **kwargs: object) -> BatchCompletion:
        monitor_kwargs.update(kwargs)
        return BatchCompletion({}, 1, 0.1)

    monkeypatch.setattr(runner_workflow, "monitor_controlled_batch", monitor)
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_: pd.DataFrame([{"strategy_name": "A"}, {"strategy_name": "B"}]),
    )
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_verified_batch_results",
        lambda names, *_: _wizard_results(names),
    )
    run_batch(
        config,
        source,
        output,
        dependencies=WorkflowDependencies(
            stop=lambda _: object(),
            start=lambda _: object(),
            client_factory=lambda _: VisibleClient(),
        ),
    )

    assert "collision_keys" not in monitor_kwargs


def test_batch_installs_strategies_in_configured_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(_config(tmp_path), strategy_batch_size=2)
    source = tmp_path / "generated"
    for name in ("A", "B", "C", "D", "E"):
        _strategy(source, name)
    observed_chunks: list[tuple[str, ...]] = []

    class ChunkClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return tuple(
                StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", path.stem, RowState.TEST)
                for path in sorted(config.strategy_dir.glob("*.json"))
            )

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    def monitor(_: object, names: tuple[str, ...], *__: object, **___: object) -> BatchCompletion:
        observed_chunks.append(names)
        return BatchCompletion({}, 1, 0.1)

    monkeypatch.setattr(runner_workflow, "monitor_controlled_batch", monitor)
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_verified_batch_results",
        lambda names, *_: _wizard_results(names),
    )
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_: pd.DataFrame([{"strategy_name": name} for name in ("A", "B", "C", "D", "E")]),
    )

    run_batch(
        config,
        source,
        tmp_path / "out" / "results.csv",
        dependencies=WorkflowDependencies(
            stop=lambda _: object(),
            start=lambda _: object(),
            client_factory=lambda _: ChunkClient(),
        ),
    )

    assert observed_chunks == [("A", "B"), ("C", "D"), ("E",)]


def test_active_output_lock_rejects_a_second_runner(tmp_path: Path) -> None:
    output = tmp_path / "results.csv"
    lock = output.with_name(f".{output.stem}.runner.lock")
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="already running"):
        runner_workflow._acquire_run_lock(output)


def test_failed_reconciliation_preserves_reports_and_logs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    client = BatchClient(config, mismatch=True)
    dependencies = WorkflowDependencies(
        stop=lambda config: object(),
        start=lambda config: object(),
        client_factory=lambda config: client,
    )
    output = tmp_path / "out" / "results.csv"

    with pytest.raises(ResultMismatchError, match="TotalPnL"):
        run_batch(config, source, output, dependencies=dependencies)

    assert config.report_dir.exists()
    assert config.wizard_result.exists()
    assert not output.exists()
    state = json.loads((output.parent / "results.state.json").read_text(encoding="utf-8"))
    assert state["state"] == "FAILED"


def test_startup_timeout_restarts_then_stops_bot(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), max_bot_restarts=1)
    source = tmp_path / "generated"
    _strategy(source, "A")
    stop_calls: list[str] = []

    class MissingBatchClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return ()

        def launch_strategy(self, name: str) -> None:
            pytest.fail("missing strategy must not be launched")

        def close(self) -> None:
            raise RuntimeError("client close failed")

    dependencies = WorkflowDependencies(
        stop=lambda config: stop_calls.append("stop"),
        start=lambda config: object(),
        client_factory=lambda config: MissingBatchClient(),
    )

    with pytest.raises(RuntimeError, match=r"1 bot restarts exhausted.*A"):
        run_batch(config, source, tmp_path / "results.csv", dependencies=dependencies)

    assert stop_calls == ["stop", "stop", "stop"]


def _tester_http_500() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://127.0.0.1:8087/htmx/tester/strategies-table")
    return httpx.HTTPStatusError(
        "tester returned 500",
        request=request,
        response=httpx.Response(500, request=request),
    )


def test_transient_http_failure_restarts_bot_and_runs_only_remaining_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    _strategy(source, "B")
    output = tmp_path / "results.csv"
    starts: list[str] = []
    stops: list[str] = []
    monitored: list[tuple[str, ...]] = []
    saved_a = WizardResult(
        "run-a", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", ""
    )

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return tuple(
                StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", name, RowState.TEST)
                for name in ("A", "B")
            )

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    def monitor(
        client: object,
        names: tuple[str, ...],
        *_: object,
        **__: object,
    ) -> BatchCompletion:
        monitored.append(names)
        if len(monitored) == 1:
            raise _tester_http_500()
        return BatchCompletion(strategies={}, polls=1, elapsed_seconds=0.1)

    monkeypatch.setattr(runner_workflow, "monitor_controlled_batch", monitor)
    monkeypatch.setattr(
        runner_workflow,
        "_validated_results_for_names",
        lambda *_: (saved_a,),
        raising=False,
    )
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_: pd.DataFrame([{"strategy_name": "A"}, {"strategy_name": "B"}]),
    )
    monkeypatch.setattr(
        runner_workflow, "_wait_for_verified_batch_results", lambda names, *_: _wizard_results(names)
    )
    dependencies = WorkflowDependencies(
        stop=lambda _: stops.append("stop"),
        start=lambda _: starts.append("start"),
        client_factory=lambda _: VisibleClient(),
    )

    result = run_batch(config, source, output, dependencies=dependencies)

    assert monitored == [("A", "B"), ("B",)]
    assert starts == ["start", "start"]
    assert stops == ["stop", "stop", "stop", "stop"]
    assert "BOT_RESTART_1" in result.events
    progress = json.loads(result.progress_file.read_text(encoding="utf-8"))
    assert progress["bot_restart_count"] == 1
    assert progress["completed_count"] == 2


def test_html_collision_retries_only_colliding_names_in_serial_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(_config(tmp_path), max_bot_restarts=1)
    source = tmp_path / "generated"
    _strategy(source, "A")
    _strategy(source, "B")
    output = tmp_path / "results.csv"
    monitored: list[tuple[str, ...]] = []
    collision_keys: list[object] = []
    saved = tuple(
        WizardResult(run_id, "", (name,), {}, f"/tester-report/my_test/{name}.html", f"{name}.html", "", "")
        for run_id, name in (("run-a", "A"), ("run-b", "B"))
    )

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return tuple(
                StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", name, RowState.TEST)
                for name in ("A", "B")
            )

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    def monitor(_: object, names: tuple[str, ...], *args: object, **kwargs: object) -> BatchCompletion:
        monitored.append(names)
        collision_keys.append(kwargs.get("collision_keys"))
        if len(monitored) == 1:
            raise BatchHtmlCollision(("A", "B"))
        return BatchCompletion({}, 1, 0.1)

    monkeypatch.setattr(runner_workflow, "monitor_controlled_batch", monitor)
    monkeypatch.setattr(runner_workflow, "_validated_results_for_names", lambda *_: saved)
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_: pd.DataFrame([{"strategy_name": "A"}, {"strategy_name": "B"}]),
    )
    monkeypatch.setattr(
        runner_workflow, "_wait_for_verified_batch_results", lambda names, *_: _wizard_results(names)
    )
    run_batch(
        config,
        source,
        output,
        dependencies=WorkflowDependencies(
            stop=lambda _: object(),
            start=lambda _: object(),
            client_factory=lambda _: VisibleClient(),
        ),
    )

    assert monitored == [("A", "B"), ("A", "B")]
    assert collision_keys[0] is None
    assert collision_keys[1] == {"A": "ONUSDT\x1f2h", "B": "ONUSDT\x1f2h"}


def test_transient_http_failure_fails_after_configured_process_restart_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(_config(tmp_path), max_bot_restarts=2)
    source = tmp_path / "generated"
    _strategy(source, "A")
    starts: list[str] = []

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (
                StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", "A", RowState.TEST),
            )

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        runner_workflow,
        "monitor_controlled_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_tester_http_500()),
    )
    monkeypatch.setattr(
        runner_workflow,
        "_validated_results_for_names",
        lambda *_: (),
        raising=False,
    )
    dependencies = WorkflowDependencies(
        stop=lambda _: None,
        start=lambda _: starts.append("start"),
        client_factory=lambda _: VisibleClient(),
    )

    with pytest.raises(RuntimeError, match=r"2 bot restarts exhausted.*A"):
        run_batch(config, source, tmp_path / "results.csv", dependencies=dependencies)

    assert starts == ["start", "start", "start"]


def test_recovery_continues_when_stopping_an_already_dead_bot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    starts: list[str] = []
    stops = 0
    monitors = 0

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", "A", RowState.TEST),)

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    def monitor(*_: object, **__: object) -> BatchCompletion:
        nonlocal monitors
        monitors += 1
        if monitors == 1:
            raise _tester_http_500()
        return BatchCompletion(strategies={}, polls=1, elapsed_seconds=0.1)

    def stop(_: RunnerConfig) -> None:
        nonlocal stops
        stops += 1
        if stops == 2:
            raise RuntimeError("bot process is already gone")

    monkeypatch.setattr(runner_workflow, "monitor_controlled_batch", monitor)
    monkeypatch.setattr(runner_workflow, "_validated_results_for_names", lambda *_: ())
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_: pd.DataFrame([{"strategy_name": "A"}]),
    )
    monkeypatch.setattr(
        runner_workflow, "_wait_for_verified_batch_results", lambda names, *_: _wizard_results(names)
    )

    result = run_batch(
        config,
        source,
        tmp_path / "results.csv",
        dependencies=WorkflowDependencies(
            stop=stop,
            start=lambda _: starts.append("start"),
            client_factory=lambda _: VisibleClient(),
        ),
    )

    assert starts == ["start", "start"]
    assert "BOT_RESTART_STOP_FAILED" in result.events


def test_transient_bot_start_failure_retries_the_remaining_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    starts = 0

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", "A", RowState.TEST),)

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    def start(_: RunnerConfig) -> None:
        nonlocal starts
        starts += 1
        if starts == 1:
            raise TimeoutError("tester did not start")

    monkeypatch.setattr(
        runner_workflow,
        "monitor_controlled_batch",
        lambda *_args, **_kwargs: BatchCompletion({}, 1, 0.1),
    )
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_: pd.DataFrame([{"strategy_name": "A"}]),
    )
    monkeypatch.setattr(
        runner_workflow, "_wait_for_verified_batch_results", lambda names, *_: _wizard_results(names)
    )

    result = run_batch(
        config,
        source,
        tmp_path / "results.csv",
        dependencies=WorkflowDependencies(
            stop=lambda _: None,
            start=start,
            client_factory=lambda _: VisibleClient(),
        ),
    )

    assert starts == 2
    assert "BOT_RESTART_1" in result.events


def test_http_400_does_not_restart_the_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    starts: list[str] = []
    request = httpx.Request("GET", "http://127.0.0.1:8087/htmx/tester/strategies-table")
    error = httpx.HTTPStatusError(
        "tester returned 400", request=request, response=httpx.Response(400, request=request)
    )

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", "A", RowState.TEST),)

        def launch_strategy(self, name: str) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        runner_workflow,
        "monitor_controlled_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(httpx.HTTPStatusError):
        run_batch(
            config,
            source,
            tmp_path / "results.csv",
            dependencies=WorkflowDependencies(
                stop=lambda _: None,
                start=lambda _: starts.append("start"),
                client_factory=lambda _: VisibleClient(),
            ),
        )

    assert starts == ["start"]


def test_plan_keeps_the_exact_validated_resume_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    output = tmp_path / "results.csv"
    inspection = runner_workflow.inspect_strategy_batch(source)
    output.with_suffix(".state.json").write_text(
        json.dumps(
            {
                "state": "FAILED",
                "expected_names": list(inspection.expected_names),
                "file_hashes": dict(inspection.file_hashes),
            }
        ),
        encoding="utf-8",
    )
    result = WizardResult("run", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", "")
    monkeypatch.setattr(runner_workflow, "_load_saved_results", lambda *_: (result,))
    monkeypatch.setattr(runner_workflow, "_validated_saved_results", lambda *_args, **_kwargs: (result,))

    plan = plan_batch(config, source, output)

    assert plan.resume_completed_names == ("A",)
    assert plan.resume_results == (result,)


def test_plan_recovers_an_interrupted_monitoring_state_without_a_live_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    output = tmp_path / "results.csv"
    inspection = runner_workflow.inspect_strategy_batch(source)
    output.with_suffix(".state.json").write_text(
        json.dumps(
            {
                "state": "MONITORING",
                "expected_names": list(inspection.expected_names),
                "file_hashes": dict(inspection.file_hashes),
            }
        ),
        encoding="utf-8",
    )
    result = WizardResult("run", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", "")
    monkeypatch.setattr(runner_workflow, "_load_saved_results", lambda *_: (result,))
    monkeypatch.setattr(runner_workflow, "_validated_saved_results", lambda *_args, **_kwargs: (result,))

    plan = plan_batch(config, source, output)

    assert plan.resume_completed_names == ("A",)


def test_plan_uses_saved_report_paths_after_archiving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    output = tmp_path / "results.csv"
    inspection = runner_workflow.inspect_strategy_batch(source)
    output.with_suffix(".state.json").write_text(
        json.dumps({"state": "FAILED", "expected_names": list(inspection.expected_names), "file_hashes": dict(inspection.file_hashes)}),
        encoding="utf-8",
    )
    saved = tmp_path / "A.html.saved"
    saved.write_text("saved", encoding="utf-8")
    output.with_name(f".{output.stem}.saved-report-paths.json").write_text(
        json.dumps({"A": str(saved)}), encoding="utf-8"
    )
    observed: dict[str, Path] = {}
    result = WizardResult("run", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", "")

    def validated(
        _: RunnerConfig,
        __: tuple[str, ...],
        candidates: tuple[WizardResult, ...],
        report_paths: dict[str, Path],
        *args: object,
        **kwargs: object,
    ) -> tuple[WizardResult, ...]:
        observed.update(report_paths)
        return (result,)

    monkeypatch.setattr(runner_workflow, "_load_saved_results", lambda *_: (result,))
    monkeypatch.setattr(runner_workflow, "_validated_saved_results", validated)

    plan = plan_batch(config, source, output)

    assert plan.resume_completed_names == ("A",)
    assert observed == {"A": saved}


def test_plan_does_not_synthesize_completion_from_html_without_wizard_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    output = tmp_path / "results.csv"
    inspection = runner_workflow.inspect_strategy_batch(source)
    output.with_suffix(".state.json").write_text(
        json.dumps(
            {
                "state": "FAILED",
                "expected_names": list(inspection.expected_names),
                "file_hashes": dict(inspection.file_hashes),
            }
        ),
        encoding="utf-8",
    )
    snapshot = tmp_path / "A__snapshot.html"
    snapshot.write_text("html only", encoding="utf-8")
    monkeypatch.setattr(runner_workflow, "_validated_results_for_names", lambda *_: ())
    monkeypatch.setattr(
        runner_workflow, "_load_snapshot_report_paths", lambda *_: {"A": snapshot}
    )

    plan = plan_batch(config, source, output)

    assert plan.resume_completed_names == ()


def test_run_stops_bot_and_publishes_progress_before_resume_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    source = tmp_path / "generated"
    _strategy(source, "A")
    output = (tmp_path / "results.csv").resolve()
    calls: list[str] = []
    light = BatchPlan(source.resolve(), ("A",), ("A.json",), (("A.json", "hash"),), (), ())
    result = WizardResult("run-a", "", ("A",), {}, "/A.html", "A.html", "", "")
    hydrated = replace(light, resume_completed_names=("A",), resume_results=(result,))

    def fake_plan(
        _: RunnerConfig,
        __: Path,
        ___: Path | None = None,
        *,
        hydrate_resume: bool = True,
    ) -> BatchPlan:
        calls.append(f"plan:{'light' if ___ is None else hydrate_resume}")
        if ___ is not None and hydrate_resume:
            assert output.with_suffix(".progress.json").is_file()
            return hydrated
        return light

    def stop(_: RunnerConfig) -> None:
        calls.append("stop")

    monkeypatch.setattr(runner_workflow, "plan_batch", fake_plan)
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_args, **_kwargs: pd.DataFrame([{"strategy_name": "A"}]),
    )

    run_batch(
        config,
        source,
        output,
        dependencies=WorkflowDependencies(stop=stop, start=lambda _: None),
    )

    assert calls[:3] == ["plan:False", "stop", "plan:True"]


def test_restart_recovers_snapshot_before_resubmitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(_config(tmp_path), max_bot_restarts=2)
    source = tmp_path / "generated"
    _strategy(source, "A")
    _strategy(source, "B")
    output = tmp_path / "results.csv"
    snapshot = tmp_path / "A.snapshot.html"
    snapshot.write_text("A", encoding="utf-8")
    snapshot_available = False
    monitored: list[tuple[str, ...]] = []
    initial_attempts: list[dict[str, int]] = []
    saved_a = WizardResult("run-a", "", ("A",), {}, "/A.html", "A.html", "", "")

    class VisibleClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return tuple(
                StrategyRow("Bybit", "ONUSDT", "mrs3", "2h", name, RowState.TEST)
                for name in ("A", "B")
            )

        def launch_strategy(self, _: str) -> None:
            pass

        def close(self) -> None:
            pass

    def monitor(
        _: object, names: tuple[str, ...], *args: object, **kwargs: object
    ) -> BatchCompletion:
        nonlocal snapshot_available
        monitored.append(names)
        initial_attempts.append(dict(kwargs.get("initial_attempt_counts", {})))
        if len(monitored) == 1:
            callback = kwargs.get("progress_callback")
            assert callable(callback)
            callback(
                {
                    "submitted_count": 2,
                    "completed_count": 0,
                    "result_count": 0,
                    "attempt_counts": {"A": 1, "B": 3},
                    "retry_count": 2,
                    "retry_reasons": {"returned_to_test": 2},
                }
            )
            snapshot_available = True
            raise _tester_http_500()
        if len(monitored) == 2:
            raise runner_workflow.BatchTimeout("tester stalled")
        return BatchCompletion({}, 1, 0.1)

    def validated(
        _: RunnerConfig,
        __: tuple[str, ...],
        report_paths: dict[str, Path] | None = None,
    ) -> tuple[WizardResult, ...]:
        return (saved_a,) if (report_paths or {}).get("A") == snapshot else ()

    monkeypatch.setattr(runner_workflow, "monitor_controlled_batch", monitor)
    monkeypatch.setattr(
        runner_workflow,
        "_load_snapshot_report_paths",
        lambda *_: {"A": snapshot} if snapshot_available else {},
    )
    monkeypatch.setattr(runner_workflow, "_validated_results_for_names", validated)
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_verified_batch_results",
        lambda names, *_args, **_kwargs: _wizard_results(names),
    )
    monkeypatch.setattr(
        runner_workflow,
        "_wait_for_stable_reconciliation",
        lambda *_args, **_kwargs: pd.DataFrame(
            [{"strategy_name": "A"}, {"strategy_name": "B"}]
        ),
    )

    result = run_batch(
        config,
        source,
        output,
        dependencies=WorkflowDependencies(
            stop=lambda _: None,
            start=lambda _: None,
            client_factory=lambda _: VisibleClient(),
        ),
    )

    assert monitored == [("A", "B"), ("B",), ("B",)]
    assert initial_attempts == [{}, {"A": 1, "B": 3}, {"A": 1, "B": 3}]
    progress = json.loads(result.progress_file.read_text(encoding="utf-8"))
    assert progress["retry_count"] == 2
    assert progress["retry_reasons"] == {"returned_to_test": 2}
    assert progress["last_restart_error"].startswith("BatchTimeout:")
    assert progress["restart_reasons"] == {"BatchTimeout": 1, "HTTPStatusError": 1}


def test_runner_has_no_report_archiver_that_renames_html() -> None:
    assert not hasattr(runner_workflow, "archive_current_reports")


def test_atomic_state_write_retries_transient_windows_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "run.state.json"
    real_replace = Path.replace
    attempts = 0

    def fail_once(source: Path, destination: Path) -> Path:
        nonlocal attempts
        if destination == target and attempts == 0:
            attempts += 1
            raise PermissionError("state file is temporarily locked")
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_once)
    monkeypatch.setattr(runner_workflow.time, "sleep", lambda _: None)

    runner_workflow._write_json_atomic(target, {"state": "CSV_COMMITTED"})

    assert attempts == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"state": "CSV_COMMITTED"}
