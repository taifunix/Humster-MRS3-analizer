from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import sys

import pandas as pd
import pytest
from openpyxl import load_workbook

from mrs3.performance_v2_selection import (
    PerformanceV2SelectionError,
    SelectionConfig,
    effective_selection_stages,
    load_selection_config,
    parse_selection_request,
    run_selection,
    write_selection_workbook,
    _research_decisions,
)


RESEARCHED_DEFAULTS = {
    "researched_pnl_dd5_ratio": "0.50",
    "researched_pnl_b_ratio": "0.60",
    "researched_b_abs": "2",
    "researched_b_rel": "0.25",
    "researched_dd5_abs": "3",
    "researched_dd5_rel": "0.25",
    "researched_dd_abs": "1",
    "researched_dd_rel": "0.25",
    "researched_points_mean_ratio": "0.40",
    "researched_points_same_floor_ratio": "0.40",
    "researched_points_cross_floor_ratio": "0.30",
    "researched_points_single_best_ratio": "0.10",
    "researched_open_ma_delta": "2.6",
    "researched_close_ma_delta": "3",
    "researched_hold_p95_ratio": "0.20",
    "researched_hold_median_ratio": "0.30",
    "researched_hold_p95_veto_ratio": "0.15",
}


def _config(path: Path, **overrides: object) -> Path:
    payload = {"unified_performance_v2": {"finalist_selection": {**overrides}}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_researched_config_has_flat_decimal_defaults_and_overrides(tmp_path: Path) -> None:
    config = load_selection_config(_config(tmp_path / "config.json"))
    for name, value in RESEARCHED_DEFAULTS.items():
        assert getattr(config, name) == Decimal(value)
    overridden = load_selection_config(_config(tmp_path / "config-override.json", researched_b_abs="2.5"))
    assert overridden.researched_b_abs == Decimal("2.5")


def test_researched_stage_ids_have_fixed_scopes_and_order() -> None:
    request = parse_selection_request({
        "symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "pair_side_pnl_upper_half", "enabled": True, "scope": "pair_side"},
            {"id": "structural_stage_1", "enabled": True, "scope": "pair_side_timeframe"},
            {"id": "structural_stage_2", "enabled": True, "scope": "pair_side_timeframe"},
            {"id": "pair_side_stage_3", "enabled": True, "scope": "pair_side"},
        ],
    })
    assert [(stage.id, stage.scope) for stage in effective_selection_stages(request, SelectionConfig(lot_variant_redundancy_enabled=False))] == [
        ("pair_side_pnl_upper_half", "pair_side"),
        ("structural_stage_1", "pair_side_timeframe"),
        ("structural_stage_2", "pair_side_timeframe"),
        ("pair_side_stage_3", "pair_side"),
    ]
    with pytest.raises(PerformanceV2SelectionError, match="STAGE_SCOPE"):
        parse_selection_request({
            "symbol": "BTCUSDT", "side": "LONG", "stages": [
                {"id": "structural_stage_1", "enabled": True, "scope": "pair_side"},
            ],
        })


def test_upper_half_stage_uses_independent_odd_medians_and_strict_threshold() -> None:
    request = parse_selection_request({
        "symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": "pair_side_pnl_upper_half", "enabled": True, "scope": "pair_side"},
        ],
    })
    rows = pd.DataFrame([
        {"strategy_id": 1, "strategy_name": "keep", "timeframe": "1h", "dd5_proxy": Decimal("30"), "ab_return_b_30d_pct": Decimal("30")},
        {"strategy_id": 2, "strategy_name": "drop", "timeframe": "1h", "dd5_proxy": Decimal("10"), "ab_return_b_30d_pct": Decimal("10")},
        {"strategy_id": 3, "strategy_name": "missing", "timeframe": "1h", "dd5_proxy": None, "ab_return_b_30d_pct": None},
        {"strategy_id": 4, "strategy_name": "zero", "timeframe": "1h", "dd5_proxy": Decimal("0"), "ab_return_b_30d_pct": Decimal("0")},
    ])
    result = run_selection(rows, request).set_index("strategy_id")
    assert not bool(result.loc[1, "eliminated_by_pair_side_pnl_upper_half"])
    assert bool(result.loc[2, "eliminated_by_pair_side_pnl_upper_half"])
    assert not bool(result.loc[3, "eliminated_by_pair_side_pnl_upper_half"])
    assert bool(result.loc[4, "eliminated_by_pair_side_pnl_upper_half"])


