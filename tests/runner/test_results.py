from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import pytest

from mrs3.runner.results import (
    ResultMismatchError,
    WizardResult,
    extract_html_strategy_name,
    load_wizard_results,
    parse_html_report,
    reconcile_results,
    write_results_csv_atomic,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"
REPORT_NAME = "my_test_run_001_of_001_ADMSTOCK_USDT_2h_2026-07-01_3.html"


def test_lightweight_strategy_name_reads_only_embedded_settings(tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    report.write_text(
        '<html><body><pre>{"name":"FAST","basic":{}}</pre></body></html>',
        encoding="utf-8",
    )

    assert extract_html_strategy_name(report) == "FAST"


def test_reconciliation_uses_embedded_name_without_full_html_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "A.html"
    report.write_text(
        '<html><body><pre>{"name":"A","basic":{}}</pre></body></html>',
        encoding="utf-8",
    )
    result = WizardResult(
        "run", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", ""
    )
    monkeypatch.setattr(
        "mrs3.runner.results.parse_html_report",
        lambda _: pytest.fail("full HTML parsing must not run for runner verification"),
    )

    frame = reconcile_results(("A",), (result,), tmp_path, Decimal("0.01"))

    assert frame.iloc[0]["strategy_name"] == "A"
    assert frame.iloc[0]["verification_mode"] == "strategy_name_only"


def test_supplied_json_maps_adm1_to_exact_report() -> None:
    result = load_wizard_results(FIXTURES / "wizard_result.json")[0]

    assert result.strategy_names == ("ADM1",)
    assert result.report_name == REPORT_NAME


def test_reconciliation_rejects_a_different_embedded_strategy_name(tmp_path: Path) -> None:
    report = tmp_path / "A.html"
    report.write_text(
        '<html><body><pre>{"name":"B","basic":{}}</pre></body></html>',
        encoding="utf-8",
    )
    result = WizardResult("run", "", ("A",), {}, "/tester-report/my_test/A.html", "A.html", "", "")

    with pytest.raises(ResultMismatchError, match="HTML strategy name differs"):
        reconcile_results(("A",), (result,), tmp_path, Decimal("0.01"))


def test_result_csv_replacement_is_atomic_on_export_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "results.csv"
    target.write_text("old-complete\n", encoding="utf-8")

    def fail(self: pd.DataFrame, path: object, *args: object, **kwargs: object) -> None:
        Path(path).write_text("partial\n", encoding="utf-8")
        raise OSError("interrupted")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail)

    with pytest.raises(OSError, match="interrupted"):
        write_results_csv_atomic(pd.DataFrame({"a": [1]}), target)

    assert target.read_text(encoding="utf-8") == "old-complete\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["results.csv"]
