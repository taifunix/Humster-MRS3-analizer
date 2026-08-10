from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import pytest

import mrs3.runner.workflow as runner_workflow
from mrs3.runner.config import RunnerConfig
from mrs3.runner.files import BatchPreparationError
from mrs3.runner.http import RowState, StrategyRow
from mrs3.runner.results import ResultMismatchError
from mrs3.runner.workflow import WorkflowDependencies, plan_batch, run_batch


FIXTURES = Path(__file__).parents[1] / "fixtures"
REPORT_FIXTURE = FIXTURES / "my_test_run_001_of_001_ADMSTOCK_USDT_2h_2026-07-01_3.html"


def _config(tmp_path: Path) -> RunnerConfig:
    bot = (tmp_path / "hb").resolve()
    bot.mkdir(parents=True)
    (bot / "hb_c.exe").write_bytes(b"test executable")
    return RunnerConfig(
        bot_root=bot,
        executable_path=(bot / "hb_c.exe").resolve(),
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=(bot / "settings_strategy").resolve(),
        report_dir=(bot / "tester/report/my_test").resolve(),
        wizard_result=(bot / "tester/wizard_result.json").resolve(),
        wizard_progress=(bot / "tester/wizard_progress.json").resolve(),
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
        json.dumps({"name": name, "settings": []}), encoding="utf-8"
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
        html = REPORT_FIXTURE.read_text(encoding="utf-8").replace('"ADM1"', '"A"')
        (self.config.report_dir / "A.html").write_text(html, encoding="utf-8")
        total_pnl = -6000 if self.mismatch else -6825.6651944
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
    assert stop_observations == [False, True]
    assert result.events.index("CSV_COMMITTED") < result.events.index(
        "STOPPED_FOR_CLEANUP"
    ) < result.events.index("RAW_ARTIFACTS_REMOVED")
    progress = json.loads(result.progress_file.read_text(encoding="utf-8"))
    assert progress["expected_count"] == 1
    assert progress["completed_count"] == 1
    assert progress["result_count"] == 1


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
