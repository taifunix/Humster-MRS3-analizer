from __future__ import annotations

import pytest

from mrs3.portfolio.live_charts import (
    AVAILABLE,
    CHART_OVERVIEW_VERSION,
    ExactChartPoint,
    MAX_PAIR_OPTIONS,
    MAX_RAW_FACTS,
    LIVE,
    PAIR,
    PORTFOLIO,
    ResourceBoundExceeded,
    UNKNOWN,
    TESTED,
    aggregate_elapsed_bucket,
    build_chart_model,
    build_chart_overview,
    canonical_chart_bytes,
    handle_chart_request,
)


def manifest() -> dict[str, object]:
    return {
        "manifest_id": "manifest-1",
        "portfolio_set": "set-1",
        "evaluation": "eval-1",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "semantic_digest": "a" * 64,
        "series_version": "series-1",
        "metrics_version": "metrics-1",
        "currency": "USDT",
        "unit": "USDT",
        "time_basis": "UTC",
        "applicability": "portfolio",
        "margin_freshness_seconds": "60",
        "member_composition": [
            {"strategy_id": "strategy-1", "symbol": "BTCUSDT", "side": "LONG"}
        ],
    }


def baseline(*, series: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "portfolio_set": "set-1",
        "evaluation": "eval-1",
        "semantic_digest": "a" * 64,
        "series_version": "series-1",
        "metrics_version": "metrics-1",
        "member_composition": manifest()["member_composition"],
        "series": series
        or {
            "equity": {
                "unit": "USDT",
                "currency": "USDT",
                "time_basis": "UTC",
                "applicability": "portfolio",
                "points": [
                    {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "100.00"},
                    {"timestamp_utc": "2026-09-07T00:00:02Z", "value": "97.50"},
                    {"timestamp_utc": "2026-09-07T00:00:04Z", "value": "101.25"},
                ],
            }
        },
    }


class Reader:
    def __init__(self, value: dict[str, object]):
        self.value = value
        self.calls: list[tuple[str, str]] = []

    def read_portfolio_run(self, run_id: str, attempt_id: str) -> dict[str, object]:
        self.calls.append((run_id, attempt_id))
        return self.value


def test_pinned_reader_and_tested_only_equity_pnl_and_dd_are_chart_ready() -> None:
    result = build_chart_model(Reader(baseline()), manifest(), view="equity")
    assert result.availability == AVAILABLE
    assert result.source == TESTED
    assert result.points[0]["timestamp_utc"] == "2026-09-07T00:00:00Z"
    assert result.points[1]["elapsed_from_start"] == "2"
    assert result.points[1]["value"] == "97.50"
    assert result.points[0]["provenance"]["semantic_digest"] == "a" * 64

    pnl = build_chart_model(Reader(baseline()), manifest(), view="net_pnl")
    assert [point["value"] for point in pnl.points] == ["0.00", "-2.50", "1.25"]
    dd = build_chart_model(Reader(baseline()), manifest(), view="drawdown")
    assert [point["value"] for point in dd.points] == ["0.00", "-2.50", "0.00"]


def test_margin_load_requires_explicit_denominator_and_keeps_decimal_strings() -> None:
    data = baseline(
        series={
            "initial_margin": {"points": [("2026-09-07T00:00:00Z", "10.00")]},
            "maintenance_margin": {"points": [("2026-09-07T00:00:00Z", "5.00")]},
        }
    )
    missing = build_chart_model(Reader(data), manifest(), view="margin_load")
    assert missing.availability == UNKNOWN
    assert missing.points == ()

    data["series"] = {
        "initial_margin": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "10.00")]},
        "maintenance_margin": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "5.00")]},
        "margin_balance": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "100.00")]},
    }
    available = build_chart_model(Reader(data), manifest(), view="margin_load")
    assert available.availability == AVAILABLE
    assert [point["value"] for point in available.points] == ["10", "5"]
    assert {point["provenance"]["denominator"] for point in available.points} == {"margin_balance"}
    assert {point["provenance"]["unit"] for point in available.points} == {"%"}
    assert all("currency" not in point["provenance"] for point in available.points)
    assert {point["provenance"]["denominator_basis"] for point in available.points} == {"same_timestamp"}

    combined = build_chart_model(Reader(data), manifest(), view="margin_load")
    assert [point["provenance"]["metric"] for point in combined.points] == ["IM", "MM"]


def test_pinned_digest_version_and_member_composition_reject_drift() -> None:
    for key, value in (
        ("semantic_digest", "b" * 64),
        ("metrics_version", "metrics-2"),
        ("member_composition", [{"strategy_id": "other", "symbol": "ETHUSDT", "side": "LONG"}]),
    ):
        data = baseline()
        data[key] = value
        result = build_chart_model(Reader(data), manifest(), view="equity")
        assert result.availability == UNKNOWN
        assert result.reason == "BASELINE_PIN_MISMATCH"


