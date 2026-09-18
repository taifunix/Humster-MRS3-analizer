from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal, getcontext, localcontext
import importlib
import inspect
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.sparse import issparse

from mrs3.portfolio.candidate_search import PortfolioCandidate, SearchResult
from mrs3.portfolio.input import PreparedWeightedInput
from mrs3.portfolio.margin import MarginCoefficient, MarginCoefficientResult
from mrs3.portfolio.position_sizing import size_composition_vector
from mrs3.portfolio.weighted_search import (
    _MarginVariant,
    _Solution,
    _candidate_for_solution,
    _candidates_for_solution,
    _select_cdar_families,
    _cdar_p30_floor,
    _margin_variant,
    _ordered_margin_variants,
    _limiter_model_coefficients,
    _precision_for,
    _sum_products,
    _solve_additional_lp,
    _solve_cdar80_lp,
    _cdar80_money,
    _cdar90_money,
    _solve_lp,
    _solve_lp_unchecked,
    LimiterReplayResult,
    bank_for_path,
    bootstrap_banks,
    nearest_rank,
    stationary_bootstrap_indices,
    derive_priorities,
    evaluate_weighted_path,
    replay_limiter,
    _select_shortlist,
    weighted_search,
)


def test_nearest_rank_uses_ceiling_rank_without_interpolation() -> None:
    assert nearest_rank((Decimal("1"), Decimal("2"), Decimal("4"), Decimal("8")), Decimal("0.95")) == Decimal("8")


def test_nearest_rank_does_not_depend_on_caller_decimal_precision() -> None:
    with localcontext() as context:
        context.prec = 2
        assert nearest_rank(tuple(Decimal(index) for index in range(1, 1002)), Decimal("0.95")) == Decimal("951")


def test_stationary_bootstrap_known_seed_restarts_and_wraps() -> None:
    assert stationary_bootstrap_indices(4, Decimal("1"), history_step_minutes=720, seed=17, block_ordinal=2, scenario_index=3) == (2, 3, 1, 2)


def test_stationary_bootstrap_continues_and_wraps_when_restart_draws_are_above_p() -> None:
    assert stationary_bootstrap_indices(4, Decimal("1"), history_step_minutes=1, seed=17, block_ordinal=2, scenario_index=3) == (2, 3, 0, 1)


def test_stationary_bootstrap_rejects_invalid_restart_probability() -> None:
    with pytest.raises(ValueError, match="BOOTSTRAP_PROBABILITY_INVALID"):
        stationary_bootstrap_indices(4, Decimal("1"), history_step_minutes=1441, seed=1)


def test_bootstrap_bank_arithmetic_does_not_depend_on_caller_decimal_precision() -> None:
    rows = ((Decimal("1.23456"),), (Decimal("-1.23000"),), (Decimal("0.00444"),))
    with localcontext() as context:
        context.prec = 2
        result = bootstrap_banks(
            rows,
            ((Decimal("1"),),),
            max_dd=Decimal("0.2"),
            common_days=Decimal("20"),
            seed=17,
            block_days=(Decimal("1"),),
            scenarios_per_family=4,
            history_step_minutes=1440,
        )
    assert result.p95_banks == ((Decimal("18.4500"),),)


def test_batched_bootstrap_reuses_common_indices_for_cancellation_and_exposes_compact_metrics() -> None:
    result = bootstrap_banks(
        ((Decimal("1"), Decimal("-1")), (Decimal("2"), Decimal("-2")), (Decimal("-1"), Decimal("1"))),
        ((Decimal("1"), Decimal("1")), (Decimal("1"), Decimal("0"))),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=7,
        block_days=(Decimal("1"), Decimal("3"), Decimal("7")),
        scenarios_per_family=3,
        history_step_minutes=1440,
    )
    assert result.risk_banks[0] == Decimal("0")
    assert result.risk_banks[1] > Decimal("0")
    assert not hasattr(result, "path")
    assert "path" not in result.manifest


def test_batched_bootstrap_is_repeatable_and_x_order_only_reorders_results() -> None:
    rows = ((Decimal("1"), Decimal("-2")), (Decimal("-2"), Decimal("1")), (Decimal("1"), Decimal("-2")))
    vectors = ((Decimal("1"), Decimal("0")), (Decimal("0"), Decimal("1")))
    kwargs = dict(max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=19, block_days=(Decimal("1"), Decimal("3")), scenarios_per_family=5, history_step_minutes=720)
    first = bootstrap_banks(rows, vectors, **kwargs)
    repeated = bootstrap_banks(rows, vectors, **kwargs)
    reversed_result = bootstrap_banks(rows, tuple(reversed(vectors)), **kwargs)
    assert first.historical_banks == repeated.historical_banks
    assert first.scenario_banks == repeated.scenario_banks
    assert first.p95_banks == repeated.p95_banks
    assert first.risk_banks == repeated.risk_banks
    assert {key: value for key, value in first.manifest.items() if key != "operational"} == {key: value for key, value in repeated.manifest.items() if key != "operational"}
    assert reversed_result.risk_banks == tuple(reversed(first.risk_banks))
    assert reversed_result.p95_banks == tuple(reversed(first.p95_banks))
    assert {key: value for key, value in reversed_result.manifest.items() if key != "operational"} == {key: value for key, value in first.manifest.items() if key != "operational"}


def test_short_common_window_still_computes_all_default_block_families() -> None:
    result = bootstrap_banks(
        ((Decimal("1"),), (Decimal("-1"),), (Decimal("1"),)),
        ((Decimal("1"),),),
        max_dd=Decimal("0.2"),
        common_days=Decimal("5"),
        seed=3,
        scenarios_per_family=2,
    )
    assert result.p95_banks[0] and len(result.p95_banks[0]) == 3
    assert [family["mean_block_days"] for family in result.manifest["families"]] == [Decimal("1"), Decimal("3"), Decimal("7")]
    assert "COMMON_DAYS_LT_10_BLOCK_DAYS:7" in result.manifest["diagnostics"]


def test_bootstrap_cancellation_returns_an_explicit_incomplete_result() -> None:
    result = bootstrap_banks(
        ((Decimal("1"),), (Decimal("-1"),), (Decimal("1"),)),
        ((Decimal("1"),),),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=3,
        block_days=(Decimal("1"),),
        scenarios_per_family=4,
        batch_size=1,
        cancel=lambda: True,
    )
    assert result.complete is False
    assert result.risk_banks == (None,)
    assert result.manifest["stopping_reason"] == "CANCELLED"


def test_bootstrap_wall_time_limit_reports_only_completed_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls <= 4 else 1.0

    monkeypatch.setattr(module.time, "perf_counter", clock)
    result = bootstrap_banks(
        ((Decimal("1"),), (Decimal("-1"),), (Decimal("1"),)),
        ((Decimal("1"),),),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=3,
        block_days=(Decimal("1"),),
        scenarios_per_family=3,
        batch_size=1,
        wall_time_limit_seconds=Decimal("0.5"),
    )
    manifest = result.manifest
    family = manifest["families"][0]
    assert result.complete is False
    assert result.risk_banks == (None,)
    assert result.p95_banks == ((None,),)
    assert manifest["stopping_reason"] == "WALL_TIME_LIMIT"
    assert family["completed_scenario_count"] == 1
    assert family["unexplored_scenario_count"] == 2
    assert manifest["actual_completed_scenario_count"] == 1
    assert manifest["unexplored_scenario_count"] == 2
    assert len(result.scenario_banks[0][0]) == 1


def test_bootstrap_serial_and_process_batches_have_identical_risk_and_p95() -> None:
    rows = ((Decimal("1"), Decimal("-2")), (Decimal("-2"), Decimal("1")), (Decimal("1"), Decimal("-2")))
    vectors = ((Decimal("1"), Decimal("0")), (Decimal("0"), Decimal("1")))
    kwargs = dict(max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=4, block_days=(Decimal("1"), Decimal("3")), scenarios_per_family=6, batch_size=2, history_step_minutes=720)
    serial = bootstrap_banks(rows, vectors, workers=1, **kwargs)
    process = bootstrap_banks(rows, vectors, workers=2, **kwargs)
    assert process.complete is True
    assert process.scenario_banks == serial.scenario_banks
    assert process.p95_banks == serial.p95_banks
    assert process.risk_banks == serial.risk_banks
    assert process.manifest["operational"]["worker_width"] == 2


def test_bootstrap_worker_payload_reports_nonzero_peak_rss() -> None:
    worker = importlib.import_module("mrs3._portfolio_process_worker")
    context = (
        np.asarray(((Decimal("1"),),), dtype=object),
        1,
        (Decimal("1"),),
        Decimal("1440"),
        3,
        Decimal("0.2"),
        64,
    )
    payload = worker._bootstrap_scenario_batch(
        {"task_id": 0, "family_ordinal": 0, "scenario_start": 0, "scenario_count": 1},
        context,
    )
    assert payload["peak_rss_bytes"] > 0


def test_bootstrap_worker_failure_returns_incomplete_manifest_without_message(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    class ExplodingBatch:
        width = 2

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __call__(self, _tasks):
            raise RuntimeError("secret worker payload")

        def close(self) -> None:
            pass

    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=10**12))
    monkeypatch.setattr(module, "_ProcessBatchEvaluator", ExplodingBatch)
    result = bootstrap_banks(
        ((Decimal("1"),), (Decimal("-1"),)),
        ((Decimal("1"),),),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=3,
        block_days=(Decimal("1"),),
        scenarios_per_family=2,
        batch_size=1,
        workers=2,
    )
    assert result.complete is False
    assert result.risk_banks == (None,)
    assert result.manifest["stopping_reason"] == "WORKER_FAILURE"
    assert result.manifest["worker_failure"] == {"reason": "WORKER_FAILURE", "exception_type": "RuntimeError"}
    assert result.manifest["unexplored_task_count"] == 2
    assert "secret worker payload" not in repr(result.manifest)


def test_bootstrap_rejects_mismatched_worker_banks_as_worker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    class BadPayloadBatch:
        width = 2

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __call__(self, tasks):
            return tuple((task_id, {
                "task_id": task_id,
                "family_ordinal": task["family_ordinal"],
                "scenario_start": task["scenario_start"],
                "scenario_count": task["scenario_count"],
                "banks": ((),),
                "restart_count": 0,
                "coverage_mask": 0,
            }) for task_id, (task,) in tasks)

        def close(self) -> None:
            pass

    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=10**12))
    monkeypatch.setattr(module, "_ProcessBatchEvaluator", BadPayloadBatch)
    result = bootstrap_banks(
        ((Decimal("1"),), (Decimal("-1"),)),
        ((Decimal("1"),), (Decimal("0"),)),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=3,
        block_days=(Decimal("1"),),
        scenarios_per_family=2,
        batch_size=1,
        workers=2,
    )
    assert result.complete is False
    assert result.risk_banks == (None, None)
    assert result.p95_banks == ((None,), (None,))
    assert result.manifest["stopping_reason"] == "WORKER_FAILURE"
    assert result.manifest["worker_failure"]["exception_type"] == "ValueError"


@pytest.mark.parametrize("fault", ("duplicate", "omitted", "foreign", "scenario"))
def test_bootstrap_rejects_worker_task_identity_mismatch(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    class BadIdentityBatch:
        width = 2

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __call__(self, tasks):
            results = tuple((task_id, {
                "task_id": task_id,
                "family_ordinal": task["family_ordinal"],
                "scenario_start": task["scenario_start"],
                "scenario_count": task["scenario_count"],
                "banks": ((Decimal("0"),),),
                "restart_count": 0,
                "coverage_mask": 1,
            }) for task_id, (task,) in tasks)
            if fault == "duplicate":
                return (results[0], results[0])
            if fault == "omitted":
                return results[:1]
            if fault == "foreign":
                task_id, payload = results[1]
                return results[:1] + ((task_id, {**payload, "task_id": 999}),)
            task_id, payload = results[1]
            return results[:1] + ((task_id, {**payload, "scenario_start": 99}),)

        def close(self) -> None:
            pass

    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=10**12))
    monkeypatch.setattr(module, "_ProcessBatchEvaluator", BadIdentityBatch)
    result = bootstrap_banks(
        ((Decimal("1"),), (Decimal("-1"),)),
        ((Decimal("1"),),),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=3,
        block_days=(Decimal("1"),),
        scenarios_per_family=2,
        batch_size=1,
        workers=2,
    )
    assert result.complete is False
    assert result.risk_banks == (None,)
    assert result.manifest["stopping_reason"] == "WORKER_FAILURE"
    assert result.manifest["worker_failure"]["reason"] == "WORKER_FAILURE"
    assert result.manifest["actual_completed_scenario_count"] == 0
    assert result.manifest["actual_completed_task_count"] == 0
    assert result.manifest["unexplored_task_count"] == 2
    assert result.manifest["unexplored_task_ids"] == (0, 1)


def test_bootstrap_memory_clamp_reduces_actual_worker_width(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    rows = ((Decimal("1"),), (Decimal("-1"),))
    vectors = ((Decimal("1"),),)
    kwargs = dict(
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=3,
        block_days=(Decimal("1"),),
        scenarios_per_family=2,
        batch_size=1,
        workers=2,
    )
    bytes_per_value = sys.getsizeof(Decimal("1")) + np.dtype(object).itemsize
    prior_estimator = (len(rows) * len(vectors) + kwargs["batch_size"] * len(vectors)) * bytes_per_value
    boundary_memory = 2 * prior_estimator
    assert boundary_memory // prior_estimator == 2
    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=10**12))
    wide = bootstrap_banks(rows, vectors, **kwargs)
    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=boundary_memory))
    narrow = bootstrap_banks(rows, vectors, **kwargs)
    assert wide.manifest["operational"]["worker_width"] == 2
    assert narrow.manifest["operational"]["worker_width"] == 1


def test_bootstrap_manifest_uses_evaluator_width_after_platform_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    class ClampedBatch:
        width = 61

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def __call__(self, tasks):
            return tuple((task_id, {
                "task_id": task_id,
                "family_ordinal": task["family_ordinal"],
                "scenario_start": task["scenario_start"],
                "scenario_count": task["scenario_count"],
                "banks": ((Decimal("0"),),),
                "restart_count": 0,
                "coverage_mask": 1,
                "peak_rss_bytes": 1,
            }) for task_id, (task,) in tasks)

        def close(self) -> None:
            pass

    monkeypatch.setattr(module.psutil, "virtual_memory", lambda: SimpleNamespace(available=10**12))
    monkeypatch.setattr(module, "_ProcessBatchEvaluator", ClampedBatch)
    result = bootstrap_banks(
        ((Decimal("1"),),),
        ((Decimal("1"),),),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        seed=3,
        block_days=(Decimal("1"),),
        scenarios_per_family=2,
        batch_size=1,
        workers=2,
    )
    assert result.manifest["operational"]["worker_width"] == 61


def test_bootstrap_prefix_reuses_completed_scenarios_and_computes_only_remainder() -> None:
    rows = ((Decimal("1"), Decimal("-2")), (Decimal("-2"), Decimal("1")), (Decimal("1"), Decimal("-2")))
    vectors = ((Decimal("1"), Decimal("0")), (Decimal("0"), Decimal("1")))
    kwargs = dict(max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=4, block_days=(Decimal("1"), Decimal("3")), batch_size=2, history_step_minutes=720)
    prefix = bootstrap_banks(rows, vectors, scenarios_per_family=2, **kwargs)
    full = bootstrap_banks(rows, vectors, scenarios_per_family=6, **kwargs)
    continued = bootstrap_banks(rows, vectors, scenarios_per_family=6, prefix_result=prefix, **kwargs)
    assert continued.scenario_banks == full.scenario_banks
    assert continued.p95_banks == full.p95_banks
    assert continued.risk_banks == full.risk_banks
    assert [item["completed_scenario_count"] for item in continued.manifest["families"]] == [6, 6]
    assert [item["unexplored_scenario_count"] for item in continued.manifest["families"]] == [0, 0]


def test_bootstrap_rejects_prefix_from_different_source_identity() -> None:
    vectors = ((Decimal("1"),),)
    prefix = bootstrap_banks(((Decimal("1"),), (Decimal("-1"),)), vectors, max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=4, block_days=(Decimal("1"),), scenarios_per_family=2)
    with pytest.raises(ValueError, match="BOOTSTRAP_PREFIX_INVALID"):
        bootstrap_banks(((Decimal("2"),), (Decimal("-1"),)), vectors, max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=4, block_days=(Decimal("1"),), scenarios_per_family=4, prefix_result=prefix)


def test_bootstrap_prefix_reuses_selected_vectors_by_digest_and_only_computes_remainder() -> None:
    rows = ((Decimal("1"), Decimal("-2")), (Decimal("-2"), Decimal("1")), (Decimal("1"), Decimal("-2")))
    all_vectors = ((Decimal("1"), Decimal("0")), (Decimal("0"), Decimal("1")), (Decimal("1"), Decimal("1")))
    selected_vectors = (all_vectors[2], all_vectors[0])
    kwargs = dict(max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=4, block_days=(Decimal("1"),), batch_size=100, history_step_minutes=720)
    prefix = bootstrap_banks(rows, all_vectors, scenarios_per_family=100, **kwargs)
    full = bootstrap_banks(rows, selected_vectors, scenarios_per_family=1000, **kwargs)
    continued = bootstrap_banks(rows, selected_vectors, scenarios_per_family=1000, prefix_result=prefix, **kwargs)
    assert continued.scenario_banks == full.scenario_banks
    assert continued.p95_banks == full.p95_banks
    assert continued.risk_banks == full.risk_banks
    assert continued.manifest["new_scenario_count"] == 900


def test_bootstrap_rejects_prefix_from_different_seed() -> None:
    rows = ((Decimal("1"),), (Decimal("-1"),))
    vectors = ((Decimal("1"),),)
    prefix = bootstrap_banks(rows, vectors, max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=4, block_days=(Decimal("1"),), scenarios_per_family=2)
    with pytest.raises(ValueError, match="BOOTSTRAP_PREFIX_INVALID"):
        bootstrap_banks(rows, vectors, max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=5, block_days=(Decimal("1"),), scenarios_per_family=4, prefix_result=prefix)


def test_p95_witness_identifies_earliest_nearest_rank_scenario() -> None:
    rows = ((Decimal("1"),), (Decimal("-2"),), (Decimal("1"),), (Decimal("-2"),))
    result = bootstrap_banks(rows, ((Decimal("1"),),), max_dd=Decimal("0.2"), common_days=Decimal("20"), seed=17, block_days=(Decimal("1"),), scenarios_per_family=4, history_step_minutes=1440)
    assert result.p95_banks == ((Decimal("40"),),)
    assert result.p95_witnesses == (({"family_ordinal": 0, "scenario_index": 3},),)
    assert result.manifest["families"][0]["restart_count"] == 12