def test_upper_half_nonpositive_reference_is_unavailable_and_passes() -> None:
    request = _structural_request("pair_side_pnl_upper_half", "pair_side")
    rows = pd.DataFrame([
        {"strategy_id": 1, "strategy_name": "zero", "timeframe": "1h", "dd5_proxy": Decimal("0"), "ab_return_b_30d_pct": Decimal("0")},
        {"strategy_id": 2, "strategy_name": "negative", "timeframe": "1h", "dd5_proxy": Decimal("-1"), "ab_return_b_30d_pct": Decimal("-1")},
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False))
    assert not result["eliminated_by_pair_side_pnl_upper_half"].any()


def test_local_upper_half_oracle_parity_for_independent_metrics() -> None:
    tools_path = Path(__file__).parents[1] / "Output" / "FilterExp" / "_tools"
    if not tools_path.is_dir():
        pytest.skip("Local research oracle is not present")
    sys.path.insert(0, str(tools_path))
    try:
        from pnl_rules import evaluate as oracle
        values = [(40, 5), (30, 30), (20, None), (10, 10), (None, 20), (0, 0), (-1, -1)]
        rows = [{"strategy_id": sid, "strategy_name": str(sid), "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h",
                 "dd5_proxy": dd5, "ab_return_b_30d_pct": b, "pnl_30d_pct": b}
                for sid, (dd5, b) in enumerate(values, 1)]
        # The research helper names the B field first and the full field second;
        # swap the fractions to map its full field to panel DD5 and B field to panel B.
        oracle_rows = [{**row, "pnl_30d_pct": row["dd5_proxy"]} for row in rows]
        expected = oracle(oracle_rows, mode="adaptive", b_fraction=Decimal("0.60"), full_fraction=Decimal("0.50"),
                          pipeline="parallel", group_scope="pair")
        request = _structural_request("pair_side_pnl_upper_half", "pair_side")
        actual = run_selection(pd.DataFrame(rows), request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
        assert {sid for sid, decision in expected.items() if decision["status"] == "DROP"} == set(actual.index[actual["eliminated_by_pair_side_pnl_upper_half"]])
    finally:
        sys.path.remove(str(tools_path))


def test_upper_half_even_reference_equality_and_independent_reasons() -> None:
    request = _structural_request("pair_side_pnl_upper_half", "pair_side")
    rows = pd.DataFrame([
        {"strategy_id": 1, "strategy_name": "1", "dd5_proxy": Decimal("40"), "ab_return_b_30d_pct": Decimal("40")},
        {"strategy_id": 2, "strategy_name": "2", "dd5_proxy": Decimal("30"), "ab_return_b_30d_pct": Decimal("30")},
        {"strategy_id": 3, "strategy_name": "3", "dd5_proxy": Decimal("20"), "ab_return_b_30d_pct": Decimal("20")},
        {"strategy_id": 4, "strategy_name": "4", "dd5_proxy": Decimal("14"), "ab_return_b_30d_pct": Decimal("21")},
        {"strategy_id": 5, "strategy_name": "5", "dd5_proxy": Decimal("10"), "ab_return_b_30d_pct": Decimal("10")},
        {"strategy_id": 6, "strategy_name": "6", "dd5_proxy": Decimal("15"), "ab_return_b_30d_pct": Decimal("18")},
        {"strategy_id": 7, "strategy_name": "7", "dd5_proxy": None, "ab_return_b_30d_pct": None},
    ])
    # Top three of six available values: 40,30,20 -> reference 30.
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert result.loc[4, "elimination_reason"] == "PAIR_SIDE_PNL_UPPER_HALF;DD5"
    assert result.loc[5, "elimination_reason"] == "PAIR_SIDE_PNL_UPPER_HALF;DD5+B"
    assert not bool(result.loc[6, "eliminated_by_pair_side_pnl_upper_half"])
    assert not bool(result.loc[7, "eliminated_by_pair_side_pnl_upper_half"])


def test_panel_ignores_spreadsheet_prefilter_markers_and_rejects_invalid_identity() -> None:
    request = _structural_request("pair_side_pnl_upper_half", "pair_side")
    rows = pd.DataFrame([
        {"strategy_id": 1, "strategy_name": "a", "timeframe": "M15", "dd5_proxy": 30, "ab_return_b_30d_pct": 30, "prefilter": None},
        {"strategy_id": 2, "strategy_name": "b", "timeframe": "M15", "dd5_proxy": 5, "ab_return_b_30d_pct": 5, "prefilter": "unknown"},
    ])
    assert run_selection(rows, request)["eliminated_by_pair_side_pnl_upper_half"].sum() == 1
    with pytest.raises(PerformanceV2SelectionError, match="RESEARCHED_INVALID_STRATEGY_ID"):
        _research_decisions(pd.DataFrame([_structural_row(1, 100), {**_structural_row(2, 200), "strategy_id": "2"}]),
                            "structural_stage_1", SelectionConfig())


def _structural_request(stage_id: str, scope: str) -> object:
    return parse_selection_request({
        "symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": stage_id, "enabled": True, "scope": scope},
        ],
    })