def test_compatible_overlay_and_incompatible_overlay_are_fail_closed() -> None:
    live = {
        "unit": "USDT",
        "currency": "USDT",
        "time_basis": "UTC",
        "applicability": "portfolio",
        "points": [{"timestamp_utc": "2026-09-07T00:00:06Z", "value": "102.00"}],
    }
    result = build_chart_model(Reader(baseline()), manifest(), view="equity", live=live, overlay=True)
    assert result.availability == AVAILABLE
    assert [point["source"] for point in result.points] == [TESTED, TESTED, TESTED, LIVE]

    incompatible = {**live, "currency": "BTC"}
    result = build_chart_model(Reader(baseline()), manifest(), view="equity", live=incompatible, overlay=True)
    assert result.availability == UNKNOWN
    assert result.reason == "OVERLAY_INCOMPATIBLE"
    assert result.tested_points and result.live_points

    missing_provenance = {"points": live["points"]}
    result = build_chart_model(Reader(baseline()), manifest(), view="equity", live=missing_provenance, overlay=True)
    assert result.availability == UNKNOWN
    assert result.reason == "OVERLAY_INCOMPATIBLE"


def test_live_overlay_derives_pnl_and_high_water_drawdown() -> None:
    live = {
        "unit": "USDT",
        "currency": "USDT",
        "time_basis": "UTC",
        "applicability": "portfolio",
        "points": [
            {"timestamp_utc": "2026-09-07T00:00:06Z", "value": "103.00"},
            {"timestamp_utc": "2026-09-07T00:00:08Z", "value": "99.00"},
        ],
    }
    pnl = build_chart_model(Reader(baseline()), manifest(), view="net_pnl", live=live, overlay=True)
    assert [point["value"] for point in pnl.live_points] == ["3.00", "-1.00"]
    assert [point["elapsed_from_start"] for point in pnl.live_points] == ["6", "8"]
    assert {point["provenance"]["derived_basis"] for point in pnl.live_points} == {"TESTED_BASELINE_ORIGIN"}
    assert [point["elapsed_from_start"] for point in pnl.points] == ["0", "2", "4", "6", "8"]
    dd = build_chart_model(Reader(baseline()), manifest(), view="drawdown", live=live, overlay=True)
    assert [point["value"] for point in dd.live_points] == ["0.00", "-4.00"]


def test_bounded_live_portfolio_recomputes_cashflow_adjusted_pnl_and_dd() -> None:
    live = {
        "currency": "USDT", "cashflows": [{"timestamp_utc": "2026-09-07T00:00:06Z", "amount": "5", "classification": "DEPOSIT", "currency": "USDT"}],
        "equity": {"unit": "USDT", "currency": "USDT", "points": [
            {"timestamp_utc": "2026-09-07T00:00:04Z", "value": "101"},
            {"timestamp_utc": "2026-09-07T00:00:08Z", "value": "110"},
        ]},
    }
    result = build_chart_overview(Reader(baseline()), manifest(), live=live)
    pnl = next(item for item in result["traces"] if item["trace"] == "net_trading_pnl")
    assert pnl["live"]["overview"]["records"][-1]["last"] == "4"


def test_bounded_live_portfolio_accepts_retained_wallet_snapshot_shape() -> None:
    live = {
        "observed_at": "2026-09-07T00:00:06Z",
        "wallet": {"equity": "105", "currency": "USDT"},
        "cashflows": [],
    }
    result = build_chart_overview(Reader(baseline()), manifest(), live=live)
    pnl = next(item for item in result["traces"] if item["trace"] == "net_trading_pnl")
    assert pnl["live"]["overview"]["records"][0]["last"] == "0"


@pytest.mark.parametrize("field", ["portfolio_set", "evaluation", "run_id", "attempt_id", "series_version"])
def test_missing_pinned_identity_is_unknown(field: str) -> None:
    data = baseline()
    data.pop(field)
    result = build_chart_model(Reader(data), manifest(), view="equity")
    assert result.availability == UNKNOWN
    assert result.reason == "BASELINE_PIN_MISMATCH"


def test_reader_must_be_read_only_shape() -> None:
    reader = Reader(baseline())
    result = build_chart_model(reader, manifest(), view="equity", live=None)
    assert result.points
    assert reader.calls == [("run-1", "attempt-1")]


def test_live_metadata_cannot_override_pinned_provenance_or_source() -> None:
    live = {
        "unit": "USDT",
        "currency": "USDT",
        "source": "evil-source",
        "provenance": {"run_id": "evil-run"},
        "points": [{"timestamp_utc": "2026-09-07T00:00:06Z", "value": "102", "source": "evil-point", "provenance": {"manifest_id": "evil"}}],
    }
    result = build_chart_model(Reader(baseline()), manifest(), view="equity", live=live)
    point = result.live_points[0]
    assert point.source == LIVE
    assert point.provenance["run_id"] == "run-1"
    assert point.provenance["manifest_id"] == "manifest-1"
    assert "source" not in point.provenance
    assert "provenance" not in point.provenance


