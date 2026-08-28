from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

import mrs3.runner.monitor as runner_monitor
from mrs3.runner.config import RunnerConfig
from mrs3.runner.http import RowState, StrategyRow
from mrs3.runner.monitor import (
    BatchHtmlCollision,
    BatchRetryExhausted,
    BatchTimeout,
    monitor_controlled_batch,
    monitor_batch,
)


@pytest.fixture(autouse=True)
def _accept_fixture_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_monitor, "_report_matches_strategy", lambda *_: True)


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
        tester_config=(bot / "tester/tester_config.json").resolve(),
        inbox_root=(tmp_path / "tester_inbox").resolve(),
        poll_interval_seconds=0.001,
        batch_timeout_seconds=0.2,
        stall_timeout_seconds=0.2,
        submission_delay_seconds=0.001,
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


class ControlledClient:
    def __init__(self, config: RunnerConfig, names: tuple[str, ...]) -> None:
        self.config = config
        self.names = names
        self.launches: list[str] = []
        self._states = {name: RowState.TEST for name in names}
        self._run_ids: dict[str, str] = {}

    def list_strategies(self) -> tuple[StrategyRow, ...]:
        return tuple(
            StrategyRow(
                "Bybit", "ONUSDT", "mrs3", "2h", name, self._states[name], None,
                self._run_ids.get(name),
            )
            for name in self.names
        )

    def launch_strategy(self, name: str) -> None:
        self.launches.append(name)
        run_id = f"run-{len(self.launches)}"
        self._states[name] = RowState.RESULT
        self._run_ids[name] = run_id
        self.config.report_dir.mkdir(parents=True, exist_ok=True)
        (self.config.report_dir / f"{name}.html").write_text("complete", encoding="utf-8")
        entries = []
        if self.config.wizard_result.exists():
            entries = json.loads(self.config.wizard_result.read_text(encoding="utf-8"))
        entries.append({"runId": run_id, "strategies": [name], "chartUrl": f"/tester-report/my_test/{name}.html"})
        self.config.wizard_result.write_text(json.dumps(entries), encoding="utf-8")


def test_controlled_monitor_limits_initial_window_and_refills_after_verified_result(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), max_parallel_submissions=2)
    client = ControlledClient(config, ("A", "B", "C"))

    completion = monitor_controlled_batch(
        client, ("A", "B", "C"), config.wizard_result, config.report_dir, config
    )

    assert client.launches[:2] == ["A", "B"]
    assert client.launches == ["A", "B", "C"]
    assert all(item.completed for item in completion.strategies.values())


def test_controlled_monitor_can_finish_partially_and_continue_after_exhaustion(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        max_parallel_submissions=2,
        max_strategy_attempts=4,
        result_report_grace_seconds=0.002,
        batch_timeout_seconds=1.0,
        stall_timeout_seconds=1.0,
    )

    class MissingClient(ControlledClient):
        def launch_strategy(self, name: str) -> None:
            if name != "MISSING":
                super().launch_strategy(name)
                return
            self.launches.append(name)
            run_id = f"run-{len(self.launches)}"
            self._states[name] = RowState.RESULT
            self._run_ids[name] = run_id
            self.config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
            entries = json.loads(self.config.wizard_result.read_text(encoding="utf-8")) if self.config.wizard_result.exists() else []
            entries.append({"runId": run_id, "strategies": [name], "chartUrl": f"/tester-report/my_test/{name}.html"})
            self.config.wizard_result.write_text(json.dumps(entries), encoding="utf-8")

    client = MissingClient(config, ("MISSING", "GOOD"))
    completion = monitor_controlled_batch(
        client,
        ("MISSING", "GOOD"),
        config.wizard_result,
        config.report_dir,
        config,
        allow_partial=True,
    )

    assert completion.failed_names == ("MISSING",)
    assert completion.strategies["MISSING"].attempts == 4
    assert completion.strategies["GOOD"].completed is True
    assert client.launches.count("GOOD") == 1