def _structural_row(
    sid: int, shift: int, *, order_count: int = 1, b: int = 10, dd5: int = 10,
    dd: int = 10, points: tuple[int, ...] = (10,), ma: tuple[int, ...] = (10,),
    close: int = 10, p95: int = 100, median: int = 80,
) -> dict[str, object]:
    row: dict[str, object] = {
        "strategy_id": sid, "strategy_name": str(sid), "symbol": "BTCUSDT", "side": "LONG",
        "timeframe": "1h", "order_count": order_count, "first_shift_bp": shift,
        "ab_return_b_30d_pct": Decimal(b), "dd5_proxy": Decimal(dd5), "max_drawdown_pct": Decimal(dd),
        "close_ma_len": Decimal(close), "holding_p95_minutes": None if p95 is None else Decimal(p95),
        "holding_median_minutes": None if median is None else Decimal(median),
    }
    for order, value in enumerate(points, 1):
        row[f"order_{order}_plateau_point_count"] = value
    for order, value in enumerate(ma, 1):
        row[f"order_{order}_open_ma_len"] = value
    return row


def test_researched_stages_disabled_are_noops() -> None:
    rows = pd.DataFrame([_structural_row(1, 100), _structural_row(2, 200, b=30)])
    for stage_id, scope in (
        ("pair_side_pnl_upper_half", "pair_side"),
        ("structural_stage_1", "pair_side_timeframe"),
        ("structural_stage_2", "pair_side_timeframe"),
        ("pair_side_stage_3", "pair_side"),
    ):
        request = parse_selection_request({
            "symbol": "BTCUSDT", "side": "LONG",
            "stages": [{"id": stage_id, "scope": scope, "enabled": False}],
        })
        result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False))
        assert not result[f"eliminated_by_{stage_id}"].any()
        assert result["finalist"].all()


def test_researched_stage_rejects_duplicate_id_across_timeframes() -> None:
    rows = pd.DataFrame([
        _structural_row(1, 100),
        {**_structural_row(1, 200), "timeframe": "4h"},
    ])
    with pytest.raises(PerformanceV2SelectionError, match="RESEARCHED_INVALID_STRATEGY_ID"):
        run_selection(rows, _structural_request("structural_stage_1", "pair_side_timeframe"),
                      SelectionConfig(lot_variant_redundancy_enabled=False))