def test_margin_load_rejects_im_mm_length_and_timestamp_mismatch() -> None:
    for mm_points in (
        [("2026-09-07T00:00:00Z", "5")],
        [("2026-09-07T00:00:01Z", "5"), ("2026-09-07T00:00:02Z", "5")],
    ):
        data = baseline(series={
            "initial_margin": {"points": [("2026-09-07T00:00:00Z", "10"), ("2026-09-07T00:00:02Z", "10")]},
            "maintenance_margin": {"points": mm_points},
            "equity": {"points": [("2026-09-07T00:00:00Z", "100"), ("2026-09-07T00:00:02Z", "100")]},
        })
        result = build_chart_model(Reader(data), manifest(), view="margin_load")
        assert result.availability == UNKNOWN
        assert result.points == ()


def test_drawdown_pct_is_a_percentage_with_named_high_water_denominator() -> None:
    result = build_chart_model(Reader(baseline()), manifest(), view="drawdown_pct")
    assert [point["value"] for point in result.points] == ["0", "-2.500", "0"]
    assert result.points[1]["provenance"]["unit"] == "%"
    assert result.points[1]["provenance"]["denominator"] == "equity_high_water"
    assert result.points[1]["provenance"]["denominator_basis"] == "running_high_water"
    assert result.provenance["elapsed_origin_utc"] == "2026-09-07T00:00:00Z"


def test_composition_must_be_explicit_and_all_pinned_fields_must_match() -> None:
    inferred = baseline()
    inferred.pop("member_composition")
    inferred["report_facts"] = {"strategy-1": {"series": baseline()["series"]}, "strategy-2": {"series": baseline()["series"]}}
    inferred.pop("series")
    result = build_chart_model(Reader(inferred), manifest(), view="equity")
    assert result.availability == UNKNOWN
    assert result.reason == "COMPOSITION_UNVERIFIABLE"

    drift = baseline()
    drift["member_composition"] = [{"strategy_id": "strategy-1", "symbol": "ETHUSDT", "side": "LONG"}]
    result = build_chart_model(Reader(drift), manifest(), view="equity")
    assert result.availability == UNKNOWN
    assert result.reason == "BASELINE_PIN_MISMATCH"


def test_multiple_report_facts_require_explicit_manifest_selection() -> None:
    data = baseline()
    facts = {key: {"series": baseline()["series"]} for key in ("strategy-1", "strategy-2")}
    data["report_facts"] = facts
    data.pop("series")
    data["member_composition"] = [
        {"strategy_id": "strategy-1", "symbol": "BTCUSDT", "side": "LONG"},
        {"strategy_id": "strategy-2", "symbol": "ETHUSDT", "side": "LONG"},
    ]
    two_member_manifest = {**manifest(), "member_composition": data["member_composition"]}
    unresolved = build_chart_model(Reader(data), two_member_manifest, view="equity")
    assert unresolved.availability == UNKNOWN
    assert unresolved.reason == "MULTIPLE_REPORT_FACTS_UNRESOLVED"
    selected = build_chart_model(Reader(data), {**two_member_manifest, "report_selection": {"member": "strategy-1"}}, view="equity")
    assert selected.availability == AVAILABLE


def test_unavailable_or_malformed_points_fail_with_precise_unknown_reason() -> None:
    unavailable = baseline()
    unavailable["series"] = {"equity": {"points": [
        {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "100"},
        {"timestamp_utc": "2026-09-07T00:00:02Z", "value": "97", "availability": "UNAVAILABLE"},
    ]}}
    result = build_chart_model(Reader(unavailable), manifest(), view="equity")
    assert result.availability == UNKNOWN
    assert result.reason == "SERIES_POINT_UNAVAILABLE"

    malformed = baseline()
    malformed["series"] = {"equity": {"points": [{"timestamp_utc": "not-a-time", "value": "100"}]}}
    result = build_chart_model(Reader(malformed), manifest(), view="equity")
    assert result.availability == UNKNOWN
    assert result.reason == "SERIES_UNAVAILABLE"


