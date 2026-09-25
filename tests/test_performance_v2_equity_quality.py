from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, Overflow, Rounded, localcontext
from importlib import import_module
from pathlib import Path

import pytest


UTC = timezone.utc
T = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
RESULT_ID = 17


def _engine():
    try:
        return import_module("mrs3.performance_v2_equity_quality")
    except ModuleNotFoundError as exc:
        pytest.fail(f"pure equity-quality engine is missing: {exc.name}", pytrace=False)


def _point(sample_index: int, timestamp: datetime, equity: object, result_id: int = RESULT_ID):
    return _engine().EquitySample(result_id, sample_index, timestamp, equity)


def _facts(
    points: list[tuple[datetime, object] | tuple[datetime, object, int]],
    *,
    age_days: int = 28,
    start: datetime | None = None,
    end: datetime = T,
):
    start = start or end - timedelta(days=age_days)
    samples = []
    for index, item in enumerate(points):
        timestamp, equity, *owner = item
        samples.append(_point(index, timestamp, equity, owner[0] if owner else RESULT_ID))
    return _engine().calculate_equity_quality_facts(RESULT_ID, start, end, samples)


def _flat_28d():
    return _facts(
        [(T - timedelta(days=28), Decimal("100")), (T, Decimal("100"))]
    )


def test_valid_equity_sample_requires_owned_utc_timestamps():
    engine = _engine()
    start = T - timedelta(days=28)
    end = T
    wrong_owner = [engine.EquitySample(18, 0, start, Decimal("100"))]
    non_utc_timestamp = [
        engine.EquitySample(
            RESULT_ID,
            0,
            start.astimezone(timezone(timedelta(hours=1))),
            Decimal("100"),
        )
    ]

    owner_facts = engine.calculate_equity_quality_facts(RESULT_ID, start, end, wrong_owner)
    timestamp_facts = engine.calculate_equity_quality_facts(RESULT_ID, start, end, non_utc_timestamp)
    naive_meta = engine.calculate_equity_quality_facts(
        RESULT_ID, start.replace(tzinfo=None), end, []
    )
    non_utc_meta = engine.calculate_equity_quality_facts(
        RESULT_ID, start.astimezone(timezone(timedelta(hours=1))), end, []
    )

    assert owner_facts.state == timestamp_facts.state == "UNKNOWN_INVALID_SOURCE"
    assert "ROW_OWNERSHIP_MISMATCH" in owner_facts.invalid_reasons
    assert "INVALID_EQUITY_TIMESTAMP_UTC" in timestamp_facts.invalid_reasons
    assert naive_meta.state == non_utc_meta.state == "UNKNOWN_INVALID_SOURCE"
    assert "INVALID_REPORT_START_UTC" in naive_meta.invalid_reasons
    assert "INVALID_REPORT_START_UTC" in non_utc_meta.invalid_reasons


@pytest.mark.parametrize(
    "bad_value",
    ["not-a-number", float("nan"), float("inf"), True],
)
def test_malformed_or_nonfinite_equity_is_structurally_invalid(bad_value: object):
    facts = _facts([(T - timedelta(days=28), Decimal("100")), (T, bad_value)])

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert facts.erf_disposition == "NOT_EVALUATED"
    assert facts.windows == ()
    assert facts.score12 is None


def test_decimal_inputs_keep_precision_beyond_binary64():
    start_value = Decimal("9007199254740993")
    end_value = Decimal("9007199254740994")
    facts = _facts(
        [(T - timedelta(days=7), start_value), (T, end_value)], age_days=7
    )

    assert facts.state == "FLAT"
    assert facts.windows[0].return_pct == Decimal("1.11022302462515641716400E-14")
    assert facts.windows[0].return_pct > 0