def test_structural_stage1_keeps_current_core_advantage_and_drops_lower_quality() -> None:
    request = _structural_request("structural_stage_1", "pair_side_timeframe")
    rows = pd.DataFrame([
        _structural_row(1, 100, b=30),
        _structural_row(2, 200, b=20),
        _structural_row(3, 100, b=10),
        _structural_row(4, 200, b=20, dd5=20, dd=5),
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert not bool(result.loc[1, "eliminated_by_structural_stage_1"])
    assert bool(result.loc[3, "eliminated_by_structural_stage_1"])
    assert "STRUCTURAL_REDUNDANCY" in str(result.loc[3, "elimination_reason"])


def test_points_exactly_at_forty_percent_does_not_rescue_workbook_18880() -> None:
    rows = pd.DataFrame([
        _structural_row(18880, 30, order_count=3, b=15, dd5=19, dd=Decimal("6.96"),
                        points=(10, 17, 38), ma=(5, 4, 4), close=6, p95=264, median=34),
        _structural_row(18954, 40, order_count=3, b=18, dd5=20, dd=Decimal("7.43"),
                        points=(5, 17, 17), ma=(4, 4, 5), close=6, p95=250, median=34),
    ])
    result = run_selection(rows, _structural_request("structural_stage_1", "pair_side_timeframe"),
                           SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert bool(result.loc[18880, "eliminated_by_structural_stage_1"])
    assert result.loc[18880, "auto_analog_of_strategy_id"] == 18954


def test_hold_rescued_replacement_remains_a_valid_analog() -> None:
    rows = pd.DataFrame([
        _structural_row(7706, 60, order_count=2, b=8, dd5=6, dd=Decimal("11.37"),
                        points=(58, 25), ma=(3, 4), close=2, p95=952, median=139),
        _structural_row(7737, 60, order_count=2, b=10, dd5=6, dd=Decimal("9.29"),
                        points=(58, 28), ma=(3, 3), close=2, p95=956, median=137),
        _structural_row(7777, 110, order_count=2, b=8, dd5=7, dd=Decimal("7.98"),
                        points=(97, 35), ma=(5, 4), close=3, p95=959, median=202),
    ])
    result = run_selection(rows, _structural_request("structural_stage_1", "pair_side_timeframe"),
                           SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert bool(result.loc[7706, "eliminated_by_structural_stage_1"])
    assert result.loc[7706, "auto_analog_of_strategy_id"] == 7737
    assert not bool(result.loc[7737, "eliminated_by_structural_stage_1"])


def test_same_stage_dropped_replacement_has_blank_analog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mrs3.performance_v2_selection as selection

    decisions = {
        1: selection._ResearchDecision("DROP", "TEST", 2),
        2: selection._ResearchDecision("DROP", "TEST", 3),
        3: selection._ResearchDecision("KEEP", "TEST"),
    }
    monkeypatch.setattr(selection, "_research_decisions", lambda *_args, **_kwargs: decisions)
    request = _structural_request("structural_stage_1", "pair_side_timeframe")
    rows = pd.DataFrame([_structural_row(sid, sid * 100) for sid in (1, 2, 3)])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert bool(result.loc[1, "eliminated_by_structural_stage_1"])
    assert not bool(result.loc[1, "finalist"])
    assert result.loc[1, "elimination_reason"] == "STRUCTURAL_STAGE_1;TEST"
    assert pd.isna(result.loc[1, "auto_analog_of_strategy_id"])
    assert result.loc[2, "auto_analog_of_strategy_id"] == 3
    path = write_selection_workbook(result.reset_index(), tmp_path / "blank-analog.xlsx", request, {"snapshot_id": "one"}, {})
    sheet = load_workbook(path, data_only=True).active
    headers = [cell.value for cell in sheet[1]]
    exported = next(row for row in sheet.iter_rows(min_row=2, values_only=True) if row[headers.index("ID")] == 1)
    assert exported[headers.index("Auto Analog Of ID")] is None


@pytest.mark.parametrize("stage_id,scope", [
    ("structural_stage_2", "pair_side_timeframe"),
    ("pair_side_stage_3", "pair_side"),
])
def test_cross_ord_points_exactly_at_forty_percent_do_not_protect(stage_id: str, scope: str) -> None:
    assert SelectionConfig().researched_points_cross_floor_ratio == Decimal("0.30")
    assert SelectionConfig().researched_points_single_best_ratio == Decimal("0.10")
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10, dd5=10, dd=10, points=(Decimal("13"),)),
        _structural_row(2, 200, order_count=2, b=20, dd5=20, dd=5,
                        points=(Decimal("7"), Decimal("8.6")), ma=(10, 10)),
    ])
    result = run_selection(rows, _structural_request(stage_id, scope),
                           SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert bool(result.loc[1, f"eliminated_by_{stage_id}"])
    assert result.loc[1, "auto_analog_of_strategy_id"] == 2

    better = rows.copy()
    better.loc[1, "order_2_plateau_point_count"] = Decimal("8.4")
    protected = run_selection(better, _structural_request(stage_id, scope),
                              SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert not bool(protected.loc[1, f"eliminated_by_{stage_id}"])


@pytest.mark.parametrize("candidate_close,expect_drop", [(5, False), (4, True)])
def test_close_ma_three_period_boundary_is_inclusive(candidate_close: int, expect_drop: bool) -> None:
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10, dd5=10, dd=10, close=2),
        _structural_row(2, 200, b=20, dd5=20, dd=5, close=candidate_close),
    ])
    result = run_selection(rows, _structural_request("structural_stage_1", "pair_side_timeframe"),
                           SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert bool(result.loc[1, "eliminated_by_structural_stage_1"]) == expect_drop


def test_open_ma_exactly_at_delta_is_not_a_protection() -> None:
    rows = pd.DataFrame([
        _structural_row(1, 100, order_count=3, b=10, dd5=10, dd=10,
                        points=(10, 10, 10), ma=(Decimal("1"), Decimal("1"), Decimal("2"))),
        _structural_row(2, 200, order_count=3, b=20, dd5=20, dd=5,
                        points=(10, 10, 10), ma=(Decimal("3.6"), Decimal("4.1"), Decimal("4.1"))),
    ])
    result = run_selection(rows, _structural_request("structural_stage_1", "pair_side_timeframe"),
                           SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert bool(result.loc[1, "eliminated_by_structural_stage_1"])
    better = rows.copy()
    better.loc[1, "order_3_open_ma_len"] = Decimal("4.4")  # mean delta 2.7
    protected = run_selection(better, _structural_request("structural_stage_1", "pair_side_timeframe"),
                              SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert not bool(protected.loc[1, "eliminated_by_structural_stage_1"])


def test_structural_stage2_uses_current_row_polarity_and_cross_points_floor() -> None:
    request = _structural_request("structural_stage_2", "pair_side_timeframe")
    rows = pd.DataFrame([
        _structural_row(1, 100, order_count=1, b=30),
        _structural_row(2, 200, order_count=2, b=20, points=(10, 10), ma=(10, 10)),
        _structural_row(3, 100, order_count=1, b=10),
        _structural_row(4, 200, order_count=2, b=20, dd5=20, dd=5, points=(10, 10), ma=(10, 10)),
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert not bool(result.loc[1, "eliminated_by_structural_stage_2"])
    assert bool(result.loc[3, "eliminated_by_structural_stage_2"])
    assert "STRUCTURAL_STAGE_2" in str(result.loc[3, "elimination_reason"])
    assert result.loc[3, "auto_analog_of_strategy_id"] == 2


def test_structural_stage3_requires_two_material_wins_and_keeps_ord4() -> None:
    request = _structural_request("pair_side_stage_3", "pair_side")
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10, dd5=10, dd=10),
        _structural_row(2, 200, b=20, dd5=20, dd=5),
        _structural_row(4, 300, order_count=4, b=30, dd5=30, dd=4,
                        points=(10, 10, 10, 10), ma=(10, 10, 10, 10)),
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert bool(result.loc[1, "eliminated_by_pair_side_stage_3"])
    assert not bool(result.loc[4, "eliminated_by_pair_side_stage_3"])


def test_panel_xlsx_reuses_reason_and_analog_columns_for_researched_stages(tmp_path: Path) -> None:
    request = _structural_request("pair_side_stage_3", "pair_side")
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10, dd5=10, dd=10),
        _structural_row(2, 200, b=20, dd5=20, dd=5),
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False))
    assert int(result.set_index("strategy_id").loc[1, "auto_analog_of_strategy_id"]) == 2
    path = write_selection_workbook(result, tmp_path / "panel-selection.xlsx", request, {"snapshot_id": "one"}, {})
    sheet = load_workbook(path, data_only=True).active
    headers = [cell.value for cell in sheet[1]]
    baseline = write_selection_workbook(
        result, tmp_path / "baseline.xlsx",
        parse_selection_request({"symbol": "BTCUSDT", "side": "LONG", "stages": []}),
        {"snapshot_id": "one"}, {},
    )
    baseline_book = load_workbook(baseline, data_only=True)
    assert load_workbook(path, data_only=True).sheetnames == baseline_book.sheetnames
    assert headers == [cell.value for cell in baseline_book.active[1]]
    assert "Причина" in headers and "Auto Analog Of ID" in headers
    assert not any("Researched" in str(value) or value in {"DROP", "KEEP"} for value in headers)
    row = next(values for values in sheet.iter_rows(min_row=2, values_only=True) if values[headers.index("ID")] == 1)
    assert "PAIR_SIDE_STAGE_3" in row[headers.index("Причина")]
    assert row[headers.index("Auto Analog Of ID")] == 2


@pytest.mark.parametrize("stage_id,scope", [
    ("structural_stage_1", "pair_side_timeframe"),
    ("pair_side_stage_3", "pair_side"),
])
def test_structural_analog_survives_final_rank_and_export(tmp_path: Path, stage_id: str, scope: str) -> None:
    request = parse_selection_request({
        "symbol": "BTCUSDT", "side": "LONG", "stages": [
            {"id": stage_id, "scope": scope, "enabled": True},
            {"id": "rank_robust_top_n", "scope": "pair_side", "enabled": True, "top_n": 1},
        ],
    })
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10, dd5=10, dd=10),
        _structural_row(2, 200, b=20, dd5=20, dd=5),
        _structural_row(3, 300, order_count=2, b=30, dd5=30, dd=4,
                        points=(1, 1), ma=(10, 10), p95=20, median=15),
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False))
    assert bool(result.set_index("strategy_id").loc[1, f"eliminated_by_{stage_id}"])
    assert not bool(result.set_index("strategy_id").loc[2, "finalist"])
    path = write_selection_workbook(result, tmp_path / f"{stage_id}.xlsx", request, {"snapshot_id": "one"}, {})
    sheet = load_workbook(path, data_only=True).active
    headers = [cell.value for cell in sheet[1]]
    row = next(values for values in sheet.iter_rows(min_row=2, values_only=True) if values[headers.index("ID")] == 1)
    assert stage_id.upper() in row[headers.index("Причина")]
    assert row[headers.index("Auto Analog Of ID")] == 2