def test_absent_manifest_and_hostile_write_methods_are_safe() -> None:
    assert build_chart_model(Reader(baseline()), None, view="equity").reason == "BASELINE_PIN_MISMATCH"

    class HostileReader(Reader):
        def write(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("chart reader attempted a write")

    result = build_chart_model(HostileReader(baseline()), manifest(), view="equity")
    assert result.availability == AVAILABLE


def test_exact_overview_is_bounded_deterministic_and_decimal_context_safe() -> None:
    points = tuple(ExactChartPoint(i, str(i), TESTED, i, f"p-{i}", AVAILABLE, {"unit": "USDT"}) for i in range(257))
    with pytest.MonkeyPatch.context() as patch:
        # The implementation owns a decimal128 local context, independent of
        # an unusually hostile caller context.
        import decimal
        previous = decimal.getcontext().copy()
        decimal.getcontext().prec = 2
        try:
            first = aggregate_elapsed_bucket(points)
            second = aggregate_elapsed_bucket(points)
        finally:
            decimal.setcontext(previous)
    assert first == second
    assert first["duration_us"] == 1_000_000
    assert len(first["records"]) <= 256
    assert first["records"][0]["count"] == 257
    assert canonical_chart_bytes(first) == canonical_chart_bytes(second)


def test_provenance_fragment_limit_keeps_exact_duration_and_fails_closed() -> None:
    points = tuple(ExactChartPoint(i, "1", TESTED, i, f"p-{i}", AVAILABLE, {"unit": str(i % 2)}) for i in range(257))
    with pytest.raises(ResourceBoundExceeded):
        aggregate_elapsed_bucket(points)


def test_record_schema_keeps_exact_observations_and_decimal_grid_elapsed() -> None:
    points = tuple(ExactChartPoint(i, str(i), TESTED, i, f"p-{i}", AVAILABLE, {"unit": "USDT"}) for i in range(257))
    records = aggregate_elapsed_bucket(points)["records"]
    record = records[0]
    assert {"bucket_index", "segment_index", "grid_start_elapsed", "grid_end_elapsed", "display_midpoint_elapsed", "first_exact", "last_exact", "exact_min", "exact_max", "count"} <= set(record)
    assert record["grid_start_elapsed"] == "0"
    assert record["first_exact"]["identity"] == "p-0"
    assert record["exact_min"]["identity"] == "p-0"


def test_exact_points_use_canonical_tie_order_and_integer_utc_microseconds() -> None:
    from mrs3.portfolio.live_charts import build_exact_points

    points = build_exact_points([
        {"timestamp_utc": "2026-09-07T00:00:00.000001Z", "value": "2", "source_ordinal": 2, "source_id": "b"},
        {"timestamp_utc": "2026-09-07T00:00:00.000001Z", "value": "1", "source_ordinal": 1, "source_id": "a"},
    ], manifest={"semantic_digest": "x"}, run={}, source=TESTED)
    assert [point.immutable_identity for point in points] == ["a", "b"]
    assert points[0].timestamp_us == 1788739200000001


def test_elapsed_bucket_splits_provenance_without_merging_adjacent_grid_cells() -> None:
    points = (
        ExactChartPoint(0, "1", TESTED, 0, "a", AVAILABLE, {"unit": "USDT"}),
        ExactChartPoint(1, "2", TESTED, 1, "b", AVAILABLE, {"unit": "BTC"}),
        ExactChartPoint(2, "3", TESTED, 2, "c", AVAILABLE, {"unit": "BTC"}),
    )
    result = aggregate_elapsed_bucket(points)
    assert len(result["records"]) == 2
    assert [record["grid_start_us"] for record in result["records"]] == [0, 0]


def test_overview_cashflow_dd_is_signed_and_negative_high_water_is_not_zero_seeded() -> None:
    data = baseline()
    data["series"]["equity"]["points"] = [
        {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "-100", "source_id": "e0"},
        {"timestamp_utc": "2026-09-07T00:00:02Z", "value": "-101", "source_id": "e1"},
        {"timestamp_utc": "2026-09-07T00:00:04Z", "value": "-99", "source_id": "e2"},
    ]
    result = build_chart_overview(Reader(data), manifest())
    drawdown = next(item for item in result["traces"] if item["trace"] == "drawdown")
    assert drawdown["summary"]["max_drawdown"] == "1"
    assert next(record for record in drawdown["overview"]["records"] if record["min"] == "-1")["min"] == "-1"
    pct = next(item for item in result["traces"] if item["trace"] == "drawdown_pct")
    assert pct["summary"]["max_drawdown_pct"] is None


def test_overview_cashflow_adjustment_excludes_t_start_and_applies_later_flow_before_equity() -> None:
    data = baseline()
    data["series"]["equity"]["points"] = [
        {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "100", "source_id": "e0"},
        {"timestamp_utc": "2026-09-07T00:00:02Z", "value": "160", "source_id": "e1"},
    ]
    data["cashflows"] = [
        {"timestamp_utc": "2026-09-07T00:00:00Z", "amount": "10", "classification": "DEPOSIT", "currency": "USDT", "source_ordinal": 0},
        {"timestamp_utc": "2026-09-07T00:00:02Z", "amount": "50", "classification": "DEPOSIT", "currency": "USDT", "source_ordinal": 0},
    ]
    result = build_chart_overview(Reader(data), manifest())
    equity = next(item for item in result["traces"] if item["trace"] == "cashflow_adjusted_equity")
    pnl = next(item for item in result["traces"] if item["trace"] == "net_trading_pnl")
    assert equity["overview"]["records"][0]["first"] == "100"
    equity_values = [record["last"] for record in equity["overview"]["records"] if record["count"]]
    pnl_values = [record["last"] for record in pnl["overview"]["records"] if record["count"]]
    assert equity_values == ["100", "110"]
    assert pnl_values == ["0", "10"]
    audit = result["cashflow_audit"]
    assert audit["count"] == 2
    assert audit["net_sum"] == "60"
    assert [event["signed_amount"] for event in audit["events"]] == ["10", "50"]


def test_cashflow_cursor_handles_deposit_withdrawal_and_three_equity_samples_once() -> None:
    data = baseline()
    data["series"]["equity"]["points"] = [
        ("2026-09-07T00:00:00Z", "100"),
        ("2026-09-07T00:00:01Z", "120"),
        ("2026-09-07T00:00:02Z", "105"),
    ]
    data["cashflows"] = [
        {"timestamp_utc": "2026-09-07T00:00:01Z", "amount": "10", "classification": "DEPOSIT", "currency": "USDT"},
        {"timestamp_utc": "2026-09-07T00:00:02Z", "amount": "5", "classification": "WITHDRAWAL", "currency": "USDT"},
    ]
    result = build_chart_overview(Reader(data), manifest())
    equity = next(item for item in result["traces"] if item["trace"] == "cashflow_adjusted_equity")
    assert [record["last"] for record in equity["overview"]["records"] if record["count"]] == ["100", "110", "100"]
    assert result["cashflow_audit"]["net_sum"] == "5"


def test_unknown_cashflow_type_blocks_metrics_and_is_audited() -> None:
    data = baseline()
    data["cashflows"] = [{"timestamp_utc": "2026-09-07T00:00:01Z", "amount": "5", "classification": "MYSTERY", "currency": "USDT"}]
    result = build_chart_overview(Reader(data), manifest())
    equity = next(item for item in result["traces"] if item["trace"] == "cashflow_adjusted_equity")
    assert equity["overview"]["records"][0]["availability"] == AVAILABLE
    assert any(record["availability"] == UNKNOWN for record in equity["overview"]["records"])
    assert result["cashflow_audit"]["availability"] == UNKNOWN


def test_overview_portfolio_has_no_pair_equity_and_pair_has_only_component_traces() -> None:
    data = baseline()
    data["pair_series"] = {"BTCUSDT|LONG": {"realized_pnl": {"points": [("2026-09-07T00:00:00Z", "2")]}}}
    result = build_chart_overview(Reader(data), manifest(), mode=PAIR, pair=("BTCUSDT", "LONG"))
    assert result["version"] == CHART_OVERVIEW_VERSION
    assert {item["trace"] for item in result["traces"]} == {"realized_pnl", "net_trading_pnl"}
    assert "equity" not in {item["trace"] for item in result["traces"]}


def test_shared_symbol_pair_requires_explicit_attribution_mapping() -> None:
    data = baseline()
    manifest_shared = {**manifest(), "member_composition": [
        {"strategy_id": "strategy-1", "symbol": "BTCUSDT", "side": "LONG"},
        {"strategy_id": "strategy-2", "symbol": "BTCUSDT", "side": "LONG"},
    ]}
    data["member_composition"] = manifest_shared["member_composition"]
    data["pair_series"] = {"BTCUSDT|LONG": {"realized_pnl": {"points": [("2026-09-07T00:00:00Z", "2")]}}}
    result = build_chart_overview(Reader(data), manifest_shared, mode=PAIR, pair=("BTCUSDT", "LONG"))
    assert result["availability"] == UNKNOWN
    data["pair_attribution"] = {"BTCUSDT|LONG": {"strategy_id": "strategy-1", "orderLinkId": "link-1"}}
    result = build_chart_overview(Reader(data), manifest_shared, mode=PAIR, pair=("BTCUSDT", "LONG"))
    assert result["availability"] == AVAILABLE


def test_pair_contour_unknown_preserves_valid_pair_components() -> None:
    data = baseline()
    data["pair_series"] = {"BTCUSDT|LONG": {"realized_pnl": {"unit": "USDT", "currency": "BTC", "points": [("2026-09-07T00:00:00Z", "2")]}}}
    result = build_chart_overview(Reader(data), manifest(), mode=PAIR, pair=("BTCUSDT", "LONG"), contour="PORTFOLIO")
    assert result["availability"] == AVAILABLE
    assert result["traces"]
    assert result["contour"]["availability"] == UNKNOWN
    assert result["contour"]["reason"] == "INCOMPATIBLE_METADATA"


def test_overview_margin_recomputes_on_denominator_events_with_exact_percent_ratio() -> None:
    data = baseline()
    data["series"] = {
        "equity": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "100"), ("2026-09-07T00:00:02Z", "100")]},
        "initial_margin": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "10")]},
        "maintenance_margin": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "5")]},
        "margin_balance": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "100"), ("2026-09-07T00:00:02Z", "200")]},
    }
    result = build_chart_overview(Reader(data), manifest())
    im = next(item for item in result["traces"] if item["trace"] == "im_load")
    assert [record["last"] for record in im["overview"]["records"] if record["count"]] == ["10", "5"]
    assert all(record["provenance"]["unit"] == "%" for record in im["overview"]["records"] if record["count"])