def test_finite_float_and_string_equity_use_decimal_str_conversion():
    start = T - timedelta(days=7)
    float_facts = _facts(
        [(start, 0.1), (T, 0.30000000000000004)],
        age_days=7,
    )
    string_facts = _facts(
        [(start, "0.1"), (T, "0.30000000000000004")],
        age_days=7,
    )

    assert float_facts.windows[0].return_pct == Decimal("200.0000000000000400")
    assert string_facts == float_facts


def test_structural_invalidity_precedes_an_in_report_nonpositive_value():
    start = T - timedelta(days=28)
    facts = _facts(
        [
            (start, Decimal("100")),
            (T - timedelta(days=1), Decimal("-1")),
            (T + timedelta(seconds=1), Decimal("90")),
        ]
    )

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert facts.erf_disposition == "NOT_EVALUATED"
    assert "EQUITY_OUTSIDE_REPORT_INTERVAL" in facts.invalid_reasons
    assert facts.nonpositive_in_report_rows == 1
    assert facts.score12 is None


def test_duplicate_sample_index_invalidates_source_before_nonpositive_classification():
    start = T - timedelta(days=28)
    samples = [
        _point(0, start, Decimal("100")),
        _point(0, T - timedelta(days=1), Decimal("0")),
        _point(2, T, Decimal("100")),
    ]

    facts = _engine().calculate_equity_quality_facts(RESULT_ID, start, T, samples)

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert "DUPLICATE_SAMPLE_INDEX" in facts.invalid_reasons
    assert facts.erf_disposition == "NOT_EVALUATED"


def test_out_of_order_source_rows_are_structurally_invalid():
    start = T - timedelta(days=14)
    samples = [
        _point(1, start + timedelta(days=7), Decimal("100")),
        _point(0, start, Decimal("90")),
        _point(2, T, Decimal("110")),
    ]

    facts = _engine().calculate_equity_quality_facts(RESULT_ID, start, T, samples)

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert "UNORDERED_EQUITY_SOURCE" in facts.invalid_reasons


def test_malformed_sample_index_invalidates_source():
    start = T - timedelta(days=7)
    samples = [
        _engine().EquitySample(RESULT_ID, 0, start, Decimal("100")),
        _engine().EquitySample(RESULT_ID, 1.5, T, Decimal("110")),
    ]

    facts = _engine().calculate_equity_quality_facts(RESULT_ID, start, T, samples)

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert "INVALID_SAMPLE_INDEX" in facts.invalid_reasons


def test_available_horizon_uses_baselines_not_report_age_alone():
    facts = _facts(
        [
            (T - timedelta(days=27), Decimal("100")),
            (T, Decimal("110")),
        ],
        age_days=30,
    )

    assert facts.available_baselines_days == (14, 7)
    assert facts.horizon_days == 14
    assert tuple(window.days for window in facts.windows) == (7, 14)


def test_windows_without_baselines_distinguish_young_from_missing_history():
    young = _facts([(T - timedelta(days=6), Decimal("100"))], age_days=6)
    old_but_missing_baseline = _facts(
        [(T - timedelta(days=6), Decimal("100"))], age_days=8
    )

    assert young.state == "INSUFFICIENT_HISTORY"
    assert old_but_missing_baseline.state == "MISSING_BASELINE"
    assert young.erf_disposition == old_but_missing_baseline.erf_disposition == "NOT_EVALUATED"
    assert young.windows == old_but_missing_baseline.windows == ()


def test_empty_old_source_is_missing_baseline():
    facts = _facts([], age_days=8)

    assert facts.state == "MISSING_BASELINE"
    assert facts.erf_disposition == "NOT_EVALUATED"
    assert facts.available_baselines_days == ()


def test_one_baseline_carries_across_all_available_windows():
    start = T - timedelta(days=28)
    facts = _facts([(start, Decimal("100"))])

    assert facts.available_baselines_days == (28, 14, 7)
    assert facts.horizon_days == 28
    assert facts.state == "FLAT"
    assert facts.equity_class == 2
    assert facts.erf_disposition == "BLOCK_IF_ERF_ENABLED"
    assert facts.reason == "H_FLAT"
    assert tuple(window.days for window in facts.windows) == (7, 14, 28)
    assert all(window.trend30 == window.endpoint30 == window.er == 0 for window in facts.windows)


