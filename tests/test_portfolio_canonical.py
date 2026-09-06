from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mrs3.portfolio.canonical import (
    MISSING,
    CanonicalEnvelope,
    canonical_bytes_v1,
    canonical_digest_v1,
    canonical_json_v1,
    decimal_value,
    enum_value,
    list_value,
    timestamp_value,
    typed_value,
    unknown_value,
    PORTFOLIO_CAPABILITY_RESULT_V1,
    PORTFOLIO_DISPOSITION_V1,
    PORTFOLIO_EVIDENCE_CLASS_V1,
    PORTFOLIO_GATE_RESULT_V1,
    PORTFOLIO_REASON_V1,
)


def _envelope(payload, *, schema_id="test", schema_version=1, type_tag="fixture"):
    return CanonicalEnvelope(
        schema_id=schema_id,
        schema_version=schema_version,
        type_tag=type_tag,
        unit_tag="1",
        payload=payload,
    )


def test_campaign_golden_vector_is_literal_and_key_order_independent():
    left = _envelope(
        {
            "name": "campaign-1",
            "config": typed_value("config", {"z": 2, "a": 1}, unit="1"),
            "universe": list_value(
                [
                    {"identity": typed_value("candidate", "ETHUSDT", unit="symbol"), "side": "SHORT"},
                    {"identity": typed_value("candidate", "BTCUSDT", unit="symbol"), "side": "LONG"},
                ],
                ordering="identity",
            ),
        },
        schema_id="portfolio_campaign",
        type_tag="campaign_snapshot",
    )
    right = _envelope(
        {
            "universe": list_value(
                [
                    {"side": "LONG", "identity": typed_value("candidate", "BTCUSDT", unit="symbol")},
                    {"identity": typed_value("candidate", "ETHUSDT", unit="symbol"), "side": "SHORT"},
                ],
                ordering="identity",
            ),
            "config": typed_value("config", {"a": 1, "z": 2}, unit="1"),
            "name": "campaign-1",
        },
        schema_id="portfolio_campaign",
        type_tag="campaign_snapshot",
    )
    expected = b'{"digest_contract":"canonical_digest_v1","payload":{"type":"campaign_snapshot","unit":"1","value":{"config":{"type":"config","unit":"1","value":{"a":1,"z":2}},"name":"campaign-1","universe":[{"identity":{"type":"candidate","unit":"symbol","value":"BTCUSDT"},"side":"LONG"},{"identity":{"type":"candidate","unit":"symbol","value":"ETHUSDT"},"side":"SHORT"}]}},"presentation_exclusions":[],"schema":{"id":"portfolio_campaign","version":1}}'
    expected_digest = "86186806756310aa9df36a67b1d3cdfaf6f4277a4f562d552fd549120611a029"
    assert canonical_bytes_v1(left) == expected
    assert canonical_bytes_v1(right) == expected
    assert canonical_digest_v1(left) == expected_digest
    assert canonical_digest_v1(right) == expected_digest


def test_semantic_result_golden_vector_orders_actions_and_series():
    value = {
        "actions": list_value(
            [
                {
                    "timestamp_utc": timestamp_value(datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc), precision="seconds"),
                    "source_ordinal": 2,
                    "kind": "close",
                },
                {
                    "timestamp_utc": timestamp_value(datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc), precision="seconds"),
                    "source_ordinal": 1,
                    "kind": "open",
                },
            ],
            ordering="timestamp_ordinal",
        ),
        "series": list_value(
            [
                {
                    "timestamp_utc": timestamp_value(datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc), precision="seconds"),
                    "source_ordinal": 1,
                    "equity": decimal_value("100.00", scale=2, unit="USDT"),
                }
            ],
            ordering="timestamp_ordinal",
        ),
    }
    expected = b'{"digest_contract":"canonical_digest_v1","payload":{"type":"semantic_result","unit":"1","value":{"actions":[{"kind":"open","source_ordinal":1,"timestamp_utc":{"precision":"seconds","type":"timestamp","unit":"UTC","value":"2025-12-31T23:59:59Z"}},{"kind":"close","source_ordinal":2,"timestamp_utc":{"precision":"seconds","type":"timestamp","unit":"UTC","value":"2026-01-01T00:00:01Z"}}],"series":[{"equity":{"rounding":null,"scale":2,"type":"decimal","unit":"USDT","value":"100.00"},"source_ordinal":1,"timestamp_utc":{"precision":"seconds","type":"timestamp","unit":"UTC","value":"2026-01-01T00:00:01Z"}}]}},"presentation_exclusions":[],"schema":{"id":"semantic_result","version":1}}'
    expected_digest = "89dde27e722ca28d0a46a669353b0be042c320929536ccb01261e4cf8759bc65"
    assert canonical_bytes_v1(_envelope(value, schema_id="semantic_result", type_tag="semantic_result")) == expected
    assert canonical_digest_v1(_envelope(value, schema_id="semantic_result", type_tag="semantic_result")) == expected_digest