def test_margin_requires_freshness_policy_and_zero_numerator_is_known() -> None:
    data = baseline()
    data["series"] = {
        "equity": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "100")]},
        "initial_margin": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "0")]},
        "maintenance_margin": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "5")]},
        "margin_balance": {"unit": "USDT", "currency": "USDT", "points": [("2026-09-07T00:00:00Z", "100")]},
    }
    no_policy = {key: value for key, value in manifest().items() if key != "margin_freshness_seconds"}
    assert next(item for item in build_chart_overview(Reader(data), no_policy)["traces"] if item["trace"] == "im_load")["overview"]["records"][0]["availability"] == UNKNOWN
    known = next(item for item in build_chart_overview(Reader(data), manifest())["traces"] if item["trace"] == "im_load")
    assert known["overview"]["records"][0]["last"] == "0"


def test_pure_chart_handler_rejects_non_get_before_reader_and_strict_query() -> None:
    class NoRead:
        def __init__(self) -> None:
            self.calls = 0

        def read_portfolio_run(self, *_args: object) -> object:
            self.calls += 1
            raise AssertionError("reader called")

    reader = NoRead()
    path = "/api/v2/portfolio/campaigns/c1/candidates/k1/charts"
    assert handle_chart_request("POST", path, {"mode": PORTFOLIO}, reader=reader, manifest=manifest()).status == 405
    assert handle_chart_request("GET", path, {"mode": PORTFOLIO, "extra": "x"}, reader=reader, manifest=manifest()).status == 400
    assert reader.calls == 0
    rejected = handle_chart_request("POST", path, {"mode": PORTFOLIO}, reader=reader, manifest=manifest())
    assert rejected.headers["Allow"] == "GET"
    assert handle_chart_request("GET", path + "?mode=PORTFOLIO", {"mode": PORTFOLIO}, reader=reader, manifest=manifest()).status == 400