def test_controlled_monitor_serializes_names_with_same_html_collision_key(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), max_parallel_submissions=2)
    client = ControlledClient(config, ("A", "B", "C"))

    monitor_controlled_batch(
        client,
        ("A", "B", "C"),
        config.wizard_result,
        config.report_dir,
        config,
        collision_keys={"A": "ONUSDT|15m", "B": "ONUSDT|15m", "C": "ONUSDT|1h"},
    )

    assert client.launches[:2] == ["A", "C"]
    assert client.launches == ["A", "C", "B"]


def test_controlled_monitor_keeps_html_lane_during_retry(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), max_parallel_submissions=2)

    class RetryLaneClient(ControlledClient):
        def __init__(self) -> None:
            super().__init__(config, ("A", "B", "C"))
            self.a_polls = 0

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            if self.launches == ["A", "C"]:
                self.a_polls += 1
                self._states["A"] = (
                    RowState.RUNNING if self.a_polls == 1 else RowState.TEST
                )
            return super().list_strategies()

        def launch_strategy(self, name: str) -> None:
            if name == "A" and not self.launches:
                self.launches.append(name)
                return
            super().launch_strategy(name)

    client = RetryLaneClient()
    monitor_controlled_batch(
        client,
        ("A", "B", "C"),
        config.wizard_result,
        config.report_dir,
        config,
        collision_keys={"A": "ONUSDT|15m", "B": "ONUSDT|15m", "C": "ONUSDT|1h"},
    )

    assert client.launches == ["A", "C", "A", "B"]


def test_controlled_monitor_rejects_shared_chart_url(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), max_parallel_submissions=2)

    class CollidingClient(ControlledClient):
        def launch_strategy(self, name: str) -> None:
            self.launches.append(name)
            run_id = f"run-{len(self.launches)}"
            self._states[name] = RowState.RESULT
            self._run_ids[name] = run_id
            self.config.report_dir.mkdir(parents=True, exist_ok=True)
            (self.config.report_dir / "shared.html").write_text("complete", encoding="utf-8")
            entries = []
            if self.config.wizard_result.exists():
                entries = json.loads(self.config.wizard_result.read_text(encoding="utf-8"))
            entries.append(
                {
                    "runId": run_id,
                    "strategies": [name],
                    "chartUrl": "/tester-report/my_test/shared.html",
                }
            )
            self.config.wizard_result.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(BatchHtmlCollision, match="A, B"):
        monitor_controlled_batch(
            CollidingClient(config, ("A", "B")),
            ("A", "B"),
            config.wizard_result,
            config.report_dir,
            config,
        )


def test_snapshot_collector_preserves_each_strategy_from_a_shared_html_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    shared = report_dir / "shared.html"
    monkeypatch.setattr(
        runner_monitor, "_report_strategy_name", lambda path: path.read_text(encoding="utf-8").strip()
    )
    collector = runner_monitor._ReportSnapshotCollector(
        ("A", "B"), report_dir, tmp_path / "snapshots"
    )

    shared.write_text("A", encoding="utf-8")
    collector.capture_once()
    collector.capture_once()
    shared.write_text("B ", encoding="utf-8")
    collector.capture_once()
    collector.capture_once()

    assert collector.snapshot_for("A").read_text(encoding="utf-8") == "A"
    assert collector.snapshot_for("B").read_text(encoding="utf-8") == "B "


def test_snapshot_collector_survives_a_partially_written_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    report = report_dir / "shared.html"
    calls = 0

    def extract_name(_: Path) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return "A"

    monkeypatch.setattr(runner_monitor, "extract_html_strategy_name", extract_name)
    collector = runner_monitor._ReportSnapshotCollector(
        ("A",), report_dir, tmp_path / "snapshots"
    )

    report.write_text("complete", encoding="utf-8")
    collector.capture_once()
    collector.capture_once()
    collector.capture_once()

    assert collector.snapshot_for("A").read_text(encoding="utf-8") == "complete"


def test_snapshot_collector_removes_stable_source_after_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    report = report_dir / "stable.html"
    monkeypatch.setattr(
        runner_monitor, "_report_strategy_name", lambda path: path.read_text(encoding="utf-8")
    )
    collector = runner_monitor._ReportSnapshotCollector(
        ("A",), report_dir, tmp_path / "snapshots", remove_source_reports=True
    )

    report.write_text("A", encoding="utf-8")
    collector.capture_once()
    collector.capture_once()

    assert not report.exists()
    assert collector.snapshot_for("A") is not None


