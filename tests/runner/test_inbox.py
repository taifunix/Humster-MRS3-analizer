from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from mrs3.runner.config import RunnerConfig
from mrs3.runner.inbox import InboxCaptureError, capture_verified_inbox
from mrs3.runner.results import WizardResult
from mrs3.runner.workflow import BatchPlan
from mrs3.panel import PanelController


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
    report.write_bytes(
        b'<pre>{"name":"A","basic":{"symbol":"ONUSDT"},"exchange":{"name":"Bybit"}}</pre>\n'
    )
    plan = BatchPlan(source, ("A",), ("A.json",), (("A.json", "hash"),), (), ())
    wizard = WizardResult("run-1", "now", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "period", "0")
    return tmp_path / "results.csv", plan, wizard, report


def test_capture_keeps_source_paths_and_panel_validates_direct_inbox(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commission_contract"]["MakerFee"] == "0.0002"
    entry = manifest["entries"][0]
    assert Path(entry["report_path"]).resolve() == report.resolve()
    assert entry["strategy_name"] == "A"
    assert entry["exchange_name"] == "Bybit"
    assert Path(entry["strategy_path"]).resolve() == (plan.strategy_source / "A.json").resolve()
    PanelController._validate_performance_inbox(inbox)


def test_capture_uses_installed_strategy_when_generated_file_is_gone(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    config.strategy_dir.mkdir(parents=True)
    installed = config.strategy_dir / "A.json"
    installed.write_bytes((plan.strategy_source / "A.json").read_bytes())
    (plan.strategy_source / "A.json").unlink()

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    entry = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))["entries"][0]
    assert Path(entry["strategy_path"]).resolve() == installed.resolve()
    PanelController._validate_performance_inbox(inbox)


def test_capture_accepts_html_escaped_strategy_settings_pre(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    report.write_text(
        '<html><body><pre>{&quot;name&quot;:&quot;A&quot;,&quot;basic&quot;:{&quot;symbol&quot;:&quot;ONUSDT&quot;}}</pre></body></html>',
        encoding="utf-8",
    )

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    entry = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))["entries"][0]
    assert Path(entry["report_path"]).read_text(encoding="utf-8") == report.read_text(
        encoding="utf-8"
    )


def test_capture_accepts_flat_tester_config_contract(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.tester_config.write_text(
        json.dumps(
            {
                "MakerFee": "0.0002",
                "TakerFee": "0.0004",
                "SlippagePercent": "0.01",
                "FundingRate": "0.0001",
                "FundingIntervalHours": "8",
            }
        ),
        encoding="utf-8",
    )
    output, plan, wizard, report = _inputs(tmp_path, config)

    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commission_contract"]["TakerFee"] == "0.0004"


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


def test_capture_manifest_hashes_source_bytes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report})

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert entry["source_strategy_sha256"] == sha256(
        Path(entry["strategy_path"]).read_bytes()
    ).hexdigest()
    assert entry["source_report_sha256"] == sha256(
        Path(entry["report_path"]).read_bytes()
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


def test_capture_uses_supplied_immutable_tester_config_snapshot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    snapshot = config.tester_config.read_bytes()
    config.tester_config.write_text(json.dumps({"tester_config": {}}), encoding="utf-8")

    inbox = capture_verified_inbox(
        config, output, plan, (wizard,), {"A": report}, tester_config_bytes=snapshot
    )

    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["tester_config_sha256"] == sha256(snapshot).hexdigest()


def test_capture_persists_v6_provenance(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    provenance = {
        "analysis_run_id": "run-v6",
        "generation_manifest_sha256": "a" * 64,
        "strategy_json_sha256": {"A.json": "b" * 64},
    }
    inbox = capture_verified_inbox(config, output, plan, (wizard,), {"A": report}, provenance=provenance)
    manifest = json.loads((inbox / "inbox_manifest.json").read_text(encoding="utf-8"))
    assert manifest["v6_provenance"] == provenance


def test_capture_rejects_incomplete_v6_provenance(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    with pytest.raises(InboxCaptureError, match="provenance is incomplete"):
        capture_verified_inbox(
            config, output, plan, (wizard,), {"A": report},
            provenance={"analysis_run_id": "run-v6"},
        )


def test_capture_rejects_v6_provenance_hashes_not_covering_batch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    with pytest.raises(InboxCaptureError, match="strategy_json_sha256"):
        capture_verified_inbox(
            config, output, plan, (wizard,), {"A": report},
            provenance={
                "analysis_run_id": "run-v6",
                "generation_manifest_sha256": "a" * 64,
                "strategy_json_sha256": "not-a-map",
            },
        )


def test_capture_rejects_malformed_v6_generation_hash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output, plan, wizard, report = _inputs(tmp_path, config)
    with pytest.raises(InboxCaptureError, match="generation_manifest_sha256"):
        capture_verified_inbox(
            config, output, plan, (wizard,), {"A": report},
            provenance={
                "analysis_run_id": "run-v6",
                "generation_manifest_sha256": "short",
                "strategy_json_sha256": {"A.json": "b" * 64},
            },
        )