def test_weighted_search_validates_base_controls_and_forwards_ordered_margin_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    members = tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members())
    coefficients = (
        MarginCoefficient(2, Decimal("0.20"), Decimal("0.02"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")),
        MarginCoefficient(1, Decimal("0.10"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")),
    )
    observed: dict[str, object] = {}

    def fake_solver(*args, **kwargs):
        observed.update(kwargs)
        return weighted_search_module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE")

    monkeypatch.setattr(weighted_search_module, "_solve_lp", fake_solver)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        target_p30=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
        max_candidates=4,
        seed=9,
        bootstrap_scenarios=2,
        screening_scenarios=1,
        workers=1,
        wall_time=Decimal("5"),
        solver_time=Decimal("3"),
    )
    assert result.status == "FAIL"
    assert observed["margin_a"] == (Decimal("0.10"), Decimal("0.20"))
    assert observed["margin_b"] == (Decimal("0.01"), Decimal("0.02"))
    assert observed["max_mm_load"] == Decimal("0.35")
    assert observed["time_limit"] == Decimal("3")


def test_weighted_search_cancel_returns_budget_limited_manifest() -> None:
    result = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        members=_members(),
        target_p30=Decimal("10"),
        bootstrap_scenarios=2,
        screening_scenarios=1,
        cancel=lambda: True,
    )
    assert result.status == "budget_limited"
    assert result.manifest["stopping_reason"] == "CANCELLED"
    assert result.manifest["complete"] is False


def test_weighted_search_cancellation_during_bootstrap_reports_unexplored_work(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    real_bootstrap = module.bootstrap_banks
    callback_count = 0

    def cancel() -> bool:
        nonlocal callback_count
        callback_count += 1
        return callback_count >= 5

    def bootstrap(rows, vectors, **kwargs):
        kwargs["batch_size"] = 1
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    result = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        members=_members(),
        target_p30=Decimal("10"),
        bootstrap_scenarios=2,
        screening_scenarios=1,
        cancel=cancel,
    )
    assert result.status == "budget_limited"
    assert result.reason == "CANCELLED"
    assert result.manifest["complete"] is False
    assert result.manifest["stopping_reason"] is not None
    assert result.manifest["bootstrap"]["stopping_reason"] == "CANCELLED"
    assert result.manifest["bootstrap"]["unexplored_scenario_count"] > 0
    assert result.manifest["remaining_work"]["unexplored_scenario_count"] > 0
    assert result.manifest["remaining_work"]["scenario_x_slots"] == (
        3 * result.manifest["parameters"]["max_candidates"] - result.manifest["scenario_checked_x_count"]
    )


def test_weighted_search_result_is_worker_width_invariant() -> None:
    kwargs = dict(
        members=_members(),
        target_p30=Decimal("10"),
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
    )
    serial = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        workers=1,
        **kwargs,
    )
    parallel = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        workers=2,
        **kwargs,
    )
    assert serial.status == parallel.status == "PASS"
    assert serial.candidates == parallel.candidates
    assert parallel.manifest["bootstrap"]["operational"]["worker_width"] > 1
    for key in (
        "solver_calls",
        "solver_call_count",
        "base_x_count",
        "selected_base_x_count",
        "base_full_checked_x_count",
        "new_x_count",
        "scenario_checked_x_count",
        "additional_pass_count",
        "shortlist",
        "additional",
        "cdar",
        "remaining_work",
        "complete",
        "stopping_reason",
    ):
        assert serial.manifest[key] == parallel.manifest[key]
    assert serial.manifest["complete"] is True
    assert serial.manifest["stopping_reason"] is None
    for key in ("bootstrap", "screening"):
        left = serial.manifest[key]
        right = parallel.manifest[key]
        if left is None or right is None:
            assert left == right
        else:
            assert {name: value for name, value in left.items() if name != "operational"} == {
             name: value for name, value in right.items() if name != "operational"
             }


def test_weighted_search_margin_pipeline_worker_width_invariant_with_real_stage_bootstraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("0"), Decimal("2")))
    ))
    real_bootstrap = module.bootstrap_banks

    def run(workers: int) -> tuple[SearchResult, list[dict[str, object]]]:
        observed: list[dict[str, object]] = []

        def bootstrap(rows, vectors, **kwargs):
            result = real_bootstrap(rows, vectors, **kwargs)
            observed.append(result.manifest)
            return result

        monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
        result = weighted_search(
            prepared,
            (Decimal("100"), Decimal("100")),
            members=members,
            common_days=Decimal("1"),
            target_p30=Decimal("0.1"),
            bank_available=Decimal("10"),
            margin_coefficients=coefficients,
            margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
            bootstrap_scenarios=1,
            screening_scenarios=1,
            max_candidates=4,
            workers=workers,
        )
        return result, observed

    serial, serial_bootstraps = run(1)
    parallel, parallel_bootstraps = run(2)

    def without_execution_details(value, *, parameters: bool = False):
        if isinstance(value, Mapping):
            return {
                key: without_execution_details(item, parameters=key == "parameters")
                for key, item in value.items()
                if key != "operational" and not (parameters and key == "workers")
            }
        if isinstance(value, list):
            return [without_execution_details(item, parameters=parameters) for item in value]
        if isinstance(value, tuple):
            return tuple(without_execution_details(item, parameters=parameters) for item in value)
        return value

    assert serial.status == parallel.status == "PASS"
    assert serial.candidates == parallel.candidates
    assert serial.manifest["additional"]["attempted"] is True
    assert serial.manifest["additional"]["outcome"] == "accepted"
    assert serial.manifest["additional"].get("bootstrap") is not None
    assert serial.manifest["cdar"]["attempted"] is True
    assert serial.manifest["cdar"]["accepted_count"] > 0
    assert len(serial_bootstraps) >= 3
    assert len(parallel_bootstraps) >= 3
    assert all(
        serial_bootstraps[index]["operational"]["worker_width"] == 1
        for index in range(3)
    )
    assert all(
        parallel_bootstraps[index]["operational"]["worker_width"] > 1
        for index in range(3)
    )
    assert without_execution_details(serial.manifest) == without_execution_details(parallel.manifest)


def test_weighted_search_full_base_bootstrap_checks_at_most_two_m_distinct_x(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    solutions = iter(
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal(index), Decimal("1"))))
        for index in range(1, 9)
    )
    bootstrap_vectors: list[tuple[tuple[Decimal, ...], ...]] = []
    real_bootstrap = module.bootstrap_banks

    def bootstrap(rows, vectors, **kwargs):
        bootstrap_vectors.append(tuple(tuple(value for value in vector) for vector in vectors))
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: next(solutions))
    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    result = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        members=_members(),
        max_targets=8,
        max_candidates=2,
        bootstrap_scenarios=1,
        screening_scenarios=1,
    )
    assert result.status == "PASS"
    assert bootstrap_vectors
    assert len(bootstrap_vectors[0]) == 8
    assert all(len(vectors) <= 2 * 2 for vectors in bootstrap_vectors[1:])
    assert result.manifest["base_full_checked_x_count"] <= 2 * 2


def test_weighted_search_screens_over_two_m_base_budget_and_reuses_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    solutions = iter(
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal(index), Decimal("1"))))
        for index in range(1, 9)
    )
    real_bootstrap = module.bootstrap_banks
    real_selection = module._screened_selection
    bootstrap_calls: list[dict[str, object]] = []
    selected_vectors: list[tuple[tuple[Decimal, ...], ...]] = []

    def bootstrap(rows, vectors, **kwargs):
        result = real_bootstrap(rows, vectors, **kwargs)
        bootstrap_calls.append({"vectors": tuple(tuple(value for value in vector) for vector in vectors), "kwargs": kwargs, "result": result})
        return result

    def select(entries, screening, limit):
        selected = real_selection(entries, screening, limit)
        selected_vectors.append(tuple(solution.x for _target, solution in selected))
        return selected

    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: next(solutions))
    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    monkeypatch.setattr(module, "_screened_selection", select)
    result = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        members=_members(),
        max_targets=8,
        max_candidates=2,
        bootstrap_scenarios=2,
        screening_scenarios=1,
    )
    assert result.status == "PASS"
    assert len(bootstrap_calls) == 2
    screening, full = bootstrap_calls
    assert len(screening["vectors"]) > 2 * 2
    assert screening["kwargs"].get("prefix_result") is None
    assert screening["kwargs"]["scenarios_per_family"] == 1
    assert selected_vectors
    assert full["vectors"] == selected_vectors[0]
    assert len(full["vectors"]) <= 2 * 2
    assert full["kwargs"]["prefix_result"] is screening["result"]
    assert full["kwargs"]["scenarios_per_family"] == 2
    assert result.manifest["screening"]["complete"] is True
    assert result.manifest["bootstrap"]["complete"] is True


def test_weighted_search_keeps_valid_time_limited_incumbent_after_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(
        weighted_search_module,
        "_solve_lp",
        lambda *args, **kwargs: weighted_search_module._SolveOutcome(
            "PASS", _Solution(Decimal("1"), (Decimal("1"), Decimal("1"))), budget_limited=True
        ),
    )
    result = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        members=_members(),
        target_p30=Decimal("10"),
        bootstrap_scenarios=2,
        screening_scenarios=1,
    )
    assert result.status == "budget_limited"
    assert result.reason == "SOLVER_TIME_LIMIT"
    assert result.candidates
    assert result.manifest["bootstrap"]["complete"] is True


def test_bootstrap_risk_is_authoritative_for_candidate_bank_eligibility() -> None:
    candidate = _candidate_for_solution(
        _Solution(Decimal("1"), (Decimal("10"),)),
        ((Decimal("1"),), (Decimal("-1"),)),
        ({"strategy_id": 1, "symbol": "A", "side": "LONG"},),
        (Decimal("100"),),
        max_dd=Decimal("0.2"),
        common_days=Decimal("20"),
        target=None,
        profile_id="P",
        scenario_id="S",
        bank_available=Decimal("20"),
        B_risk=Decimal("40"),
    )
    assert candidate is None


def _prepared(rows: tuple[tuple[str, ...], ...], strategy_ids=(1, 2)) -> PreparedWeightedInput:
    t = len(rows)
    timestamps = tuple(
        datetime(2024, 1, 1, tzinfo=timezone.utc).replace(minute=5 * index).isoformat().replace("+00:00", "Z")
        for index in range(t + 1)
    )
    return PreparedWeightedInput(
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 0, 5 * t, tzinfo=timezone.utc),
        5,
        timestamps,
        tuple(strategy_ids),
        tuple(tuple(Decimal(value) for value in row) for row in rows),
        tuple(tuple(True for _ in row) for row in rows),
        tuple(tuple(None for _ in row) for row in rows),
        {},
        {},
        "fixture",
    )


def _members(count: int = 2):
    return tuple(
        {"symbol": f"S{index}", "side": "LONG", "strategy_id": index + 1, "result_id": 100 + index}
        for index in range(count)
    )


def _evidence_coefficients(rates, strategy_ids=None, *, max_notional=Decimal("1000000")):
    ids = tuple(range(1, len(rates) + 1)) if strategy_ids is None else tuple(strategy_ids)
    return tuple(
        MarginCoefficient(strategy_id, initial, maintenance, "CONSERVATIVE_BOUND", max_notional=max_notional)
        for strategy_id, (initial, maintenance) in zip(ids, rates)
    )


def test_bank_for_path_matches_independent_peak_drawdown_calculation() -> None:
    g = (Decimal("0"), Decimal("1000"), Decimal("400"))
    bank = bank_for_path(g, Decimal("0.20"))
    equity = tuple(Decimal("2000") + value for value in g)
    peaks = []
    high = Decimal("0")
    for value in equity:
        high = max(high, value)
        peaks.append(high)
    dd = max((peak - value) / peak for peak, value in zip(peaks, equity))
    assert bank == Decimal("2000")
    assert dd <= Decimal("0.20")


def test_negative_first_observation_keeps_initial_bank_as_peak() -> None:
    evaluated = evaluate_weighted_path(((Decimal("-1"),),), (Decimal("10"),), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert evaluated["bank_for_path"] == Decimal("50")
    assert evaluated["max_drawdown_fraction"] == Decimal("0.2")
    assert evaluated["max_drawdown_pct"] == Decimal("20.0")


def test_bank_for_path_ignores_a_low_global_decimal_precision() -> None:
    original = getcontext().prec
    try:
        with localcontext() as context:
            context.prec = 2
            expected = bank_for_path((Decimal("0"), Decimal("1000"), Decimal("400")), Decimal("0.20"))
        with localcontext() as context:
            context.prec = 2
            actual = bank_for_path((Decimal("0"), Decimal("1000"), Decimal("400")), Decimal("0.20"))
        assert actual == expected == Decimal("2000")
    finally:
        getcontext().prec = original


def test_precision_bound_does_not_scale_with_matrix_cell_count() -> None:
    assert _precision_for(tuple(Decimal("1.2345") for _ in range(10_000))) < 256
    assert _precision_for((Decimal("1e100"), Decimal("1"))) >= 101


def test_sum_products_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError, match="VECTOR_SHAPE_MISMATCH"):
        _sum_products((Decimal("1"),), (Decimal("1"), Decimal("2")))