def test_portfolio_set_golden_vector_orders_loads():
    value = {
        "members": list_value(
            [
                {"identity": typed_value("member", "b", unit="1")},
                {"identity": typed_value("member", "a", unit="1")},
            ],
            ordering="identity",
        ),
        "loads": list_value(
            [
                {"symbol": "ETHUSDT", "direction": "SHORT", "member_identity": "b", "weight": 2},
                {"symbol": "BTCUSDT", "direction": "LONG", "member_identity": "a", "weight": 1},
            ],
            ordering="load",
        ),
    }
    expected = b'{"digest_contract":"canonical_digest_v1","payload":{"type":"portfolio_set","unit":"1","value":{"loads":[{"direction":"LONG","member_identity":"a","symbol":"BTCUSDT","weight":1},{"direction":"SHORT","member_identity":"b","symbol":"ETHUSDT","weight":2}],"members":[{"identity":{"type":"member","unit":"1","value":"a"}},{"identity":{"type":"member","unit":"1","value":"b"}}]}},"presentation_exclusions":[],"schema":{"id":"portfolio_set","version":1}}'
    expected_digest = "54e240d8fe9bd2c2d727afe29421f8ba0c5469c25b2c906dd0e15e1f26725249"
    envelope = _envelope(value, schema_id="portfolio_set", type_tag="portfolio_set")
    assert canonical_bytes_v1(envelope) == expected
    assert canonical_digest_v1(envelope) == expected_digest


def test_types_units_unknown_reason_missing_and_null_change_digest():
    base = {"value": typed_value("quantity", 1, unit="contracts")}
    missing = _envelope({**base, "optional": MISSING})
    null = _envelope({**base, "optional": None})
    unknown = _envelope({**base, "optional": unknown_value("LIQUIDITY_MISSING")})
    changed_unit = _envelope({"value": typed_value("quantity", 1, unit="USDT")})
    changed_type = _envelope({"value": typed_value("price", 1, unit="contracts")})
    changed_reason = _envelope({**base, "optional": unknown_value("LIQUIDITY_STALE")})
    assert len({canonical_digest_v1(v) for v in (missing, null, unknown)}) == 3
    assert canonical_digest_v1(_envelope(base)) != canonical_digest_v1(changed_unit)
    assert canonical_digest_v1(_envelope(base)) != canonical_digest_v1(changed_type)
    assert canonical_digest_v1(unknown) != canonical_digest_v1(changed_reason)


def test_timestamp_is_utc_and_precision_is_schema_declared():
    utc = timestamp_value(datetime(2026, 1, 1, 2, 0, 0, tzinfo=timezone.utc), precision="seconds")
    plus_two = timestamp_value(datetime(2026, 1, 1, 3, 0, 0, tzinfo=timezone(timedelta(hours=1))), precision="seconds")
    assert canonical_json_v1(_envelope({"at": utc})) == canonical_json_v1(_envelope({"at": plus_two}))
    with pytest.raises(ValueError):
        timestamp_value(datetime(2026, 1, 1, 0, 0, 0, 1, tzinfo=timezone.utc), precision="seconds")
    with pytest.raises(ValueError):
        timestamp_value(datetime(2026, 1, 1, 0, 0, 0), precision="seconds")


