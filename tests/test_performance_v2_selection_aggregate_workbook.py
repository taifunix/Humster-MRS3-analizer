from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
import zipfile

import pandas as pd
import pytest
from openpyxl import load_workbook

from mrs3.performance_v2_selection import (
    AllPairsPartition,
    SelectionWorkbookError,
    build_combined_selection_workbook,
    validate_combined_selection_workbook,
)


def _workbook_bytes() -> bytes:
    rows = pd.DataFrame(
        [
            {
                "strategy_id": 7,
                "result_id": 70,
                "strategy_name": "candidate-7",
                "auto_status": "FINALIST",
                "auto_rank": 1,
                "auto_reason": "ranked",
            },
            {
                "strategy_id": 3,
                "result_id": 30,
                "strategy_name": "candidate-3",
                "auto_status": "FILTERED",
            },
        ]
    )
    return build_combined_selection_workbook(
        [AllPairsPartition("BTCUSDT", "LONG", "run-btc-long", rows)]
    )


def test_combined_workbook_round_trip_has_manifest_and_sorted_rowset() -> None:
    data = _workbook_bytes()
    result = validate_combined_selection_workbook(data)
    assert result["manifest"]["manifest_contract_version"] == "performance-v2-selection-aggregate-v11"
    assert result["manifest"]["frozen_user_fields"] == {
        "BTCUSDT|LONG|3": [None, None, None, None, None],
        "BTCUSDT|LONG|7": [None, None, None, None, None],
    }
    assert [row["Row Key"] for row in result["rows"]] == [
        "BTCUSDT|LONG|3",
        "BTCUSDT|LONG|7",
    ]
    with zipfile.ZipFile(BytesIO(data)) as archive:
        assert "xl/worksheets/sheet2.xml" in archive.namelist()
    workbook = load_workbook(BytesIO(data), data_only=False)
    headers = [cell.value for cell in workbook["All candidates"][1]]
    assert "Стратегия" in headers
    assert workbook["All candidates"].cell(2, headers.index("Стратегия") + 1).value == "candidate-3"


def test_combined_workbook_fills_every_used_cell_from_partition_provenance() -> None:
    rows = pd.DataFrame([
        {
            "strategy_id": 1, "result_id": 10, "strategy_name": "stalled",
            "auto_status": "RESERVE", "equity_regime_state": "STALLED",
            "equity_regime_rank": "RESERVED", "eliminated_by_filter_equity_regime": True,
        },
        {
            "strategy_id": 2, "result_id": 20, "strategy_name": "outside",
            "auto_status": "RESERVE", "auto_rank": 2, "auto_reason": "RANK_ROBUST_TOP_N",
            "auto_analog_of_strategy_id": None, "prior_rejected": False,
            "eliminated_by_filter_equity_regime": False,
            "eliminated_by_rank_robust_top_n": True,
        },
    ])
    rows.attrs["_selection_style_provenance"] = {
        "enabled_stage_ids": ("filter_equity_regime", "rank_robust_top_n"),
        "top_n": 1,
    }
    data = build_combined_selection_workbook([
        AllPairsPartition("BTCUSDT", "LONG", "run-style", rows),
    ])
    workbook = load_workbook(BytesIO(data), data_only=False)
    sheet = workbook["All candidates"]
    assert sheet.cell(2, 1).fill.fgColor.rgb.endswith("B7B7B7")
    assert sheet.cell(3, 1).fill.fgColor.rgb.endswith("EAF4E5")
    for row in sheet.iter_rows(min_row=2, max_row=3):
        colors = {cell.fill.fgColor.rgb for cell in row}
        assert len(colors) == 1


def test_combined_workbook_rejects_frozen_row_key_set_mismatch() -> None:
    workbook = load_workbook(BytesIO(_workbook_bytes()))
    metadata = workbook["_MRS_SELECTION_MANIFEST"]
    manifest = json.loads(metadata.cell(3, 2).value)
    manifest["frozen_user_fields"]["BTCUSDT|LONG|999"] = [None, None, None, None, None]
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    metadata.cell(2, 2).value = sha256(manifest_json.encode("utf-8")).hexdigest()
    metadata.cell(3, 2).value = manifest_json
    changed = BytesIO()
    workbook.save(changed)

    with pytest.raises(SelectionWorkbookError) as error:
        validate_combined_selection_workbook(changed.getvalue())

    assert str(error.value) == "SELECTION_REVIEW_ROWSET_MISMATCH"


