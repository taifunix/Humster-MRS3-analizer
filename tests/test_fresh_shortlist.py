from __future__ import annotations

import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_fresh_analysis_strategies import _make_analysis


def _append_structure(path: Path, structure: dict[str, object]) -> None:
    import duckdb

    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "insert into structures values (?, ?)",
            ["BTCUSDT|LONG|1h", json.dumps(structure, sort_keys=True, separators=(",", ":"))],
        )
    finally:
        connection.close()


def _read_structure(path: Path) -> dict[str, object]:
    import duckdb

    connection = duckdb.connect(str(path), read_only=True)
    try:
        return json.loads(connection.execute("select payload_json from structures limit 1").fetchone()[0])
    finally:
        connection.close()


def _replace_structure(path: Path, structure: dict[str, object]) -> None:
    import duckdb

    connection = duckdb.connect(str(path))
    try:
        connection.execute("delete from structures")
        connection.execute(
            "insert into structures values (?, ?)",
            ["BTCUSDT|LONG|1h", json.dumps(structure, sort_keys=True, separators=(",", ":"))],
        )
    finally:
        connection.close()


def test_prepare_and_evaluate_produce_compact_content_bound_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_analysis_strategies
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)

    prepared = prepare_fresh_shortlist(database, analysis_id)
    original_outcome = fresh_analysis_strategies._pretest_ab_outcome
    outcome_calls = 0

    def count_outcome(evidence: object, enabled: bool) -> tuple[str, str, str | None]:
        nonlocal outcome_calls
        outcome_calls += 1
        return original_outcome(evidence, enabled)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_analysis_strategies, "_pretest_ab_outcome", count_outcome)
    result = evaluate_fresh_shortlist(prepared, (False, False, False), workers=1)

    assert prepared.analysis_id == analysis_id
    assert len(prepared.artifact_sha256) == 64
    assert result.ready_candidate_ids == ("STR-READY",)
    assert result.candidates[0].filter_status == "READY_AFTER_FILTERS"
    assert result.groups[0].order_counts == (0, 1, 0, 0)
    assert result.groups[0].ready == 1
    assert result.groups[0].deferred == 0
    assert outcome_calls == 1
    expected_token = sha256(json.dumps({
        "analysis_id": analysis_id,
        "artifact_sha256": prepared.artifact_sha256,
        "engine_version": result.filter_engine_version,
        "options": {
            "pretest_ab_enabled": False,
            "ladder_enabled": False,
            "pareto_enabled": False,
        },
        "ready_candidate_ids": ["STR-READY"],
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert result.selection_token == expected_token


def test_ladder_uses_first_order_and_filters_before_pareto(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    import duckdb

    connection = duckdb.connect(str(database))
    try:
        row = json.loads(connection.execute("select payload_json from structures").fetchone()[0])
        source_point = json.loads(connection.execute(
            "select payload_json from points where payload_json like '%event-b%'"
        ).fetchone()[0])
    finally:
        connection.close()
    ladder_rejected = json.loads(json.dumps(row))
    ladder_rejected["structure_id"] = "LADDER-REJECTED"
    ladder_rejected["candidate_id"] = "LADDER-REJECTED"
    ladder_rejected["orders"][1]["open_ma"] = 5
    # It is otherwise numerically superior. Upstream ladder rejection means it
    # cannot dominate the candidate that survives the earlier stage.
    ladder_rejected["orders"][0]["source_pnl_pct"] = 99
    ladder_rejected["orders"][1]["source_pnl_pct"] = 99
    changed_point = json.loads(json.dumps(source_point))
    changed_point["point_id"] = "BTCUSDT|LONG|1h|300|5|9"
    changed_point["open_ma"] = 5
    changed_point["_event_ids"] = ["event-c"]
    changed_point["event_ids_hash"] = sha256(b"event-c").hexdigest()
    ladder_rejected["orders"][1]["point_id"] = changed_point["point_id"]
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "insert into points values (?, ?)",
            ["BTCUSDT|LONG|1h", json.dumps(changed_point, sort_keys=True, separators=(",", ":"))],
        )
    finally:
        connection.close()
    _append_structure(database, ladder_rejected)

    result = evaluate_fresh_shortlist(
        prepare_fresh_shortlist(database, analysis_id), (False, True, True), workers=1,
    )

    rejected = next(item for item in result.candidates if item.candidate_id == "LADDER-REJECTED")
    assert rejected.filter_status == "DEFERRED_LADDER"
    assert rejected.reason == "OPEN_MA_OUTSIDE_FIRST_ORDER_PLUS_MINUS_1"
    assert rejected.dominator_candidate_id is None
    assert result.ready_candidate_ids == ("STR-READY",)


@pytest.mark.parametrize("value", [True, False, None, "nope", "0", "-1", "100.001", "0.0001", float("inf")])
def test_min_shift_parser_rejects_invalid_thresholds(value: object) -> None:
    from mrs3.fresh_shortlist import normalize_min_shift

    with pytest.raises(ValueError, match="min_shift_pct"):
        normalize_min_shift(True, value)


def test_min_shift_parser_canonicalizes_enabled_and_discards_valid_disabled_threshold() -> None:
    from mrs3.fresh_shortlist import normalize_min_shift, parse_fresh_shortlist_request

    assert normalize_min_shift(True, "0.3") == (True, "0.300")
    assert normalize_min_shift(True, "0.300") == (True, "0.300")
    assert normalize_min_shift(False, "100.000") == (False, None)
    assert normalize_min_shift(False, None) == (False, None)
    assert normalize_min_shift(False, "0.0001") == (False, None)
    assert normalize_min_shift(False, "not-a-number") == (False, None)
    assert parse_fresh_shortlist_request({"filter_version": "shortlist-v2", "min_shift_enabled": True, "min_shift_pct": "0.3"}) == ((False, False, False), True, "0.300")


def test_min_shift_threshold_is_exactly_percent_to_basis_points() -> None:
    from mrs3.fresh_shortlist import min_shift_threshold_bp

    assert {
        value: min_shift_threshold_bp(value)
        for value in ("0.001", "0.100", "0.250", "0.300", "1.000", "2.500", "100.000")
    } == {
        "0.001": Decimal("0.100"), "0.100": Decimal("10.000"),
        "0.250": Decimal("25.000"), "0.300": Decimal("30.000"),
        "1.000": Decimal("100.000"), "2.500": Decimal("250.000"),
        "100.000": Decimal("10000.000"),
    }


def test_min_shift_requires_v2_filter_version() -> None:
    from mrs3.fresh_shortlist import parse_fresh_shortlist_request

    with pytest.raises(ValueError, match="filter_version"):
        parse_fresh_shortlist_request({"min_shift_enabled": True, "min_shift_pct": "0.3"})
    with pytest.raises(ValueError, match="min_shift_enabled"):
        parse_fresh_shortlist_request({"filter_version": "shortlist-v2", "min_shift_pct": "0.3"})


def test_min_shift_filters_below_threshold_before_pareto(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    base = _read_structure(database)
    weak = json.loads(json.dumps(base))
    weak["candidate_id"] = weak["structure_id"] = "SHIFT-WEAK"
    weak["orders"][0]["shift_bp"] = 99
    weak["orders"][1]["shift_bp"] = 300
    # Keep source point facts aligned with the persisted order shift.
    import duckdb
    connection = duckdb.connect(str(database))
    try:
        point = json.loads(connection.execute("select payload_json from points where payload_json like '%event-a%'").fetchone()[0])
        point["point_id"] = "BTCUSDT|LONG|1h|99|3|9"
        point["shift_bp"] = 99
        connection.execute("insert into points values (?, ?)", ["BTCUSDT|LONG|1h", json.dumps(point, sort_keys=True, separators=(",", ":"))])
    finally:
        connection.close()
    weak["orders"][0]["point_id"] = "BTCUSDT|LONG|1h|99|3|9"
    _append_structure(database, weak)

    result = evaluate_fresh_shortlist(
        prepare_fresh_shortlist(database, analysis_id),
        (False, True, True), workers=1, min_shift_enabled=True, min_shift_pct="1.000",
    )

    deferred = next(item for item in result.candidates if item.candidate_id == "SHIFT-WEAK")
    assert deferred.filter_status == "DEFERRED_MIN_SHIFT"
    assert deferred.reason == "ORDER_SHIFT_BELOW_MINIMUM"
    assert deferred.dominator_candidate_id is None
    assert result.ready_candidate_ids == ("STR-READY",)
    assert result.min_shift_enabled is True
    assert result.min_shift_pct == "1.000"
    assert result.groups[0].ready == 1
    assert result.groups[0].deferred == 1
    assert result.groups[0].all_count == 2


def test_min_shift_canonical_tokens_match_and_missing_shift_defers() -> None:
    from mrs3.fresh_shortlist import (
        FreshCandidateMetrics, FreshOrderMetrics, FreshScopeFacts, PreparedFreshAnalysis,
        evaluate_fresh_shortlist,
    )

    order = FreshOrderMetrics(3, None, "P", 1, 1, 1, 1)  # type: ignore[arg-type]
    candidate = FreshCandidateMetrics("C", "S", "BTCUSDT", "LONG", "1h", 9, 1, "READY_MRS3_STRUCTURE", (order,), None)
    prepared = PreparedFreshAnalysis("a" * 64, "b" * 64, (FreshScopeFacts("BTCUSDT|LONG|1h", "BTCUSDT", "LONG", "1h", 1, None),), (candidate,))

    first = evaluate_fresh_shortlist(prepared, (False, False, False), workers=1, min_shift_enabled=True, min_shift_pct="0.3")
    second = evaluate_fresh_shortlist(prepared, (False, False, False), workers=1, min_shift_enabled=True, min_shift_pct="0.300")
    assert first.selection_token == second.selection_token
    assert first.ready_candidate_ids == ()
    assert first.candidates[0].filter_status == "DEFERRED_MIN_SHIFT"
    assert first.candidates[0].reason == "ORDER_SHIFT_UNKNOWN"
    assert first.filter_engine_version == "shortlist-v2-engine-2"

    legacy = evaluate_fresh_shortlist(prepared, (False, False, False), workers=1)
    assert legacy.filter_engine_version == "shortlist-v2-engine-1"
    assert legacy.selection_token != first.selection_token


@pytest.mark.parametrize("bad_shift", [True, False, "abc", "", float("nan"), float("inf")])
def test_min_shift_rejects_malformed_present_first_shift_and_disabled_ignores_it(bad_shift: object) -> None:
    from mrs3.fresh_shortlist import (
        FreshCandidateMetrics, FreshOrderMetrics, FreshScopeFacts, PreparedFreshAnalysis,
        evaluate_fresh_shortlist,
    )

    candidate = FreshCandidateMetrics(
        "BAD-SHIFT", "BAD-SHIFT", "BTCUSDT", "LONG", "1h", 9, 1,
        "READY_MRS3_STRUCTURE",
        (FreshOrderMetrics(3, bad_shift, "P", Decimal("1"), Decimal("1"), 1, 1),),  # type: ignore[arg-type]
        None,
    )
    prepared = PreparedFreshAnalysis(
        "1" * 64, "2" * 64,
        (FreshScopeFacts("BTCUSDT|LONG|1h", "BTCUSDT", "LONG", "1h", 1, None),),
        (candidate,),
    )
    with pytest.raises(ValueError, match="fresh shortlist order Shift is invalid"):
        evaluate_fresh_shortlist(
            prepared, (False, False, False), workers=1,
            min_shift_enabled=True, min_shift_pct="0.300",
        )
    disabled = evaluate_fresh_shortlist(prepared, (False, False, False), workers=1)
    assert disabled.ready_candidate_ids == ("BAD-SHIFT",)


def test_min_shift_only_limits_the_first_order() -> None:
    from mrs3.fresh_shortlist import (
        FreshCandidateMetrics, FreshOrderMetrics, FreshScopeFacts, PreparedFreshAnalysis,
        evaluate_fresh_shortlist,
    )

    orders = (
        FreshOrderMetrics(3, 30, "P1", Decimal("1"), Decimal("1"), 1, 1),
        FreshOrderMetrics(4, 40, "P2", Decimal("1"), Decimal("1"), 1, 1),
    )
    candidate = FreshCandidateMetrics("C2", "S2", "BTCUSDT", "LONG", "1h", 9, 2, "READY_MRS3_STRUCTURE", orders, None)
    prepared = PreparedFreshAnalysis("c" * 64, "d" * 64, (FreshScopeFacts("BTCUSDT|LONG|1h", "BTCUSDT", "LONG", "1h", 1, None),), (candidate,))

    result = evaluate_fresh_shortlist(prepared, (False, False, False), workers=1, min_shift_enabled=True, min_shift_pct="0.300")

    assert result.ready_candidate_ids == ("C2",)


def test_min_shift_enabled_below_any_present_shift_preserves_disabled_membership() -> None:
    from mrs3.fresh_shortlist import (
        FreshCandidateMetrics, FreshOrderMetrics, FreshScopeFacts, PreparedFreshAnalysis,
        evaluate_fresh_shortlist,
    )

    candidate = FreshCandidateMetrics(
        "C3", "S3", "BTCUSDT", "LONG", "1h", 9, 2, "READY_MRS3_STRUCTURE",
        (FreshOrderMetrics(3, 30, "P1", Decimal("1"), Decimal("1"), 1, 1),
         FreshOrderMetrics(4, None, "P2", Decimal("1"), Decimal("1"), 1, 1)), None,
    )
    prepared = PreparedFreshAnalysis(
        "e" * 64, "f" * 64,
        (FreshScopeFacts("BTCUSDT|LONG|1h", "BTCUSDT", "LONG", "1h", 1, None),),
        (candidate,),
    )
    disabled = evaluate_fresh_shortlist(prepared, (False, False, False), workers=1)
    enabled = evaluate_fresh_shortlist(
        prepared, (False, False, False), workers=1,
        min_shift_enabled=True, min_shift_pct="0.001",
    )
    assert enabled.ready_candidate_ids == disabled.ready_candidate_ids == ("C3",)
    assert [(item.filter_status, item.reason, item.dominator_candidate_id) for item in enabled.candidates] == [
        (item.filter_status, item.reason, item.dominator_candidate_id) for item in disabled.candidates
    ]
    assert enabled.groups == disabled.groups
    assert enabled.filter_engine_version != disabled.filter_engine_version


def test_pareto_keeps_full_equality_and_uses_economic_strictness(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    import duckdb

    connection = duckdb.connect(str(database))
    try:
        row = json.loads(connection.execute("select payload_json from structures").fetchone()[0])
    finally:
        connection.close()
    equal = json.loads(json.dumps(row))
    equal["structure_id"] = "STR-EQUAL"
    equal["candidate_id"] = "STR-EQUAL"
    _append_structure(database, equal)
    worse = json.loads(json.dumps(row))
    worse["structure_id"] = "STR-WORSE"
    worse["candidate_id"] = "STR-WORSE"
    for order in worse["orders"]:
        order["source_pnl_pct"] = 9
        order["source_dd_pct"] = 2
    _append_structure(database, worse)

    result = evaluate_fresh_shortlist(
        prepare_fresh_shortlist(database, analysis_id), (False, False, True), workers=1,
    )

    assert result.ready_candidate_ids == ("STR-EQUAL", "STR-READY")
    deferred = next(item for item in result.candidates if item.candidate_id == "STR-WORSE")
    assert deferred.filter_status == "DEFERRED_PARETO"
    assert deferred.reason == "JOINT_PARETO_DOMINATED"
    assert deferred.dominator_candidate_id == "STR-EQUAL"


@pytest.mark.parametrize("value", [True, 1.5, "2", None, 5])
def test_prepare_rejects_nonintegral_or_wrong_type_order_counts(tmp_path: Path, value: object) -> None:
    from mrs3.fresh_shortlist import prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    import duckdb

    connection = duckdb.connect(str(database))
    try:
        row = json.loads(connection.execute("select payload_json from structures").fetchone()[0])
        row["order_count"] = value
        connection.execute("delete from structures")
        connection.execute(
            "insert into structures values (?, ?)",
            ["BTCUSDT|LONG|1h", json.dumps(row)],
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="order_count"):
        prepare_fresh_shortlist(database, analysis_id)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("open_ma", True, "open_ma"),
        ("open_ma", 3.5, "open_ma"),
        ("source_dd_pct", -0.01, "source_dd_pct"),
        ("plateau_point_count", 0, "plateau_point_count"),
        ("source_pnl_pct", float("nan"), "source_pnl_pct"),
    ],
)
def test_prepare_rejects_malformed_order_metrics(tmp_path: Path, field: str, value: object, message: str) -> None:
    from mrs3.fresh_shortlist import prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    structure = _read_structure(database)
    structure["orders"][0][field] = value
    _replace_structure(database, structure)

    with pytest.raises(ValueError, match=message):
        prepare_fresh_shortlist(database, analysis_id)


def test_prepare_rejects_order_shape_id_and_canonical_sequence_errors(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    structure = _read_structure(database)
    structure["orders"][0]["id"] = 2
    _replace_structure(database, structure)
    with pytest.raises(ValueError, match="order id"):
        prepare_fresh_shortlist(database, analysis_id)

    sequence_database = tmp_path / "sequence.analysis-v6.duckdb"
    sequence_id, _surface = _make_analysis(sequence_database)
    structure = _read_structure(sequence_database)
    structure["orders"] = list(reversed(structure["orders"]))
    for order in structure["orders"]:
        order.pop("id", None)
    _replace_structure(sequence_database, structure)
    with pytest.raises(ValueError, match="canonical sequence"):
        prepare_fresh_shortlist(sequence_database, sequence_id)


def test_pretest_evidence_is_required_for_v2_and_legacy_ab_requires_rebuild(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist
    import duckdb

    legacy = tmp_path / "legacy.analysis-v6.duckdb"
    legacy_id, _surface = _make_analysis(legacy, legacy=True)
    prepared = prepare_fresh_shortlist(legacy, legacy_id)
    assert evaluate_fresh_shortlist(prepared, (False, False, False), workers=1).ready_candidate_ids == ("STR-READY",)
    with pytest.raises(ValueError, match="PRETEST_AB_EVIDENCE_UNAVAILABLE"):
        evaluate_fresh_shortlist(prepared, (True, False, False), workers=1)

    current = tmp_path / "current.analysis-v6.duckdb"
    current_id, _surface = _make_analysis(current)
    connection = duckdb.connect(str(current))
    try:
        rows = connection.execute("select scope_key, payload_json from points").fetchall()
        for scope, raw in rows:
            point = json.loads(raw)
            if point["point_id"].endswith("|300|4|9"):
                point.pop("pretest_ab")
                connection.execute(
                    "update points set payload_json=? where scope_key=? and payload_json=?",
                    [json.dumps(point, sort_keys=True, separators=(",", ":")), scope, raw],
                )
    finally:
        connection.close()
    with pytest.raises(ValueError, match="missing required fields.*pretest_ab"):
        prepare_fresh_shortlist(current, current_id)


def test_pretest_ab_rejection_is_applied_before_ladder_and_pareto(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist
    import duckdb

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    connection = duckdb.connect(str(database))
    try:
        rows = connection.execute("select scope_key, payload_json from points").fetchall()
        for scope, raw in rows:
            point = json.loads(raw)
            if point["point_id"].endswith("|100|3|9"):
                point["pretest_ab"]["b_pnl"] = "0"
                point["pretest_ab"]["b_round_trips"] = 1
                connection.execute(
                    "update points set payload_json=? where scope_key=? and payload_json=?",
                    [json.dumps(point, sort_keys=True, separators=(",", ":")), scope, raw],
                )
    finally:
        connection.close()

    result = evaluate_fresh_shortlist(
        prepare_fresh_shortlist(database, analysis_id), (True, True, True), workers=1,
    )
    candidate = result.candidates[0]
    assert candidate.filter_status == "DEFERRED_PRETEST_AB"
    assert candidate.reason == "DECLINE_GT_THRESHOLD"
    assert candidate.pretest_ab_decline_pct == "100"


def test_all_eight_options_preserve_all_ready_and_deferred_count_invariants(tmp_path: Path) -> None:
    from itertools import product
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    _append_structure(database, {
        "structure_id": "STR-DEFERRED", "candidate_id": "STR-DEFERRED", "symbol": "BTCUSDT",
        "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 3,
        "status": "DEFERRED",
    })
    prepared = prepare_fresh_shortlist(database, analysis_id)

    for options in product((False, True), repeat=3):
        group = evaluate_fresh_shortlist(prepared, options, workers=1).groups[0]
        assert group.all_count == 2
        assert group.ready + group.deferred == group.all_count
        assert sum(group.order_counts) == group.ready
        assert group.order_counts == (0, 1, 0, 0)


def test_persisted_non_ready_modern_candidate_is_not_reported_as_legacy(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    structure = _read_structure(database)
    structure["status"] = "DEFERRED"
    _replace_structure(database, structure)

    result = evaluate_fresh_shortlist(
        prepare_fresh_shortlist(database, analysis_id), (False, False, False), workers=1,
    )

    candidate = result.candidates[0]
    assert candidate.filter_status == "DEFERRED_NOT_READY"
    assert candidate.pretest_ab_status == "DISABLED"
    assert candidate.pretest_ab_reason == "DISABLED"


def test_manifest_empty_scope_and_plateau_counts_are_scope_qualified(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist
    import duckdb

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    connection = duckdb.connect(str(database))
    try:
        manifest = {
            key: (json.loads(value) if value[:1] in ("{", "[", '\"') else value)
            for key, value in connection.execute("select key, value from manifest").fetchall()
        }
        manifest["scope_digests"]["ETHUSDT|LONG|1h"] = "e" * 64
        identity = dict(manifest)
        identity.pop("analysis_id")
        analysis_id = sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        manifest["analysis_id"] = analysis_id
        for key, value in manifest.items():
            encoded = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
            connection.execute("update manifest set value=? where key=?", [encoded, key])
        connection.execute("insert into scope_runs values (?, ?, ?)", ["ETHUSDT|LONG|1h", "e" * 64, "f" * 64])
        for _ in range(2):
            connection.execute(
                "insert into plateaus values (?, ?)",
                ["BTCUSDT|LONG|1h", json.dumps({"plateau_id": "PLATEAU-SHARED"})],
            )
        connection.execute(
            "insert into plateaus values (?, ?)",
            ["ETHUSDT|LONG|1h", json.dumps({"plateau_id": "PLATEAU-SHARED"})],
        )
    finally:
        connection.close()

    result = evaluate_fresh_shortlist(prepare_fresh_shortlist(database, analysis_id), (False, False, False), workers=1)
    assert [(group.scope_key, group.plateau_count, group.all_count) for group in result.groups] == [
        ("BTCUSDT|LONG|1h", 1, 1), ("ETHUSDT|LONG|1h", 1, 0),
    ]


def test_candidate_ids_are_normalized_and_collisions_rejected(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import prepare_fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    for identity, structure_id in ((1, "NUMERIC"), ("1", "STRING")):
        _append_structure(database, {
            "structure_id": structure_id, "candidate_id": identity, "symbol": "BTCUSDT",
            "side": "LONG", "timeframe": "1h", "common_close_ma": 9, "order_count": 1,
            "status": "DEFERRED",
        })
    with pytest.raises(ValueError, match="duplicate or colliding candidate identity: 1"):
        prepare_fresh_shortlist(database, analysis_id)


def test_manifest_scope_lineage_and_structure_payload_scope_must_agree(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import prepare_fresh_shortlist
    import duckdb

    database = tmp_path / "payload.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    structure = _read_structure(database)
    structure["symbol"] = "ETHUSDT"
    _replace_structure(database, structure)
    with pytest.raises(ValueError, match="structure scope disagrees"):
        prepare_fresh_shortlist(database, analysis_id)

    lineage = tmp_path / "lineage.analysis-v6.duckdb"
    lineage_id, _surface = _make_analysis(lineage)
    connection = duckdb.connect(str(lineage))
    try:
        connection.execute("update scope_runs set scope_digest=?", ["f" * 64])
    finally:
        connection.close()
    with pytest.raises(ValueError, match="scope_runs disagree"):
        prepare_fresh_shortlist(lineage, lineage_id)


def test_pareto_grouping_boundaries_and_non_economic_gain_keep_candidates(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import evaluate_fresh_shortlist, prepare_fresh_shortlist
    import duckdb

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    base = _read_structure(database)

    # A one-order structure is a separate Pareto group from the 2ORD candidate.
    one_order = json.loads(json.dumps(base))
    one_order["structure_id"] = "ONE-ORDER"
    one_order["candidate_id"] = "ONE-ORDER"
    one_order["order_count"] = 1
    one_order["orders"] = one_order["orders"][:1]
    _append_structure(database, one_order)

    # Better plateau size alone cannot dominate without strict PnL or DD gain.
    base["orders"][0]["plateau_point_count"] = 2
    base["orders"][1]["plateau_point_count"] = 2
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "update structures set payload_json=? where payload_json like '%STR-READY%'",
            [json.dumps(base, sort_keys=True, separators=(",", ":"))],
        )
    finally:
        connection.close()
    plateau_only = json.loads(json.dumps(base))
    plateau_only["structure_id"] = "PLATEAU-ONLY"
    plateau_only["candidate_id"] = "PLATEAU-ONLY"
    plateau_only["orders"][0]["plateau_point_count"] = 1
    plateau_only["orders"][1]["plateau_point_count"] = 1
    _append_structure(database, plateau_only)

    result = evaluate_fresh_shortlist(
        prepare_fresh_shortlist(database, analysis_id), (False, False, True), workers=1,
    )
    assert result.ready_candidate_ids == ("ONE-ORDER", "PLATEAU-ONLY", "STR-READY")

    # Different common close MAs never share one comparison group.
    different_cma = json.loads(json.dumps(base))
    different_cma["structure_id"] = "DIFFERENT-CMA"
    different_cma["candidate_id"] = "DIFFERENT-CMA"
    different_cma["common_close_ma"] = 10
    _append_structure(database, different_cma)
    after_cma = evaluate_fresh_shortlist(
        prepare_fresh_shortlist(database, analysis_id), (False, False, True), workers=1,
    )
    assert "DIFFERENT-CMA" in after_cma.ready_candidate_ids


def test_artifact_digest_streams_instead_of_reading_whole_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3.fresh_analysis_strategies import _file_digest

    path = tmp_path / "bytes.duckdb"
    content = b"content-bound-shortlist" * 100_000
    path.write_bytes(content)
    expected = sha256(content).hexdigest()

    def forbidden_read_bytes(_self: Path) -> bytes:
        raise AssertionError("whole-file read is forbidden")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)
    assert _file_digest(path) == expected


def test_executor_reuses_cached_snapshot_and_invalidates_same_size_content_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    table_reads = 0
    digest_calls = 0
    original_load = fresh_shortlist._load_table
    from mrs3 import fresh_analysis_strategies
    original_digest = fresh_analysis_strategies._file_digest

    def count_load(connection: object, table: str) -> object:
        nonlocal table_reads
        table_reads += 1
        return original_load(connection, table)  # type: ignore[arg-type]

    def count_digest(path: Path) -> str:
        nonlocal digest_calls
        digest_calls += 1
        return original_digest(path)

    monkeypatch.setattr(fresh_shortlist, "_load_table", count_load)
    monkeypatch.setattr(fresh_analysis_strategies, "_file_digest", count_digest)
    executor = fresh_shortlist.FreshShortlistExecutor()
    first = executor.evaluate(database, analysis_id, (False, False, False), workers=1)
    second = executor.evaluate(database, analysis_id, (False, False, False), workers=1)
    assert first is second
    assert table_reads == 3
    assert digest_calls == 3  # cold start+end, then one warm action-start digest

    import duckdb

    connection = duckdb.connect(str(database))
    try:
        row = json.loads(connection.execute("select payload_json from structures").fetchone()[0])
        old_size = database.stat().st_size
        for order in row["orders"]:
            order["source_pnl_pct"] = 11.0
        connection.execute("update structures set payload_json=?", [json.dumps(row, sort_keys=True, separators=(",", ":"))])
        new_size = database.stat().st_size
    finally:
        connection.close()
    assert old_size == new_size
    third = executor.evaluate(database, analysis_id, (False, False, False), workers=1)
    assert third.selection_token != first.selection_token
    assert table_reads == 6
    assert digest_calls == 5  # changed-content cold start+end


def test_executor_cache_is_bounded_and_small_cap_falls_back_without_changing_results(tmp_path: Path) -> None:
    from itertools import product
    from mrs3.fresh_shortlist import FreshShortlistExecutor

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    baseline = FreshShortlistExecutor(cache_limit_bytes=1)
    expected = baseline.evaluate(database, analysis_id, (False, False, False), workers=1)
    assert baseline.cache_info.prepared_bytes == 0
    assert baseline.cache_info.evaluation_count == 0

    bounded = FreshShortlistExecutor(cache_limit_bytes=2 * 1024 * 1024, evaluation_limit=3)
    for options in product((False, True), repeat=3):
        result = bounded.evaluate(database, analysis_id, options, workers=2)
        assert result.analysis_id == analysis_id
        assert len(result.selection_token) == 64
    assert bounded.cache_info.evaluation_count <= 3
    assert bounded.cache_info.total_bytes <= 2 * 1024 * 1024
    assert baseline.evaluate(database, analysis_id, (False, False, False), workers=4).selection_token == expected.selection_token


def test_executor_coalesces_four_identical_followers_and_rejects_the_fifth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    executor = fresh_shortlist.FreshShortlistExecutor()
    original_prepare = fresh_shortlist.prepare_fresh_shortlist
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def slow_prepare(path: Path, identity: str, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        return original_prepare(path, identity, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_shortlist, "prepare_fresh_shortlist", slow_prepare)
    with ThreadPoolExecutor(max_workers=6) as pool:
        leader = pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
        assert entered.wait(5)
        followers = [
            pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
            for _ in range(4)
        ]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with executor._condition:
                if executor._followers == 4:
                    break
            time.sleep(0.01)
        with executor._condition:
            assert executor._followers == 4
        rejected = pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
        with pytest.raises(fresh_shortlist.ShortlistBusyError) as busy:
            rejected.result(timeout=5)
        assert busy.value.code == "SHORTLIST_BUSY"
        assert busy.value.status_code == 409 and busy.value.retry_after == 1
        release.set()
        results = [leader.result(timeout=10), *(future.result(timeout=10) for future in followers)]
    assert calls == 1
    assert len({result.selection_token for result in results}) == 1


def test_executor_different_work_is_busy_and_identical_wait_timeout_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    first_path = tmp_path / "first.analysis-v6.duckdb"
    first_id, _surface = _make_analysis(first_path)
    second_path = tmp_path / "second.analysis-v6.duckdb"
    second_id, _surface = _make_analysis(second_path)
    executor = fresh_shortlist.FreshShortlistExecutor(wait_timeout_seconds=0.05)
    original_prepare = fresh_shortlist.prepare_fresh_shortlist
    entered, release = threading.Event(), threading.Event()

    def slow_prepare(path: Path, identity: str, **kwargs: object) -> object:
        entered.set()
        assert release.wait(5)
        return original_prepare(path, identity, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_shortlist, "prepare_fresh_shortlist", slow_prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(executor.evaluate, first_path, first_id, (False, False, False), workers=1)
        assert entered.wait(5)
        with pytest.raises(fresh_shortlist.ShortlistBusyError):
            executor.evaluate(second_path, second_id, (False, False, False), workers=1)
        with pytest.raises(fresh_shortlist.ShortlistBusyError):
            executor.evaluate(first_path, first_id, (False, False, False), workers=1)
        release.set()
        leader.result(timeout=10)
    assert executor.evaluate(first_path, first_id, (False, False, False), workers=1).selection_token


def test_executor_releases_singleflight_slot_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mrs3 import fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    original_prepare = fresh_shortlist.prepare_fresh_shortlist
    calls = 0

    def fail_once(path: Path, identity: str, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("fixture preparation error")
        return original_prepare(path, identity, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_shortlist, "prepare_fresh_shortlist", fail_once)
    executor = fresh_shortlist.FreshShortlistExecutor()
    with pytest.raises(ValueError, match="fixture preparation error"):
        executor.evaluate(database, analysis_id, (False, False, False), workers=1)
    result = executor.evaluate(database, analysis_id, (False, False, False), workers=1)
    assert result.ready_candidate_ids == ("STR-READY",)
    assert calls == 2


def test_executor_rejects_incomplete_cached_or_singleflight_state(tmp_path: Path) -> None:
    from mrs3.fresh_shortlist import FreshShortlistExecutor

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    options = (False, False, False)
    prepared, result = FreshShortlistExecutor().evaluate_with_prepared(
        database, analysis_id, options, workers=1,
    )
    prepared_key = (analysis_id, prepared.artifact_sha256, "prepared-v1")
    action_key = (prepared_key, options)

    broken_cache = FreshShortlistExecutor()
    broken_cache._evaluations[(prepared_key, options)] = (result, 1)
    with pytest.raises(RuntimeError, match="cache has no prepared analysis"):
        broken_cache.evaluate(database, analysis_id, options, workers=1)

    broken_singleflight = FreshShortlistExecutor()
    broken_singleflight._active_key = action_key
    broken_singleflight._active_done = True
    with pytest.raises(RuntimeError, match="single-flight completed without a result"):
        broken_singleflight.evaluate(database, analysis_id, options, workers=1)


def test_executor_does_not_share_process_control_exceptions_with_followers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    original_prepare = fresh_shortlist.prepare_fresh_shortlist
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def interrupt_once(path: Path, identity: str, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(5)
            raise SystemExit("leader interrupted")
        return original_prepare(path, identity, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_shortlist, "prepare_fresh_shortlist", interrupt_once)
    executor = fresh_shortlist.FreshShortlistExecutor()
    with ThreadPoolExecutor(max_workers=3) as pool:
        leader = pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
        assert entered.wait(5)
        followers = [
            pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
            for _ in range(2)
        ]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with executor._condition:
                if executor._followers == 2:
                    break
            time.sleep(0.005)
        with executor._condition:
            assert executor._followers == 2
        release.set()
        with pytest.raises(SystemExit, match="leader interrupted"):
            leader.result(timeout=10)
        follower_errors = [future.exception(timeout=10) for future in followers]
        assert all(isinstance(error, RuntimeError) for error in follower_errors)
        assert len({id(error) for error in follower_errors}) == 2
        assert all("leader interrupted" in str(error) for error in follower_errors)

    result = executor.evaluate(database, analysis_id, (False, False, False), workers=1)
    assert result.ready_candidate_ids == ("STR-READY",)
    assert calls == 2


def test_executor_preserves_invalid_artifact_error_class_for_followers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    database = tmp_path / "invalid.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    structure = _read_structure(database)
    structure["order_count"] = True
    _replace_structure(database, structure)

    original_prepare = fresh_shortlist.prepare_fresh_shortlist
    entered, release = threading.Event(), threading.Event()

    def blocked_prepare(path: Path, identity: str, **kwargs: object) -> object:
        entered.set()
        assert release.wait(5)
        return original_prepare(path, identity, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_shortlist, "prepare_fresh_shortlist", blocked_prepare)
    executor = fresh_shortlist.FreshShortlistExecutor()
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
        assert entered.wait(5)
        follower = pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with executor._condition:
                if executor._followers == 1:
                    break
            time.sleep(0.005)
        with executor._condition:
            assert executor._followers == 1
        release.set()
        leader_error = leader.exception(timeout=10)
        follower_error = follower.exception(timeout=10)

    assert isinstance(leader_error, ValueError)
    assert isinstance(follower_error, ValueError)
    assert follower_error is not leader_error
    assert str(follower_error) == str(leader_error)
    assert follower_error.__cause__ is leader_error


def test_executor_preserves_custom_multi_argument_error_for_followers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    class FixtureDomainError(Exception):
        def __init__(self, detail: dict[str, str], code: int) -> None:
            self.detail = detail
            self.code = code
            super().__init__(detail, code)

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    entered, release = threading.Event(), threading.Event()
    error_detail = {"stage": "prepare"}

    def blocked_prepare(path: Path, identity: str, **kwargs: object) -> object:
        entered.set()
        assert release.wait(5)
        raise FixtureDomainError(error_detail, 731)

    monkeypatch.setattr(fresh_shortlist, "prepare_fresh_shortlist", blocked_prepare)
    executor = fresh_shortlist.FreshShortlistExecutor()
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
        assert entered.wait(5)
        follower = pool.submit(executor.evaluate, database, analysis_id, (False, False, False), workers=1)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with executor._condition:
                if executor._followers == 1:
                    break
            time.sleep(0.005)
        with executor._condition:
            assert executor._followers == 1
        release.set()
        leader_error = leader.exception(timeout=10)
        follower_error = follower.exception(timeout=10)

    assert isinstance(leader_error, FixtureDomainError)
    assert isinstance(follower_error, FixtureDomainError)
    assert follower_error is not leader_error
    assert follower_error.args == leader_error.args
    assert follower_error.detail == error_detail
    assert follower_error.code == 731
    assert follower_error.__cause__ is leader_error


def test_shortlist_busy_error_uses_code_as_message() -> None:
    from mrs3.fresh_shortlist import ShortlistBusyError

    assert str(ShortlistBusyError()) == "SHORTLIST_BUSY"


def test_cold_preparation_rejects_artifact_mutation_before_publication_of_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist
    import duckdb

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    original_prepare = fresh_shortlist.prepare_fresh_shortlist
    changed = False

    def mutate_then_prepare(path: Path, identity: str, **kwargs: object) -> object:
        nonlocal changed
        if not changed:
            changed = True
            connection = duckdb.connect(str(path))
            try:
                row = json.loads(connection.execute("select payload_json from structures").fetchone()[0])
                for order in row["orders"]:
                    order["source_pnl_pct"] = 11.0
                connection.execute(
                    "update structures set payload_json=?",
                    [json.dumps(row, sort_keys=True, separators=(",", ":"))],
                )
            finally:
                connection.close()
        return original_prepare(path, identity, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_shortlist, "prepare_fresh_shortlist", mutate_then_prepare)
    executor = fresh_shortlist.FreshShortlistExecutor()
    with pytest.raises(ValueError, match="changed during shortlist preparation"):
        executor.evaluate(database, analysis_id, (False, False, False), workers=1)
    assert executor.cache_info.prepared_bytes == 0


def test_numpy_pareto_matches_decimal_oracle_and_worker_count_does_not_change_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    base = _read_structure(database)
    for index, pnl in enumerate((11.0, 10.0, 9.0, 9.0), start=1):
        candidate = json.loads(json.dumps(base))
        candidate["structure_id"] = f"CMA9-{index}"
        candidate["candidate_id"] = f"CMA9-{index}"
        for order in candidate["orders"]:
            order["source_pnl_pct"] = pnl
        _append_structure(database, candidate)
        other_group = json.loads(json.dumps(candidate))
        other_group["structure_id"] = f"CMA10-{index}"
        other_group["candidate_id"] = f"CMA10-{index}"
        other_group["common_close_ma"] = 10
        _append_structure(database, other_group)

    prepared = fresh_shortlist.prepare_fresh_shortlist(database, analysis_id)
    same_group = [item for item in prepared.candidates if item.common_close_ma == 9]
    numpy_result = fresh_shortlist._pareto_group_numpy(
        tuple(reversed(same_group)), temporary_array_budget_bytes=1024 * 1024,
    )
    decimal_result = fresh_shortlist._pareto_group_decimal(same_group)
    assert numpy_result == decimal_result

    monkeypatch.setattr(fresh_shortlist, "_MIN_PARALLEL_PARETO_WORK", 0)
    serial = fresh_shortlist.evaluate_fresh_shortlist(prepared, (False, False, True), workers=1)
    parallel = fresh_shortlist.evaluate_fresh_shortlist(prepared, (False, False, True), workers=4)
    assert serial == parallel


def test_numpy_temp_budget_is_divided_across_workers_and_fallback_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mrs3 import fresh_shortlist

    database = tmp_path / "analysis.analysis-v6.duckdb"
    analysis_id, _surface = _make_analysis(database)
    base = _read_structure(database)
    for group_number in (1, 2):
        for index in range(2):
            candidate = json.loads(json.dumps(base))
            candidate["structure_id"] = f"G{group_number}-{index}"
            candidate["candidate_id"] = f"G{group_number}-{index}"
            candidate["common_close_ma"] = 9 + group_number
            _append_structure(database, candidate)
    prepared = fresh_shortlist.prepare_fresh_shortlist(database, analysis_id)
    monkeypatch.setattr(fresh_shortlist, "_MIN_PARALLEL_PARETO_WORK", 0)
    budgets: list[int] = []
    original_numpy = fresh_shortlist._pareto_group_numpy

    def record_budget(group: object, *, temporary_array_budget_bytes: int) -> object:
        budgets.append(temporary_array_budget_bytes)
        return original_numpy(group, temporary_array_budget_bytes=temporary_array_budget_bytes)  # type: ignore[arg-type]

    monkeypatch.setattr(fresh_shortlist, "_pareto_group_numpy", record_budget)
    constrained = fresh_shortlist.evaluate_fresh_shortlist(
        prepared, (False, False, True), workers=2, temporary_array_budget_bytes=120,
    )
    assert max(budgets) <= 60
    budgets.clear()
    exact = fresh_shortlist.evaluate_fresh_shortlist(
        prepared, (False, False, True), workers=1, temporary_array_budget_bytes=1024 * 1024,
    )
    assert budgets and max(budgets) == 1024 * 1024
    assert constrained.ready_candidate_ids == exact.ready_candidate_ids
    assert constrained.selection_token == exact.selection_token