def test_lp_uses_sparse_constraints_without_changing_authoritative_bank(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    real_linprog = weighted_search_module.linprog
    observed: dict[str, object] = {}

    def capture_linprog(*args, **kwargs):
        observed["A_ub"] = kwargs["A_ub"]
        return real_linprog(*args, **kwargs)

    monkeypatch.setattr(weighted_search_module, "linprog", capture_linprog)
    rows = tuple((Decimal("0.1"), Decimal("-0.1")) for _ in range(256))
    outcome = _solve_lp_unchecked(
        rows,
        (Decimal("10"), Decimal("10")),
        (Decimal("1"), Decimal("1")),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("100"),
        maximize=True,
        symbol_cap_groups={},
    )

    assert outcome.status == "PASS"
    assert outcome.solution is not None
    assert outcome.solution.bank == Decimal("1")
    matrix = observed["A_ub"]
    assert issparse(matrix)
    assert matrix.format == "csr"
    assert matrix.shape == (3 * len(rows) - 1, 1 + 2 + len(rows))
    assert matrix.nnz < matrix.shape[0] * 12


@pytest.mark.parametrize(
    ("a_values", "b_values", "expected_x"),
    (
        ((Decimal("1"),), (Decimal("0.1"),), Decimal("16")),
        ((Decimal("0.1"),), (Decimal("1"),), Decimal("8")),
    ),
)
def test_lp_enforces_initial_im_and_mm_bounds(
    a_values: tuple[Decimal, ...],
    b_values: tuple[Decimal, ...],
    expected_x: Decimal,
) -> None:
    outcome = _solve_lp(
        ((Decimal("0.125"),), (Decimal("-0.25"),)),
        (Decimal("100"),),
        (Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("20"),
        maximize=True,
        margin_a=a_values,
        margin_b=b_values,
        max_mm_load=Decimal("0.5"),
        symbol_cap_groups={},
    )

    assert outcome.status == "PASS"
    assert outcome.solution is not None
    assert outcome.solution.x[0] == expected_x


def test_lp_margin_bounds_do_not_add_initial_off_reserve_constraint() -> None:
    outcome = _solve_lp(
        ((Decimal("0.125"),), (Decimal("-0.25"),)),
        (Decimal("100"),),
        (Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("10"),
        maximize=True,
        margin_a=(Decimal("1"),),
        margin_b=(Decimal("0.1"),),
        symbol_cap_groups={},
        max_mm_load=Decimal("0.5"),
    )

    assert outcome.status == "PASS"
    assert outcome.solution is not None
    # A release reserve of 40% would cap IM at 4.8 for B=10; discovery uses
    # only the necessary I_all <= (1-m)B relaxation and therefore reaches 8.
    assert outcome.solution.x == (Decimal("8"),)


def test_lp_reports_margin_dominated_bank_when_path_has_no_drawdown() -> None:
    outcome = _solve_lp(
        ((Decimal("1"),),),
        (Decimal("100"),),
        (Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("10"),
        maximize=True,
        symbol_cap_groups={},
        margin_a=(Decimal("1"),),
        margin_b=(Decimal("0.1"),),
        max_mm_load=Decimal("0.5"),
    )

    assert outcome.status == "PASS"
    assert outcome.solution is not None
    assert outcome.solution.x == (Decimal("8"),)
    assert outcome.solution.bank == Decimal("10")


def test_lp_margin_rate_vectors_are_a_strict_trust_boundary() -> None:
    common = dict(
        normalized_delta=((Decimal("0"),),),
        capacities=(Decimal("10"),),
        coefficients=(Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("10"),
        maximize=True,
        symbol_cap_groups={},
        margin_a=(Decimal("0.1"),),
        margin_b=(Decimal("0.1"),),
    )
    with pytest.raises(ValueError, match="max_mm_load must be below one"):
        _solve_lp_unchecked(**common, max_mm_load=Decimal("1"))
    with pytest.raises(ValueError, match="MARGIN_COEFFICIENT_SHAPE_MISMATCH"):
        _solve_lp_unchecked(**{**common, "margin_a": (Decimal("0.1"), Decimal("0.2"))}, max_mm_load=Decimal("0.5"))
    with pytest.raises(ValueError):
        _solve_lp_unchecked(**{**common, "margin_b": (Decimal("-0.1"),)}, max_mm_load=Decimal("0.5"))


def test_lp_forwards_fixed_highs_resource_options(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    real_linprog = weighted_search_module.linprog
    observed: dict[str, object] = {}

    def capture_linprog(*args, **kwargs):
        observed["options"] = kwargs["options"]
        return real_linprog(*args, **kwargs)

    monkeypatch.setattr(weighted_search_module, "linprog", capture_linprog)
    outcome = _solve_lp_unchecked(
        ((Decimal("0"),),),
        (Decimal("8"),),
        (Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("10"),
        maximize=True,
        symbol_cap_groups={},
        time_limit=Decimal("30"),
    )

    assert outcome.status == "PASS"
    assert observed["options"] == {"threads": 1, "time_limit": 30.0}


def test_lp_keeps_only_compact_solver_residual_summary() -> None:
    outcome = _solve_lp_unchecked(
        ((Decimal("0"),),),
        (Decimal("8"),),
        (Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("10"),
        maximize=True,
        symbol_cap_groups={},
    )

    assert outcome.status == "PASS"
    assert outcome.residuals is not None
    assert set(outcome.residuals) == {"ineqlin", "lower", "upper"}
    assert all(isinstance(value, Decimal) for value in outcome.residuals.values())


def test_internal_lp_builders_require_symbol_cap_groups() -> None:
    assert inspect.signature(_solve_lp_unchecked).parameters["symbol_cap_groups"].default is inspect.Parameter.empty
    assert inspect.signature(_solve_additional_lp).parameters["symbol_cap_groups"].default is inspect.Parameter.empty


def test_time_limited_valid_incumbent_is_budget_limited_not_optimal(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    observed: dict[str, object] = {}

    def timed_out(*args, **kwargs):
        observed["called"] = True
        return SimpleNamespace(
            success=False,
            status=1,
            message="Time limit reached",
            x=np.asarray((Decimal("10"), Decimal("8"), Decimal("8")), dtype=object),
        )

    monkeypatch.setattr(weighted_search_module, "linprog", timed_out)
    outcome = _solve_lp_unchecked(
        ((Decimal("1"),),),
        (Decimal("8"),),
        (Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("10"),
        maximize=True,
        symbol_cap_groups={},
    )

    assert observed["called"] is True
    assert outcome.status == "PASS"
    assert outcome.solution is not None
    assert outcome.budget_limited is True
    assert outcome.optimal is False
    assert outcome.solver_status == 1


def test_time_limited_result_without_incumbent_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")

    monkeypatch.setattr(
        weighted_search_module,
        "linprog",
        lambda *args, **kwargs: SimpleNamespace(success=False, status=1, message="Time limit reached", x=None),
    )
    outcome = _solve_lp_unchecked(
        ((Decimal("1"),),),
        (Decimal("8"),),
        (Decimal("1"),),
        max_dd=Decimal("0.2"),
        target=None,
        bank_available=Decimal("10"),
        maximize=True,
        symbol_cap_groups={},
    )

    assert outcome.status == "ERROR"
    assert outcome.reason == "SOLVER_ERROR"


def _additional_lp_kwargs() -> dict[str, object]:
    return {
        "normalized_delta": ((Decimal("0"), Decimal("0")),),
        "capacities": (Decimal("10"), Decimal("10")),
        "bank_fixed": Decimal("10"),
        "max_dd": Decimal("0.20"),
        "objective_coefficients": (Decimal("1"), Decimal("1")),
        "L": 1,
        "priorities": (5, 5),
        "margin_a": (Decimal("1"), Decimal("1")),
        "margin_b": (Decimal("0"), Decimal("0")),
        "max_mm_load": Decimal("0.35"),
        "reserve": Decimal("0.40"),
        "symbol_cap_groups": {},
    }


def test_additional_lp_fixed_bank_uses_confirmed_top_l_and_unknown_full_im() -> None:
    unknown = _solve_additional_lp(**_additional_lp_kwargs(), limiter_release_status="UNKNOWN")
    confirmed = _solve_additional_lp(
        **_additional_lp_kwargs(),
        limiter_release_status="CONFIRMED",
    )
    assert unknown.status == confirmed.status == "PASS"
    assert unknown.solution is not None and confirmed.solution is not None
    assert sum(confirmed.solution.x) > sum(unknown.solution.x)
    assert unknown.solution.bank == confirmed.solution.bank == Decimal("10")


def test_additional_lp_n6_epigraph_rows_match_hand_enumerated_loss_and_held_top_l(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    observed: dict[str, object] = {}

    def incumbent(*args, **kwargs):
        observed["A_ub"] = kwargs["A_ub"]
        # N=6, L=3: close priority 5 fully (x0,x1), then top one of the
        # priority-4 boundary (x2,x3).  Confirmed held top-L is top 3 of all
        # six IM contributions.  The compact vector includes z/w epigraphs.
        return SimpleNamespace(
            success=True,
            status=0,
            message="optimal",
            x=np.asarray(
                (
                    Decimal("100"),
                    Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"),
                    Decimal("0"), Decimal("0.045"),
                    Decimal("1"), Decimal("0"), Decimal("0"),
                    Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
                ),
                dtype=object,
            ),
        )

    monkeypatch.setattr(weighted_search_module, "linprog", incumbent)
    outcome = _solve_additional_lp(
        normalized_delta=((Decimal("0"),) * 6,),
        capacities=(Decimal("1"),) * 6,
        bank_fixed=Decimal("100"),
        max_dd=Decimal("0.20"),
        objective_coefficients=(Decimal("0"),) * 6,
        L=3,
        priorities=(5, 5, 4, 4, 3, 2),
        margin_a=(Decimal("1"),) * 6,
        margin_b=(Decimal("0"),) * 6,
        max_mm_load=Decimal("0.35"),
        reserve=Decimal("0.40"),
        symbol_cap_groups={},
        limiter_release_status="CONFIRMED",
    )
    assert outcome.status == "PASS"
    assert outcome.solution is not None and outcome.solution.x == (Decimal("1"),) * 6
    matrix = observed["A_ub"].toarray()
    # DD rows 0..1; margin rows 2..4; loss epigraph rows 5..6; loss relation
    # row 7: .015*(x0+x1+top1(x2,x3)) <= LE.
    assert matrix[7, 1:7].tolist() == [0.015, 0.015, 0.0, 0.0, 0.0, 0.0]
    assert matrix[7, 8] == -1.0 and matrix[7, 9] == 0.015
    assert matrix[7, 10] == matrix[7, 11] == 0.015
    # Confirmed Iheld is top-3 over all participants: 3*z + sum(w) is used
    # in the held margin row, not an L-scaled I_all shortcut.
    assert matrix[4, 12] == 3.0
    assert matrix[4, 13:19].tolist() == [1.0] * 6


def test_additional_lp_rejects_fixed_bank_when_incumbent_needs_more_bank(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")

    def incumbent(*args, **kwargs):
        # B=10, x=1 and path (1,-2) require B=9, so this is valid for the
        # fixed bank; the next case changes the adverse move to require >10.
        return SimpleNamespace(
            success=True,
            status=0,
            message="optimal",
            x=np.asarray((Decimal("10"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("0")), dtype=object),
        )

    monkeypatch.setattr(weighted_search_module, "linprog", incumbent)
    outcome = _solve_additional_lp(
        normalized_delta=((Decimal("1"),), (Decimal("-3"),)),
        capacities=(Decimal("1"),),
        bank_fixed=Decimal("10"),
        max_dd=Decimal("0.20"),
        objective_coefficients=(Decimal("1"),),
        L=0,
        priorities=(1,),
        margin_a=(Decimal("0"),),
        margin_b=(Decimal("0"),),
        max_mm_load=Decimal("0.35"),
        reserve=Decimal("0.40"),
        symbol_cap_groups={},
    )
    assert outcome.status == "ERROR"
    assert outcome.reason == "BANK_UNAVAILABLE"


def test_additional_lp_regenerates_one_p95_witness_and_stays_sparse(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    real_linprog = weighted_search_module.linprog
    observed: dict[str, object] = {}

    def capture(*args, **kwargs):
        observed["A_ub"] = kwargs["A_ub"]
        return real_linprog(*args, **kwargs)

    monkeypatch.setattr(weighted_search_module, "linprog", capture)
    outcome = _solve_additional_lp(
        normalized_delta=((Decimal("1"),), (Decimal("-2"),), (Decimal("1"),), (Decimal("-2"),)),
        capacities=(Decimal("1"),),
        bank_fixed=Decimal("100"),
        max_dd=Decimal("0.20"),
        objective_coefficients=(Decimal("1"),),
        L=0,
        priorities=(1,),
        margin_a=(Decimal("0"),),
        margin_b=(Decimal("0"),),
        max_mm_load=Decimal("0.35"),
        reserve=Decimal("0.40"),
        symbol_cap_groups={},
        p95_witness={"family_ordinal": 0, "scenario_index": 3},
        bootstrap_manifest={
            "seed": 17,
            "history_step_minutes": Decimal("720"),
            "families": ({"mean_block_days": Decimal("1")},),
        },
    )
    assert outcome.status == "PASS"
    matrix = observed["A_ub"]
    assert issparse(matrix) and matrix.format == "csr"
    # Two 4-point DD blocks, three margin rows, and no loss/hold epigraph for
    # off/unknown: the witness is one extra path, not all scenarios.
    assert matrix.shape == (2 * (3 * 4 - 1) + 3, 1 + 1 + 2 * 4 + 1)
    assert matrix.nnz < matrix.shape[0] * matrix.shape[1]


def test_additional_lp_timeout_incumbent_and_no_incumbent_are_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(
        weighted_search_module,
        "linprog",
        lambda *args, **kwargs: SimpleNamespace(
            success=False,
            status=1,
            message="Time limit reached",
            x=np.asarray((Decimal("10"), Decimal("1"), Decimal("1"), Decimal("0")), dtype=object),
        ),
    )
    kwargs = dict(
        normalized_delta=((Decimal("0"),),),
        capacities=(Decimal("1"),),
        bank_fixed=Decimal("10"),
        max_dd=Decimal("0.20"),
        objective_coefficients=(Decimal("1"),),
        L=0,
        priorities=(1,),
        margin_a=(Decimal("0"),),
        margin_b=(Decimal("0"),),
        max_mm_load=Decimal("0.35"),
        reserve=Decimal("0.40"),
        symbol_cap_groups={},
    )
    limited = _solve_additional_lp(**kwargs)
    assert limited.status == "PASS" and limited.budget_limited and not limited.optimal
    monkeypatch.setattr(
        weighted_search_module,
        "linprog",
        lambda *args, **kwargs: SimpleNamespace(success=False, status=1, message="Time limit reached", x=None),
    )
    failed = _solve_additional_lp(**kwargs)
    assert failed.status == "ERROR" and failed.reason == "SOLVER_ERROR"


def test_cdar80_money_and_fixed_bank_lp_use_money_tail() -> None:
    g = (Decimal("0"), Decimal("100"), Decimal("40"), Decimal("40"), Decimal("20"))
    high = Decimal("0")
    drawdowns = []
    for value in g:
        high = max(high, value)
        drawdowns.append(high - value)
    assert _cdar80_money(tuple(drawdowns)) == Decimal("80")
    outcome = _solve_cdar80_lp(
        normalized_delta=((Decimal("0"),), (Decimal("100"),), (Decimal("-60"),), (Decimal("0"),), (Decimal("-20"),)),
        capacities=(Decimal("1"),),
        bank_fixed=Decimal("100"),
        max_dd=Decimal("0.20"),
        p30_floor=Decimal("0"),
        objective_coefficients=(Decimal("1"),),
        L=0,
        priorities=(1,),
        margin_a=(Decimal("0"),),
        margin_b=(Decimal("0"),),
        max_mm_load=Decimal("0.35"),
        reserve=Decimal("0.40"),
        symbol_cap_groups={},
    )
    assert outcome.status == "PASS"
    assert outcome.solution is not None and outcome.solution.bank == Decimal("100")


def test_candidate_publishes_exact_cdar80_and_cdar90_without_series() -> None:
    candidate = _candidate_for_solution(
        _Solution(Decimal("40"), (Decimal("1"),)),
        ((Decimal("10"),), (Decimal("-1"),), (Decimal("-1"),), (Decimal("-1"),), (Decimal("-1"),), (Decimal("-1"),), (Decimal("-1"),), (Decimal("-1"),), (Decimal("-1"),), (Decimal("-1"),)),
        ({"strategy_id": 1, "symbol": "A", "side": "LONG"},),
        (Decimal("1"),),
        max_dd=Decimal("0.20"),
        common_days=Decimal("1"),
        target=None,
        profile_id="P",
        scenario_id="S",
    )
    assert candidate is not None
    assert candidate.metrics["cdar_peak80_usdt"] == Decimal("8.5")
    assert candidate.metrics["cdar_peak90_usdt"] == Decimal("9")
    assert all("series" not in key and key not in {"path", "equity"} for key in candidate.metrics)


def test_cdar_fractional_tail_uses_exact_decimal_weight_for_seven_observations() -> None:
    drawdowns = tuple(Decimal(str(value)) for value in range(7))
    with localcontext() as context:
        context.prec = 64
        expected_cdar80 = Decimal("40") / Decimal("7")
    assert _cdar80_money(drawdowns) == expected_cdar80
    assert _cdar90_money(drawdowns) == Decimal("6")


def test_cdar_families_choose_target_then_l1_distance_with_stable_ties() -> None:
    def source(identity: str, target: str, x: tuple[str, str]):
        candidate = PortfolioCandidate(
            schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=(),
            metrics={"p30_common_usdt_30d": Decimal(target)},
            status="PASS",
        )
        return candidate, Decimal(target), _Solution(Decimal("10"), tuple(Decimal(value) for value in x)), {}

    selected = _select_cdar_families(tuple(reversed((
        source("z-high", "20", ("5", "5")),
        source("a-high", "20", ("5", "5")),
        source("z-far", "10", ("9", "1")),
        source("a-far", "10", ("1", "9")),
    ))))
    assert [item[0].identity for item in selected] == ["a-high", "a-far"]


def test_cdar_families_collapse_proportional_rays_and_keep_highest_target() -> None:
    def source(identity: str, target: str, x: tuple[str, str]):
        candidate = PortfolioCandidate(
            schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=(),
            metrics={"p30_common_usdt_30d": Decimal(target)},
            status="PASS",
        )
        return candidate, Decimal(target), _Solution(Decimal("10"), tuple(Decimal(value) for value in x)), {}

    selected = _select_cdar_families((
        source("a-ray-low", "10", ("1", "1")),
        source("z-ray-high", "20", ("2", "2")),
        source("far", "30", ("9", "1")),
        source("invalid", "NaN", ("1", "9")),
    ))
    assert [item[0].identity for item in selected] == ["far", "z-ray-high"]


def test_cdar_families_choose_highest_target_for_lower_target_first_same_x() -> None:
    def source(identity: str, target: str):
        candidate = PortfolioCandidate(
            schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=(),
            metrics={"p30_common_usdt_30d": Decimal(target)},
            status="PASS",
        )
        return candidate, Decimal(target), _Solution(Decimal("10"), (Decimal("1"), Decimal("1"))), {}

    selected = _select_cdar_families((source("a-same-low", "10"), source("z-same-high", "20")))
    assert [item[0].identity for item in selected] == ["z-same-high"]


def test_cdar_families_collapse_nonterminating_proportional_ray() -> None:
    def source(identity: str, target: str, x: tuple[str, str]):
        candidate = PortfolioCandidate(
            schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=(),
            metrics={"p30_common_usdt_30d": Decimal(target)},
            status="PASS",
        )
        return candidate, Decimal(target), _Solution(Decimal("10"), tuple(Decimal(value) for value in x)), {}

    selected = _select_cdar_families((
        source("a-ray-low", "10", ("1", "2")),
        source("z-ray-high", "20", ("1e100", "2e100")),
        source("far", "30", ("9", "1")),
    ))
    assert [item[0].identity for item in selected] == ["far", "z-ray-high"]


def test_cdar_family_none_source_target_uses_realized_p30_for_maximize_upper() -> None:
    candidate = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="P",
        scenario_id="S",
        identity="maximize-upper",
        members=(),
        metrics={"p30_common_usdt_30d": Decimal("20")},
        status="PASS",
    )
    selected = _select_cdar_families(((candidate, None, _Solution(Decimal("10"), (Decimal("1"),)), {}),))
    assert selected[0][0].identity == "maximize-upper"


def test_cdar_p30_floor_uses_maximum_of_positive_target_and_baseline() -> None:
    assert _cdar_p30_floor(Decimal("12"), Decimal("10")) == Decimal("12")
    assert _cdar_p30_floor(Decimal("5"), Decimal("10")) == Decimal("9.50")
    assert _cdar_p30_floor(None, Decimal("10")) == Decimal("9.50")
    assert _cdar_p30_floor(Decimal("0"), Decimal("10")) == Decimal("9.50")
    assert _cdar_p30_floor(None, Decimal("-10")) == Decimal("0")


def test_huge_finite_lp_input_fails_closed() -> None:
    prepared = _prepared((("1e10000", "0"),))
    result = weighted_search(prepared, capacities=(Decimal("1"), Decimal("1")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=1)
    assert result.status == "FAIL"
    assert result.reason == "SOLVER_ERROR"


def test_realistic_scale_lp_reports_authoritative_bank_that_satisfies_drawdown() -> None:
    cycle = ((Decimal("0.4"), Decimal("-0.2")), (Decimal("-0.4"), Decimal("0.3")), (Decimal("0.1"), Decimal("-0.2")), (Decimal("0.1"), Decimal("0")))
    rows = cycle * 75
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    prepared = PreparedWeightedInput(
        start,
        start + timedelta(minutes=5 * len(rows)),
        5,
        tuple((start + timedelta(minutes=5 * index)).isoformat().replace("+00:00", "Z") for index in range(len(rows) + 1)),
        (1, 2),
        rows,
        tuple((True, True) for _ in rows),
        tuple((None, None) for _ in rows),
        {},
        {},
        "realistic-scale",
    )
    result = weighted_search(prepared, capacities=(Decimal("10000"), Decimal("1000000")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100000"))
    assert result.status == "PASS"
    candidate = result.candidates[0]
    x = tuple(member["x_usdt"] for member in candidate.members)
    evaluated = evaluate_weighted_path(rows, x, max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert candidate.metrics["required_bank_usdt"] == max(
        Decimal("1"), evaluated["bank_for_path"], candidate.metrics["B_risk_usdt"]
    )
    assert candidate.metrics["bank_for_path_usdt"] == evaluated["bank_for_path"]
    assert evaluated["max_drawdown_fraction"] <= Decimal("0.2")


def test_capacity_mapping_requires_one_unambiguous_strategy_key() -> None:
    prepared = _prepared((("0.4", "0.1"), ("-0.2", "0.2")))
    with pytest.raises(ValueError, match="AMBIGUOUS_CAPACITY_KEY"):
        weighted_search(prepared, capacities={1: Decimal("100"), "1": Decimal("200"), 2: Decimal("100")}, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100"))
    with pytest.raises(ValueError, match="MISSING_CAPACITY_1"):
        weighted_search(prepared, capacities={"S0": Decimal("100"), 2: Decimal("100")}, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100"))
    for stale in ({1: Decimal("100"), 2: Decimal("100"), 3: Decimal("50")}, {1: Decimal("100"), 2: Decimal("100"), "S0": Decimal("50")}):
        with pytest.raises(ValueError, match="UNKNOWN_CAPACITY_KEY"):
            weighted_search(prepared, capacities=stale, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("100"))


def test_capacity_mapping_and_sequence_are_equivalent() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    sequence = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450"))
    mapping = weighted_search(prepared, capacities={1: Decimal("100"), 2: Decimal("100")}, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450"))
    assert tuple(member["x_usdt"] for member in sequence.candidates[0].members) == tuple(member["x_usdt"] for member in mapping.candidates[0].members)
    assert sequence.candidates[0].metrics["required_bank_usdt"] == mapping.candidates[0].metrics["required_bank_usdt"]
    assert sequence.candidates[0].identity == mapping.candidates[0].identity


def test_capacity_sequence_requires_exact_member_count() -> None:
    prepared = _prepared((("0.4", "0.1"),))
    for capacities in ((Decimal("100"),), (Decimal("100"), Decimal("100"), Decimal("100"))):
        with pytest.raises(ValueError, match="CAPACITY_SHAPE_MISMATCH"):
            weighted_search(prepared, capacities=capacities, members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"))


def test_authoritative_bank_available_check_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(weighted_search_module, "bank_for_path", lambda path, max_dd: Decimal("1.00000000001"))
    prepared = _prepared((("0.4", "0.1"),))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), bank_available=Decimal("1"))
    assert result.status == "FAIL"
    assert result.reason == "BANK_UNAVAILABLE"


def test_genuine_infeasible_target_keeps_target_infeasible_reason() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("1000"))
    assert result.status == "FAIL"
    assert result.reason == "TARGET_INFEASIBLE"


def test_solver_error_fails_closed_without_partial_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(weighted_search_module, "linprog", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("solver down")))
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=2)
    assert result.status == "FAIL"
    assert result.reason == "SOLVER_ERROR"
    assert result.candidates == ()


def test_solver_error_stops_before_bootstrap_after_a_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    calls = 0

    def solver(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return weighted_search_module._SolveOutcome("PASS", _Solution(Decimal("1"), (Decimal("1"), Decimal("1"))))
        return weighted_search_module._SolveOutcome("ERROR", reason="SOLVER_ERROR")

    bootstrap_called = False

    def bootstrap(*args, **kwargs):
        nonlocal bootstrap_called
        bootstrap_called = True
        raise AssertionError("technical solver errors must stop before bootstrap")

    monkeypatch.setattr(weighted_search_module, "_solve_lp", solver)
    monkeypatch.setattr(weighted_search_module, "bootstrap_banks", bootstrap)
    result = weighted_search(
        _prepared((("0.4", "0.1"), ("0.1", "0.1"))),
        (Decimal("100"), Decimal("100")),
        members=_members(),
        max_dd=Decimal("0.2"),
        common_days=Decimal("1"),
        max_targets=2,
    )
    assert result.status == "FAIL"
    assert result.reason == "SOLVER_ERROR"
    assert result.candidates == ()
    assert bootstrap_called is False


def test_weighted_lp_returns_candidate_search_result_without_raw_payload() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.20"), common_days=Decimal("1"), target_p30=Decimal("450"))

    assert isinstance(result, SearchResult)
    assert not isinstance(result, __import__("mrs3.portfolio.search", fromlist=["SearchResult"]).SearchResult)
    assert result.mode == "WEIGHTED_V1"
    assert result.status == "PASS"
    assert isinstance(result.candidates[0], PortfolioCandidate)
    assert result.candidates[0].metrics["p30_common_usdt_30d"] >= Decimal("450")
    member = result.candidates[0].members[0]
    assert member["x_usdt"] >= 0
    assert member["capacity_usdt"] == Decimal("100")
    assert all(key not in member for key in ("normalized_delta", "equity", "actions", "cycles"))
    assert all(key not in result.candidates[0].metrics for key in ("normalized_delta", "equity", "actions", "cycles"))


def test_negative_participant_is_retained_when_it_reduces_required_bank() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.20"), common_days=Decimal("1"), target_p30=Decimal("450"))
    sizes = {member["strategy_id"]: member["x_usdt"] for member in result.candidates[0].members}
    assert sizes[2] > 0


def test_weighted_search_rejects_any_invalid_participant_cell() -> None:
    prepared = _prepared((("0.1", "0.2"), ("0", "0")))
    prepared = PreparedWeightedInput(
        prepared.period_start_utc,
        prepared.period_end_utc,
        prepared.history_step_minutes,
        prepared.timestamps_utc,
        prepared.strategy_ids,
        prepared.normalized_delta,
        ((True, False), (True, True)),
        prepared.reasons,
        prepared.cycles,
        prepared.diagnostics,
        prepared.preparation_key,
    )
    with pytest.raises(ValueError, match="INVALID_PARTICIPANT_CELL"):
        weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"))


def test_evaluate_weighted_path_recomputes_drawdown_after_diversifier_removed() -> None:
    deltas = ((Decimal("0.4"), Decimal("-0.2")), (Decimal("-0.4"), Decimal("0.3")), (Decimal("0.1"), Decimal("-0.2")), (Decimal("0.1"), Decimal("0")))
    with_diversifier = evaluate_weighted_path(deltas, (Decimal("100"), Decimal("50")), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    without_diversifier = evaluate_weighted_path(deltas, (Decimal("100"), Decimal("0")), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert without_diversifier["bank_for_path"] > with_diversifier["bank_for_path"]


def test_priority_is_close_stress_order_not_an_entry_queue() -> None:
    members = (
        {"strategy_id": "slow", "mean_hold": "10", "hold90": "10", "mean_net_pnl": "10"},
        {"strategy_id": "fast", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "3"},
    )
    priorities = derive_priorities(members)
    assert priorities["fast"] == 1 and priorities["slow"] == 2


def test_priority_uses_numeric_strategy_ties_and_does_not_average_partial_cycles() -> None:
    members = (
        {"strategy_id": 10, "cycles": ({"duration_seconds": Decimal("3600"), "net_pnl": Decimal("2")},)},
        {"strategy_id": 2, "cycles": ({"duration_seconds": Decimal("3600"), "net_pnl": Decimal("2")},)},
        {"strategy_id": 1, "cycles": ({"duration_seconds": Decimal("3600"),}, {"duration_seconds": Decimal("3600"), "net_pnl": Decimal("4")})},
    )
    details = __import__("mrs3.portfolio.weighted_search", fromlist=["priority_details"]).priority_details(members)
    assert details[2]["priority"] == details[10]["priority"] == 1
    assert details[1]["slot_score"] is None


def test_limiter_replay_releases_before_equal_timestamp_starts_and_skips_without_queue() -> None:
    result = replay_limiter(
        (
            {"cycle_id": "A", "strategy_id": "A", "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
            {"cycle_id": "B", "strategy_id": "B", "first_fill": 1, "final_flat": 3, "common_window_normalized_return": "20", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
            {"cycle_id": "C", "strategy_id": "C", "first_fill": 2, "final_flat": 4, "common_window_normalized_return": "30", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
        ),
        1,
        common_days=Decimal("1"),
    )
    assert result.status == "MODEL"
    assert result.accepted_cycle_ids == ("A", "C") and result.rejected_cycle_ids == ("B",)
    assert result.p30_limiter == Decimal("1200")


def test_same_symbol_opposite_sides_share_one_lp_capacity_and_candidate_cap() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("1", "1"),), strategy_ids=(1, 2))
    members = (
        {"symbol": " btcusdt ", "side": "LONG", "strategy_id": 1, "result_id": 101},
        {"symbol": "BTCUSDT", "side": "SHORT", "strategy_id": 2, "result_id": 102},
    )

    with pytest.raises(ValueError, match=r"SYMBOL_CAPACITY_MISMATCH:BTCUSDT"):
        weighted_search(
            prepared,
            (Decimal("100"), Decimal("101")),
            members=members,
            max_dd=Decimal("0.2"),
            common_days=Decimal("1"),
            max_targets=1,
        )

    with pytest.raises(ValueError, match=r"MISSING_SYMBOL"):
        module._symbol_cap_groups(({"strategy_id": 1},), (Decimal("100"),))
    with pytest.raises(ValueError, match=r"MISSING_SYMBOL"):
        module._symbol_cap_groups(({"strategy_id": 1, "symbol": None},), (Decimal("100"),))
    with pytest.raises(ValueError, match=r"MISSING_SYMBOL"):
        module._symbol_cap_groups(({"strategy_id": 1, "pair": "BTCUSDT"},), (Decimal("100"),))

    with pytest.raises(ValueError, match=r"SYMBOL_CAPACITY_EXCEEDED:BTCUSDT"):
        module._validate_symbol_cap_vector(
            (Decimal("60"), Decimal("50")), members, (Decimal("100"), Decimal("100"))
        )
    module._validate_symbol_cap_vector(
        (Decimal("60"), Decimal("40")), members, (Decimal("100"), Decimal("100"))
    )

    failure_reason: list[str] = []
    assert module._candidates_for_solution(
        _Solution(Decimal("1000"), (Decimal("60"), Decimal("50"))),
        prepared.normalized_delta,
        members,
        (Decimal("100"), Decimal("100")),
        max_dd=Decimal("0.2"),
        common_days=Decimal("1"),
        target=None,
        profile_id="P",
        scenario_id="S",
        failure_reason=failure_reason,
    ) == ()
    assert failure_reason == ["SYMBOL_CAPACITY_EXCEEDED:BTCUSDT"]

    outcome = _solve_lp_unchecked(
        ((Decimal("1"), Decimal("1")),),
        (Decimal("100"), Decimal("100")),
        (Decimal("1"), Decimal("1")),
        max_dd=Decimal("0.2"),
        target=Decimal("150"),
        bank_available=None,
        maximize=False,
        symbol_cap_groups={"BTCUSDT": (0, 1)},
    )
    assert outcome.status == "INFEASIBLE" and outcome.reason == "LP_INFEASIBLE"


def test_replay_same_symbol_opposite_sides_use_distinct_slots() -> None:
    cycles = (
        {
            "cycle_id": "long",
            "strategy_id": 1,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "first_fill": 0,
            "final_flat": 2,
            "common_window_normalized_return": "10",
            "equity_attribution": True,
            "attribution_complete": True,
        },
        {
            "cycle_id": "short",
            "strategy_id": 2,
            "symbol": "BTCUSDT",
            "side": "SHORT",
            "first_fill": 0,
            "final_flat": 2,
            "common_window_normalized_return": "20",
            "equity_attribution": True,
            "attribution_complete": True,
        },
    )
    one = replay_limiter(cycles, 1, common_days=Decimal("1"))
    two = replay_limiter(cycles, 2, common_days=Decimal("1"))
    assert one.accepted_cycle_ids == ("long",)
    assert one.rejected_cycle_ids == ("short",)
    assert two.accepted_cycle_ids == ("long", "short")
    assert two.rejected_cycle_ids == ()


def test_limiter_model_coefficients_follow_replay_mask_and_scale_by_x() -> None:
    cycles = (
        {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "0.10", "attribution_complete": True},
        {"cycle_id": "B", "strategy_id": 2, "first_fill": 2, "final_flat": 4, "common_window_normalized_return": "-0.05", "attribution_complete": True},
    )
    x = {1: Decimal("10"), 2: Decimal("20")}
    replay = replay_limiter(cycles, 1, common_days=Decimal("1"), x=x)
    status, coefficients = _limiter_model_coefficients(cycles, (1, 2), replay, common_days=Decimal("1"))

    assert status == "MODEL"
    assert coefficients == (Decimal("3.0"), Decimal("-1.5"))
    assert _sum_products(coefficients, tuple(x.values())) == replay.p30_limiter
    off = replay_limiter(cycles, 0, common_days=Decimal("1"), common_p30=Decimal("0"), x=x)
    assert off.status == "MODEL" and off.p30_limiter == _sum_products(coefficients, tuple(x.values()))


def test_limiter_model_coefficients_fails_closed_for_unavailable_attribution() -> None:
    cycle = {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "0.10", "attribution_complete": False}
    replay = LimiterReplayResult("UNKNOWN", None, accepted_mask=(True,))
    assert _limiter_model_coefficients((cycle,), (1,), replay, common_days=Decimal("1")) == ("UNKNOWN", None)


def test_limiter_replay_requires_attribution_and_supports_known_carry_in() -> None:
    missing = replay_limiter(({"cycle_id": "A", "strategy_id": "A", "first_fill": 0, "final_flat": 2, "net_pnl": "10"},), 1, common_days=Decimal("1"))
    assert missing.status == "UNKNOWN"
    known = replay_limiter((
        {"cycle_id": "A", "strategy_id": "A", "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "10", "carry_in_count": 1, "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
    ), 1, common_days=Decimal("1"), period_start=0, period_end=2)
    assert known.status == "MODEL" and known.accepted_cycle_ids == ("A",)


def test_limiter_replay_does_not_reuse_raw_realized_pnl_without_normalized_contribution() -> None:
    result = replay_limiter((
        {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "net_pnl": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
    ), 1, common_days=Decimal("1"))
    assert result.status == "UNKNOWN"


def test_limiter_replay_l0_requires_and_verifies_common_p30() -> None:
    cycle = {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"}
    assert replay_limiter((cycle,), 0, common_days=Decimal("1"), common_p30=Decimal("300")).status == "MODEL"
    assert replay_limiter((cycle,), 0, common_days=Decimal("1"), common_p30=Decimal("301")).status == "UNKNOWN"


def test_limiter_replay_equal_start_uses_numeric_strategy_id_without_queue() -> None:
    def cycle(cycle_id, strategy_id, pnl):
        return {"cycle_id": cycle_id, "strategy_id": strategy_id, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": pnl, "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"}

    result = replay_limiter((cycle("ten", 10, "10"), cycle("two", 2, "20")), 1, common_days=Decimal("1"))
    assert result.accepted_cycle_ids == ("two",) and result.rejected_cycle_ids == ("ten",)


def test_limiter_replay_normalizes_timestamp_offsets_and_keeps_right_edge_open_occupied() -> None:
    result = replay_limiter((
        {"cycle_id": "B", "strategy_id": 2, "first_fill": "2026-01-01T01:00:00+01:00", "final_flat": None, "common_window_normalized_return": "20", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
        {"cycle_id": "A", "strategy_id": 1, "first_fill": "2026-01-01T00:00:00Z", "final_flat": "2026-01-01T00:30:00Z", "common_window_normalized_return": "10", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
        {"cycle_id": "C", "strategy_id": 3, "first_fill": "2026-01-01T01:30:00Z", "final_flat": "2026-01-01T01:45:00Z", "common_window_normalized_return": "30", "equity_attribution": True, "attribution_complete": True, "boundary_upnl_start": "0", "boundary_upnl_end": "0", "net_cost": "0"},
    ), 1, common_days=Decimal("1"), period_start="2026-01-01T00:00:00Z", period_end="2026-01-01T02:00:00Z")
    assert result.status == "MODEL" and result.accepted_cycle_ids == ("A", "C") and result.rejected_cycle_ids == ("B",)


def test_priority_uses_effective_hold_and_selected_x_with_duration_seconds_and_normalized_pnl() -> None:
    details = __import__("mrs3.portfolio.weighted_search", fromlist=["priority_details"]).priority_details(
        ({"strategy_id": 7, "cycles": (
            {"duration_seconds": "3600", "normalized_pnl": "10"},
            {"duration_seconds": "7200", "normalized_pnl": "20"},
        )},),
        {7: "2"},
    )
    assert details[7]["T_eff"] == Decimal("1.75")
    assert details[7]["mean_net_pnl"] == Decimal("30")
    assert details[7]["slot_score"] == Decimal(30) / Decimal("1.75")
    assert __import__("mrs3.portfolio.weighted_search", fromlist=["priority_details"]).priority_details(({"strategy_id": 7, "duration_seconds": "3600", "normalized_pnl": "10"},))[7]["slot_score"] is None


def test_priority_marks_incomplete_cycle_evidence_unknown_and_keeps_nonpositive_last() -> None:
    priorities = derive_priorities((
        {"strategy_id": 1, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "10"},
        {"strategy_id": 2, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "0"},
        {"strategy_id": 3, "cycles": ({"duration_seconds": "3600", "normalized_pnl": "1"}, {"duration_seconds": "3600"})},
    ))
    assert priorities[1] == 1 and priorities[2] == 2 and priorities[3] == 2
    assert derive_priorities(({"strategy_id": 4, "mean_hold": "1", "hold90": "1"},))[4] == 1


def test_priority_creates_no_more_than_five_groups_and_uses_half_group_max() -> None:
    members = tuple({"strategy_id": index, "mean_hold": "1", "hold90": "1", "mean_net_pnl": str(score)} for index, score in enumerate((100, 49, 24, 11, 5, 2), 1))
    priorities = derive_priorities(members)
    assert [priorities[index] for index in range(1, 7)] == [1, 2, 3, 4, 5, 5]


def test_priority_mixed_absolute_and_normalized_units_fails_closed() -> None:
    members = (
        {"strategy_id": 1, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "10"},
        {"strategy_id": 2, "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "10"},
    )
    assert derive_priorities(members, (Decimal("1"), Decimal("1"))) == {1: None, 2: None}


def test_limiter_replay_rejects_mixed_timestamp_types_and_missing_complete_attribution() -> None:
    base = {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "10"}
    mixed = replay_limiter((base, {**base, "cycle_id": "B", "strategy_id": 2, "first_fill": "1970-01-01T00:00:01Z", "final_flat": "1970-01-01T00:00:02Z", "attribution_complete": True}), 1, common_days=Decimal("1"))
    missing = replay_limiter((base,), 1, common_days=Decimal("1"))
    assert mixed.status == missing.status == "UNKNOWN"


def test_limiter_replay_carry_in_overflow_and_open_right_edge_fail_closed() -> None:
    carry = replay_limiter(({
        "cycle_id": "A", "strategy_id": 1, "first_fill": -1, "final_flat": 2,
        "carry_in_count": 2, "common_window_normalized_return": "10", "attribution_complete": True,
    },), 1, common_days=Decimal("1"), period_start=0, period_end=2)
    open_without_edge = replay_limiter(({
        "cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": None,
        "common_window_normalized_return": "10", "attribution_complete": True,
    },), 1, common_days=Decimal("1"))
    assert carry.status == open_without_edge.status == "UNKNOWN"


def test_limiter_replay_uses_ordinal_then_cycle_id_and_records_simultaneous_conflict() -> None:
    def cycle(cycle_id, ordinal, strategy_id):
        return {"cycle_id": cycle_id, "source_ordinal": ordinal, "strategy_id": strategy_id, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "10", "attribution_complete": True}

    result = replay_limiter((cycle("later", 2, 10), cycle("first", 1, 2)), 1, common_days=Decimal("1"))
    assert result.accepted_cycle_ids == ("first",) and result.rejected_cycle_ids == ("later",)
    assert result.witness["simultaneous_conflict_count"] == 1
    assert result.witness["simultaneous_conflicts"][0]["rejected_cycle_ids"] == ("later",)


def test_limiter_replay_numeric_ids_precede_lexical_ids_and_priority_never_changes_admission() -> None:
    def cycle(cycle_id, strategy_id, priority):
        return {"cycle_id": cycle_id, "strategy_id": strategy_id, "priority": priority, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "10", "attribution_complete": True}

    numeric = replay_limiter((cycle("lexical", "2", 1), cycle("numeric", 10, 5)), 1, common_days=Decimal("1"))
    assert numeric.accepted_cycle_ids == ("numeric",)
    first = replay_limiter((cycle("A", 10, 5), cycle("B", 2, 1)), 1, common_days=Decimal("1"))
    reversed_priorities = replay_limiter((cycle("A", 10, 1), cycle("B", 2, 5)), 1, common_days=Decimal("1"))
    assert first.accepted_cycle_ids == reversed_priorities.accepted_cycle_ids == ("B",)


def test_limiter_replay_off_requires_exact_common_p30_and_rejects_alias_or_raw_pnl() -> None:
    cycle = {"cycle_id": "A", "strategy_id": 1, "first_fill": 0, "final_flat": 2, "common_window_normalized_return": "10", "attribution_complete": True}
    mismatch = replay_limiter((cycle,), 0, common_days=Decimal("1"), common_p30=Decimal("300.00001"))
    alias = replay_limiter(({**cycle, "common_window_normalized_return": None, "common_window_equity": "10", "equity_contribution": "10"},), 1, common_days=Decimal("1"))
    raw = replay_limiter(({**cycle, "common_window_normalized_return": None, "realized_pnl": "10", "fees": "1"},), 1, common_days=Decimal("1"))
    assert mismatch.status == alias.status == raw.status == "UNKNOWN"


def test_weighted_search_keeps_negative_overall_target_empty() -> None:
    prepared = _prepared((("-0.1", "-0.2"), ("0", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert result.status == "FAIL"
    assert result.reason == "NO_POSITIVE_TARGET"


def test_weighted_lp_is_no_worse_than_a_coarse_grid() -> None:
    deltas = (("0.5", "-0.1"), ("-0.1", "0.3"), ("0.1", "-0.2"))
    prepared = _prepared(deltas)
    result = weighted_search(prepared, capacities=(Decimal("10"), Decimal("10")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("75"))
    assert result.status == "PASS"
    lp_bank = result.candidates[0].metrics["bank_for_path_usdt"]
    grid_banks = []
    for x0 in (Decimal("0"), Decimal("2.5"), Decimal("5"), Decimal("7.5"), Decimal("10")):
        for x1 in (Decimal("0"), Decimal("2.5"), Decimal("5"), Decimal("7.5"), Decimal("10")):
            evaluated = evaluate_weighted_path(
                tuple(tuple(Decimal(value) for value in row) for row in deltas), (x0, x1), max_dd=Decimal("0.2"), common_days=Decimal("1"),
            )
            if evaluated["p30_common"] >= Decimal("75"):
                grid_banks.append(evaluated["bank_for_path"])
    assert grid_banks
    assert lp_bank <= min(grid_banks) + Decimal("0.000001")


def test_homogeneous_participants_keep_the_same_ray() -> None:
    first = _prepared((("0.2", "0.4"), ("0.1", "0.2")))
    second = weighted_search(first, capacities=(Decimal("10"), Decimal("10")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("270"))
    assert second.status == "PASS"
    members = second.candidates[0].members
    assert members[0]["x_usdt"] == members[1]["x_usdt"] == Decimal("10")


def test_active_capacity_changes_share_and_identity() -> None:
    prepared = _prepared((("0.4", "0.1"), ("-0.2", "0.2"), ("0.1", "0"), ("0.1", "0")))
    broad = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    constrained = weighted_search(prepared, capacities=(Decimal("100"), Decimal("20")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    assert broad.status == constrained.status == "PASS"
    assert broad.candidates[0].identity != constrained.candidates[0].identity
    assert broad.candidates[0].members != constrained.candidates[0].members


def test_missing_strategy_id_is_rejected_and_shuffled_members_are_reassociated() -> None:
    prepared = _prepared((("0.4", "0.1"), ("-0.2", "0.2"), ("0.1", "0"), ("0.1", "0")))
    missing = ({"symbol": "S0", "side": "LONG", "result_id": 100}, {"symbol": "S1", "side": "LONG", "strategy_id": 2, "result_id": 101})
    with pytest.raises(ValueError, match="MEMBER_STRATEGY_SHAPE_MISMATCH"):
        weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=missing, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    shuffled = weighted_search(prepared, capacities={1: Decimal("100"), 2: Decimal("20")}, members=(_members()[1], _members()[0]), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("900"))
    assert shuffled.status == "PASS"
    assert [member["strategy_id"] for member in shuffled.candidates[0].members] == [1, 2]


def test_weighted_target_frontier_k1_and_k2_have_deterministic_order_and_counts() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    one = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=1)
    two = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=2)
    assert [candidate.metrics["target_p30_usdt_30d"] for candidate in one.candidates] == [Decimal("600")]
    assert [candidate.metrics["target_p30_usdt_30d"] for candidate in two.candidates] == [Decimal("600"), Decimal("300")]
    assert one.total_combinations == one.evaluated == 1
    assert two.total_combinations == two.evaluated == 2


def test_weighted_target_frontier_deduplicates_initial_targets_and_refines_active_capacity_interval() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    result = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=4)
    targets = [candidate.metrics["target_p30_usdt_30d"] for candidate in result.candidates]
    assert targets[:3] == [Decimal("600"), Decimal("300"), Decimal("450")]
    assert len(targets) == len(set(targets)) == 4
    assert targets[3] == Decimal("150")
    assert result.total_combinations == result.evaluated == 4
    first = result.candidates[0].members
    refined = result.candidates[-1].members
    assert tuple(member["x_usdt"] for member in first) != tuple(member["x_usdt"] for member in refined)


def test_explicit_target_and_available_bank_do_not_generate_a_frontier() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    explicit = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450"), max_targets=8)
    available = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), bank_available=Decimal("200"), max_targets=8)
    assert len(explicit.candidates) == len(available.candidates) == 1


def test_no_candidate_reasons_distinguish_explicit_bank_and_frontier_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3")))
    bank_limited = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), bank_available=Decimal("0.5"))
    assert bank_limited.status == "FAIL"
    assert bank_limited.reason == "LP_INFEASIBLE"
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    monkeypatch.setattr(weighted_search_module, "_solve_lp", lambda *args, **kwargs: weighted_search_module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE"))
    frontier = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=2)
    assert frontier.status == "FAIL"
    assert frontier.reason == "FRONTIER_INFEASIBLE"


def test_low_global_precision_preserves_frontier_targets_and_identity() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    default = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=4)
    with localcontext() as context:
        context.prec = 2
        low_precision = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), max_targets=4)
        direct_300 = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("300"), max_targets=4)
    assert direct_300.status == default.status == "PASS"
    assert [candidate.metrics["target_p30_usdt_30d"] for candidate in low_precision.candidates] == [candidate.metrics["target_p30_usdt_30d"] for candidate in default.candidates]
    assert [candidate.identity for candidate in low_precision.candidates] == [candidate.identity for candidate in default.candidates]


def test_candidate_members_are_immutably_frozen() -> None:
    prepared = _prepared((("0.4", "-0.2"), ("-0.4", "0.3"), ("0.1", "-0.2"), ("0.1", "0")))
    candidate = weighted_search(prepared, capacities=(Decimal("100"), Decimal("100")), members=_members(), max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("450")).candidates[0]
    original_identity = candidate.identity
    with pytest.raises(TypeError):
        candidate.members[0]["x_usdt"] = Decimal("1")
    assert candidate.identity == original_identity


def test_rounding_out_a_planned_diversifier_recomputes_realized_drawdown() -> None:
    rows = ((Decimal("1"), Decimal("-1")), (Decimal("-2"), Decimal("2")), (Decimal("2"), Decimal("0")))
    prepared = _prepared(tuple(tuple(str(value) for value in row) for row in rows))
    members = (
        {"symbol": "A", "side": "LONG", "strategy_id": 1, "result_id": 101, "strategy_orders": ({"order_id": 1, "lot_x": Decimal("1")},)},
        {"symbol": "B", "side": "LONG", "strategy_id": 2, "result_id": 102, "strategy_orders": ({"order_id": 1, "lot_x": Decimal("1")},)},
    )
    plan = weighted_search(prepared, capacities=(Decimal("100"), Decimal("5")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("3150"))
    assert plan.status == "PASS"
    planned_x = tuple(member["x_usdt"] for member in plan.candidates[0].members)
    references = {
        "A": {"qty_step": "0.1", "min_qty": "0.1", "max_qty": "100", "min_notional": "1"},
        "B": {"qty_step": "0.01", "min_qty": "0.1", "max_qty": "100", "min_notional": "1"},
    }
    sized = size_composition_vector(
        members,
        {"A": {"position_cap_usdt": "100", "round_down_usdt": "0.01"}, "B": {"position_cap_usdt": "5", "round_down_usdt": "0.01"}},
        references,
        {"A": Decimal("100"), "B": Decimal("100")},
        targets=planned_x,
    )
    assert sized.status == "PASS"
    assert sized.exclusions[0].reason == "SIZE_BELOW_MINIMUM_QTY"
    realized_x = tuple(next((member["actual_size_usdt"] for member in sized.members if member["strategy_id"] == strategy_id), Decimal("0")) for strategy_id in (1, 2))
    planned = evaluate_weighted_path(rows, planned_x, max_dd=Decimal("0.2"), common_days=Decimal("1"))
    realized = evaluate_weighted_path(rows, realized_x, max_dd=Decimal("0.2"), common_days=Decimal("1"))
    assert realized["bank_for_path"] > planned["bank_for_path"]


def _margin15_fixture():
    x = (Decimal("1000"),) * 15
    members = tuple({"strategy_id": index, "symbol": f"S{index}", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for index in range(15))
    coefficients = _evidence_coefficients(((Decimal("0.10"), Decimal("0.005")),) * 15, range(15), max_notional=Decimal("1000"))
    cycles = tuple({
        "cycle_id": index, "strategy_id": index, "first_fill": index * 2,
        "final_flat": index * 2 + 1, "common_window_normalized_return": "0.001",
        "attribution_complete": True,
    } for index in range(15))
    options = {
        "max_dd": Decimal("0.10"), "reserve": Decimal("0.40"),
        "max_mm_load": Decimal("0.35"),
        "priorities": {index: 1 for index in range(15)},
        "strategy_ids": tuple(range(15)), "limiter_release_status": "CONFIRMED",
        "release_evidence": {"digest": "fixture", "identity": "fixture"},
        "cycles": cycles, "common_days": Decimal("1"), "common_p30": Decimal("450"),
    }
    return x, members, coefficients, options


def test_margin_variants_check_every_strict_l_before_bank_rejection() -> None:
    x, members, coefficients, options = _margin15_fixture()
    primary, variants = _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))
    assert primary is not None and primary.L == 10
    assert tuple(item.L for item in variants) == tuple(range(15))
    assert variants[0].status == "FAIL" and variants[10].status == "PASS"
    assert all(item.L != 15 for item in variants)


def test_margin_replay_is_compact_and_model_order_uses_replayed_p30() -> None:
    x, members, coefficients, options = _margin15_fixture()
    _primary, variants = _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))
    assert all(item.p30_status == "MODEL" for item in variants)
    assert variants[10].p30 == Decimal("450")
    compact = {"L": variants[10].L, "p30": variants[10].p30, "mask": variants[10].replay.accepted_mask}
    assert "cycle_id" not in compact and "cycles" not in compact


def test_unknown_fixed_bank_order_is_off_then_strict_l_descending() -> None:
    x, members, coefficients, options = _margin15_fixture()
    options = {key: value for key, value in options.items() if key not in {"cycles", "common_p30"}}
    _primary, variants = _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))
    ordered = _ordered_margin_variants(variants, bank_available=Decimal("2000"))
    assert all(item.p30_status == "UNKNOWN" for item in ordered)
    assert [item.L for item in ordered] == list(range(10, 0, -1))


def test_margin_candidate_recomputes_priority_and_identity_for_supplied_x() -> None:
    members = (
        {"strategy_id": 1, "symbol": "A", "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "1"},
        {"strategy_id": 2, "symbol": "B", "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "1"},
    )
    common = dict(max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None, profile_id="P", scenario_id="S", margin_coefficients=_evidence_coefficients(((Decimal("0.1"), Decimal("0.01")),) * 2), margin_kwargs={"limiter_release_status": "UNKNOWN"})
    first = _candidates_for_solution(_Solution(Decimal("1"), (Decimal("10"), Decimal("1"))), ((Decimal("0"), Decimal("0")),), members, (Decimal("100"), Decimal("100")), **common)[0]
    second = _candidates_for_solution(_Solution(Decimal("1"), (Decimal("1"), Decimal("10"))), ((Decimal("0"), Decimal("0")),), members, (Decimal("100"), Decimal("100")), **common)[0]
    assert [member["priority"] for member in first.members] == [1, 2]
    assert [member["priority"] for member in second.members] == [2, 1]
    assert first.identity != second.identity
    assert all(key not in first.metrics for key in ("cycles", "equity", "actions"))


def test_path_bank_dominates_margin_risk_and_fixed_bank_eligibility() -> None:
    members = (
        {"strategy_id": 1, "symbol": "A", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "symbol": "B", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
    )
    candidate = _candidates_for_solution(
        _Solution(Decimal("1800"), (Decimal("100"), Decimal("100"))),
        ((Decimal("1"), Decimal("1")), (Decimal("-1"), Decimal("-1"))),
        members,
        (Decimal("100"), Decimal("100")),
        max_dd=Decimal("0.10"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=_evidence_coefficients(((Decimal("0.10"), Decimal("0.01")),) * 2),
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
        bank_available=Decimal("1800"),
    )[0]
    assert candidate.metrics["bank_for_path_usdt"] == Decimal("1800")
    assert candidate.metrics["B_required_margin_usdt"] == Decimal("1800")
    assert candidate.metrics["bank_feasible"] is True


def test_margin_option_alias_is_rejected_before_l_evaluation() -> None:
    x, members, coefficients, options = _margin15_fixture()
    options = {**options, "m": Decimal("0.1")}
    with pytest.raises(ValueError, match="MARGIN_OPTION_UNKNOWN"):
        _margin_variant(x, members, coefficients, options=options, bank_available=Decimal("2000"))


def test_weighted_search_blocks_bare_or_unknown_margin_coefficients_but_accepts_frozen_evidence() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    members = tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members())
    kwargs = {"limiter_release_status": "UNKNOWN"}
    bare = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), margin_coefficients=((Decimal("0.1"), Decimal("0.01")),) * 2, margin_kwargs=kwargs)
    unknown = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), margin_coefficients=MarginCoefficientResult("UNKNOWN", (), "UNKNOWN"), margin_kwargs=kwargs)
    valid = MarginCoefficientResult("PASS", tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", {"declared_state_count": 1, "evaluated_state_count": 1}, max_notional=Decimal("100")) for index in range(2)), "CONSERVATIVE_BOUND", witness={"declared_state_count": 2, "evaluated_state_count": 2})
    accepted = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=members, max_dd=Decimal("0.2"), common_days=Decimal("1"), target_p30=Decimal("10"), margin_coefficients=valid, margin_kwargs=kwargs)
    assert bare.status == unknown.status == "FAIL"
    assert accepted.status == "PASS"


def test_domainless_margin_evidence_fails_at_public_boundaries() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    domainless = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND") for index in range(2))
    blocked = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=_members(),
        target_p30=Decimal("10"),
        margin_coefficients=domainless,
    )
    from mrs3.portfolio.margin import evaluate_weighted_margin
    mapped_none = evaluate_weighted_margin(
        (Decimal("10"),),
        {1: {"a": Decimal("0.1"), "b": Decimal("0.01"), "evidence_class": "CONSERVATIVE_BOUND", "max_notional": None}},
        strategy_ids=(1,),
        priorities=(1,),
    )
    assert blocked.status == "FAIL" and blocked.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert mapped_none.status == "UNKNOWN" and mapped_none.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_margin_risk_and_bank_options_are_rejected_as_user_overrides() -> None:
    x, members, coefficients, options = _margin15_fixture()
    for key in ("B_risk", "bank_available"):
        with pytest.raises(ValueError, match="MARGIN_OPTION_UNKNOWN"):
            _margin_variant(x, members, coefficients, options={**options, key: Decimal("1")}, bank_available=Decimal("2000"))


def test_margin_strategy_and_priority_shapes_are_exact() -> None:
    x, members, coefficients, options = _margin15_fixture()
    with pytest.raises(ValueError, match="MARGIN_STRATEGY_SHAPE_MISMATCH"):
        _margin_variant(x, members, coefficients, options={**options, "strategy_ids": tuple(reversed(range(15)))}, bank_available=Decimal("2000"))
    with pytest.raises(ValueError, match="PRIORITY_SHAPE_MISMATCH"):
        _margin_variant(x, members, coefficients, options={**options, "priorities": (1,)}, bank_available=Decimal("2000"))


def test_frontier_preserves_real_candidate_before_domain_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("1", "1"), ("1", "1")))
    members = tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members())
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("200")) for index in range(2))
    solutions = iter((
        weighted_search_module._SolveOutcome("PASS", _Solution(Decimal("1"), (Decimal("10"), Decimal("10")))),
        weighted_search_module._SolveOutcome("PASS", _Solution(Decimal("1"), (Decimal("201"), Decimal("10")))),
    ))
    monkeypatch.setattr(weighted_search_module, "_solve_lp", lambda *args, **kwargs: next(solutions))
    monkeypatch.setattr(weighted_search_module, "_solve_cdar80_lp", lambda *args, **kwargs: weighted_search_module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE"))
    result = weighted_search(
        prepared,
        (Decimal("200"), Decimal("200")),
        members=members,
        common_days=Decimal("1"),
        max_targets=2,
        margin_coefficients=coefficients,
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "PASS" and len(result.candidates) == 1
    assert result.warnings == ("MARGIN_COEFFICIENT_DOMAIN_EXCEEDED",)


def test_priority_unknown_is_reported_at_margin_boundary() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    members = (
        {"strategy_id": 1, "symbol": "S0", "side": "LONG", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "symbol": "S1", "side": "LONG", "mean_hold": "1", "hold90": "1", "mean_normalized_pnl": "1"},
    )
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        target_p30=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "FAIL" and result.reason == "PRIORITY_UNKNOWN"


def test_margin_arguments_without_coefficients_fail_closed() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=_members(),
        target_p30=Decimal("10"),
        margin_kwargs={"limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "FAIL" and result.reason == "MARGIN_BOUND_UNAVAILABLE"


def test_bundled_margin_and_separate_margin_arguments_conflict() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    with pytest.raises(ValueError, match="MARGIN_OPTION_CONFLICT"):
        weighted_search(
            prepared,
            (Decimal("100"), Decimal("100")),
            members=_members(),
            target_p30=Decimal("10"),
            margin={"coefficients": coefficients, "limiter_release_status": "UNKNOWN"},
            margin_coefficients=coefficients,
        )


def test_bundled_margin_applies_coefficients_and_options_to_candidate_metrics() -> None:
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members()),
        target_p30=Decimal("10"),
        margin={"coefficients": coefficients, "limiter_release_status": "UNKNOWN"},
    )
    assert result.status == "PASS"
    metrics = result.candidates[0].metrics
    assert all(metrics[key] is not None for key in ("I_all_usdt", "M_all_usdt", "I_held_usdt", "B_margin_usdt", "B_required_margin_usdt"))


def test_sequence_priorities_are_published_in_strategy_id_order() -> None:
    members = (
        {"strategy_id": 1, "symbol": "A", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "symbol": "B", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
    )
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    candidate = _candidates_for_solution(
        _Solution(Decimal("1"), (Decimal("10"), Decimal("10"))),
        ((Decimal("0"), Decimal("0")),),
        members,
        (Decimal("100"), Decimal("100")),
        max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=coefficients,
        margin_kwargs={"strategy_ids": (1, 2), "priorities": (5, 1), "limiter_release_status": "UNKNOWN"},
    )[0]
    assert [member["priority"] for member in candidate.members] == [5, 1]


def test_public_weighted_margin_rejects_bare_coefficients_and_domain_overflow() -> None:
    from mrs3.portfolio.margin import evaluate_weighted_margin

    bare = evaluate_weighted_margin((Decimal("10"),), ((Decimal("0.1"), Decimal("0.01")),), strategy_ids=(1,), priorities=(1,))
    overflow = evaluate_weighted_margin(
        (Decimal("101"),),
        (MarginCoefficient(1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")),),
        strategy_ids=(1,), priorities=(1,),
    )
    assert bare.status == "UNKNOWN" and bare.reason == "MARGIN_BOUND_UNAVAILABLE"
    assert overflow.status == "UNKNOWN" and overflow.reason == "MARGIN_COEFFICIENT_DOMAIN_EXCEEDED"


def test_explicit_l_equal_n_is_rejected() -> None:
    x, members, coefficients, options = _margin15_fixture()
    with pytest.raises(ValueError, match="LIMITER_RANGE_INVALID"):
        _margin_variant(x, members, coefficients, options={**options, "L": 15}, bank_available=Decimal("2000"))


def test_margin_infeasible_later_frontier_keeps_prior_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    weighted_search_module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("0.4", "0.1"), ("0.1", "0.1")))
    member_rows = _members()
    coefficients = tuple(MarginCoefficient(index + 1, Decimal("0.1"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")) for index in range(2))
    first = _candidate_for_solution(
        _Solution(Decimal("1"), (Decimal("1"), Decimal("1"))),
        ((Decimal("0"), Decimal("0")),), member_rows, (Decimal("100"), Decimal("100")),
        max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None, profile_id="P", scenario_id="S",
    )
    calls = 0

    def candidates(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            _kwargs["failure_reason"].append("MARGIN_BOUND_FAILED")
        return (first,) if calls == 1 else ()

    monkeypatch.setattr(weighted_search_module, "_candidates_for_solution", candidates)
    result = weighted_search(prepared, (Decimal("100"), Decimal("100")), members=member_rows, common_days=Decimal("1"), max_targets=2, margin_coefficients=coefficients, margin_kwargs={"limiter_release_status": "UNKNOWN"})
    assert result.status == "PASS" and result.candidates == (first,)
    assert result.warnings and result.warnings[-1] == "MARGIN_BOUND_FAILED"


def _rescue_fixture():
    members = (
        {"strategy_id": 1, "symbol": "A", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
        {"strategy_id": 2, "symbol": "B", "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"},
    )
    return {
        "max_dd": Decimal("0.10"), "reserve": Decimal("0.40"),
        "max_mm_load": Decimal("0.35"),
        "limiter_release_status": "UNKNOWN",
    }, members, _evidence_coefficients(((Decimal("0.10"), Decimal("0.005")),) * 2, max_notional=Decimal("20000"))


def test_proportional_rescue_appends_a_distinct_recomputed_candidate() -> None:
    options, members, coefficients = _rescue_fixture()
    candidates = _candidates_for_solution(
        _Solution(Decimal("50000"), (Decimal("10000"), Decimal("10000"))),
        ((Decimal("0"), Decimal("0")),), members, (Decimal("20000"), Decimal("20000")),
        max_dd=Decimal("0.10"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=coefficients,
        margin_kwargs=options, bank_available=Decimal("2000"),
    )
    assert len(candidates) == 2
    original, reduced = candidates
    assert tuple(member["x_usdt"] for member in original.members) == (Decimal("10000"), Decimal("10000"))
    assert all(Decimal("0") <= member["x_usdt"] <= Decimal("20000") for member in reduced.members)
    assert reduced.metrics["required_bank_usdt"] <= Decimal("2000")
    assert original.metrics["bank_feasible"] is False and reduced.metrics["bank_feasible"] is True
    assert reduced.identity != original.identity
    assert "replay_accepted_mask" not in reduced.metrics
    assert all("replay_accepted_mask" not in item for item in reduced.metrics["limiter_variants"] if item is not None)


def test_failed_proportional_rescue_keeps_only_the_original_candidate() -> None:
    options, members, coefficients = _rescue_fixture()
    candidates = _candidates_for_solution(
        _Solution(Decimal("50000"), (Decimal("10000"), Decimal("10000"))),
        ((Decimal("0"), Decimal("0")),), members, (Decimal("20000"), Decimal("20000")),
        max_dd=Decimal("0.10"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S", margin_coefficients=coefficients,
        margin_kwargs=options, bank_available=Decimal("1.1"),
    )
    assert len(candidates) == 1
    assert tuple(member["x_usdt"] for member in candidates[0].members) == (Decimal("10000"), Decimal("10000"))
    assert candidates[0].metrics["bank_feasible"] is False


def test_proposal_revalidation_calls_validator_once_and_preserves_on_failure() -> None:
    members = ({"strategy_id": 1, "symbol": "A"}, {"strategy_id": 2, "symbol": "B"})
    original = _candidate_for_solution(
        _Solution(Decimal("1"), (Decimal("1"), Decimal("1"))),
        ((Decimal("0"), Decimal("0")),), members, (Decimal("10"), Decimal("10")),
        max_dd=Decimal("0.2"), common_days=Decimal("1"), target=None,
        profile_id="P", scenario_id="S",
    )
    calls: list[tuple[Decimal, ...]] = []
    accepted = __import__("mrs3.portfolio.weighted_search", fromlist=["_revalidate_proposed_x"])._revalidate_proposed_x(
        original, (Decimal("2"), Decimal("1")), ((Decimal("0"), Decimal("0")),), members,
        (Decimal("10"), Decimal("10")), max_dd=Decimal("0.2"), common_days=Decimal("1"),
        target=None, profile_id="P", scenario_id="S", validator=lambda value: calls.append(value) or True,
    )
    assert len(accepted) == 2 and len(calls) == 1
    assert all(key not in accepted[-1].metrics for key in ("cycles", "equity", "actions"))
    rejected_calls: list[tuple[Decimal, ...]] = []
    rejected = __import__("mrs3.portfolio.weighted_search", fromlist=["_revalidate_proposed_x"])._revalidate_proposed_x(
        original, (Decimal("2"), Decimal("1")), ((Decimal("0"), Decimal("0")),), members,
        (Decimal("10"), Decimal("10")), max_dd=Decimal("0.2"), common_days=Decimal("1"),
        target=None, profile_id="P", scenario_id="S", validator=lambda value: rejected_calls.append(value) or False,
    )
    assert rejected == (original,) and len(rejected_calls) == 1


def _additional_search_fixture() -> tuple[PreparedWeightedInput, tuple[dict[str, object], ...], tuple[MarginCoefficient, ...]]:
    prepared = _prepared((("1", "1"),))
    members = tuple({**member, "mean_hold": "1", "hold90": "1", "mean_net_pnl": "1"} for member in _members())
    coefficients = tuple(
        MarginCoefficient(index + 1, Decimal("0.10"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100"))
        for index in range(2)
    )
    return prepared, members, coefficients


def test_additional_pass_orders_bootstrap_lp_and_full_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    order: list[tuple[str, tuple[tuple[Decimal, ...], ...] | None]] = []
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: (
        order.append(("additional_lp", None)) or module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0"))))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: (
        order.append(("cdar_lp", None)) or module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1"))))
    ))
    real_bootstrap = module.bootstrap_banks

    def bootstrap(rows, vectors, **kwargs):
        order.append(("bootstrap", tuple(tuple(value for value in vector) for vector in vectors)))
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert [item[0] for item in order] == ["bootstrap", "additional_lp", "bootstrap", "cdar_lp"]
    assert order[0][1] == ((Decimal("1"), Decimal("1")),)
    assert order[2][1] == ((Decimal("2"), Decimal("0")),)
    assert result.manifest["additional"]["outcome"] == "accepted"
    assert result.manifest["base_full_checked_x_count"] == 1
    assert result.manifest["new_x_count"] == 1
    assert result.manifest["scenario_checked_x_count"] == 2
    assert result.manifest["lp_call_count"] == 3
    assert "replay_accepted_mask" not in result.manifest["additional"]
    assert result.manifest["additional"]["replay_accepted_count"] == 0
    assert result.manifest["additional"]["replay_total_count"] == 0


def test_additional_pass_same_x_uses_cache_without_bootstrap_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    bootstrap_calls = 0
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("3"), Decimal("3")))))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("3.00"), Decimal("3.00")))))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("3.000"), Decimal("3.000")))))
    real_bootstrap = module.bootstrap_banks

    def bootstrap(rows, vectors, **kwargs):
        nonlocal bootstrap_calls
        bootstrap_calls += 1
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert bootstrap_calls == 1
    assert result.manifest["additional"]["outcome"] == "rejected"
    assert result.manifest["additional"]["reason"] == "DUPLICATE_X"
    assert result.manifest["new_x_count"] == 0
    assert result.manifest["base_full_checked_x_count"] == 1
    assert result.manifest["scenario_checked_x_count"] == 1
    assert len({item.identity for item in result.candidates}) == len(result.candidates)


@pytest.mark.parametrize("model_coefficients", [None, ()])
def test_additional_model_objective_requires_nonempty_model_coefficients(
    monkeypatch: pytest.MonkeyPatch,
    model_coefficients: tuple[Decimal, ...] | None,
) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    captured: dict[str, object] = {}
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity="objective-primary",
        members=(
            {"strategy_id": 2, "symbol": "S2", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 2},
            {"strategy_id": 1, "symbol": "S1", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 5},
        ),
        metrics={"limiter_L": 1, "limiter_release_status": "CONFIRMED", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("1"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")},
        status="PASS",
    )
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: (primary,))
    variant = _MarginVariant(
        SimpleNamespace(status="PASS", L=1, ell=1, B_required=Decimal("1")),
        LimiterReplayResult("MODEL", Decimal("1"), accepted_mask=(True,)),
    )
    monkeypatch.setattr(module, "_margin_variant", lambda *args, **kwargs: (variant, (variant,)))
    monkeypatch.setattr(module, "_limiter_model_coefficients", lambda *args, **kwargs: ("MODEL", model_coefficients))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: (
        captured.update(kwargs) or module._SolveOutcome("ERROR", reason="LP_INFEASIBLE")
    ))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.manifest["additional"]["objective_status"] == "UNKNOWN"
    assert captured["objective_coefficients"] == (Decimal("30"), Decimal("30"))


def test_additional_failure_preserves_base_and_marks_partial_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome("ERROR", reason="SOLVER_ERROR"))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE"))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS" and len(result.candidates) == 1
    assert result.manifest["additional"]["outcome"] == "rejected"
    assert result.manifest["additional"]["reason"] == "SOLVER_ERROR"

    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0"))), budget_limited=True
    ))
    limited = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert limited.status == "budget_limited" and len(limited.candidates) == 1
    assert limited.manifest["additional"]["reason"] == "SOLVER_TIME_LIMIT"


def test_additional_pass_skips_risk_limited_base_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("1"), (Decimal("1"), Decimal("1")))
    ))
    real_bootstrap = module.bootstrap_banks

    def bootstrap(rows, vectors, **kwargs):
        result = real_bootstrap(rows, vectors, **kwargs)
        return replace(result, risk_banks=(Decimal("10"),))

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    additional_calls = 0

    def additional(*args, **kwargs):
        nonlocal additional_calls
        additional_calls += 1
        return module._SolveOutcome("PASS", _Solution(Decimal("20"), (Decimal("2"), Decimal("0"))))

    monkeypatch.setattr(module, "_solve_additional_lp", additional)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("20"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert additional_calls == 0
    assert result.manifest["additional"]["reason"] == "NO_ELIGIBLE_MARGIN_CANDIDATE"


def test_additional_pass_skips_missing_risk_bank_instead_of_treating_it_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity="missing-risk",
        members=tuple({"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 1} for index in range(2)),
        metrics={"limiter_L": 1, "limiter_release_status": "UNKNOWN", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": None, "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")},
        status="PASS",
    )
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: (primary,))
    additional_calls = 0

    def additional(*args, **kwargs):
        nonlocal additional_calls
        additional_calls += 1
        return module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0"))))

    monkeypatch.setattr(module, "_solve_additional_lp", additional)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert additional_calls == 0
    assert result.manifest["additional"]["reason"] == "NO_ELIGIBLE_MARGIN_CANDIDATE"


@pytest.mark.parametrize(
    "metric, value, reason",
    [
        ("p30_common_usdt_30d", Decimal("NaN"), "P30_COMMON_INVALID"),
        ("p30_common_usdt_30d", "abc", "P30_COMMON_INVALID"),
        ("max_drawdown_fraction", float("nan"), "MAX_DRAWDOWN_INVALID"),
    ],
)
def test_additional_seed_invalid_metrics_skip_without_destroying_valid_base(
    monkeypatch: pytest.MonkeyPatch,
    metric: str,
    value: object,
    reason: str,
) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    metrics = {"limiter_L": 1, "limiter_release_status": "UNKNOWN", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("1"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")}
    metrics[metric] = value
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity=f"invalid-{metric}-{value}",
        members=tuple({"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 1} for index in range(2)),
        metrics=metrics,
        status="PASS",
    )
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: (primary,))
    additional_calls = 0

    def additional(*args, **kwargs):
        nonlocal additional_calls
        additional_calls += 1
        return module._SolveOutcome("ERROR", reason="LP_INFEASIBLE")

    monkeypatch.setattr(module, "_solve_additional_lp", additional)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    if metric == "p30_common_usdt_30d":
        assert result.status == "FAIL"
        assert result.candidates == ()
    else:
        assert result.status == "PASS"
        assert len(result.candidates) == 1 and result.candidates[0].identity == primary.identity
    assert additional_calls == 0
    assert result.manifest["additional"]["eligibility_skip_reasons"][reason] == 1


def test_candidate_seed_vector_is_keyed_by_strategy_id_and_allows_zero_dropped_members() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    reordered = (
        {"strategy_id": 2, "symbol": "B", "x_usdt": Decimal("2"), "capacity_usdt": Decimal("20"), "priority": 2},
        {"strategy_id": 1, "symbol": "A", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("10"), "priority": 5},
    )
    assert module._candidate_vector_by_strategy_id(reordered, (1, 2), (Decimal("10"), Decimal("20"))) == (
        (Decimal("1"), Decimal("2")),
        (5, 2),
    )
    zero_dropped = ({"strategy_id": 2, "symbol": "B", "x_usdt": Decimal("2"), "capacity_usdt": Decimal("20"), "priority": 2},)
    assert module._candidate_vector_by_strategy_id(zero_dropped, (1, 2), (Decimal("10"), Decimal("20"))) == (
        (Decimal("0"), Decimal("2")),
        (None, 2),
    )
    with pytest.raises(ValueError, match="CANDIDATE_STRATEGY_ID_UNKNOWN"):
        module._candidate_vector_by_strategy_id(({"strategy_id": 3, "x_usdt": Decimal("1")},), (1, 2), (Decimal("10"), Decimal("20")))
    with pytest.raises(ValueError, match="CANDIDATE_STRATEGY_ID_DUPLICATE"):
        module._candidate_vector_by_strategy_id((reordered[0], reordered[0]), (1, 2), (Decimal("10"), Decimal("20")))


@pytest.mark.parametrize(
    "candidate_members",
    (
        ({"strategy_id": 1, "pair": "BTCUSDT", "x_usdt": Decimal("1")},),
        (
            {"strategy_id": 1, "symbol": "BTCUSDT", "x_usdt": Decimal("1")},
            {"strategy_id": 2, "x_usdt": Decimal("1")},
        ),
    ),
)
def test_candidate_seed_vector_requires_symbol_on_every_member(candidate_members) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    with pytest.raises(ValueError, match="MISSING_SYMBOL"):
        module._candidate_vector_by_strategy_id(
            candidate_members,
            tuple(range(1, len(candidate_members) + 1)),
            (Decimal("10"),) * len(candidate_members),
        )


def test_additional_seed_sort_skips_malformed_p30_common_and_keeps_valid_base(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    outcomes = iter((
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))),
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("1")))),
    ))
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: next(outcomes))

    def candidate(*args, **kwargs):
        solution = args[0]
        p30 = None if solution.x[0] == Decimal("1") else Decimal("10")
        return (PortfolioCandidate(
            schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
            profile_id="WEIGHTED",
            scenario_id="WEIGHTED_V1",
            identity=f"seed-{solution.x[0]}",
            members=tuple({"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": value, "capacity_usdt": Decimal("100"), "priority": 1} for index, value in enumerate(solution.x)),
            metrics={"limiter_L": 1, "limiter_release_status": "UNKNOWN", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("1"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": p30},
            status="PASS",
        ),)

    monkeypatch.setattr(module, "_candidates_for_solution", candidate)
    additional_calls = 0

    def additional(*args, **kwargs):
        nonlocal additional_calls
        additional_calls += 1
        return module._SolveOutcome("ERROR", reason="LP_INFEASIBLE")

    monkeypatch.setattr(module, "_solve_additional_lp", additional)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        bank_available=None,
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_targets=2,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert len(result.candidates) == 2
    assert additional_calls == 1
    assert result.manifest["additional"]["reason"] == "LP_INFEASIBLE"
    assert result.manifest["additional"]["p30_common_skipped_count"] == 1
    assert result.manifest["additional"]["p30_common_skip_reason"] == "P30_COMMON_INVALID"


def test_additional_seed_uses_largest_p95_family_and_earliest_tie_witness(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    solution = _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome("PASS", solution))
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity="p95-primary",
        members=tuple({"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 1} for index in range(2)),
        metrics={"limiter_L": 1, "limiter_release_status": "UNKNOWN", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("1"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")},
        status="PASS",
    )
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: (primary,))
    fake_bootstrap = module._BootstrapBankResult(
        historical_banks=(Decimal("1"),),
        scenario_banks=(((Decimal("1"),),),),
        p95_banks=((Decimal("10"), Decimal("20"), Decimal("20")),),
        risk_banks=(Decimal("20"),),
        manifest={"families": (), "block_days": (Decimal("1"), Decimal("2"), Decimal("3")), "diagnostics": (), "seed": 731, "history_step_minutes": Decimal("5"), "numpy_version": "test"},
        vector_digests=(module._digest({"x": solution.x}),),
        scenario_counts=(1, 1, 1),
            p95_witnesses=((
                {"family_ordinal": 0, "scenario_index": 3},
                {"family_ordinal": 20, "scenario_index": 0},
                {"family_ordinal": 10, "scenario_index": 1},
            ),),
    )
    monkeypatch.setattr(module, "bootstrap_banks", lambda *args, **kwargs: fake_bootstrap)
    variant = _MarginVariant(
        SimpleNamespace(status="PASS", L=1, ell=1, B_required=Decimal("1")),
        LimiterReplayResult("UNKNOWN", None),
    )
    monkeypatch.setattr(module, "_margin_variant", lambda *args, **kwargs: (variant, (variant,)))
    captured: dict[str, object] = {}

    def additional(*args, **kwargs):
        captured.update(kwargs)
        return module._SolveOutcome("ERROR", reason="LP_INFEASIBLE")

    monkeypatch.setattr(module, "_solve_additional_lp", additional)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert captured["p95_witness"] == {"family_ordinal": 10, "scenario_index": 1}
    assert result.manifest["additional"]["p95_witness"] == {"family_ordinal": 10, "scenario_index": 1}


def test_additional_risk_boundary_accepts_just_below_solver_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity="risk-boundary",
        members=tuple({"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 1} for index in range(2)),
        metrics={"limiter_L": 1, "limiter_release_status": "UNKNOWN", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("10.00000005"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")},
        status="PASS",
    )
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: (primary,))
    additional_calls = 0

    def additional(*args, **kwargs):
        nonlocal additional_calls
        additional_calls += 1
        return module._SolveOutcome("ERROR", reason="LP_INFEASIBLE")

    monkeypatch.setattr(module, "_solve_additional_lp", additional)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert additional_calls == 1


def test_additional_pass_uses_primary_variant_for_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity="z-primary",
        members=(
            {"strategy_id": 2, "symbol": "S2", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 2},
            {"strategy_id": 1, "symbol": "S1", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 5},
        ),
        metrics={"limiter_L": 1, "limiter_release_status": "CONFIRMED", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("1"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")},
        status="PASS",
    )
    alternate = PortfolioCandidate(
        schema_version=primary.schema_version,
        profile_id=primary.profile_id,
        scenario_id=primary.scenario_id,
        identity="a-alternate",
        members=primary.members,
        metrics={**primary.metrics, "limiter_L": 2},
        status="PASS",
    )
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("1"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: (primary, alternate))
    captured: dict[str, object] = {}

    def additional(*args, **kwargs):
        captured.update(kwargs)
        return module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0"))))

    monkeypatch.setattr(module, "_solve_additional_lp", additional)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "CONFIRMED", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert captured["L"] == 1
    assert captured["limiter_release_status"] == "CONFIRMED"
    assert captured["priorities"] == (5, 2)


def test_incomplete_additional_bootstrap_does_not_count_unverified_x(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    real_bootstrap = module.bootstrap_banks
    calls = 0

    def bootstrap(rows, vectors, **kwargs):
        nonlocal calls
        calls += 1
        result = real_bootstrap(rows, vectors, **kwargs)
        if calls == 2:
            return replace(result, complete=False, risk_banks=(None,), manifest={**result.manifest, "stopping_reason": "WALL_TIME_LIMIT"})
        return result

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("1"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE"))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "budget_limited"
    assert result.manifest["new_x_count"] == 0
    assert result.manifest["scenario_checked_x_count"] == 1
    assert len(result.candidates) == 1
    assert result.manifest["additional"]["outcome"] == "rejected"
    assert result.manifest["additional"]["reason"] == "WALL_TIME_LIMIT"
    assert all(tuple(member["x_usdt"] for member in candidate.members) != (Decimal("2"), Decimal("0")) for candidate in result.candidates)


def test_additional_postcheck_removes_zero_members_before_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    compacted: dict[str, object] = {}
    real_compact = module._compact_additional_inputs

    def compact(*args, **kwargs):
        value = real_compact(*args, **kwargs)
        compacted["active_x"] = value[1]
        compacted["options"] = value[5]
        return value

    monkeypatch.setattr(module, "_compact_additional_inputs", compact)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("1"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE"))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    alternatives = [candidate for candidate in result.candidates if candidate.members and candidate.members[0]["x_usdt"] == Decimal("2")]
    assert alternatives and len(alternatives[0].members) == 1
    assert compacted["active_x"] == (Decimal("2"),)
    assert compacted["options"]["strategy_ids"] == (1,)
    assert len(compacted["options"]["priorities"]) == 1
    metrics = alternatives[0].metrics
    assert metrics["B_margin_usdt"] is not None and metrics["B_required_margin_usdt"] is not None
    assert metrics["B_risk_usdt"] is not None
    assert metrics["p30_common_usdt_30d"] >= Decimal("0.1")


def test_additional_revalidation_uses_derived_fixed_bank_when_available_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity="fixed-bank-primary",
        members=tuple({"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), "priority": 1} for index in range(2)),
        metrics={"limiter_L": 1, "limiter_release_status": "UNKNOWN", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("1"), "required_bank_usdt": Decimal("10"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")},
        status="PASS",
    )
    proposed = replace(primary, identity="fixed-bank-proposed", metrics={**primary.metrics, "required_bank_usdt": Decimal("11")})
    observed_banks: list[object] = []

    def candidates(*args, **kwargs):
        if len(args[1][0]) == 2:
            return (primary,)
        observed_banks.append(kwargs.get("bank_available"))
        return () if kwargs.get("bank_available") == Decimal("10") else (proposed,)

    monkeypatch.setattr(module, "_candidates_for_solution", candidates)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE"))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=None,
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert len(result.candidates) == 1 and result.candidates[0].identity == primary.identity
    assert observed_banks == [Decimal("10")]
    assert result.manifest["additional"]["outcome"] == "rejected"
    assert result.manifest["additional"]["reason"] == "REVALIDATION_FAILED"


@pytest.mark.parametrize("failure, expected_reason", [("bank", "BANK_FIXED_UNAVAILABLE"), ("priority", "PRIORITY_UNKNOWN")])
def test_additional_early_skip_reports_factual_fixed_bank_or_priority_reason(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_reason: str,
) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    metrics = {"limiter_L": 1, "limiter_release_status": "UNKNOWN", "B_margin_usdt": Decimal("10"), "B_required_margin_usdt": Decimal("10"), "B_risk_usdt": Decimal("1"), "required_bank_usdt": Decimal("10"), "max_drawdown_fraction": Decimal("0"), "p30_common_usdt_30d": Decimal("10")}
    if failure == "bank":
        metrics["required_bank_usdt"] = None
    seed_members = tuple({"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100"), **({} if failure == "priority" else {"priority": 1})} for index in range(2))
    primary = PortfolioCandidate(
        schema_version="MRS3_PORTFOLIO_CANDIDATE_V1",
        profile_id="WEIGHTED",
        scenario_id="WEIGHTED_V1",
        identity=f"skip-{failure}",
        members=seed_members,
        metrics=metrics,
        status="PASS",
    )
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: (primary,))
    if failure == "priority":
        monkeypatch.setattr(module, "derive_priorities", lambda *args, **kwargs: {1: None, 2: None})
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=None,
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert len(result.candidates) == 1
    assert result.manifest["additional"]["reason"] == expected_reason


def test_additional_identity_collision_preserves_single_candidate_and_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    real_candidates = module._candidates_for_solution
    base_identity: str | None = None

    def candidates(*args, **kwargs):
        nonlocal base_identity
        result = real_candidates(*args, **kwargs)
        if len(args[1][0]) == 2:
            base_identity = result[0].identity if result else None
            return result
        assert base_identity is not None
        return tuple(replace(candidate, identity=base_identity) for candidate in result)

    monkeypatch.setattr(module, "_candidates_for_solution", candidates)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert len(result.candidates) == 1
    assert result.manifest["additional"]["outcome"] == "rejected"
    assert result.manifest["additional"]["reason"] == "DUPLICATE_IDENTITY"


def test_cdar_same_x_runs_after_additional_without_new_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    order: list[str] = []
    bootstrap_calls = 0
    real_bootstrap = module.bootstrap_banks

    def bootstrap(rows, vectors, **kwargs):
        nonlocal bootstrap_calls
        bootstrap_calls += 1
        order.append("bootstrap")
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: (
        order.append("additional") or module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1"))))
    ))
    cdar_calls = 0

    def cdar(*args, **kwargs):
        nonlocal cdar_calls
        cdar_calls += 1
        order.append("cdar")
        return module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1"))))

    monkeypatch.setattr(module, "_solve_cdar80_lp", cdar)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert order == ["bootstrap", "additional", "cdar"]
    assert cdar_calls == 1
    assert bootstrap_calls == 1
    assert result.manifest["cdar"]["solve_count"] == 1
    assert result.manifest["new_x_count"] == 0


def test_cdar_changed_x_bootstraps_fresh_and_freezes_bank_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    real_bootstrap = module.bootstrap_banks
    bootstrap_calls: list[dict[str, object]] = []
    captured: dict[str, object] = {}

    def bootstrap(rows, vectors, **kwargs):
        bootstrap_calls.append({"vectors": vectors, **kwargs})
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))

    def cdar(*args, **kwargs):
        captured.update(kwargs)
        return module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0"))))

    monkeypatch.setattr(module, "_solve_cdar80_lp", cdar)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("58"),
        bank_available=Decimal("20"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert len(bootstrap_calls) == 2
    assert bootstrap_calls[1]["vectors"] == ((Decimal("2"), Decimal("0")),)
    assert bootstrap_calls[1]["prefix_result"] is None
    assert bootstrap_calls[1]["scenarios_per_family"] == 2
    assert captured["bank_fixed"] == Decimal("10")
    assert captured["p30_floor"] == Decimal("58")
    assert captured["L"] == 0
    assert captured["priorities"] == (5, 1)
    assert captured["limiter_release_status"] == "UNKNOWN"
    assert result.manifest["cdar"]["accepted_count"] == 1
    assert result.manifest["new_x_count"] == 1
    assert result.candidates[-1].metrics["required_bank_usdt"] == Decimal("10")


def test_cdar_bootstrap_cancellation_keeps_verified_base_and_reports_unexplored_work(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    real_bootstrap = module.bootstrap_banks
    bootstrap_count = 0

    def bootstrap(rows, vectors, **kwargs):
        nonlocal bootstrap_count
        bootstrap_count += 1
        if bootstrap_count == 2:
            kwargs["cancel"] = lambda: True
            kwargs["batch_size"] = 1
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)

    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=2,
        screening_scenarios=1,
        max_candidates=2,
        cancel=lambda: False,
    )
    assert result.status == "budget_limited"
    assert result.reason == "CANCELLED"
    assert result.candidates
    assert len(result.candidates) <= 2
    assert len({item.identity for item in result.candidates}) == len(result.candidates)
    assert result.manifest["base_full_checked_x_count"] == 1
    assert result.manifest["new_x_count"] == 0
    assert result.manifest["cdar"]["reasons"][-1] == "CANCELLED"
    assert result.manifest["remaining_work"]["unexplored_scenario_count"] > 0
    assert result.manifest["stopping_reason"] is not None


def test_cdar_rejects_incumbent_with_bank_different_from_frozen_bank(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("11"), (Decimal("2"), Decimal("0")))
    ))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert result.manifest["cdar"]["accepted_count"] == 0
    assert result.manifest["cdar"]["reasons"] == ("FIXED_BANK_MISMATCH",)
    assert result.manifest["new_x_count"] == 0


def test_cdar_rejects_changed_x_below_the_frozen_p30_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("0"), Decimal("1")))
    ))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("58"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert result.manifest["cdar"]["accepted_count"] == 0
    assert result.manifest["cdar"]["reasons"] == ("REVALIDATION_FAILED",)
    assert result.manifest["new_x_count"] == 1
    assert len(result.candidates) == 1


def test_cdar_incomplete_bootstrap_preserves_base_and_does_not_count_new_x(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    real_bootstrap = module.bootstrap_banks
    calls = 0

    def bootstrap(rows, vectors, **kwargs):
        nonlocal calls
        calls += 1
        result = real_bootstrap(rows, vectors, **kwargs)
        if calls == 2:
            return replace(result, complete=False, risk_banks=(None,), manifest={**result.manifest, "stopping_reason": "WALL_TIME_LIMIT"})
        return result

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "budget_limited"
    assert result.manifest["new_x_count"] == 0
    assert result.manifest["scenario_checked_x_count"] == 1
    assert result.manifest["cdar"]["reasons"] == ("WALL_TIME_LIMIT",)
    assert len(result.candidates) == 1


def test_cdar_missing_bootstrap_summary_key_preserves_base_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared, members, coefficients = _additional_search_fixture()
    real_bootstrap = module.bootstrap_banks
    calls = 0

    def bootstrap(rows, vectors, **kwargs):
        nonlocal calls
        calls += 1
        result = real_bootstrap(rows, vectors, **kwargs)
        if calls == 2:
            manifest = dict(result.manifest)
            manifest.pop("diagnostics", None)
            return replace(result, manifest=manifest)
        return result

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))
    ))
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (5, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert len(result.candidates) == 1
    assert result.manifest["cdar"]["reasons"] == ("BOOTSTRAP_SUMMARY_INVALID",)


@pytest.mark.parametrize("additional_outcome", ("skipped", "rejected", "accepted"))
def test_cdar_remains_eligible_after_each_additional_outcome(
    monkeypatch: pytest.MonkeyPatch, additional_outcome: str
) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("1",),), strategy_ids=(1,))
    source_members = _members(1)
    coefficients = (MarginCoefficient(1, Decimal("0.10"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100")),)
    order: list[str] = []

    def candidate_for(solution, *args, **kwargs):
        x = tuple(solution.x)
        identity = "base" if x == (Decimal("1"),) else "additional"
        metrics = {
            "p30_common_usdt_30d": Decimal("10"),
            "limiter_L": 0,
            "max_drawdown_fraction": Decimal("0"),
            "required_bank_usdt": Decimal("10"),
        }
        if additional_outcome != "skipped":
            metrics.update({
                "B_margin_usdt": Decimal("10"),
                "B_required_margin_usdt": Decimal("10"),
                "B_risk_usdt": Decimal("1"),
            })
        return (PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="WEIGHTED",
            scenario_id="WEIGHTED_V1",
            identity=identity,
            members=({"strategy_id": 1, "symbol": "S1", "x_usdt": x[0], "capacity_usdt": Decimal("100"), "priority": 1},),
            metrics=metrics,
            status="PASS",
        ),)

    monkeypatch.setattr(module, "_candidates_for_solution", candidate_for)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: (
        order.append("base_lp") or module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"),)))
    ))
    if additional_outcome == "skipped":
        monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: pytest.fail("additional LP should be skipped"))
    elif additional_outcome == "rejected":
        monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: (
            order.append("additional_lp") or module._SolveOutcome("INFEASIBLE", reason="LP_INFEASIBLE")
        ))
    else:
        monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: (
            order.append("additional_lp") or module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"),)))
        ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: (
        order.append("cdar_lp") or module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"),)))
    ))
    real_bootstrap = module.bootstrap_banks

    def bootstrap(rows, vectors, **kwargs):
        order.append("bootstrap")
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    result = weighted_search(
        prepared,
        (Decimal("100"),),
        members=source_members,
        common_days=Decimal("1"),
        target_p30=Decimal("0.1"),
        bank_available=Decimal("10"),
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (1,)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=2,
    )
    assert result.status == "PASS"
    assert result.manifest["additional"]["outcome"] == ("skipped" if additional_outcome == "skipped" else additional_outcome)
    assert result.manifest["cdar"]["attempted"] is True
    assert result.manifest["cdar"]["solve_count"] == 1
    assert order[-1] == "cdar_lp"