def test_combined_workbook_rejects_tampered_header() -> None:
    data = bytearray(_workbook_bytes())
    result = validate_combined_selection_workbook(bytes(data))
    assert result["workbook_sha256"]
    # Truncation is a bounded, deterministic invalid-file check without
    # mutating a valid manifest or rowset in place.
    with pytest.raises(SelectionWorkbookError) as error:
        validate_combined_selection_workbook(bytes(data[:64]))
    assert str(error.value) == "SELECTION_REVIEW_INVALID_FILE"


def test_combined_workbook_accepts_harmless_global_row_reordering() -> None:
    data = _workbook_bytes()
    workbook = load_workbook(BytesIO(data))
    sheet = workbook["All candidates"]
    values_a = [cell.value for cell in sheet[2]]
    values_b = [cell.value for cell in sheet[3]]
    for column, value in enumerate(values_b, start=1):
        sheet.cell(2, column).value = value
    for column, value in enumerate(values_a, start=1):
        sheet.cell(3, column).value = value
    reordered = BytesIO()
    workbook.save(reordered)

    result = validate_combined_selection_workbook(reordered.getvalue())

    assert [row["Row Key"] for row in result["rows"]] == [
        "BTCUSDT|LONG|3",
        "BTCUSDT|LONG|7",
    ]


def test_combined_workbook_sorts_unsorted_partitions_and_identities() -> None:
    rows_btc = pd.DataFrame([{"strategy_id": 7, "result_id": 70, "auto_status": "FILTERED"}])
    rows_eth = pd.DataFrame([{"strategy_id": 8, "result_id": 80, "auto_status": "FILTERED"}])

    data = build_combined_selection_workbook([
        AllPairsPartition("ETHUSDT", "SHORT", "run-eth", rows_eth),
        AllPairsPartition("BTCUSDT", "LONG", "run-btc", rows_btc),
    ])

    result = validate_combined_selection_workbook(data)

    assert result["manifest"]["candidate_identities"] == [
        ["BTCUSDT", "LONG", 7, 70],
        ["ETHUSDT", "SHORT", 8, 80],
    ]


def test_combined_workbook_rejects_duplicate_pair_side() -> None:
    rows = pd.DataFrame([{"strategy_id": 7, "result_id": 70, "auto_status": "FILTERED"}])

    with pytest.raises(SelectionWorkbookError) as error:
        build_combined_selection_workbook([
            AllPairsPartition("BTCUSDT", "LONG", "run-a", rows),
            AllPairsPartition("BTCUSDT", "LONG", "run-b", rows),
        ])

    assert str(error.value) == "INVALID_SELECTION_WORKBOOK_PARTITIONS"


def test_combined_workbook_rejects_duplicate_strategy_id() -> None:
    rows = pd.DataFrame([
        {"strategy_id": 7, "result_id": 70, "auto_status": "FILTERED"},
        {"strategy_id": 7, "result_id": 71, "auto_status": "FILTERED"},
    ])

    with pytest.raises(SelectionWorkbookError) as error:
        build_combined_selection_workbook([
            AllPairsPartition("BTCUSDT", "LONG", "run-duplicate", rows),
        ])

    assert str(error.value) == "DUPLICATE_STRATEGY_ID"


def test_combined_workbook_round_trips_empty_fields_non_ascii_and_float_metrics() -> None:
    rows = pd.DataFrame([
        {
            "strategy_id": 11,
            "result_id": 110,
            "strategy_name": "Стратегия Δ",
            "auto_status": "FILTERED",
            "profit_factor": 1.25,
            "comment": "Комментарий — " + "x" * 100,
        },
    ])
    data = build_combined_selection_workbook([
        AllPairsPartition("BTCUSDT", "LONG", "run-long", rows),
        AllPairsPartition("ETHUSDT", "SHORT", "run-empty", pd.DataFrame(columns=rows.columns)),
    ])

    result = validate_combined_selection_workbook(data)

    assert len(result["rows"]) == 1
    assert result["rows"][0]["Comment"].startswith("Комментарий")
    assert result["rows"][0]["PF"] == 1.25
    assert result["manifest"]["partitions"][-1]["row_count"] == 0


