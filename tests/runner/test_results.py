from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import pytest

from mrs3.runner.results import (
    ResultMismatchError,
    load_wizard_results,
    parse_html_report,
    reconcile_results,
    write_results_csv_atomic,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"
REPORT_NAME = "my_test_run_001_of_001_ADMSTOCK_USDT_2h_2026-07-01_3.html"


def test_supplied_json_maps_adm1_to_exact_report() -> None:
    result = load_wizard_results(FIXTURES / "wizard_result.json")[0]

    assert result.strategy_names == ("ADM1",)
    assert result.report_name == REPORT_NAME


def test_supplied_html_enriches_json_and_matches_core_metrics() -> None:
    report = parse_html_report(FIXTURES / REPORT_NAME)
    frame = reconcile_results(
        ("ADM1",),
        load_wizard_results(FIXTURES / "wizard_result.json"),
        FIXTURES,
        Decimal("0.01"),
    )
    row = frame.iloc[0]

    assert report.strategy_name == "ADM1"
    assert row["strategy_name"] == "ADM1"
    assert row["symbol"] == "ADMSTOCK_USDT"
    assert row["timeframe"] == "2h"
    assert row["profit_factor"] == pytest.approx(0.0070)
    assert row["total_pnl"] == pytest.approx(-6825.6651944)
    assert row["trade_row_count"] == 2
    assert json.loads(row["strategy_settings_json"])["name"] == "ADM1"


def test_metric_mismatch_fails_with_exact_field_name(tmp_path: Path) -> None:
    document = json.loads((FIXTURES / "wizard_result.json").read_text(encoding="utf-8"))
    document[0]["stats"]["TotalPnL"] = -6000
    path = tmp_path / "wizard_result.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ResultMismatchError, match="TotalPnL"):
        reconcile_results(
            ("ADM1",), load_wizard_results(path), FIXTURES, Decimal("0.01")
        )


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