def test_stage3_never_compares_across_pairs_or_sides() -> None:
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10, dd5=10, dd=10),
        {**_structural_row(2, 200, b=20, dd5=20, dd=5), "symbol": "ETHUSDT"},
        {**_structural_row(3, 200, b=20, dd5=20, dd=5), "side": "SHORT"},
    ])
    result = run_selection(rows, _structural_request("pair_side_stage_3", "pair_side"),
                           SelectionConfig(lot_variant_redundancy_enabled=False))
    assert not result["eliminated_by_pair_side_stage_3"].any()


def test_stage1_missing_timeframe_uses_available_pair_side_key() -> None:
    rows = pd.DataFrame([_structural_row(1, 100), _structural_row(2, 200, b=20)]).drop(columns="timeframe")
    result = run_selection(rows, _structural_request("structural_stage_1", "pair_side_timeframe"),
                           SelectionConfig(lot_variant_redundancy_enabled=False))
    assert bool(result.set_index("strategy_id").loc[1, "eliminated_by_structural_stage_1"])


def test_structural_stage3_missing_hold_is_incomplete_and_cannot_replace() -> None:
    request = _structural_request("pair_side_stage_3", "pair_side")
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10, dd5=10, dd=10),
        _structural_row(2, 200, b=20, dd5=20, dd=5, p95=None),
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert not result["eliminated_by_pair_side_stage_3"].any()


