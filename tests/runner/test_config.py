from __future__ import annotations

import json
from pathlib import Path

import pytest

from mrs3.runner.config import RunnerConfig, RunnerConfigError, UnsafePathError, validate_report_directory


def test_accepts_exact_my_test_under_configured_bot_root(tmp_path: Path) -> None:
    bot = tmp_path / "hb"
    target = bot / "tester" / "report" / "my_test"

    assert validate_report_directory(target, bot) == target.resolve()


def test_runner_config_requires_tester_evidence_paths() -> None:
    with pytest.raises(TypeError, match="tester_config.*inbox_root"):
        RunnerConfig(
            Path("bot"),
            Path("bot/hb_c.exe"),
            "http://127.0.0.1:8087",
            8087,
            Path("bot/settings_strategy"),
            Path("bot/tester/report/my_test"),
            Path("bot/tester/wizard_result.json"),
            Path("bot/tester/wizard_progress.json"),
        )


@pytest.mark.parametrize("relative", [".", "tester", "tester/report", "other/my_test"])
def test_rejects_broad_or_wrong_cleanup_target(tmp_path: Path, relative: str) -> None:
    bot = tmp_path / "hb"

    with pytest.raises(UnsafePathError):
        validate_report_directory(bot / relative, bot)


def test_runner_config_resolves_paths_and_runtime_values(tmp_path: Path) -> None:
    bot = tmp_path / "hamster"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tester_runner": {
                    "bot_root": "hamster",
                    "executable": "hb_c.exe",
                    "base_url": "http://127.0.0.1:8087",
                    "port": 8087,
                    "strategy_dir": "settings_strategy",
                    "report_dir": "tester/report/my_test",
                    "wizard_result": "tester/wizard_result.json",
                    "wizard_progress": "tester/wizard_progress.json",
                    "tester_config": "tester/tester_config.json",
                    "inbox_root": "data/tester_inbox",
                    "bot_args": ["--port", "8087"],
                    "poll_interval_seconds": 0.25,
                    "max_parallel_submissions": 10,
                    "max_strategy_attempts": 4,
                    "max_bot_restarts": 30,
                    "submission_delay_seconds": 0.2,
                }
            }
        ),
        encoding="utf-8",
    )

    config = RunnerConfig.from_json(config_path)

    assert config.bot_root == bot.resolve()
    assert config.executable_path == (bot / "hb_c.exe").resolve()
    assert config.strategy_dir == (bot / "settings_strategy").resolve()
    assert config.report_dir == (bot / "tester" / "report" / "my_test").resolve()
    assert config.tester_config == (bot / "tester" / "tester_config.json").resolve()
    assert config.inbox_root == (tmp_path / "data" / "tester_inbox").resolve()
    assert config.bot_args == ("--port", "8087")
    assert config.poll_interval_seconds == pytest.approx(0.25)
    assert config.max_parallel_submissions == 10
    assert config.max_strategy_attempts == 4
    assert config.max_bot_restarts == 30
    assert config.submission_delay_seconds == pytest.approx(0.2)
    assert config.result_report_grace_seconds == pytest.approx(15)


def test_runner_config_rejects_boolean_worker_count(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tester_runner": {
        "bot_root": str(tmp_path / "hb"), "executable": "hb_c.exe",
        "base_url": "http://127.0.0.1:8087", "port": 8087,
        "strategy_dir": "settings_strategy", "report_dir": "tester/report/my_test",
        "wizard_result": "tester/wizard_result.json", "wizard_progress": "tester/wizard_progress.json",
        "tester_config": "config_tester.json", "inbox_root": "data/tester_inbox",
        "max_parallel_submissions": True,
    }}), encoding="utf-8")

    with pytest.raises(RunnerConfigError, match="max_parallel_submissions"):
        RunnerConfig.from_json(path)


@pytest.mark.parametrize(
    ("base_url", "port"),
    [
        ("https://127.0.0.1:8087", 8087),
        ("http://example.com:8087", 8087),
        ("http://127.0.0.1:8088", 8087),
    ],
)
def test_runner_config_rejects_nonlocal_or_mismatched_endpoint(
    tmp_path: Path, base_url: str, port: int
) -> None:
    raw = {
        "tester_runner": {
            "bot_root": str(tmp_path / "hb"),
            "executable": "hb_c.exe",
            "base_url": base_url,
            "port": port,
            "strategy_dir": "strategies",
            "report_dir": "tester/report/my_test",
            "wizard_result": "tester/wizard_result.json",
            "wizard_progress": "tester/wizard_progress.json",
            "tester_config": "tester/tester_config.json",
            "inbox_root": "data/tester_inbox",
        }
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RunnerConfigError):
        RunnerConfig.from_json(path)


def test_runner_config_rejects_nonstandard_strategy_directory(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "tester_runner": {
                    "bot_root": str(tmp_path / "hb"),
                    "executable": "hb_c.exe",
                    "base_url": "http://127.0.0.1:8087",
                    "port": 8087,
                    "strategy_dir": "tester",
                    "report_dir": "tester/report/my_test",
                    "wizard_result": "tester/wizard_result.json",
                    "wizard_progress": "tester/wizard_progress.json",
                    "tester_config": "tester/tester_config.json",
                    "inbox_root": "data/tester_inbox",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(UnsafePathError, match="settings_strategy"):
        RunnerConfig.from_json(path)


def test_runner_config_rejects_inbox_inside_bot_root(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"tester_runner": {
                "bot_root": str(tmp_path / "hb"),
                "executable": "hb_c.exe",
                "base_url": "http://127.0.0.1:8087",
                "port": 8087,
                "strategy_dir": "settings_strategy",
                "report_dir": "tester/report/my_test",
                "wizard_result": "tester/wizard_result.json",
                "wizard_progress": "tester/wizard_progress.json",
                "tester_config": "tester/tester_config.json",
                "inbox_root": "hb/data/tester_inbox",
            }}
        ),
        encoding="utf-8",
    )
    with pytest.raises(UnsafePathError, match="outside bot_root"):
        RunnerConfig.from_json(path)