def test_snapshot_collector_ignores_reports_older_than_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    old = report_dir / "old.html"
    old.write_text("A", encoding="utf-8")
    monkeypatch.setattr(
        runner_monitor, "_report_strategy_name", lambda path: path.read_text(encoding="utf-8").strip()
    )
    collector = runner_monitor._ReportSnapshotCollector(("A",), report_dir, tmp_path / "snapshots")

    collector.capture_once()
    collector.capture_once()

    assert collector.snapshot_for("A") is None


def test_controlled_monitor_closes_snapshot_collector_after_client_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    lifecycle: list[str] = []

    class Collector:
        def __init__(self, *_: object) -> None:
            pass

        def start(self) -> None:
            lifecycle.append("start")

        def close(self) -> None:
            lifecycle.append("close")

        def snapshot_for(self, _: str) -> Path | None:
            return None

        def discard(self, _: str) -> None:
            pass

    class BrokenClient:
        def list_strategies(self) -> tuple[StrategyRow, ...]:
            raise OSError("tester connection was lost")

        def launch_strategy(self, _: str) -> None:
            raise OSError("tester connection was lost")

    monkeypatch.setattr(runner_monitor, "_ReportSnapshotCollector", Collector)

    with pytest.raises(OSError, match="connection was lost"):
        monitor_controlled_batch(
            BrokenClient(),
            ("A",),
            config.wizard_result,
            config.report_dir,
            config,
            snapshot_report_dir=tmp_path / "snapshots",
        )

    assert lifecycle == ["start", "close"]


def test_controlled_monitor_rejects_stale_result_from_before_launch(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        max_strategy_attempts=1,
        batch_timeout_seconds=0.03,
        stall_timeout_seconds=0.03,
    )
    config.report_dir.mkdir(parents=True)
    (config.report_dir / "old.html").write_text("old", encoding="utf-8")
    config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
    config.wizard_result.write_text(
        json.dumps(
            [
                {
                    "runId": "old-run",
                    "strategies": ["A"],
                    "chartUrl": "/tester-report/my_test/old.html",
                }
            ]
        ),
        encoding="utf-8",
    )

    class StaleClient:
        launches = 0

        def launch_strategy(self, _: str) -> None:
            self.launches += 1

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (_row(RowState.RESULT, run_id="old-run"),)

    client = StaleClient()
    with pytest.raises(BatchTimeout):
        monitor_controlled_batch(
            client,
            ("A",),
            config.wizard_result,
            config.report_dir,
            config,
        )

    assert client.launches == 1


def test_controlled_monitor_baselines_stale_row_even_if_wizard_entry_appears_late(
    tmp_path: Path,
) -> None:
    config = replace(
        _config(tmp_path),
        max_strategy_attempts=1,
        batch_timeout_seconds=0.03,
        stall_timeout_seconds=0.03,
    )
    config.report_dir.mkdir(parents=True)
    (config.report_dir / "old.html").write_text("old", encoding="utf-8")

    class LateWizardClient:
        launches = 0

        def launch_strategy(self, _: str) -> None:
            self.launches += 1
            config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
            config.wizard_result.write_text(
                json.dumps(
                    [
                        {
                            "runId": "old-run",
                            "strategies": ["A"],
                            "chartUrl": "/tester-report/my_test/old.html",
                        }
                    ]
                ),
                encoding="utf-8",
            )

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (_row(RowState.RESULT, run_id="old-run"),)

    client = LateWizardClient()
    with pytest.raises(BatchTimeout):
        monitor_controlled_batch(
            client,
            ("A",),
            config.wizard_result,
            config.report_dir,
            config,
        )

    assert client.launches == 1


