from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_config_examples_union_runner_tuning_and_performance_capture_paths() -> None:
    required = {
        "max_parallel_submissions",
        "strategy_batch_size",
        "max_strategy_attempts",
        "max_bot_restarts",
        "submission_delay_seconds",
        "result_report_grace_seconds",
        "tester_config",
        "inbox_root",
    }

    for filename in ("config.example.json", "config.local.json.example"):
        document = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        assert required <= document["tester_runner"].keys()
        assert document["tester_runner"]["inbox_root"] == "data/tester_inbox"
