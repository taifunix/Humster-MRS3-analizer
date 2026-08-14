from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

import mrs3.runner.inbox as runner_inbox
from mrs3.runner.config import RunnerConfig
from mrs3.runner.inbox import InboxCaptureError, capture_verified_inbox
from mrs3.runner.results import WizardResult
from mrs3.runner.workflow import BatchPlan


def _config(tmp_path: Path, *, complete: bool = True) -> RunnerConfig:
    bot = tmp_path / "bot"
    tester = bot / "tester"
    tester.mkdir(parents=True)
    tester_config = tester / "tester_config.json"
    commission = {
        "MakerFee": "0.0002",
        "TakerFee": "0.0004",
        "SlippagePercent": "0.01",
        "FundingRate": "0.0001",
        "FundingIntervalHours": "8",
    }
    if not complete:
        commission.pop("MakerFee")
    tester_config.write_text(json.dumps({"tester_config": commission}), encoding="utf-8")
    return RunnerConfig(
        bot_root=bot,
        executable_path=bot / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=bot / "settings_strategy",
        report_dir=tester / "report" / "my_test",
        wizard_result=tester / "wizard_result.json",
        wizard_progress=tester / "wizard_progress.json",
        tester_config=tester_config,
        inbox_root=tmp_path / "data" / "tester_inbox",
    )


def _inputs(tmp_path: Path, config: RunnerConfig) -> tuple[Path, BatchPlan, WizardResult, Path]:
    source = tmp_path / "strategies"
    source.mkdir()
    (source / "A.json").write_text(
        json.dumps({"name": "A", "exchange": {"name": "Bybit"}, "settings": []}),
        encoding="utf-8",
    )
    report = tmp_path / "A.html"
    report.write_bytes(b'<pre>{"name":"A","exchange":{"name":"Bybit"}}</pre>\n')
    plan = BatchPlan(source, ("A",), ("A.json",), (("A.json", "hash"),), (), ())
    wizard = WizardResult("run-1", "now", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "period", "0")
    return tmp_path / "results.csv", plan, wizard, report


def test_capture_copies_exact_html_strategy_and_fee_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commission_contract"]["MakerFee"] == "0.0002"
    entry = manifest["entries"][0]
    assert (inbox / entry["report_path"]).read_bytes() == report.read_bytes()
    assert entry["strategy_name"] == "A"
    assert entry["exchange_name"] == "Bybit"
    assert (inbox / entry["strategy_path"]).read_bytes() == b'{"exchange":{"name":"Bybit"},"name":"A","settings":[]}'


def test_capture_rejects_missing_maker_fee(tmp_path: Path) -> None:
    config = _config(tmp_path, complete=False)
    output, plan, wizard, report = _inputs(tmp_path, config)

    with pytest.raises(InboxCaptureError, match="MakerFee"):
        capture_verified_inbox(config, output, plan, (wizard,), {"A": report})


def test_capture_rejects_duplicate_verified_results(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)

    with pytest.raises(InboxCaptureError, match="duplicate"):
        capture_verified_inbox(config, output, plan, (wizard, wizard), {"A": report})


def test_capture_manifest_hashes_destination_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    atomic_copy = runner_inbox._atomic_bytes

    def corrupt_copy(target: Path, data: bytes) -> bytes:
        if target.name == "inbox_manifest.json":
            return atomic_copy(target, data)
        copied = b"corrupted:" + data
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(copied)
        return copied

    monkeypatch.setattr(runner_inbox, "_atomic_bytes", corrupt_copy)

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert entry["source_strategy_sha256"] == sha256(
        (inbox / entry["strategy_path"]).read_bytes()
    ).hexdigest()
    assert entry["source_report_sha256"] == sha256(
        (inbox / entry["report_path"]).read_bytes()
    ).hexdigest()


def test_capture_rejects_blank_exchange_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    (plan.strategy_source / "A.json").write_text(
        json.dumps({"name": "A", "exchange": {"name": "  "}, "settings": []}),
        encoding="utf-8",
    )

    with pytest.raises(InboxCaptureError, match="exchange.name"):
        capture_verified_inbox(config, output, plan, (wizard,), {"A": report})