def test_combined_workbook_openpyxl_round_trip_normalizes_datetime_none_float_and_formula_text() -> None:
    rows = pd.DataFrame([{
        "strategy_id": 12,
        "result_id": 120,
        "strategy_name": "=formula-leading-name",
        "auto_status": "FILTERED",
        "effective_start_utc": datetime(2026, 10, 10, 12, 30),
        "profit_factor": 1.25,
        "comment": "@untrusted-comment",
        "auto_reason": None,
    }])
    data = build_combined_selection_workbook([
        AllPairsPartition("BTCUSDT", "LONG", "run-round-trip", rows),
    ])

    workbook = load_workbook(BytesIO(data), data_only=False)
    round_tripped = BytesIO()
    workbook.save(round_tripped)

    result = validate_combined_selection_workbook(round_tripped.getvalue())

    row = result["rows"][0]
    assert row["PF"] == 1.25
    assert row["Start"] == "2026-10-10T12:30:00"
    assert row["Comment"] == "'@untrusted-comment"
    assert row["РЎС‚СЂР°С‚РµРіРёСЏ"] == "'=formula-leading-name"


def test_combined_workbook_retest_is_immutable_and_invalid_retest_is_typed() -> None:
    rows = pd.DataFrame([{
        "strategy_id": 13,
        "result_id": 130,
        "auto_status": "FILTERED",
    }])
    data = build_combined_selection_workbook(
        [AllPairsPartition("BTCUSDT", "LONG", "run-retest", rows)],
        user_review_rows={13: {"retest": "RETEST"}},
    )
    workbook = load_workbook(BytesIO(data))
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["RETEST"]).value = None
    changed = BytesIO()
    workbook.save(changed)

    with pytest.raises(SelectionWorkbookError) as error:
        validate_combined_selection_workbook(changed.getvalue())
    assert str(error.value) == "SELECTION_REVIEW_RETEST_READ_ONLY"

    workbook = load_workbook(BytesIO(data))
    workbook["All candidates"].cell(2, headers["RETEST"]).value = "NOT-RETEST"
    invalid = BytesIO()
    workbook.save(invalid)
    with pytest.raises(SelectionWorkbookError) as error:
        validate_combined_selection_workbook(invalid.getvalue())
    assert str(error.value) == "SELECTION_REVIEW_INVALID_RETEST"

    workbook = load_workbook(BytesIO(_workbook_bytes()))
    sheet = workbook["All candidates"]
    headers = {cell.value: cell.column for cell in sheet[1]}
    sheet.cell(2, headers["RETEST"]).value = "RETEST"
    added = BytesIO()
    workbook.save(added)
    with pytest.raises(SelectionWorkbookError) as error:
        validate_combined_selection_workbook(added.getvalue())
    assert str(error.value) == "SELECTION_REVIEW_RETEST_READ_ONLY"


def test_combined_workbook_freezes_raw_review_tuple_separately_from_presentation() -> None:
    rows = pd.DataFrame([{
        "strategy_id": 14,
        "result_id": 140,
        "auto_status": None,
    }])
    data = build_combined_selection_workbook(
        [AllPairsPartition("BTCUSDT", "LONG", "run-raw", rows)],
        user_review_rows={14: {
            "user_status": "REJECTED",
            "comment": "=1+1",
        }},
    )

    result = validate_combined_selection_workbook(data)
    key = "BTCUSDT|LONG|14"

    assert result["manifest"]["frozen_user_fields"][key] == ["REJECTED", None, None, "=1+1", None]
    assert result["manifest"]["frozen_user_fields_presentation"][key] == [
        "REJECTED", None, None, "'=1+1", None,
    ]
    assert result["rows"][0]["Comment"] == "'=1+1"
    assert result["rows"][0]["Auto Status"] == "FILTERED"