def test_chart_resource_declarations_fail_closed_without_allocating_raw_facts() -> None:
    assert build_chart_overview(Reader(baseline()), manifest(), raw_fact_count=MAX_RAW_FACTS + 1)["availability"] == UNKNOWN
    assert build_chart_overview(Reader(baseline()), manifest(), pair_options_count=MAX_PAIR_OPTIONS + 1)["availability"] == UNKNOWN


def test_semantic_provenance_does_not_fragment_each_point() -> None:
    points = tuple(ExactChartPoint(index, "1", TESTED, index, f"p-{index}", AVAILABLE, {
        "source_kind": "REST", "run_id": "r", "attempt_id": "a", "manifest_id": "m",
        "semantic_digest": "s", "series_version": "sv", "metrics_version": "mv", "unit": "USDT",
        "source_id": f"source-{index}", "source_sequence": index, "observed_at_us": index,
    }) for index in range(257))
    result = aggregate_elapsed_bucket(points)
    assert result["original_point_count"] == 257
    assert len(result["records"]) <= 256


def test_build_exact_points_same_semantics_stays_one_bounded_segment() -> None:
    from mrs3.portfolio.live_charts import build_exact_points

    raw = [{"timestamp_us": index, "value": "1", "source_id": f"p-{index}",
            "source_kind": "REST", "source_sequence": index,
            "observed_at_us": index, "canonical_payload_digest": f"d-{index}"}
           for index in range(257)]
    exact = build_exact_points(raw, manifest={"semantic_digest": "s"}, run={}, source=TESTED)
    result = aggregate_elapsed_bucket(exact)
    assert len(exact) == 257
    assert len(result["records"]) <= 256


def test_shared_symbol_across_sides_requires_attribution_mapping() -> None:
    data = baseline()
    composition = [
        {"strategy_id": "strategy-1", "symbol": "BTCUSDT", "side": "LONG"},
        {"strategy_id": "strategy-2", "symbol": "BTCUSDT", "side": "SHORT"},
    ]
    data["member_composition"] = composition
    shared_manifest = {**manifest(), "member_composition": composition}
    data["pair_series"] = {"BTCUSDT|LONG": {"realized_pnl": {"points": [("2026-09-07T00:00:00Z", "2")]}}}
    assert build_chart_overview(Reader(data), shared_manifest, mode=PAIR, pair=("BTCUSDT", "LONG"))["availability"] == UNKNOWN


def test_pair_live_uses_selected_pair_facts_and_net_requires_complete_components() -> None:
    data = baseline()
    data["pair_series"] = {"BTCUSDT|LONG": {"realized_pnl": {"points": [("2026-09-07T00:00:00Z", "2")]}}}
    live = {
        "net_pnl": {"points": [("2026-09-07T00:00:00Z", "999")]},
        "pair_series": {"BTCUSDT|LONG": {"realized_pnl": {"points": [("2026-09-07T00:00:00Z", "3")]}}},
    }
    result = build_chart_overview(Reader(data), manifest(), mode=PAIR, pair=("BTCUSDT", "LONG"), live=live)
    realized = next(item for item in result["traces"] if item["trace"] == "realized_pnl")
    assert realized["live"]["overview"]["records"][0]["last"] == "3"
    net = next(item for item in result["traces"] if item["trace"] == "net_trading_pnl")
    assert net["availability"] == UNKNOWN and net["reason"] == "PAIR_COMPONENTS_INCOMPLETE"