def test_cdar_obeys_shared_solver_and_full_scenario_quotas(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("1", "1"),), strategy_ids=(1, 2))
    source_members = _members(2)
    coefficients = tuple(
        MarginCoefficient(index + 1, Decimal("0.10"), Decimal("0.01"), "CONSERVATIVE_BOUND", max_notional=Decimal("100"))
        for index in range(2)
    )
    base_solutions = iter((
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))),
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("2"), Decimal("0")))),
    ))
    cdar_solutions = iter((
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("3"), Decimal("0")))),
        module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("0"), Decimal("3")))),
    ))

    def candidate_for(solution, *args, **kwargs):
        x = tuple(solution.x)
        identity = "candidate-" + "-".join(str(value) for value in x)
        return (PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="WEIGHTED",
            scenario_id="WEIGHTED_V1",
            identity=identity,
            members=tuple(
                {"strategy_id": index + 1, "symbol": f"S{index + 1}", "x_usdt": value, "capacity_usdt": Decimal("100"), "priority": 1}
                for index, value in enumerate(x) if value > 0
            ),
            metrics={
                "p30_common_usdt_30d": Decimal("10"),
                "limiter_L": 0,
                "limiter_release_status": "UNKNOWN",
                "max_drawdown_fraction": Decimal("0"),
                "required_bank_usdt": Decimal("10"),
                "B_margin_usdt": Decimal("10"),
                "B_required_margin_usdt": Decimal("10"),
                "B_risk_usdt": Decimal("1"),
            },
            status="PASS",
        ),)

    monkeypatch.setattr(module, "_candidates_for_solution", candidate_for)
    monkeypatch.setattr(module, "_solve_lp", lambda *args, **kwargs: next(base_solutions))
    monkeypatch.setattr(module, "_solve_additional_lp", lambda *args, **kwargs: module._SolveOutcome(
        "PASS", _Solution(Decimal("10"), (Decimal("1"), Decimal("1")))
    ))
    monkeypatch.setattr(module, "_solve_cdar80_lp", lambda *args, **kwargs: next(cdar_solutions))
    bootstrap_calls: list[tuple[tuple[Decimal, ...], ...]] = []
    real_bootstrap = module.bootstrap_banks

    def bootstrap(rows, vectors, **kwargs):
        bootstrap_calls.append(tuple(tuple(value for value in vector) for vector in vectors))
        return real_bootstrap(rows, vectors, **kwargs)

    monkeypatch.setattr(module, "bootstrap_banks", bootstrap)
    result = weighted_search(
        prepared,
        (Decimal("100"), Decimal("100")),
        members=source_members,
        common_days=Decimal("1"),
        max_targets=2,
        margin_coefficients=coefficients,
        margin_kwargs={"L": 0, "limiter_release_status": "UNKNOWN", "priorities": (1, 1)},
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=4,
    )
    assert result.status == "PASS"
    assert result.manifest["cdar"]["solve_count"] <= 2
    assert result.manifest["cdar"]["solve_count"] == 2
    assert result.manifest["lp_call_count"] == 5 <= 20
    assert result.manifest["cdar"]["new_x_count"] == 2
    assert result.manifest["scenario_checked_x_count"] == 4 <= 3 * 4
    assert result.manifest["base_full_checked_x_count"] <= 2 * 4
    assert result.manifest["base_full_checked_x_count"] == 2
    assert result.manifest["new_x_count"] <= 4
    assert result.manifest["additional_pass_count"] <= 1
    assert len(result.candidates) <= 4
    assert bootstrap_calls[0] == ((Decimal("1"), Decimal("1")), (Decimal("2"), Decimal("0")))
    assert bootstrap_calls[1:] == [((Decimal("3"), Decimal("0")),), ((Decimal("0"), Decimal("3")),)]