def test_nonpositive_equity_anywhere_in_report_blocks_even_outside_selected_h():
    facts = _facts(
        [
            (T - timedelta(days=30), Decimal("-1")),
            (T - timedelta(days=14), Decimal("100")),
            (T, Decimal("110")),
        ],
        age_days=30,
    )

    assert facts.available_baselines_days == ()
    assert facts.horizon_days is None
    assert facts.state == "NONPOSITIVE_EQUITY"
    assert facts.erf_disposition == "BLOCK"
    assert facts.windows == ()
    assert facts.nonpositive_in_report_rows == 1


def test_exact_zero_equity_is_nonpositive_and_blocks():
    start = T - timedelta(days=7)
    facts = _facts(
        [
            (start, Decimal("100")),
            (T - timedelta(days=1), Decimal("0")),
            (T, Decimal("110")),
        ],
        age_days=7,
    )

    assert facts.state == "NONPOSITIVE_EQUITY"
    assert facts.erf_disposition == "BLOCK"
    assert facts.nonpositive_in_report_rows == 1
    assert facts.windows == ()
    assert facts.score12 is None


def test_nonpositive_outside_report_is_source_invalid_not_a_nonpositive_block():
    facts = _facts(
        [
            (T - timedelta(days=28) - timedelta(seconds=1), Decimal("0")),
            (T - timedelta(days=28), Decimal("100")),
            (T, Decimal("110")),
        ]
    )

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert facts.erf_disposition == "NOT_EVALUATED"
    assert facts.nonpositive_in_report_rows == 0
    assert "EQUITY_OUTSIDE_REPORT_INTERVAL" in facts.invalid_reasons


def test_non_equity_sample_type_is_structurally_invalid():
    facts = _engine().calculate_equity_quality_facts(
        RESULT_ID, T - timedelta(days=28), T, [object()]
    )

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert facts.erf_disposition == "NOT_EVALUATED"
    assert "MALFORMED_EQUITY_SAMPLE" in facts.invalid_reasons


def test_reversed_report_range_is_structurally_invalid():
    facts = _engine().calculate_equity_quality_facts(
        RESULT_ID, T + timedelta(hours=1), T, []
    )

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert facts.erf_disposition == "NOT_EVALUATED"
    assert "INVALID_REPORT_RANGE" in facts.invalid_reasons


def test_duplicate_timestamp_uses_highest_index_for_grid_and_keeps_raw_risk_path():
    start = T - timedelta(days=14)
    at_day_7 = start + timedelta(days=7)
    samples = [
        _point(0, start, Decimal("100")),
        _point(1, at_day_7, Decimal("200")),
        _point(2, at_day_7, Decimal("100")),
        _point(3, T, Decimal("100")),
    ]

    facts = _engine().calculate_equity_quality_facts(RESULT_ID, start, T, samples)
    main = facts.windows[-1]

    assert facts.state == "FLAT"
    assert facts.duplicate_timestamp_count == 1
    assert main.trend30 == main.endpoint30 == main.er == 0
    assert facts.drawdown == Decimal("0.5")
    assert facts.peak_gap == Decimal("0.5")


def test_exact_left_and_right_rows_are_not_replaced_by_synthetic_anchors():
    start = T - timedelta(days=7)
    samples = [
        _point(0, start, Decimal("100")),
        _point(1, start + timedelta(days=2), Decimal("110")),
        _point(2, T, Decimal("120")),
        _point(3, T, Decimal("115")),
    ]

    facts = _engine().calculate_equity_quality_facts(RESULT_ID, start, T, samples)

    assert facts.raw_h_path_points == 4
    expected_risk = Decimal("0.04166666666666666666666666666666666667")
    assert facts.drawdown == expected_risk
    assert facts.peak_gap == expected_risk