def test_decimal_never_uses_float_and_rounding_is_explicit():
    assert '"value":"1.23"' in canonical_json_v1(_envelope({"amount": decimal_value(Decimal("1.23"), scale=2, unit="USDT")}))
    with pytest.raises(TypeError):
        decimal_value(1.23, scale=2, unit="USDT")
    with pytest.raises(TypeError):
        canonical_json_v1(_envelope({"amount": 1.23}))
    with pytest.raises(ValueError):
        canonical_json_v1(_envelope({"amount": decimal_value("1.239", scale=2, unit="USDT")}))
    rounded = decimal_value("1.239", scale=2, rounding="ROUND_HALF_UP", unit="USDT")
    assert '"value":"1.24"' in canonical_json_v1(_envelope({"amount": rounded}))
    with pytest.raises(ValueError):
        decimal_value("1.239", scale=2, rounding="ROUND_NOT_A_MODE", unit="USDT")


def test_source_ordinal_is_required_for_time_ordered_lists():
    with pytest.raises(ValueError):
        canonical_json_v1(_envelope({"items": list_value(
            [{"timestamp_utc": timestamp_value(datetime(2026, 1, 1, tzinfo=timezone.utc), precision="seconds")}],
            ordering="timestamp_ordinal",
        )}))


def test_source_ordinal_changes_digest_and_duplicate_order_keys_fail():
    action = {"timestamp_utc": timestamp_value(datetime(2026, 1, 1, tzinfo=timezone.utc), precision="seconds"), "source_ordinal": 1, "kind": "open"}
    same_time_different_ordinal = {**action, "source_ordinal": 2}
    first = _envelope({"actions": list_value([action], ordering="timestamp_ordinal")})
    second = _envelope({"actions": list_value([same_time_different_ordinal], ordering="timestamp_ordinal")})
    assert canonical_digest_v1(first) != canonical_digest_v1(second)
    duplicate = _envelope({"actions": list_value([action, dict(action)], ordering="timestamp_ordinal")})
    with pytest.raises(ValueError):
        canonical_json_v1(duplicate)


def test_enum_sets_are_exact_versioned_contracts():
    assert PORTFOLIO_DISPOSITION_V1 == frozenset({
        "RESEARCH_ONLY", "RECOMMENDATION_READY", "NEEDS_RETEST",
        "NEEDS_RESCREEN", "INSUFFICIENT_EVIDENCE", "NONDETERMINISTIC_RESULT",
    })
    assert PORTFOLIO_GATE_RESULT_V1 == frozenset({"PASS", "FAIL", "UNKNOWN"})
    assert PORTFOLIO_EVIDENCE_CLASS_V1 == frozenset({"OBSERVED", "CALCULATED", "CONSERVATIVE_BOUND", "COARSE_ESTIMATE", "UNKNOWN"})
    assert PORTFOLIO_CAPABILITY_RESULT_V1 == frozenset({"CONFIRMED_CAPABILITY", "APPROVED_CONSERVATIVE_BOUND", "BLOCKING_UNKNOWN"})
    assert PORTFOLIO_REASON_V1 == frozenset({
        "TURNOVER_MISSING", "TURNOVER_STALE", "TURNOVER_REQUEST_FAILED",
        "LIQUIDITY_MISSING", "LIQUIDITY_STALE", "LIQUIDITY_QUALITY_INSUFFICIENT",
        "FEE_RATE_UNKNOWN", "SIZING_ENVELOPE_UNBOUNDED",
        "EQUITY_DENOMINATOR_INVALID", "EQUITY_PATH_MISSING", "EQUITY_COVERAGE_INSUFFICIENT",
        "LEVERAGE_MISMATCH", "POST_ROUNDING_MINIMUM", "POST_ROUNDING_GEOMETRY",
        "ENUMERATION_FALLBACK_USED", "MARGIN_BOUND_FAILED", "MARGIN_BOUND_UNAVAILABLE",
        "VALIDATION_FAILED", "NO_VALIDATION_PASS", "SEMANTIC_RESULT_DIVERGENCE",
        "EXECUTABLE_PAYLOAD_CHANGED", "PORTFOLIO_SET_CHANGED", "OPEN_POLICY",
        "LOCK_OWNER_UNVERIFIABLE", "LOCK_MANUAL_CLEAR",
    })
    assert enum_value("portfolio_gate_result_v1", "PASS")
    with pytest.raises(ValueError):
        enum_value("portfolio_gate_result_v1", "MAYBE")