def test_controlled_monitor_accepts_fresh_snapshot_with_reused_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(_config(tmp_path), report_stability_polls=1)
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / "fresh.html"
    report.write_text("fresh", encoding="utf-8")
    config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
    config.wizard_result.write_text(
        json.dumps(
            [{"runId": "reused", "strategies": ["A"], "chartUrl": "/tester-report/my_test/old.html"}]
        ),
        encoding="utf-8",
    )

    class FreshCollector:
        def __init__(self, *_args: object) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def discard(self, _: str) -> None:
            pass

        def snapshot_for(self, _: str) -> Path:
            return report

    class ReusedResultClient:
        def launch_strategy(self, _: str) -> None:
            pass

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (_row(RowState.RESULT, run_id="reused"),)

    monkeypatch.setattr(runner_monitor, "_ReportSnapshotCollector", FreshCollector)

    completion = monitor_controlled_batch(
        ReusedResultClient(),
        ("A",),
        config.wizard_result,
        config.report_dir,
        config,
        snapshot_report_dir=tmp_path / "snapshots",
    )

    assert completion.strategies["A"].completed
    assert completion.strategies["A"].report_path == report


def test_controlled_monitor_enforces_attempt_budget_from_previous_restart(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), max_strategy_attempts=4)

    class Client:
        launches = 0

        def launch_strategy(self, _: str) -> None:
            self.launches += 1

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            return (_row(RowState.TEST),)

    client = Client()
    with pytest.raises(BatchRetryExhausted, match="A"):
        monitor_controlled_batch(
            client,
            ("A",),
            config.wizard_result,
            config.report_dir,
            config,
            initial_attempt_counts={"A": 4},
        )

    assert client.launches == 0


def test_controlled_monitor_preserves_colliding_reports_during_serial_repair(
    tmp_path: Path,
) -> None:
    config = replace(_config(tmp_path), max_parallel_submissions=2)

    class CollidingClient(ControlledClient):
        def launch_strategy(self, name: str) -> None:
            self.launches.append(name)
            run_id = f"run-{len(self.launches)}"
            self._states[name] = RowState.RESULT
            self._run_ids[name] = run_id
            self.config.report_dir.mkdir(parents=True, exist_ok=True)
            (self.config.report_dir / "shared.html").write_text(name, encoding="utf-8")
            entries = []
            if self.config.wizard_result.exists():
                entries = json.loads(self.config.wizard_result.read_text(encoding="utf-8"))
            entries.append({"runId": run_id, "strategies": [name], "chartUrl": "/tester-report/my_test/shared.html"})
            self.config.wizard_result.write_text(json.dumps(entries), encoding="utf-8")

    client = CollidingClient(config, ("A", "B"))
    completion = monitor_controlled_batch(
        client,
        ("A", "B"),
        config.wizard_result,
        config.report_dir,
        config,
        collision_keys={"A": "ONUSDT|15m", "B": "ONUSDT|15m"},
        verified_report_dir=tmp_path / "verified",
    )

    assert {item.report_path.read_text(encoding="utf-8") for item in completion.strategies.values()} == {"A", "B"}


def test_controlled_monitor_spaces_each_tester_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(
        _config(tmp_path), max_parallel_submissions=2, submission_delay_seconds=0.2
    )
    pauses: list[float] = []
    monkeypatch.setattr(runner_monitor.time, "sleep", pauses.append)

    monitor_controlled_batch(
        ControlledClient(config, ("A", "B", "C")),
        ("A", "B", "C"),
        config.wizard_result,
        config.report_dir,
        config,
    )

    assert pauses.count(0.2) == 2


def test_controlled_monitor_retries_strategy_returned_to_test(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class RetryClient(ControlledClient):
        def __init__(self) -> None:
            super().__init__(config, ("A",))
            self.polls_after_first_launch = 0

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            if self.launches == ["A"]:
                self.polls_after_first_launch += 1
                self._states["A"] = (
                    RowState.RUNNING if self.polls_after_first_launch == 1 else RowState.TEST
                )
            return super().list_strategies()

        def launch_strategy(self, name: str) -> None:
            if self.launches:
                super().launch_strategy(name)
                return
            self.launches.append(name)

    client = RetryClient()
    completion = monitor_controlled_batch(
        client, ("A",), config.wizard_result, config.report_dir, config
    )

    assert client.launches == ["A", "A"]
    assert completion.strategies["A"].completed


def test_controlled_monitor_retries_test_row_that_never_started(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), result_report_grace_seconds=0.001)

    class NeverStartedClient(ControlledClient):
        def launch_strategy(self, name: str) -> None:
            if not self.launches:
                self.launches.append(name)
                return
            super().launch_strategy(name)

    snapshots: list[dict[str, object]] = []
    client = NeverStartedClient(config, ("A",))
    completion = monitor_controlled_batch(
        client,
        ("A",),
        config.wizard_result,
        config.report_dir,
        config,
        progress_callback=snapshots.append,
    )

    assert client.launches == ["A", "A"]
    assert completion.strategies["A"].completed
    assert snapshots[-1]["retry_reasons"] == {"test_after_launch_grace": 1}