def test_missing_left_and_right_raw_rows_add_exactly_one_carry_anchor_each():
    start = T - timedelta(days=7)
    facts = _facts(
        [
            (start - timedelta(hours=1), Decimal("100")),
            (start + timedelta(hours=2), Decimal("100")),
            (T - timedelta(hours=1), Decimal("110")),
        ],
        age_days=8,
    )

    assert facts.raw_h_path_points == 4
    assert facts.state == "GROWING"


def test_score_is_half_even_quantized_and_scale_invariant():
    start = T - timedelta(days=7)
    points = [(start, Decimal("100")), (T, Decimal("110"))]
    ordinary = _facts(points, age_days=7)
    scaled = _facts(
        [(stamp, value * Decimal("1e30")) for stamp, value in points], age_days=7
    )

    assert ordinary.state == "GROWING"
    assert ordinary.score12 == Decimal("7.887739018289")
    assert ordinary.score12 == ordinary.score12.quantize(Decimal("0.000000000001"))
    assert scaled.score12 == ordinary.score12
    assert scaled.windows[0].trend30 == ordinary.windows[0].trend30
    assert scaled.windows[0].endpoint30 == ordinary.windows[0].endpoint30
    assert scaled.windows[0].er == ordinary.windows[0].er
    assert scaled.drawdown == ordinary.drawdown == 0
    assert scaled.peak_gap == ordinary.peak_gap == 0
    assert scaled.windows[0].return_pct == ordinary.windows[0].return_pct


def test_inserting_a_redundant_between_grid_sample_does_not_change_grid_metrics():
    start = T - timedelta(days=7)
    sparse = _facts([(start, Decimal("100")), (T, Decimal("110"))], age_days=7)
    redundant = _facts(
        [
            (start, Decimal("100")),
            (start + timedelta(hours=3), Decimal("100")),
            (T, Decimal("110")),
        ],
        age_days=7,
    )

    assert sparse.windows == redundant.windows


def test_declining_horizon_blocks_and_receives_negative_score():
    facts = _facts(
        [
            (T - timedelta(days=28), Decimal("120")),
            (T - timedelta(days=7), Decimal("100")),
            (T, Decimal("90")),
        ]
    )

    assert facts.state == "DECLINING_OR_MIXED"
    assert facts.equity_class == 3
    assert facts.erf_disposition == "BLOCK"
    assert facts.score12 is not None and facts.score12 < 0


def test_nondeclining_but_not_up_horizon_is_class2_block():
    start = T - timedelta(days=7)
    facts = _facts(
        [(start, Decimal("100")), (T, Decimal("100.0000000047"))],
        age_days=7,
    )

    assert facts.state == "DECLINING_OR_MIXED"
    assert facts.equity_class == 2
    assert facts.erf_disposition == "BLOCK"
    assert facts.reason == "H_NONDECLINING_NOT_UP"
    assert facts.windows[0].trend30 < Decimal("1e-8")
    assert facts.windows[0].endpoint30 > Decimal("1e-8")


def test_short_decline_demotes_growing_horizon_without_blocking_it():
    facts = _facts(
        [
            (T - timedelta(days=28), Decimal("100")),
            (T - timedelta(days=21), Decimal("120")),
            (T - timedelta(days=3), Decimal("110")),
            (T, Decimal("110")),
        ]
    )

    assert facts.state == "WEAKENING"
    assert facts.equity_class == 1
    assert facts.erf_disposition == "PASS"


