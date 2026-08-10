from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from mrs3.runner.config import RunnerConfig
from mrs3.runner.http import RowState, StrategyRow
from mrs3.runner.monitor import BatchTimeout, monitor_batch


def _config(tmp_path: Path) -> RunnerConfig:
    bot = (tmp_path / "hb").resolve()
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
        batch_timeout_seconds=0.2,
        stall_timeout_seconds=0.2,
        report_stability_polls=2,
        metric_tolerance=Decimal("0.01"),
    )


def _row(
    state: RowState, percent: float | None = None, run_id: str | None = None
) -> StrategyRow:
    return StrategyRow("Bybit", "A_USDT", "mrs3", "15m", "A", state, percent, run_id)


class ScriptedClient:
    def __init__(self, tables: list[tuple[StrategyRow, ...]], on_poll=None) -> None:
        self.tables = tables
        self.polls = 0
        self.on_poll = on_poll

    def list_strategies(self) -> tuple[StrategyRow, ...]:
        index = min(self.polls, len(self.tables) - 1)
        self.polls += 1
        if self.on_poll:
            self.on_poll(self.polls)
        return self.tables[index]


def test_monitor_tracks_test_to_percent_to_stable_result(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def publish(poll: int) -> None:
        if poll != 4:
            return
        config.report_dir.mkdir(parents=True)
        (config.report_dir / "A.html").write_text("complete report", encoding="utf-8")
        config.wizard_result.write_text(
            json.dumps(
                [
                    {
                        "runId": "abc123",
                        "strategies": ["A"],
                        "chartUrl": "/tester-report/my_test/A.html",
                        "stats": {},
                    }
                ]
            ),
            encoding="utf-8",
        )

    client = ScriptedClient(
        [
            (_row(RowState.TEST),),
            (_row(RowState.RUNNING, 0),),
            (_row(RowState.RUNNING, 62),),
            (_row(RowState.RESULT, run_id="abc123"),),
            (_row(RowState.RESULT, run_id="abc123"),),
        ],
        publish,
    )

    completion = monitor_batch(
        client, ("A",), config.wizard_result, config.report_dir, config
    )

    progress = completion.strategies["A"]
    assert progress.percent_history == (0, 62)
    assert progress.completed
    assert progress.run_id == "abc123"
    assert progress.report_path == config.report_dir / "A.html"


def test_monitor_publishes_compact_progress_snapshots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snapshots: list[dict[str, object]] = []

    def publish(poll: int) -> None:
        if poll != 2:
            return
        config.report_dir.mkdir(parents=True)
        (config.report_dir / "A.html").write_text("complete", encoding="utf-8")
        config.wizard_result.write_text(
            json.dumps(
                [
                    {
                        "runId": "run-a",
                        "strategies": ["A"],
                        "chartUrl": "/tester-report/my_test/A.html",
                        "stats": {},
                    }
                ]
            ),
            encoding="utf-8",
        )

    client = ScriptedClient(
        [
            (_row(RowState.RUNNING, 35),),
            (_row(RowState.RESULT, run_id="run-a"),),
            (_row(RowState.RESULT, run_id="run-a"),),
        ],
        publish,
    )

    monitor_batch(
        client,
        ("A",),
        config.wizard_result,
        config.report_dir,
        config,
        progress_callback=snapshots.append,
    )

    assert snapshots[0]["expected_count"] == 1
    assert snapshots[0]["running_count"] == 1
    assert snapshots[0]["active"][0] == {
        "name": "A",
        "state": "RUNNING",
        "percent": 35,
    }
    assert snapshots[-1]["completed_count"] == 1
    assert snapshots[-1]["result_count"] == 1
    assert snapshots[-1]["active"] == []


def test_result_button_without_matching_json_and_stable_html_is_not_complete(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path), batch_timeout_seconds=0.01, stall_timeout_seconds=0.01
    )
    client = ScriptedClient([(_row(RowState.RESULT, run_id="abc123"),)])

    with pytest.raises(BatchTimeout):
        monitor_batch(client, ("A",), config.wizard_result, config.report_dir, config)