def test_exact_margin_panel_has_only_im_and_mm_traces_and_schema_totals() -> None:
    result = build_chart_overview(Reader(baseline()), manifest())
    assert "margin_load" not in {item["trace"] for item in result["traces"]}
    assert isinstance(result["bucket_duration_us"], str)
    assert {"original_point_count", "display_bucket_count", "gap_count"} <= set(result["totals"])


def test_handler_requires_registry_or_exact_manifest_identity() -> None:
    path = "/api/v2/portfolio/campaigns/c1/candidates/k1/charts"
    response = handle_chart_request("GET", path, {"mode": PORTFOLIO}, reader=Reader(baseline()), manifest=manifest())
    assert response.status == 404


def test_gap_blocks_dd_and_pnl_until_complete_baseline() -> None:
    data = baseline()
    data["series"]["equity"]["points"] = [
        {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "100"},
        {"timestamp_utc": "2026-09-07T00:00:01Z", "availability": "UNKNOWN", "reason": "GAP"},
        {"timestamp_utc": "2026-09-07T00:00:02Z", "value": "110"},
    ]
    result = build_chart_overview(Reader(data), manifest())
    for name in ("net_trading_pnl", "drawdown", "drawdown_pct"):
        trace = next(item for item in result["traces"] if item["trace"] == name)
        assert trace["summary"].get("max_drawdown", trace["summary"].get("max_drawdown_pct")) is None
    data["series"]["equity"]["points"][2]["baseline_complete"] = True
    recovered = build_chart_overview(Reader(data), manifest())
    pnl = next(item for item in recovered["traces"] if item["trace"] == "net_trading_pnl")
    assert any(record["last"] == "0" for record in pnl["overview"]["records"] if record["count"])


def test_explicit_duration_rejects_points_outside_grid() -> None:
    points = (ExactChartPoint(0, "1", TESTED, 0, "a", AVAILABLE, {}),
              ExactChartPoint(256, "2", TESTED, 1, "b", AVAILABLE, {}))
    with pytest.raises(ValueError):
        aggregate_elapsed_bucket(points, start_us=0, duration_us=1)


def test_fixed_seconds_ladder_selects_smallest_width() -> None:
    one_second = aggregate_elapsed_bucket((ExactChartPoint(0, "1", TESTED, 0, "a", AVAILABLE, {}),
                                           ExactChartPoint(1_000_000, "2", TESTED, 1, "b", AVAILABLE, {})))
    over_256_seconds = aggregate_elapsed_bucket((ExactChartPoint(0, "1", TESTED, 0, "a", AVAILABLE, {}),
                                                 ExactChartPoint(257_000_000, "2", TESTED, 1, "b", AVAILABLE, {})))
    assert one_second["duration_us"] == 1_000_000
    assert over_256_seconds["duration_us"] == 5_000_000