def test_shortlist_deduplicates_identity_and_keeps_global_p30_point() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: tuple[str, str], p30: str, *, L: int = 0, limiter_p30: str | None = None) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=tuple(
                {"strategy_id": index + 1, "x_usdt": Decimal(value), "capacity_usdt": Decimal("100")}
                for index, value in enumerate(x)
                if Decimal(value) > 0
            ),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": L,
                "limiter_p30_status": "MODEL" if limiter_p30 is not None else "UNKNOWN",
                "p30_limiter_model_usdt_30d": None if limiter_p30 is None else Decimal(limiter_p30),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, manifest = module._select_shortlist(
        (
            candidate("duplicate", ("1", "1"), "99"),
            candidate("global", ("2", "0"), "100", L=1, limiter_p30="90"),
            candidate("duplicate", ("1", "1"), "99"),
        ),
        max_candidates=2,
    )
    assert [item.identity for item in selected] == ["global", "duplicate"]
    assert manifest["deduplicated_count"] == 1
    assert manifest["identity_collision_count"] == 0


def test_shortlist_uses_model_primary_then_off_and_next_model_controls() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, *, status: str, model_p30: str | None = None) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal("100"),
                "limiter_L": L,
                "limiter_p30_status": status,
                "p30_limiter_model_usdt_30d": None if model_p30 is None else Decimal(model_p30),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, manifest = module._select_shortlist(
        (
            candidate("off", 0, status="UNKNOWN"),
            candidate("model-low", 1, status="MODEL", model_p30="10"),
            candidate("model-high", 2, status="MODEL", model_p30="20"),
            candidate("unknown-high", 3, status="UNKNOWN"),
        ),
        max_candidates=3,
        bank_available=Decimal("10"),
    )
    assert [item.identity for item in selected] == ["model-high", "off", "model-low"]


