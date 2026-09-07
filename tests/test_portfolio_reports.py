from __future__ import annotations

from decimal import Decimal
import json

import pytest

from mrs3.portfolio.reports import (
    ReportNormalizationError,
    compare_semantic_results,
    normalize_report,
)


def fixture_report(*, actions=None, equity=True, run_id="run", attempt_id="attempt", member="A"):
    actions = actions or [
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "2"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "CLOSE", "side": "LONG", "size": "2", "pnl": "4"},
    ]
    value = {
        "schema": "portfolio_report_v1", "version": 1,
        "identity": {"run_id": run_id, "attempt_id": attempt_id, "member": member},
        "action_count": len(actions),
        "period": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:02Z"},
        "actions": actions,
        "series": {"equity": [
            {"timestamp": "2026-01-01T00:00:00Z", "value": "100"},
            {"timestamp": "2026-01-01T00:00:02Z", "value": "103"},
        ]} if equity else {},
    }
    return value


def test_sanitized_fixture_preserves_decimal_order_and_unavailable_series():
    report = normalize_report(fixture_report())
    assert report.actions[0].size == Decimal("2")
    assert report.actions[1].source_ordinal == 1
    assert report.series["wallet"] == ()
    assert report.available_series == ("equity",)
    assert report.cycles[0].realized_pnl == Decimal("4")
    assert report.cycles[0].maximum_position == Decimal("2")
    assert report.cycles[0].execution_count == 2


@pytest.mark.parametrize("change", [
    lambda value: value.update(action_count=1),
    lambda value: value["identity"].update(member="B"),
])
def test_malformed_count_and_identity_fail_closed(change):
    value = fixture_report()
    change(value)
    with pytest.raises(ReportNormalizationError):
        normalize_report(value, member="A")


def test_unknown_physical_report_requires_injected_decoder():
    with pytest.raises(ReportNormalizationError, match="decoder") as raised:
        normalize_report(b"unknown html")
    assert raised.value.code == "Q06_BLOCKING_UNKNOWN"
    assert raised.value.capability_result == "BLOCKING_UNKNOWN"


def test_sanitized_structured_fixture_bytes_are_accepted():
    raw = json.dumps(fixture_report(), separators=(",", ":")).encode()
    report = normalize_report(raw)
    assert report.raw_digest


def test_decoder_keeps_raw_digest_separate_from_semantic_digest():
    value = fixture_report()
    first = normalize_report(b"one", decoder=lambda _: value, source_report_name="A.html")
    second = normalize_report(b"two", decoder=lambda _: value, source_report_name="A.html")
    assert first.raw_digest != second.raw_digest
    assert first.semantic_digest == second.semantic_digest
    assert compare_semantic_results(first, second, executable_identity={"binary": "fixture"}) == "DETERMINISTIC"


def test_interleaved_reductions_and_additions_are_one_cycle():
    actions = [
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "2"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "INCREASE", "side": "LONG", "size": "3"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "REDUCE", "side": "LONG", "size": "1"},
        {"timestamp": "2026-01-01T00:00:02Z", "symbol": "BTCUSDT", "action": "CLOSE", "side": "LONG", "size": "4"},
    ]
    report = normalize_report(fixture_report(actions=actions))
    cycle = report.cycles[0]
    assert (cycle.maximum_position, cycle.execution_count, cycle.closed_at) == (Decimal("5"), 4, "2026-01-01T00:00:02.000000Z")