def test_quiet_multiday_tail_with_up_h_and_flat_shorts_remains_growing():
    facts = _facts(
        [
            (T - timedelta(days=28), Decimal("100")),
            (T - timedelta(days=21), Decimal("110")),
        ]
    )

    assert facts.state == "GROWING"
    assert facts.equity_class == 0
    assert facts.erf_disposition == "PASS"
    assert facts.horizon_days == 28
    assert all(
        facts.windows[index].trend30 == facts.windows[index].endpoint30 == 0
        for index in (0, 1)
    )
    assert facts.windows[-1].trend30 > Decimal("1e-8")
    assert facts.windows[-1].endpoint30 > Decimal("1e-8")


def test_canonical_facts_use_stable_utc_and_decimal_strings():
    facts = _flat_28d()
    encoded = facts.to_canonical_dict()

    assert encoded["result_id"] == RESULT_ID
    assert encoded["report_start_utc"].endswith("Z")
    assert encoded["report_end_utc"].endswith("Z")
    assert isinstance(encoded["score12"], str)
    assert isinstance(encoded["windows"][0]["trend30"], str)
    assert not {"actions", "positions"}.intersection(encoded)


def test_canonical_evaluated_facts_have_exact_shape_order_and_repeatable_json():
    start = T - timedelta(days=7)
    facts = _engine().calculate_equity_quality_facts(
        RESULT_ID,
        start,
        T,
        [_point(0, start, Decimal("100")), _point(1, T, Decimal("100"))],
    )
    expected = {
        "algo_version": "equity-quality-r7.3-v1",
        "result_id": RESULT_ID,
        "report_start_utc": "2026-09-18T12:00:00Z",
        "report_end_utc": "2026-09-25T12:00:00Z",
        "state": "FLAT",
        "reason": "H_FLAT",
        "erf_disposition": "BLOCK_IF_ERF_ENABLED",
        "raw_sample_count": 2,
        "in_report_sample_count": 2,
        "nonpositive_in_report_rows": 0,
        "duplicate_timestamp_count": 0,
        "invalid_reasons": [],
        "available_baselines_days": [7],
        "horizon_days": 7,
        "equity_class": 2,
        "windows": [
            {
                "days": 7,
                "start_utc": "2026-09-18T12:00:00Z",
                "end_utc": "2026-09-25T12:00:00Z",
                "trend30": "0",
                "endpoint30": "0",
                "return_pct": "0",
                "er": "0",
                "grid_points": 29,
            }
        ],
        "drawdown": "0",
        "peak_gap": "0",
        "raw_h_path_points": 2,
        "score12": "0",
    }
    encoded = facts.to_canonical_dict()

    assert list(encoded) == list(expected)
    assert encoded == expected
    assert list(encoded["windows"][0]) == list(expected["windows"][0])
    first = json.dumps(encoded, separators=(",", ":"), allow_nan=False).encode("utf-8")
    second = json.dumps(facts.to_canonical_dict(), separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert first == second


def test_canonical_not_evaluated_facts_have_exact_shape_order_and_repeatable_json():
    facts = _facts([], age_days=8)
    expected = {
        "algo_version": "equity-quality-r7.3-v1",
        "result_id": RESULT_ID,
        "report_start_utc": "2026-09-17T12:00:00Z",
        "report_end_utc": "2026-09-25T12:00:00Z",
        "state": "MISSING_BASELINE",
        "reason": "MISSING_BASELINE",
        "erf_disposition": "NOT_EVALUATED",
        "raw_sample_count": 0,
        "in_report_sample_count": 0,
        "nonpositive_in_report_rows": 0,
        "duplicate_timestamp_count": 0,
        "invalid_reasons": [],
        "available_baselines_days": [],
        "horizon_days": None,
        "equity_class": None,
        "windows": [],
        "drawdown": None,
        "peak_gap": None,
        "raw_h_path_points": 0,
        "score12": None,
    }
    encoded = facts.to_canonical_dict()

    assert list(encoded) == list(expected)
    assert encoded == expected
    first = json.dumps(encoded, separators=(",", ":"), allow_nan=False).encode("utf-8")
    second = json.dumps(facts.to_canonical_dict(), separators=(",", ":"), allow_nan=False).encode("utf-8")
    assert first == second


def test_engine_ast_rejects_forbidden_data_io_and_strategy_dependencies():
    source = Path(_engine().__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_imports = {
        "duckdb", "pandas", "pathlib", "os", "shutil", "glob",
        "socket", "urllib", "http", "requests",
    }
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    referenced = {
        node.id.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    } | {
        node.attr.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }

    assert not forbidden_imports.intersection(imported)
    assert not {"actions", "positions", "open"}.intersection(referenced)


def test_extreme_finite_decimal_ratio_returns_deterministic_invalid_facts():
    facts = _facts(
        [
            (T - timedelta(days=28), Decimal("1e-999999")),
            (T, Decimal("1e999999")),
        ]
    )

    assert facts.state == "UNKNOWN_INVALID_SOURCE"
    assert facts.reason == "DECIMAL_METRIC_ERROR"
    assert facts.erf_disposition == "NOT_EVALUATED"
    assert facts.invalid_reasons == ("DECIMAL_METRIC_ERROR",)


def test_metric_calculation_is_independent_of_ambient_decimal_context():
    start = T - timedelta(days=7)
    growth_samples = [
        _point(0, start, Decimal("100")),
        _point(1, T, Decimal("110")),
    ]
    extreme_samples = [
        _point(0, T - timedelta(days=28), Decimal("1e-999999")),
        _point(1, T, Decimal("1e999999")),
    ]

    def bundle(samples: list):
        facts = _engine().calculate_equity_quality_facts(
            RESULT_ID,
            samples[0].timestamp_utc,
            T,
            samples,
        )
        canonical = facts.to_canonical_dict()
        encoded = json.dumps(canonical, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return facts, canonical, encoded

    default_growth = bundle(growth_samples)
    default_extreme = bundle(extreme_samples)
    assert default_growth[0].state == "GROWING"
    assert default_extreme[0].reason == "DECIMAL_METRIC_ERROR"

    with localcontext() as ambient:
        ambient.prec = 7
        ambient.Emin = -10
        ambient.Emax = 10
        ambient.capitals = 0
        ambient.clamp = 1
        ambient.traps[Overflow] = False
        ambient.flags[Rounded] = True
        extreme_without_overflow_trap = bundle(extreme_samples)
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        growth_with_inexact_traps = bundle(growth_samples)
        extreme_with_ambient_traps = bundle(extreme_samples)

    assert extreme_without_overflow_trap == default_extreme
    assert growth_with_inexact_traps == default_growth
    assert extreme_with_ambient_traps == default_extreme


def test_decimal_38_12_input_envelope_keeps_canonical_metrics_bounded():
    start = T - timedelta(days=7)
    minimum = Decimal("0.000000000001")
    maximum = Decimal("99999999999999999999999999.999999999999")
    facts = _engine().calculate_equity_quality_facts(
        RESULT_ID,
        start,
        T,
        [_point(0, start, minimum), _point(1, T, maximum)],
    )

    assert facts.state == "GROWING"
    canonical = facts.to_canonical_dict()
    assert canonical["windows"][0]["return_pct"] == "9999999999999999999999999999999999999800"
    numeric_text = [
        canonical["drawdown"],
        canonical["peak_gap"],
        canonical["score12"],
        *(
            window[key]
            for window in canonical["windows"]
            for key in ("trend30", "endpoint30", "return_pct", "er")
        ),
    ]
    # DECIMAL(38,12) has at most 38 source digits; the maximum return percent
    # is below 1e40, so the schema-derived plain-text ceiling is 40 digits.
    assert all(isinstance(value, str) and "E" not in value.upper() for value in numeric_text)
    assert all(len(value) <= 40 for value in numeric_text)
    serialized = json.dumps(canonical, separators=(",", ":"), allow_nan=False)
    assert format(minimum, "f") not in serialized
    assert format(maximum, "f") not in serialized