def test_shortlist_manifest_separates_non_pass_filtering_from_identity_deduplication() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, status: str, p30: object, bank: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={"p30_common_usdt_30d": p30, "limiter_L": 1, "required_bank_usdt": Decimal(bank)},
            status=status,
        )

    _selected, manifest = module._select_shortlist(
        (candidate("failed", "FAIL", Decimal("100"), "10"), candidate("over-cap", "PASS", Decimal("100"), "20"), candidate("invalid", "PASS", None, "10")),
        max_candidates=1,
        bank_available=Decimal("10"),
    )
    assert manifest["pass_filtered_count"] == 1
    assert manifest["deduplicated_count"] == 0
    assert manifest["step_counts"]["identity_dedupe"] == 0
    assert manifest["invalid_p30_count"] == 1
    assert manifest["bank_infeasible_count"] == 1
    assert manifest["invalid_vector_or_level_count"] == 0
    assert manifest["bank_available"] == Decimal("10")
    assert manifest["max_candidates"] == 1
    assert manifest["selection_eligible_count"] == 1
    assert manifest["eligible_unselected_count"] == 1
    assert manifest["candidate_count"] == manifest["invalid_vector_or_level_count"] + manifest["invalid_metric_count"] + manifest["bank_infeasible_count"] + manifest["selection_eligible_count"]


