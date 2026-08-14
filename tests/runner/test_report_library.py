from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pandas as pd

import mrs3.runner.report_library as report_library
from mrs3.runner.config import RunnerConfig
from mrs3.runner.results import WizardResult


def _config(tmp_path: Path) -> RunnerConfig:
    bot = (tmp_path / "hb").resolve()
    return RunnerConfig(
        bot_root=bot,
        executable_path=bot / "hb_c.exe",
        base_url="http://127.0.0.1:8087",
        port=8087,
        strategy_dir=bot / "settings_strategy",
        report_dir=bot / "tester/report/my_test",
        wizard_result=bot / "tester/wizard_result.json",
        wizard_progress=bot / "tester/wizard_progress.json",
        tester_config=bot / "tester/tester_config.json",
        inbox_root=tmp_path / "inbox",
        metric_tolerance=Decimal("0.01"),
    )


def _result(name: str) -> WizardResult:
    return WizardResult("run", "", (name,), {}, f"/tester-report/my_test/{name}.html", f"{name}.html", "", "")


def test_library_publishes_reconciled_html_and_removes_only_identical_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    report = config.report_dir / "A.html"
    report.write_text("verified", encoding="utf-8")
    monkeypatch.setattr(
        report_library,
        "reconcile_results",
        lambda *args, **kwargs: pd.DataFrame([{"strategy_name": "A"}]),
    )

    first = report_library.publish_verified_reports(
        config, ("A",), (_result("A"),), {"A": report}, "ONUSDT", apply=True
    )

    target = config.report_dir.parent / "ONUSDT_reports" / "A.html"
    assert first.accepted_count == 1
    assert target.read_text(encoding="utf-8") == "verified"
    assert report.exists()
    assert first.manifest_path is not None

    duplicate = config.report_dir / "again.html"
    duplicate.write_text("verified", encoding="utf-8")
    second = report_library.publish_verified_reports(
        config, ("A",), (_result("A"),), {"A": duplicate}, "ONUSDT", apply=True
    )

    assert second.duplicate_count == 1
    assert not duplicate.exists()
    assert second.entries[0].safe_to_delete


def test_library_keeps_conflicting_or_unverified_report(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    config.report_dir.mkdir(parents=True)
    target_dir = config.report_dir.parent / "ONUSDT_reports"
    target_dir.mkdir()
    (target_dir / "A.html").write_text("old", encoding="utf-8")
    report = config.report_dir / "A.html"
    report.write_text("new", encoding="utf-8")
    monkeypatch.setattr(
        report_library,
        "reconcile_results",
        lambda *args, **kwargs: pd.DataFrame([{"strategy_name": "A"}]),
    )

    audit = report_library.publish_verified_reports(
        config, ("A",), (_result("A"),), {"A": report}, "ONUSDT", apply=True
    )

    assert audit.conflict_count == 1
    assert report.exists()
    assert not audit.entries[0].safe_to_delete