def test_controlled_monitor_retries_result_without_report_html(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), result_report_grace_seconds=0.001)

    class MissingReportClient(ControlledClient):
        def launch_strategy(self, name: str) -> None:
            if self.launches:
                super().launch_strategy(name)
                return
            self.launches.append(name)
            self._states[name] = RowState.RESULT
            self._run_ids[name] = "lost-report"
            self.config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
            self.config.wizard_result.write_text(
                json.dumps(
                    [{
                        "runId": "lost-report",
                        "strategies": [name],
                        "chartUrl": f"/tester-report/my_test/{name}.html",
                    }]
                ),
                encoding="utf-8",
            )

    client = MissingReportClient(config, ("A",))
    completion = monitor_controlled_batch(
        client, ("A",), config.wizard_result, config.report_dir, config
    )

    assert client.launches == ["A", "A"]
    assert completion.strategies["A"].completed


def test_controlled_monitor_waits_for_delayed_report_before_retrying(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), result_report_grace_seconds=0.1)

    class DelayedReportClient(ControlledClient):
        def __init__(self) -> None:
            super().__init__(config, ("A",))
            self.result_polls = 0

        def launch_strategy(self, name: str) -> None:
            self.launches.append(name)
            self._states[name] = RowState.RESULT
            self._run_ids[name] = "delayed-report"
            self.config.wizard_result.parent.mkdir(parents=True, exist_ok=True)
            self.config.wizard_result.write_text(
                json.dumps([{
                    "runId": "delayed-report",
                    "strategies": [name],
                    "chartUrl": f"/tester-report/my_test/{name}.html",
                }]),
                encoding="utf-8",
            )

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            self.result_polls += 1
            if self.result_polls == 3:
                self.config.report_dir.mkdir(parents=True, exist_ok=True)
                (self.config.report_dir / "A.html").write_text("A", encoding="utf-8")
            return super().list_strategies()

    client = DelayedReportClient()
    completion = monitor_controlled_batch(
        client, ("A",), config.wizard_result, config.report_dir, config
    )

    assert client.launches == ["A"]
    assert completion.strategies["A"].completed


def test_controlled_monitor_retries_html_that_belongs_to_another_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(_config(tmp_path), result_report_grace_seconds=0.001)
    client = ControlledClient(config, ("A",))
    monkeypatch.setattr(
        runner_monitor, "_report_matches_strategy", lambda *_: len(client.launches) > 1
    )

    completion = monitor_controlled_batch(
        client, ("A",), config.wizard_result, config.report_dir, config
    )

    assert client.launches == ["A", "A"]
    assert completion.strategies["A"].completed


def test_controlled_monitor_fails_after_maximum_retries(tmp_path: Path) -> None:
    config = replace(_config(tmp_path), max_strategy_attempts=2)

    class ExhaustedClient(ControlledClient):
        def __init__(self) -> None:
            super().__init__(config, ("A",))
            self.poll = 0

        def list_strategies(self) -> tuple[StrategyRow, ...]:
            if self.launches:
                self.poll += 1
                self._states["A"] = RowState.RUNNING if self.poll % 2 else RowState.TEST
            return super().list_strategies()

        def launch_strategy(self, name: str) -> None:
            self.launches.append(name)

    with pytest.raises(BatchRetryExhausted, match="A"):
        monitor_controlled_batch(
            ExhaustedClient(), ("A",), config.wizard_result, config.report_dir, config
        )


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