def test_shortlist_identity_collision_fails_closed_in_any_input_order() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity="collision",
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={"p30_common_usdt_30d": Decimal(p30), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
        )

    low, high = candidate("10"), candidate("20")
    first, first_manifest = module._select_shortlist((low, high), max_candidates=1, bank_available=Decimal("10"))
    reversed_result, reversed_manifest = module._select_shortlist((high, low), max_candidates=1, bank_available=Decimal("10"))
    assert first == reversed_result == ()
    assert first_manifest["selected_identities"] == reversed_manifest["selected_identities"] == ()
    assert first_manifest["identity_collision_count"] == reversed_manifest["identity_collision_count"] == 2
    assert first_manifest["deduplicated_count"] == reversed_manifest["deduplicated_count"] == 0
    assert len((low, high)) == first_manifest["candidate_count"] + first_manifest["deduplicated_count"] + first_manifest["identity_collision_count"]

    duplicate, duplicate_manifest = module._select_shortlist((candidate("10"), candidate("10")), max_candidates=1, bank_available=Decimal("10"))
    assert tuple(item.identity for item in duplicate) == ("collision",)
    assert duplicate_manifest["identity_collision_count"] == 0
    assert duplicate_manifest["deduplicated_count"] == 1
    assert 2 == duplicate_manifest["candidate_count"] + duplicate_manifest["deduplicated_count"] + duplicate_manifest["identity_collision_count"]

    scaled = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="collision",
        members=({"capacity_usdt": Decimal("100.00"), "strategy_id": 1, "x_usdt": Decimal("1.00")},),
        metrics={
            "required_bank_usdt": Decimal("10.00"),
            "limiter_L": 1,
            "p30_common_usdt_30d": Decimal("10.00"),
        },
    )
    scaled_selected, scaled_manifest = module._select_shortlist((candidate("10.0"), scaled), max_candidates=1, bank_available=Decimal("10"))
    assert tuple(item.identity for item in scaled_selected) == ("collision",)
    assert scaled_manifest["identity_collision_count"] == 0
    assert scaled_manifest["deduplicated_count"] == 1


def test_shortlist_configured_bank_allows_same_x_controls_with_different_required_banks() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, bank: str, *, status: str, model_p30: str | None = None) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal("100"),
                "limiter_L": L,
                "limiter_p30_status": status,
                "p30_limiter_model_usdt_30d": None if model_p30 is None else Decimal(model_p30),
                "required_bank_usdt": Decimal(bank),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    candidates = (
        candidate("model-low", 1, "20", status="MODEL", model_p30="20"),
        candidate("off", 0, "10", status="UNKNOWN"),
        candidate("model-high", 2, "30", status="MODEL", model_p30="30"),
    )
    selected, manifest = module._select_shortlist(candidates, max_candidates=3, bank_available=Decimal("100"))
    reversed_selected, reversed_manifest = module._select_shortlist(tuple(reversed(candidates)), max_candidates=3, bank_available=Decimal("100"))
    assert [item.identity for item in selected] == ["model-high", "off", "model-low"]
    assert manifest["step_counts"]["controls"] == 2
    assert tuple(item.identity for item in reversed_selected) == tuple(item.identity for item in selected)
    assert reversed_manifest["step_counts"] == manifest["step_counts"]


def test_shortlist_configured_bank_skips_over_cap_global_p30_source() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: str, p30: str, bank: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal(x), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": 1,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal(bank),
            },
        )

    selected, _manifest = module._select_shortlist(
        (candidate("over-cap", "2", "100", "200"), candidate("feasible", "1", "90", "50")),
        max_candidates=1,
        bank_available=Decimal("100"),
    )
    assert [item.identity for item in selected] == ["feasible"]


def test_shortlist_unknown_primary_ignores_invalid_p30_off_candidate() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, p30: object) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": p30,
                "limiter_L": L,
                "limiter_p30_status": "UNKNOWN",
                "required_bank_usdt": Decimal("10"),
            },
        )

    selected, manifest = module._select_shortlist(
        (candidate("invalid-off", 0, "not-a-number"), candidate("valid-l1", 1, Decimal("100"))),
        max_candidates=1,
        bank_available=Decimal("10"),
    )
    assert [item.identity for item in selected] == ["valid-l1"]
    assert manifest["step_counts"]["primary"] == 1


def test_shortlist_controls_keep_one_candidate_per_limiter_level() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, p30: str, model_p30: str | None = None) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": L,
                "limiter_p30_status": "MODEL" if model_p30 is not None else "UNKNOWN",
                "p30_limiter_model_usdt_30d": None if model_p30 is None else Decimal(model_p30),
                "required_bank_usdt": Decimal("10"),
            },
        )

    candidates = (
        candidate("off-a", 0, "100"),
        candidate("model-high-duplicate", 2, "95", "25"),
        candidate("model-low", 1, "90", "20"),
        candidate("off-b", 0, "80"),
        candidate("model-high", 2, "100", "30"),
    )
    selected, manifest = module._select_shortlist(candidates, max_candidates=3, bank_available=Decimal("10"))
    assert [item.identity for item in selected] == ["model-high", "off-a", "model-low"]
    assert len({item.metrics["limiter_L"] for item in selected}) == 3
    assert manifest["step_counts"]["controls"] == 2


def test_shortlist_no_cap_reserves_minimum_bank_after_one_off_control() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, bank: str, status: str, model_p30: str | None) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal("100"),
                "limiter_L": L,
                "limiter_p30_status": status,
                "p30_limiter_model_usdt_30d": None if model_p30 is None else Decimal(model_p30),
                "required_bank_usdt": Decimal(bank),
            },
        )

    selected, manifest = module._select_shortlist(
        (
            candidate("primary", 1, "30", "MODEL", "30"),
            candidate("off-a", 0, "40", "UNKNOWN", None),
            candidate("off-b", 0, "50", "UNKNOWN", None),
            candidate("minimum-bank", 3, "10", "MODEL", "5"),
        ),
        max_candidates=3,
    )
    assert [item.identity for item in selected] == ["primary", "off-a", "minimum-bank"]
    assert manifest["step_counts"]["controls"] == 2


def test_shortlist_round_robin_rotates_categories_before_restarting_interval() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: tuple[str, str], p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=tuple(
                {"strategy_id": index + 1, "x_usdt": Decimal(value), "capacity_usdt": Decimal("100")}
                for index, value in enumerate(x)
            ),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": 1,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal("10"),
            },
        )

    candidates = (
        candidate("primary", ("1", "0"), "100"),
        candidate("scale-a", ("2", "0"), "90"),
        candidate("scale-b", ("3", "0"), "80"),
        candidate("scale-c", ("4", "0"), "70"),
        candidate("cdar", ("5", "0"), "60"),
        candidate("family-filler", ("0", "1"), "10"),
    )
    selected, manifest = module._select_shortlist(
        candidates,
        max_candidates=4,
        bank_available=Decimal("10"),
        origins={"scale-a": "scale", "scale-b": "scale", "scale-c": "scale", "cdar": "cdar"},
    )
    assert [item.identity for item in selected] == ["primary", "family-filler", "scale-a", "cdar"]
    assert manifest["selected_origins"] == ("scale", "scale", "scale", "cdar")


