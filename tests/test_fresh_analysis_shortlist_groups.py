"""The shortlist must say what the analysis actually found.

The panel renders a table headed `1ORD | 2ORD | 3ORD | 4ORD`, grouped by
Pair · Side · TF. The endpoint returned one ungrouped row per structure with a
single `order_count`, so the panel wrote every candidate's order count into the
`4ORD` column and hardcoded a dash into the other three. A real analysis of 171
two-order structures therefore read as 171 four-order candidates, and the eight
one-order candidates in `base_one_order` were invisible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_fresh_analysis_strategies import _make_analysis, _order, _point, _template


def _add_rows(path: Path, table: str, rows: list[dict]) -> None:
    import duckdb

    connection = duckdb.connect(str(path))
    try:
        connection.executemany(
            f"insert into {table} values (?, ?)",
            [
                ("BTCUSDT|LONG|1h", json.dumps(row, sort_keys=True, separators=(",", ":")))
                for row in rows
            ],
        )
    finally:
        connection.close()


def _base_selection() -> dict:
    """`base_one_order` stores the selected single-order *point*, not a structure.

    Verified against a real analysis: the row carries `point_id`, `plateau_id`
    and the point metrics, and has no `structure_id`, `order_count` or `orders`.
    """
    return _point("BTCUSDT|LONG|1h|100|3|9", 100, 3, "event-a")


def test_the_one_order_column_is_not_permanently_empty(tmp_path: Path) -> None:
    """The base selection is a real result and must be counted, not hidden."""
    from mrs3.fresh_analysis_strategies import list_fresh_analysis_shortlist

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    _add_rows(database, "base_one_order", [_base_selection()])

    result = list_fresh_analysis_shortlist(database, analysis_id)

    assert result["groups"][0]["counts"]["1ORD"] == 1
    # It is a point, not a structure, so it cannot be generated and is not offered.
    assert result["groups"][0]["candidate_ids"] == ["STR-READY"]


def test_the_shortlist_is_grouped_with_a_count_per_order_bucket(tmp_path: Path) -> None:
    """One row per Pair · Side · TF, with the counts the table headers promise."""
    from mrs3.fresh_analysis_strategies import list_fresh_analysis_shortlist

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    _add_rows(database, "base_one_order", [_base_selection()])

    result = list_fresh_analysis_shortlist(database, analysis_id)

    assert len(result["groups"]) == 1, "one row per scope, not one per candidate"
    group = result["groups"][0]
    assert (group["pair"], group["side"], group["timeframe"]) == ("BTCUSDT", "LONG", "1h")
    # The bucket a candidate belongs to is its own order count, never the last column.
    assert group["counts"] == {"1ORD": 1, "2ORD": 1, "3ORD": 0, "4ORD": 0}
    assert group["ready"] == 1 and group["total"] == 2
    assert group["candidate_ids"] == ["STR-READY"]


def test_a_candidate_that_is_not_ready_is_counted_but_not_offered(tmp_path: Path) -> None:
    """A deferred structure is visible as work in progress, not as a choice."""
    from mrs3.fresh_analysis_strategies import list_fresh_analysis_shortlist

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database, ready=False)

    result = list_fresh_analysis_shortlist(database, analysis_id)

    group = result["groups"][0]
    assert group["total"] == 1 and group["ready"] == 0
    assert group["counts"]["2ORD"] == 1, "it is still a two-order structure"
    assert group["candidate_ids"] == [], "only READY candidates may be selected"


def test_a_base_selection_is_never_offered_as_a_generatable_candidate(tmp_path: Path) -> None:
    """Showing a candidate the generator refuses would be a new dead end."""
    from mrs3.fresh_analysis_strategies import generate_fresh_analysis_strategies

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    _add_rows(database, "base_one_order", [_base_selection()])
    template = tmp_path / "template.json"
    template.write_text(json.dumps(_template()), encoding="utf-8")

    from mrs3.config import AlgorithmConfig

    with pytest.raises(ValueError, match="absent from fresh analysis"):
        generate_fresh_analysis_strategies(
            database, analysis_id, ["BTCUSDT|LONG|1h|100|3|9"], [("BTCUSDT", "LONG", "1h")],
            template, tmp_path / "out", AlgorithmConfig.defaults(),
        )


def test_a_duplicate_candidate_identity_is_refused(tmp_path: Path) -> None:
    """Two candidates under one identity would silently shadow each other."""
    from mrs3.fresh_analysis_strategies import list_fresh_analysis_shortlist

    database = tmp_path / "run.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    connection_rows = [{
        "structure_id": "STR-READY", "symbol": "BTCUSDT", "side": "LONG", "timeframe": "1h",
        "common_close_ma": 9, "order_count": 3, "orders": [], "status": "DEFERRED",
    }]
    _add_rows(database, "structures", connection_rows)

    with pytest.raises(ValueError, match="duplicate candidate"):
        list_fresh_analysis_shortlist(database, analysis_id)