def test_censored_cycle_and_missing_optional_fields_are_explicit():
    actions = [{"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "1"}]
    report = normalize_report(fixture_report(actions=actions))
    cycle = report.cycles[0]
    assert cycle.censored and cycle.closed_at is None
    assert "OPEN_AT_END" in cycle.diagnostics
    assert cycle.fees is None
    assert report.actions[0].price is None and report.actions[0].fee is None and report.actions[0].funding is None


def test_same_timestamp_uses_source_ordinal():
    actions = [
        {"timestamp": "2026-01-01T00:00:00Z", "source_ordinal": 2, "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "1"},
        {"timestamp": "2026-01-01T00:00:00Z", "source_ordinal": 1, "symbol": "BTCUSDT", "action": "CLOSE", "side": "LONG", "size": "1"},
    ]
    report = normalize_report(fixture_report(actions=actions))
    assert tuple(action.source_ordinal for action in report.actions) == (1, 2)


def test_mixed_timestamp_precision_has_fixed_utc_ordering_at_boundaries():
    raw = fixture_report()
    raw["period"] = {"start": "2026-01-01T01:00:00+01:00", "end": "2026-01-01T00:00:02.000001Z"}
    raw["actions"][1]["timestamp"] = "2026-01-01T00:00:01.000000Z"
    report = normalize_report(raw)
    assert report.report_start == "2026-01-01T00:00:00.000000Z"
    assert report.report_end == "2026-01-01T00:00:02.000001Z"
    assert report.actions[0].timestamp_utc < report.actions[1].timestamp_utc


def test_leading_close_is_unknown_carry_in_without_phantom_short():
    from mrs3.portfolio.metrics import calculate_metrics

    raw = fixture_report(actions=[
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "CLOSE", "size": "1"},
    ])
    report = normalize_report(raw)
    cycle = report.cycles[0]
    assert cycle.carry_in and cycle.side is None and cycle.closed_at == "2026-01-01T00:00:00.000000Z"
    assert "CARRY_IN_DIRECTION_UNKNOWN" in cycle.diagnostics
    assert calculate_metrics(report).actual_concurrency == 0


@pytest.mark.parametrize("field", ["parser_version", "metrics_version"])
def test_report_provenance_version_must_match_normalizer(field):
    raw = fixture_report()
    raw[field] = "untrusted-spoof"
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


def test_report_source_name_uses_caller_provenance():
    raw = fixture_report()
    raw["source_report_name"] = "untrusted-name.html"
    report = normalize_report(raw, source_report_name="actual-name.html")
    assert report.source_report_name == "actual-name.html"
    assert report.parser_version == "portfolio_report_parser_v1"
    assert report.metrics_version == "portfolio_report_metrics_v1"


@pytest.mark.parametrize("extra", [
    {"extra_fact": []},
    {"series": {"unmapped_curve": [["2026-01-01T00:00:00Z", "1"]]}},
])
def test_unknown_versioned_report_fields_fail_closed(extra):
    raw = fixture_report()
    raw.update(extra)
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


def test_reversal_bounds_terminating_cycle_and_marks_both_cycles():
    raw = fixture_report(actions=[
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "1"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "SHORT", "size": "6"},
    ])
    report = normalize_report(raw)
    old, new = report.cycles
    assert old.side == "LONG" and old.maximum_position == Decimal("1")
    assert old.close_attribution == "UNKNOWN"
    assert "UNEXPECTED_REVERSAL" in old.diagnostics
    assert new.side == "SHORT" and new.maximum_position == Decimal("5")
    assert "UNEXPECTED_REVERSAL" in new.diagnostics


@pytest.mark.parametrize("action", [
    {"action": "MYSTERY", "side": "LONG", "size": "1"},
    {"action": "OPEN", "side": "DIAGONAL", "size": "1"},
])
def test_ambiguous_action_vocab_fails_closed(action):
    raw = fixture_report(actions=[{"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", **action}])
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


@pytest.mark.parametrize("field", ["post_side", "pre_side"])
def test_unknown_derived_action_side_fails_closed(field):
    raw = fixture_report(actions=[{
        "timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT",
        "action": "OPEN", "side": "LONG", "size": "1", field: "DIAGONAL",
    }])
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


def test_multiple_action_aliases_fail_closed():
    raw = fixture_report(actions=[{
        "timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT",
        "action": "OPEN", "kind": "OPEN", "side": "LONG", "size": "1",
    }])
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


def test_multiple_summary_aliases_fail_closed():
    raw = fixture_report()
    raw["portfolio"] = {"realized_pnl": "4", "realised_pnl": "4"}
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


def test_normalized_series_name_collision_fails_closed():
    raw = fixture_report()
    raw["series"]["Equity"] = raw["series"]["equity"]
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


@pytest.mark.parametrize("availability", ["PARTIAL", "ESTIMATED", "STALE"])
def test_unknown_series_availability_fails_closed(availability):
    raw = fixture_report()
    raw["series"]["equity"][0]["availability"] = availability
    with pytest.raises(ReportNormalizationError) as raised:
        normalize_report(raw)
    assert raised.value.code == "REPORT_SCHEMA_INVALID"


def test_reversal_counts_one_execution_and_marks_ambiguous_attribution():
    raw = fixture_report(actions=[
        {"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "LONG", "size": "1"},
        {"timestamp": "2026-01-01T00:00:01Z", "symbol": "BTCUSDT", "action": "OPEN", "side": "SHORT", "size": "6"},
    ])
    report = normalize_report(raw)
    assert sum(cycle.execution_count for cycle in report.cycles) == report.action_count
    assert all("REVERSAL_ATTRIBUTION_AMBIGUOUS" in cycle.diagnostics for cycle in report.cycles)


def test_semantic_comparison_requires_same_known_executable_identity():
    first = normalize_report(fixture_report())
    second = normalize_report(fixture_report())
    assert compare_semantic_results(first, second) == "UNKNOWN"
    assert compare_semantic_results(first, second, first_executable_identity={"binary": "a"}, second_executable_identity={"binary": "b"}) == "UNKNOWN"
    assert compare_semantic_results(first, second, first_executable_identity={"binary": "a"}, second_executable_identity={"binary": "a"}) == "DETERMINISTIC"