def test_shortlist_unknown_limiter_orders_off_then_strict_l_descending() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": L,
                "limiter_p30_status": "UNKNOWN",
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, manifest = module._select_shortlist(
        (candidate("l2", 2, "100"), candidate("off", 0, "90"), candidate("l1", 1, "80")),
        max_candidates=3,
        bank_available=Decimal("10"),
    )
    assert [item.identity for item in selected] == ["off", "l2", "l1"]
    assert manifest["step_counts"]["controls"] == 2


def test_shortlist_unknown_limiter_uses_nearest_lower_levels_when_off_missing() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(100 - L),
                "limiter_L": L,
                "limiter_p30_status": "UNKNOWN",
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, manifest = module._select_shortlist(
        (candidate("l3", 3), candidate("l2", 2), candidate("l1", 1)),
        max_candidates=3,
        bank_available=Decimal("10"),
    )
    assert [item.identity for item in selected] == ["l3", "l2", "l1"]
    assert manifest["step_counts"]["controls"] == 2


def test_shortlist_without_bank_cap_reserves_minimum_required_bank_control() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, bank: str, *, status: str, model_p30: str | None = None) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal("100"),
                "limiter_L": L,
                "limiter_p30_status": status,
                "p30_limiter_model_usdt_30d": None if model_p30 is None else Decimal(model_p30),
                "required_bank_usdt": Decimal(bank),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, _manifest = module._select_shortlist(
        (
            candidate("model-high", 2, "20", status="MODEL", model_p30="20"),
            candidate("model-low", 1, "40", status="MODEL", model_p30="10"),
            candidate("minimum-bank", 0, "10", status="UNKNOWN"),
        ),
        max_candidates=3,
    )
    assert [item.identity for item in selected] == ["model-high", "minimum-bank", "model-low"]


def test_shortlist_without_bank_cap_keeps_minimum_bank_even_when_it_is_not_off() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, L: int, bank: str, model_p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal("100"),
                "limiter_L": L,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(model_p30),
                "required_bank_usdt": Decimal(bank),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, _manifest = module._select_shortlist(
        (
            candidate("primary", 1, "20", "30"),
            candidate("off", 0, "20", "20"),
            candidate("model-control", 2, "20", "10"),
            candidate("minimum-bank", 3, "10", "1"),
        ),
        max_candidates=3,
    )
    assert [item.identity for item in selected] == ["primary", "off", "minimum-bank"]


def test_shortlist_prefers_new_composition_families_before_same_family_targets() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: tuple[str, str], p30: str, target: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=tuple(
                {"strategy_id": index + 1, "x_usdt": Decimal(value), "capacity_usdt": Decimal("100")}
                for index, value in enumerate(x)
                if Decimal(value) > 0
            ),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "target_p30_usdt_30d": Decimal(target),
                "limiter_L": 1,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, _manifest = module._select_shortlist(
        (
            candidate("primary", ("1", "1"), "100", "100"),
            candidate("same-family-lower", ("2", "2"), "70", "70"),
            candidate("new-family-a", ("2", "0"), "90", "90"),
            candidate("new-family-b", ("0", "2"), "80", "80"),
        ),
        max_candidates=3,
        bank_available=Decimal("10"),
    )
    assert [item.identity for item in selected] == ["primary", "new-family-b", "new-family-a"]


def test_shortlist_round_robin_alternates_scale_cdar_and_limiter_origins() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, p30: str, L: int, x: tuple[str, str]) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=tuple(
                {"strategy_id": index + 1, "x_usdt": Decimal(value), "capacity_usdt": Decimal("100")}
                for index, value in enumerate(x)
            ),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": L,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    candidates = (
        candidate("primary", "100", 1, ("1", "0")),
        candidate("scale", "90", 2, ("0", "1")),
        candidate("cdar", "80", 3, ("1", "1")),
        candidate("limiter", "70", 4, ("2", "1")),
    )
    selected, manifest = module._select_shortlist(
        candidates,
        max_candidates=4,
        bank_available=Decimal("10"),
        origins={"primary": "scale", "scale": "scale", "cdar": "cdar", "limiter": "limiter"},
    )
    assert [item.identity for item in selected] == ["primary", "scale", "cdar", "limiter"]
    assert manifest["selected_origins"] == ("scale", "scale", "cdar", "limiter")


@pytest.mark.parametrize(("label", "expected"), (("base", "scale"), ("scale", "scale"), ("cdar", "cdar"), ("limiter", "limiter")))
def test_shortlist_origin_maps_actual_producer_labels(label: str, expected: str) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: str, p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal(x), "capacity_usdt": Decimal("100")},),
            metrics={"p30_common_usdt_30d": Decimal(p30), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
        )

    selected, manifest = module._select_shortlist(
        (candidate("primary", "1", "100"), candidate("variant", "2", "90")),
        max_candidates=2,
        bank_available=Decimal("10"),
        origins={"variant": label},
    )
    assert tuple(item.identity for item in selected) == ("primary", "variant")
    assert manifest["selected_origins"] == ("scale", expected)


def test_shortlist_unknown_origin_fails_closed() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: str, p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal(x), "capacity_usdt": Decimal("100")},),
            metrics={"p30_common_usdt_30d": Decimal(p30), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
        )

    with pytest.raises(ValueError, match="SHORTLIST_ORIGIN_UNKNOWN"):
        module._select_shortlist(
            (candidate("primary", "1", "100"), candidate("variant", "2", "90")),
            max_candidates=2,
            bank_available=Decimal("10"),
            origins={"primary": "mystery"},
        )


def test_shortlist_primary_reports_resolved_cdar_origin_and_step() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    candidate = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="cdar-primary",
        members=({"strategy_id": 1, "x_usdt": Decimal("1")},),
        metrics={"p30_common_usdt_30d": Decimal("100"), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
    )
    selected, manifest = module._select_shortlist(
        (candidate,), max_candidates=1, bank_available=Decimal("10"), origins={"cdar-primary": "cdar"}
    )
    assert selected == (candidate,)
    assert manifest["selected_origins"] == ("cdar",)
    assert manifest["selected_steps"] == ("primary",)


@pytest.mark.parametrize("limit", (1, 2))
def test_shortlist_unknown_origin_fails_before_limit_dependent_selection(limit: int) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: str, p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal(x)},),
            metrics={"p30_common_usdt_30d": Decimal(p30), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
        )

    with pytest.raises(ValueError, match="SHORTLIST_ORIGIN_UNKNOWN"):
        module._select_shortlist(
            (candidate("primary", "1", "100"), candidate("variant", "2", "90")),
            max_candidates=limit,
            bank_available=Decimal("10"),
            origins={"variant": "mystery"},
        )


def test_shortlist_is_independent_of_candidate_input_order() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: str, p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": int(identity[-1]), "x_usdt": Decimal(x), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": 1,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    candidates = (candidate("candidate-1", "1", "10"), candidate("candidate-2", "2", "20"), candidate("candidate-3", "3", "30"))
    first, _ = module._select_shortlist(candidates, max_candidates=3, bank_available=Decimal("10"))
    reversed_result, _ = module._select_shortlist(tuple(reversed(candidates)), max_candidates=3, bank_available=Decimal("10"))
    assert tuple(item.identity for item in first) == tuple(item.identity for item in reversed_result)


def test_shortlist_fills_after_family_diversity_with_lower_target_points() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: str, p30: str, target: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": int(identity.split("-")[0]), "x_usdt": Decimal(x), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "target_p30_usdt_30d": Decimal(target),
                "limiter_L": 1,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    candidates = (
        candidate("1-primary", "1", "100", "100"),
        candidate("2-high", "2", "90", "90"),
        candidate("3-high", "3", "80", "80"),
        candidate("1-low", "1", "30", "30"),
        candidate("2-low", "2", "20", "20"),
        candidate("3-low", "3", "10", "10"),
    )
    selected, manifest = module._select_shortlist(candidates, max_candidates=5, bank_available=Decimal("10"))
    assert [item.identity for item in selected] == ["1-primary", "3-high", "2-high"]
    assert manifest["step_counts"]["families"] == 2
    assert manifest["step_counts"]["targets"] == 0


def test_shortlist_respects_m_and_three_l_variants_per_exact_x() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: str, L: int, p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal(x), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": L,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, _manifest = module._select_shortlist(
        (
            candidate("l3", "1", 3, "100"),
            candidate("off", "1", 0, "100"),
            candidate("l2", "1", 2, "90"),
            candidate("l1", "1", 1, "80"),
            candidate("other", "2", 1, "10"),
        ),
        max_candidates=4,
        bank_available=Decimal("10"),
    )
    assert [item.identity for item in selected] == ["l3", "off", "l2", "other"]
    assert len(selected) == 4


def test_shortlist_partial_selection_omits_unknown_p30_instead_of_zero_ranking() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, p30: object) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": p30,
                "limiter_L": 1,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal("1"),
                "required_bank_usdt": Decimal("10"),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )

    selected, manifest = module._select_shortlist(
        (candidate("valid", Decimal("1")), candidate("unknown", "not-a-number")),
        max_candidates=2,
        bank_available=Decimal("10"),
    )
    assert [item.identity for item in selected] == ["valid"]
    assert manifest["invalid_p30_count"] == 1


def test_shortlist_all_invalid_p30_fails_closed_without_numeric_zero() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, p30: object) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={"p30_common_usdt_30d": p30, "limiter_L": 0, "required_bank_usdt": Decimal("10")},
        )

    selected, manifest = module._select_shortlist(
        (candidate("missing", None), candidate("malformed", "not-a-number")),
        max_candidates=2,
    )
    assert selected == ()
    assert manifest["invalid_p30_count"] == 2
    assert manifest["selected_count"] == 0


def test_shortlist_excludes_malformed_limiter_levels_from_selection() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, limiter: object, x: object = Decimal("1"), *, include_x: bool = True) -> PortfolioCandidate:
        member = {"strategy_id": 1, "capacity_usdt": Decimal("100")}
        if include_x:
            member["x_usdt"] = x
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=(member,),
            metrics={"p30_common_usdt_30d": Decimal("100"), "limiter_L": limiter, "required_bank_usdt": Decimal("10")},
        )

    selected, _manifest = module._select_shortlist(
        (
            candidate("missing", None),
            candidate("text", "bad"),
            candidate("negative", -1),
            candidate("boolean", True),
            candidate("bad-x", 1, "bad"),
            candidate("missing-x", 1, include_x=False),
        ),
        max_candidates=5,
        bank_available=Decimal("10"),
    )
    assert selected == ()


def test_shortlist_rejects_mixed_strategy_id_presence_without_dropping_nonzero_member() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    candidate = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="mixed-ids",
        members=(
            {"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},
            {"x_usdt": Decimal("2"), "capacity_usdt": Decimal("100")},
        ),
        metrics={"p30_common_usdt_30d": Decimal("100"), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
    )
    selected, manifest = module._select_shortlist((candidate,), max_candidates=1, bank_available=Decimal("10"))
    assert selected == ()
    assert manifest["invalid_vector_or_level_count"] == 1


@pytest.mark.parametrize(
    "members",
    (
        (
            {"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},
            {"strategy_id": 1, "x_usdt": Decimal("2"), "capacity_usdt": Decimal("100")},
        ),
        (
            {"strategy_id": None, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},
        ),
    ),
)
def test_shortlist_rejects_duplicate_or_none_strategy_ids(members: tuple[dict[str, object], ...]) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    candidate = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="bad-ids",
        members=members,
        metrics={"p30_common_usdt_30d": Decimal("100"), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
    )
    selected, manifest = module._select_shortlist((candidate,), max_candidates=1, bank_available=Decimal("10"))
    assert selected == ()
    assert manifest["invalid_vector_or_level_count"] == 1


def test_shortlist_rejects_fully_unlabeled_candidate_in_labeled_universe() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, members: tuple[dict[str, object], ...], p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=members,
            metrics={"p30_common_usdt_30d": Decimal(p30), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
        )

    selected, manifest = module._select_shortlist(
        (
            candidate("labeled", ({"strategy_id": 1, "x_usdt": Decimal("1")}, {"strategy_id": 2, "x_usdt": Decimal("2")}), "100"),
            candidate("unlabeled", ({"x_usdt": Decimal("2")}, {"x_usdt": Decimal("1")}), "90"),
        ),
        max_candidates=2,
        bank_available=Decimal("10"),
    )
    assert tuple(item.identity for item in selected) == ("labeled",)
    assert manifest["invalid_vector_or_level_count"] == 1


def test_shortlist_rejects_malformed_required_bank_without_bank_cap() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    candidate = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="bad-bank",
        members=({"strategy_id": 1, "x_usdt": Decimal("1")},),
        metrics={"p30_common_usdt_30d": Decimal("100"), "limiter_L": 1, "required_bank_usdt": "bad"},
    )
    selected, manifest = module._select_shortlist((candidate,), max_candidates=1)
    assert selected == ()
    assert manifest["invalid_metric_count"] == 1
    assert manifest["invalid_p30_count"] == 0


def test_shortlist_rejects_model_without_valid_limiter_p30() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    candidate = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="bad-model-p30",
        members=({"strategy_id": 1, "x_usdt": Decimal("1")},),
        metrics={
            "p30_common_usdt_30d": Decimal("100"),
            "limiter_L": 1,
            "limiter_p30_status": "MODEL",
            "p30_limiter_model_usdt_30d": "bad",
            "required_bank_usdt": Decimal("10"),
        },
    )
    selected, manifest = module._select_shortlist((candidate,), max_candidates=1, bank_available=Decimal("10"))
    assert selected == ()
    assert manifest["invalid_metric_count"] == 1
    assert manifest["invalid_p30_count"] == 0


@pytest.mark.parametrize("status", ("model", "GARBAGE", None))
def test_shortlist_rejects_invalid_limiter_status(status: object) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    candidate = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="bad-status",
        members=({"strategy_id": 1, "x_usdt": Decimal("1")},),
        metrics={"p30_common_usdt_30d": Decimal("100"), "limiter_L": 1, "limiter_p30_status": status, "required_bank_usdt": Decimal("10")},
    )
    selected, manifest = module._select_shortlist((candidate,), max_candidates=1, bank_available=Decimal("10"))
    assert selected == ()
    assert manifest["invalid_metric_count"] == 1


def test_shortlist_limiter_aliases_skip_none_primary_values() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    candidate = PortfolioCandidate(
        schema_version="portfolio_candidate_v1",
        profile_id="P",
        scenario_id="S",
        identity="fallback-aliases",
        members=({"strategy_id": 1, "x_usdt": Decimal("1")},),
        metrics={
            "p30_common_usdt_30d": Decimal("100"),
            "limiter_L": None,
            "L": 1,
            "limiter_p30_status": None,
            "p30_limiter_status": "MODEL",
            "p30_limiter_model_usdt_30d": Decimal("10"),
            "required_bank_usdt": Decimal("10"),
        },
    )
    selected, manifest = module._select_shortlist((candidate,), max_candidates=1, bank_available=Decimal("10"))
    assert selected == (candidate,)
    assert manifest["invalid_metric_count"] == 0
    assert manifest["invalid_vector_or_level_count"] == 0


def test_shortlist_collapses_nonterminating_proportional_rays_exactly() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")

    def candidate(identity: str, x: tuple[str, str], p30: str, cdar: str, bank: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=tuple(
                {"strategy_id": index + 1, "x_usdt": Decimal(value), "capacity_usdt": Decimal("100")}
                for index, value in enumerate(x)
            ),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": 1,
                "limiter_p30_status": "MODEL",
                "p30_limiter_model_usdt_30d": Decimal(p30),
                "required_bank_usdt": Decimal(bank),
                "cdar_peak80_usdt": Decimal(cdar),
            },
        )

    candidates = (
        candidate("global", ("1", "1"), "30", "9", "10"),
        candidate("ray-p30", ("1", "2"), "20", "7", "50"),
        candidate("ray-cdar", ("1e100", "2e100"), "20", "4", "50"),
        candidate("ray-bank", ("1e-100", "2e-100"), "20", "4", "10"),
        candidate("ray-low", ("10", "20"), "10", "1", "5"),
    )
    selected, manifest = module._select_shortlist(candidates, max_candidates=5, bank_available=Decimal("100"))
    reversed_selected, reversed_manifest = module._select_shortlist(tuple(reversed(candidates)), max_candidates=5, bank_available=Decimal("100"))
    assert [item.identity for item in selected[:2]] == ["global", "ray-bank"]
    assert manifest["step_counts"]["families"] == 1
    assert tuple(item.identity for item in reversed_selected) == tuple(item.identity for item in selected)
    assert reversed_manifest["selected_origins"] == manifest["selected_origins"]


def test_shortlist_distinguishes_one_ulp_after_hundred_digit_proportional_ray() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    huge = "1" + ("0" * 150)
    proportional = "2" + ("0" * 150)
    one_ulp = proportional[:-1] + "1"

    def candidate(identity: str, x: tuple[str, str], p30: str) -> PortfolioCandidate:
        return PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=tuple(
                {"strategy_id": index + 1, "x_usdt": Decimal(value), "capacity_usdt": Decimal("100000000000000000000000000000000000000000000000000")}
                for index, value in enumerate(x)
            ),
            metrics={"p30_common_usdt_30d": Decimal(p30), "limiter_L": 1, "required_bank_usdt": Decimal("10")},
        )

    selected, manifest = module._select_shortlist(
        (candidate("primary", (huge, proportional), "100"), candidate("one-ulp", (huge, one_ulp), "90")),
        max_candidates=2,
        bank_available=Decimal("10"),
    )
    assert tuple(item.identity for item in selected) == ("primary", "one-ulp")
    assert manifest["step_counts"]["families"] == 1


def test_shortlist_canonicalizes_numeric_equal_x_scales_for_three_l_cap() -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    values = (100, "100", Decimal("100.00"), Decimal("1E+2"))
    candidates = tuple(
        PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=f"x-{index}",
            members=({"strategy_id": 1, "x_usdt": value},),
            metrics={
                "p30_common_usdt_30d": Decimal("100"),
                "limiter_L": index,
                "limiter_p30_status": "UNKNOWN",
                "required_bank_usdt": Decimal("10"),
            },
        )
        for index, value in enumerate(values)
    )
    selected, _manifest = module._select_shortlist(candidates, max_candidates=4, bank_available=Decimal("10"))
    assert tuple(item.identity for item in selected) == ("x-0", "x-3", "x-2")


def test_weighted_search_marks_same_x_neighbor_variants_as_limiter_and_is_repeatable(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("mrs3.portfolio.weighted_search")
    prepared = _prepared((("0.4",), ("0.1",)), strategy_ids=(1,))
    members = _members(1)
    variants = tuple(
        PortfolioCandidate(
            schema_version="portfolio_candidate_v1",
            profile_id="P",
            scenario_id="S",
            identity=identity,
            members=({"strategy_id": 1, "x_usdt": Decimal("1"), "capacity_usdt": Decimal("100")},),
            metrics={
                "p30_common_usdt_30d": Decimal(p30),
                "limiter_L": limiter,
                "limiter_p30_status": status,
                "p30_limiter_model_usdt_30d": Decimal(p30) if status == "MODEL" else None,
                "required_bank_usdt": Decimal(bank),
                "cdar_peak80_usdt": Decimal("1"),
            },
        )
        for identity, limiter, status, p30, bank in (
            ("base", 1, "MODEL", "30", "10"),
            ("neighbor", 2, "MODEL", "20", "20"),
            ("off", 0, "UNKNOWN", "10", "30"),
        )
    )
    monkeypatch.setattr(
        module,
        "_solve_lp",
        lambda *args, **kwargs: module._SolveOutcome("PASS", _Solution(Decimal("10"), (Decimal("1"),))),
    )
    monkeypatch.setattr(module, "_candidates_for_solution", lambda *args, **kwargs: variants)

    first = weighted_search(
        prepared,
        (Decimal("100"),),
        members=members,
        bank_available=Decimal("100"),
        max_targets=1,
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=3,
    )
    second = weighted_search(
        prepared,
        (Decimal("100"),),
        members=members,
        bank_available=Decimal("100"),
        max_targets=1,
        bootstrap_scenarios=1,
        screening_scenarios=1,
        max_candidates=3,
    )
    assert first.status == second.status == "PASS"
    assert first.manifest["shortlist"]["selected_identities"] == second.manifest["shortlist"]["selected_identities"]
    assert first.manifest["shortlist"]["selected_origins"] == second.manifest["shortlist"]["selected_origins"]
    assert first.manifest["shortlist"]["selected_origins"] == ("scale", "limiter", "limiter")