def test_structural_invalid_ord_is_retained_and_cannot_replace() -> None:
    request = _structural_request("structural_stage_1", "pair_side_timeframe")
    rows = pd.DataFrame([
        _structural_row(1, 100, b=10),
        {**_structural_row(2, 200, b=30), "order_count": "1"},
    ])
    result = run_selection(rows, request, SelectionConfig(lot_variant_redundancy_enabled=False)).set_index("strategy_id")
    assert not bool(result.loc[1, "eliminated_by_structural_stage_1"])
    assert not bool(result.loc[2, "eliminated_by_structural_stage_1"])


def test_local_research_oracle_parity_across_three_structural_stages() -> None:
    tools_path = Path(__file__).parents[1] / "Output" / "FilterExp" / "_tools"
    if not tools_path.is_dir():
        pytest.skip("Local research oracles are not present")
    sys.path.insert(0, str(tools_path))
    try:
        from performance_v2_rules import evaluate as stage1_oracle, evaluate_cross_ord as stage2_oracle
        from pair_side_stage3 import evaluate as stage3_oracle

        source = [
            _structural_row(11, 100, b=12, dd5=14, dd=12, points=(12,), p95=100, median=80),
            _structural_row(12, 100, b=18, dd5=19, dd=9, points=(11,), p95=100, median=80),
            _structural_row(13, 200, b=20, dd5=20, dd=8, points=(10,), p95=110, median=85),
            _structural_row(14, 200, order_count=2, b=22, dd5=23, dd=7, points=(12, 8), ma=(10, 10)),
            _structural_row(15, 300, order_count=2, b=25, dd5=25, dd=6, points=(8, 9), ma=(10, 10)),
            _structural_row(16, 300, order_count=3, b=28, dd5=28, dd=5, points=(10, 9, 8), ma=(10, 10, 10)),
            _structural_row(17, 300, order_count=4, b=30, dd5=30, dd=4, points=(10, 10, 10, 10), ma=(10, 10, 10, 10)),
        ]
        oracle_rows = [{
            "ID": row["strategy_id"], "Пара": row["symbol"], "Side": row["side"], "ТФ": row["timeframe"],
            "ORD": row["order_count"], "1 Shift": Decimal(row["first_shift_bp"]) / 100,
            "PnL B/30д, %": row["ab_return_b_30d_pct"], "PnL DD5/30": row["dd5_proxy"],
            "DD": row["max_drawdown_pct"], "Close": row["close_ma_len"],
            "Points": " / ".join(str(row[f"order_{n}_plateau_point_count"]) for n in range(1, row["order_count"] + 1)),
            "MA": " / ".join(str(row[f"order_{n}_open_ma_len"]) for n in range(1, row["order_count"] + 1)),
            "Hold p95": row["holding_p95_minutes"], "Hold M": row["holding_median_minutes"], "Итог": "Да",
        } for row in source]
        config = SelectionConfig(lot_variant_redundancy_enabled=False)
        frame = pd.DataFrame(source)
        oracle1 = stage1_oracle(oracle_rows)
        actual1 = run_selection(frame, _structural_request("structural_stage_1", "pair_side_timeframe"), config).set_index("strategy_id")
        assert {sid for sid, decision in oracle1.items() if decision.final == "DROP"} == set(actual1.index[actual1["eliminated_by_structural_stage_1"]])
        remaining = {sid for sid, decision in oracle1.items() if decision.final != "DROP"}
        oracle2 = stage2_oracle(oracle_rows, oracle1)
        actual2 = run_selection(frame.loc[frame.strategy_id.isin(remaining)], _structural_request("structural_stage_2", "pair_side_timeframe"), config).set_index("strategy_id")
        assert {sid for sid, decision in oracle2.items() if decision.final == "DROP"} == set(actual2.index[actual2["eliminated_by_structural_stage_2"]])
        remaining -= {sid for sid, decision in oracle2.items() if decision.final == "DROP"}
        oracle3 = stage3_oracle([row for row in oracle_rows if row["ID"] in remaining])
        actual3 = run_selection(frame.loc[frame.strategy_id.isin(remaining)], _structural_request("pair_side_stage_3", "pair_side"), config).set_index("strategy_id")
        assert {sid for sid, decision in oracle3.items() if decision.final == "DROP"} == set(actual3.index[actual3["eliminated_by_pair_side_stage_3"]])
        analog_count = 0
        for actual, stage_id in ((actual1, "structural_stage_1"), (actual2, "structural_stage_2"), (actual3, "pair_side_stage_3")):
            for _, dropped in actual.loc[actual[f"eliminated_by_{stage_id}"]].iterrows():
                analog = dropped["auto_analog_of_strategy_id"]
                if pd.notna(analog):
                    analog_count += 1
                    assert not bool(actual.loc[int(analog), f"eliminated_by_{stage_id}"])
        assert analog_count > 0
    finally:
        sys.path.remove(str(tools_path))