def test_bucket_ladder_allows_sixteen_escalations(monkeypatch: pytest.MonkeyPatch) -> None:
    import mrs3.portfolio.live_charts as charts

    attempts = 0
    real_aggregate = charts._aggregate_once

    def aggregate_on_last_width(points, width, start, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 17:
            return tuple({"index": index} for index in range(257))
        return real_aggregate(points, width, start, **kwargs)

    monkeypatch.setattr(charts, "_aggregate_once", aggregate_on_last_width)
    point = ExactChartPoint(0, "1", TESTED, 0, "a", AVAILABLE, {})
    assert aggregate_elapsed_bucket((point,))["duration_us"] == charts._BUCKET_LADDER_US[16]
    assert attempts == 17


def test_route_resource_reason_is_contract_stable() -> None:
    class Registry:
        def campaign_exists(self, _campaign: str) -> bool:
            return True

        def candidate(self, campaign: str, candidate: str) -> dict[str, str]:
            return {"campaign_id": campaign, "candidate_id": candidate}

        def pair_options(self, _campaign: str, _candidate: str) -> list[str]:
            return ["x"] * (MAX_PAIR_OPTIONS + 1)

    path = "/api/v2/portfolio/campaigns/c1/candidates/k1/charts"
    response = handle_chart_request("GET", path, {"mode": PORTFOLIO}, service=Registry())
    assert response.status == 413 and response.body["error"]["reason"] == "RESOURCE_BOUND_EXCEEDED"


def test_declared_raw_limit_at_boundary_does_not_double_count_aliases() -> None:
    result = build_chart_overview(Reader(baseline()), manifest(), raw_fact_count=MAX_RAW_FACTS)
    assert result["availability"] == AVAILABLE


def test_cashflow_equal_time_order_is_canonical_and_audit_keeps_raw_fields() -> None:
    data = baseline()
    data["series"]["equity"]["points"] = [
        {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "100", "source_id": "e0"},
        {"timestamp_utc": "2026-09-07T00:00:02Z", "value": "120", "source_id": "e1"},
    ]
    data["cashflows"] = [
        {"timestamp_utc": "2026-09-07T00:00:01Z", "observed_at_utc": "2026-09-07T00:00:01.000002Z",
         "source_kind": "WS", "source_id": "z", "source_sequence": 2, "direction": "IN",
         "amount": "7", "classification": "DEPOSIT", "currency": "USDT"},
        {"timestamp_utc": "2026-09-07T00:00:01Z", "observed_at_utc": "2026-09-07T00:00:01.000001Z",
         "source_kind": "REST", "source_id": "a", "source_sequence": 1, "direction": "IN",
         "amount": "3", "classification": "DEPOSIT", "currency": "USDT"},
    ]
    result = build_chart_overview(Reader(data), manifest())
    events = result["cashflow_audit"]["events"]
    assert [event["source_id"] for event in events] == ["a", "z"]
    assert events[0]["direction"] == "IN"
    assert events[0]["amount"] == "3"
    assert events[0]["currency"] == "USDT"
    assert events[0]["classification"] == "DEPOSIT"


def test_exact_points_reject_conflicting_same_identity_payload() -> None:
    from mrs3.portfolio.live_charts import build_exact_points

    with pytest.raises(ValueError, match="conflicting chart point payload"):
        build_exact_points([
            {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "1", "source_id": "same"},
            {"timestamp_utc": "2026-09-07T00:00:00Z", "value": "2", "source_id": "same"},
        ])


def test_bucket_records_expose_digest_without_raw_membership_array() -> None:
    points = tuple(ExactChartPoint(index, str(index), TESTED, index, f"p-{index}", AVAILABLE, {"unit": "USDT"})
                   for index in range(4))
    record = aggregate_elapsed_bucket(points)["records"][0]
    assert "membership" not in record
    assert record["provenance_membership_digest"]


def test_raw_fact_preflight_happens_before_normalization() -> None:
    from collections.abc import Sequence

    class HugePoints(Sequence[object]):
        def __len__(self) -> int:
            return MAX_RAW_FACTS + 1

        def __getitem__(self, _index: int) -> object:
            raise AssertionError("normalization inspected facts after cap")

    data = baseline()
    data["series"]["equity"]["points"] = HugePoints()
    result = build_chart_overview(Reader(data), manifest())
    assert result == {
        "version": CHART_OVERVIEW_VERSION,
        "availability": UNKNOWN,
        "reason": "CHART_RAW_FACTS_LIMIT_EXCEEDED",
        "traces": [],
    }


def test_pair_handler_requires_server_pair_proof_when_service_has_no_pair_registry() -> None:
    class CandidateOnlyRegistry:
        def campaign_exists(self, _campaign: str) -> bool:
            return True

        def candidate(self, _campaign: str, _candidate: str) -> dict[str, object]:
            return {"campaign_id": "c1", "candidate_id": "k1"}

    path = "/api/v2/portfolio/campaigns/c1/candidates/k1/charts"
    response = handle_chart_request("GET", path,
                                    {"mode": PAIR, "symbol": "BTCUSDT", "side": "LONG"},
                                    service=CandidateOnlyRegistry())
    assert response.status == 404
    assert response.body["error"]["reason"] == "PAIR_NOT_FOUND"


def test_finalizer_resource_exhaustion_survives_valueerror_boundary_and_route_maps_413() -> None:
    data = baseline()
    data["series"]["equity"]["points"] = [
        {"timestamp_utc": "2026-09-07T00:00:00Z", "value": str(index),
         "source_id": f"point-{index}", "unit": "USDT" if index % 2 else "USD"}
        for index in range(257)
    ]
    data_manifest = {**manifest(), "campaign_id": "c1", "candidate_id": "k1"}
    result = build_chart_overview(Reader(data), data_manifest)
    assert result["availability"] == UNKNOWN
    assert result["reason"] == "RESOURCE_BOUND_EXCEEDED"
    path = "/api/v2/portfolio/campaigns/c1/candidates/k1/charts"
    response = handle_chart_request("GET", path, {"mode": PORTFOLIO}, reader=Reader(data), manifest=data_manifest)
    assert response.status == 413
    assert response.body["error"]["reason"] == "RESOURCE_BOUND_EXCEEDED"


def test_trace_limit_counts_tested_and_nested_live_traces(monkeypatch: pytest.MonkeyPatch) -> None:
    import mrs3.portfolio.live_charts as charts

    monkeypatch.setattr(charts, "MAX_TRACES", 6)
    live = baseline()["series"]
    result = build_chart_overview(Reader(baseline()), manifest(), live=live)
    assert result["availability"] == UNKNOWN
    assert result["reason"] == "CHART_TRACE_LIMIT_EXCEEDED"
