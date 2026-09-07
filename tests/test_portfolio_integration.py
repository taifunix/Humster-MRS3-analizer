from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json

import pytest

from mrs3.portfolio.integration import (
    Attempt,
    FAIL,
    INSUFFICIENT_EVIDENCE,
    PASS,
    RESEARCH_ONLY,
    PortfolioIntegration,
    PortfolioIntegrationError,
    ResumeSnapshot,
    UNKNOWN,
    _candidate_universe_digest,
    _variant_id,
    evaluate_research_risk,
    freeze_campaign,
)
from mrs3.portfolio.metrics import calculate_metrics
from mrs3.portfolio.reports import normalize_report
from mrs3.portfolio.search import search_portfolios
from mrs3.portfolio.store import PortfolioStore
from tests.test_portfolio_reports import fixture_report


def candidate(*, side: str = "LONG", symbol: str = "BTCUSDT", pnl: str = "10", scalar: str = "100") -> dict[str, object]:
    return {
        "status": "FINALIST", "symbol": symbol, "side": side,
        "strategy_id": f"strategy-{side.lower()}", "result_id": f"result-{side.lower()}",
        "timeframe": "1h", "geometry": {"entry": "ma"}, "lot_x": "1",
        "orders": [{"quantity": "100", "qty_step": "1", "min_qty": "1"}],
        "liquidity_scalar_pct_max": scalar, "margin_scalar_pct_max": scalar,
        "exchange_scalar_pct_max": scalar, "d100": "1", "d100_currency": "USDT",
        "d100_timestamp_ms": 1, "d100_expires_at_ms": 100,
        "net_pnl": pnl, "initial_margin": "1", "runtime": {"mode": "linear"},
    }


def capability() -> dict[str, object]:
    return {"dual_tf": True, "dedicated_close": True, "opposite_opening": True, "common_runtime_fields": ["mode"]}


def campaign(*, budget: int = 10, policy: dict[str, object] | None = None, grid: tuple[str, ...] = ("10", "20"), upstream_periods=((0, 10),), hypothesis_values=("h1",), resume_from=None, selection_period=(0, 10)):
    return freeze_campaign(
        (0, 10), (10, 20), upstream_selection={"surface": "selection-1", "period": selection_period},
        upstream_used_periods=upstream_periods, hypotheses=hypothesis_values, warmup={"seconds": 1},
        state_boundary="inclusive-start-exclusive-end", initial_wallet={"USDT": "100"},
        initial_equity={"amount": "100", "currency": "USDT"}, initial_positions={}, attribution="carry-in",
        evidence_minimum={"cycles": 1}, profile="AGGRESSIVE", config_id="config-1",
        policy_ids=policy or {"risk": "portfolio_optimizer_research_risk_v1", "pnl": "pnl-1", "liquidity": "liq-1", "ranking": "rank-1"},
        sizing_grid=grid, ranking_policy={"version": "rank-1", "metrics": [{"field": "net_pnl", "direction": "DESC"}], "tie_breaker": "candidate_id"},
        total_test_budget=budget, evaluation_clock=10, refinement_rounds=1, resume_from=resume_from,
        capability=capability(), current_equity={"amount": "100", "currency": "USDT", "timestamp_ms": 1, "expires_at_ms": 100},
        dd_cap_pct="20", composition=None, priorities=None, opposite_policy="KEEP_OPPOSITE", limiter=0,
    )


def evidence(*, dd: str = "1", im: str = "10", mm: str = "10", balance: str = "100") -> dict[str, object]:
    return {"actual_equity_dd_pct": dd, "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": [{"timestamp": 1, "expires_at_ms": 100, "currency": "USDT", "margin_balance": balance, "calculated_total_im": im, "calculated_total_mm": mm}]}


def run(campaign_value, candidates, **kwargs):
    options = {"capability": capability(), "current_equity": {"amount": "100", "currency": "USDT", "timestamp_ms": 1, "expires_at_ms": 100}, "now_ms": 10, "dd_cap_pct": "20"}
    options.update(kwargs.pop("search_options", {}))
    test = kwargs.pop("test", lambda value: True)
    importer = kwargs.pop("import_result", lambda value: evidence())
    store = kwargs.pop("store", None)
    return PortfolioIntegration(campaign_value, store=store).run(candidates, search_options=options, test=test, import_result=importer, **kwargs)


def test_freeze_rejects_overlap_and_keeps_immutable_digest():
    with pytest.raises(PortfolioIntegrationError, match="overlap"):
        campaign_value = campaign()
        freeze_campaign((0, 11), (10, 20), upstream_selection={"id": "u"}, warmup=1, state_boundary="b", initial_wallet="w", initial_equity="e", initial_positions={}, attribution="a", evidence_minimum=1, profile="AGGRESSIVE", config_id="c", policy_ids={"risk": "r"}, sizing_grid=("1",), ranking_policy={"version": "v", "metrics": [{"field": "net_pnl", "direction": "DESC"}], "tie_breaker": "candidate_id"}, total_test_budget=1)
    frozen = campaign()
    assert frozen.identity == frozen.digest
    with pytest.raises(TypeError):
        frozen.policy_ids["risk"] = "changed"  # type: ignore[index]


def test_windows_deep_freeze_caller_nested_mutation_cannot_change_campaign():
    mutable_window = {"start": 0, "end": 10, "metadata": {"label": "development"}}
    frozen = replace(campaign(), development_window=mutable_window)
    before = frozen.as_dict()
    mutable_window["metadata"]["label"] = "changed"
    assert frozen.as_dict() == before
    assert frozen.digest == before["canonical_digest"]


def test_risk_checks_are_joint_and_fail_even_with_high_pnl():
    result = evaluate_research_risk(evidence(dd="1", im="10", mm="90"), profile="AGGRESSIVE", now_ms=10)
    assert result.status == FAIL
    assert result.checks[2].name == "account_mm_load"
    assert result.checks[2].status == FAIL


def test_unknown_risk_evidence_never_becomes_pass():
    result = evaluate_research_risk({"actual_equity_dd_pct": "1"}, profile="AGGRESSIVE")
    assert result.status == "UNKNOWN"
    assert all(check.status == "UNKNOWN" for check in result.checks)


def test_all_grid_points_are_tested_and_resume_does_not_repeat_callbacks():
    frozen = campaign(budget=10, grid=("10", "20"))
    calls: list[str] = []
    result = run(frozen, [candidate()], test=lambda value: calls.append("test") or True, import_result=lambda value: calls.append("import") or evidence())
    assert len(calls) == 8  # two variants for each of development and validation
    resumed = run(frozen, [candidate()], resume=result, test=lambda value: calls.append("rerun") or True, import_result=lambda value: evidence())
    assert "rerun" not in calls
    assert resumed.selected == result.selected


def test_shared_budget_records_not_tested_attempts_and_refuses_relaxation():
    frozen = campaign(budget=1, grid=("10", "20"))
    result = run(frozen, [candidate()])
    assert result.exhausted
    assert any(attempt.status == "NOT_TESTED_BUDGET" for attempt in result.attempts)
    failed = run(frozen, [candidate()], import_result=lambda value: evidence(mm="90"))
    assert failed.reason == "NO_VALIDATION_PASS"
    assert all(attempt.reason != "AUTO_RELAXED" for attempt in failed.attempts)


def test_refinement_consumes_the_same_budget_and_can_supply_new_variants():
    frozen = campaign(budget=4, grid=("10",))
    initial = candidate()
    refined = candidate(symbol="ETHUSDT")
    calls: list[str] = []

    def test(value):
        calls.append(value.slot.symbol)
        return {"status": "FAIL"} if value.slot.symbol == "BTCUSDT" else True

    result = run(frozen, [initial], test=test, refine=lambda attempts: [refined], rounds=1)
    assert "ETHUSDT" in calls
    assert any(attempt.phase == "refine" for attempt in result.attempts)


def test_frozen_validation_order_wins_over_validation_return():
    frozen = campaign(budget=20, grid=("10",))
    validation_order: list[str] = []

    def validation(value):
        validation_order.append(value.slot.symbol)
        return True

    result = run(frozen, [candidate(symbol="BTCUSDT", pnl="20"), candidate(symbol="ETHUSDT", pnl="10")], validation_test=validation)
    assert result.status == RESEARCH_ONLY
    assert result.selected == result.finalists[0]
    assert validation_order == ["BTCUSDT", "ETHUSDT"]


def test_missing_policy_is_explicit_open_policy_and_no_candidate_is_feasible():
    missing = run(campaign(policy={"risk": "portfolio_optimizer_research_risk_v1"}), [candidate()])
    assert missing.status == RESEARCH_ONLY and missing.reason == "OPEN_POLICY"
    empty = run(campaign(), [{"status": "RESERVE", "symbol": "BTCUSDT", "side": "LONG"}])
    assert empty.status == INSUFFICIENT_EVIDENCE
    assert empty.reason in {"NO_FINALIST", "NO_FEASIBLE_CANDIDATE"}


def test_global_liquidity_sees_every_variant_and_accounts_keep_deposits():
    frozen = campaign(budget=10, grid=("10",))
    seen = []

    def screen(variants, accounts):
        seen.append((len(variants), accounts))
        return True

    accounts = {"a": {"deposit": "100"}, "b": {"deposit": "250"}}
    result = run(frozen, [candidate(), candidate(symbol="ETHUSDT")], accounts=accounts, global_liquidity_gate=screen)
    assert result.status == RESEARCH_ONLY
    assert seen and seen[0][0] == 2
    assert result.accounts == accounts


def test_callback_verdicts_fail_closed_and_precheck_exception_is_explicit():
    frozen = campaign(budget=10, grid=("10",))
    rejected = run(frozen, [candidate()], precheck=lambda value: (_ for _ in ()).throw(RuntimeError("boom")))
    precheck_attempt = next(attempt for attempt in rejected.attempts if attempt.phase == "development")
    assert precheck_attempt.status == "PRECHECK_REJECTED"
    assert precheck_attempt.reason == "PRECHECK_EXCEPTION"

    failed = run(frozen, [candidate()], test=lambda value: "ERROR")
    development = next(attempt for attempt in failed.attempts if attempt.phase == "development" and attempt.status == "FAILED")
    assert development.reason == "ERROR"

    validation = run(frozen, [candidate()], validation_test=lambda value: "UNKNOWN")
    validation_attempt = next(attempt for attempt in validation.attempts if attempt.phase == "validation")
    assert validation_attempt.status == "VALIDATION_FAILED"
    assert validation_attempt.reason == "UNKNOWN"


def test_resume_with_consumed_budget_preserves_validation_pass_without_callbacks():
    frozen = campaign(budget=2, grid=("10",))
    first = run(frozen, [candidate()], validation_test=lambda value: True)
    assert first.selected is not None
    calls: list[str] = []
    resumed = run(frozen, [candidate()], resume=first, test=lambda value: calls.append("test") or True, validation_test=lambda value: calls.append("validation") or True)
    assert resumed.selected == first.selected
    assert not calls
    assert len(resumed.attempts) == len(first.attempts)


def test_larger_budget_requires_linked_campaign_and_tests_prior_budget_markers():
    original = campaign(budget=1, grid=("10",))
    first = run(original, [candidate()])
    assert any(attempt.status == "NOT_TESTED_BUDGET" for attempt in first.attempts)
    continuation = campaign(budget=2, grid=("10",), resume_from=original)
    with pytest.raises(PortfolioIntegrationError, match="campaign digest"):
        run(continuation, [candidate()], resume=first)
    resumed = run(continuation, [candidate()], resume=first.resume_snapshot)
    assert isinstance(first.resume_snapshot, ResumeSnapshot)
    assert not any(attempt.status == "NOT_TESTED_BUDGET" for attempt in resumed.attempts)
    assert {attempt.phase for attempt in resumed.attempts if attempt.status in {"IMPORTED", "VALIDATION_PASS"}} == {"development", "validation"}
    assert len({(attempt.identity, attempt.phase) for attempt in resumed.attempts}) == len(resumed.attempts)


def test_runtime_search_facts_cannot_change_after_freeze():
    frozen = campaign(budget=4, grid=("10",))
    changed_equity = {"amount": "101", "currency": "USDT", "timestamp_ms": 1, "expires_at_ms": 100}
    with pytest.raises(PortfolioIntegrationError, match="current_equity"):
        run(frozen, [candidate()], search_options={"current_equity": changed_equity})
    with pytest.raises(PortfolioIntegrationError, match="dd_cap_pct"):
        run(frozen, [candidate()], search_options={"dd_cap_pct": "19"})


def test_refinement_rejection_is_excluded_from_joint_global_liquidity_screen():
    frozen = campaign(budget=5, grid=("10",))
    refined = candidate(symbol="ETHUSDT")
    seen: list[str] = []

    def test(value):
        return False

    def screen(variants):
        seen.extend(variant.slot.symbol for variant in variants)
        return False if "ETHUSDT" in seen else True

    result = run(frozen, [candidate()], test=test, refine=lambda attempts: [refined], rounds=1, global_liquidity_gate=screen)
    assert result.reason == "NO_VALIDATION_PASS"
    assert "ETHUSDT" not in seen


def test_global_liquidity_uses_imported_development_members_for_initial_and_refined_paths():
    refined = candidate(symbol="ETHUSDT")
    direct_seen: list[str] = []
    direct = run(
        campaign(budget=5, grid=("10",)),
        [refined],
        global_liquidity_gate=lambda variants: direct_seen.extend(item.slot.symbol for item in variants) or True,
    )
    refined_seen: list[str] = []
    calls = 0

    def development(value):
        nonlocal calls
        calls += 1
        return calls > 1

    refined_result = run(
        campaign(budget=5, grid=("10",)),
        [candidate()],
        test=development,
        refine=lambda attempts: [refined],
        rounds=1,
        global_liquidity_gate=lambda variants: refined_seen.extend(item.slot.symbol for item in variants) or True,
    )
    assert direct.status == RESEARCH_ONLY
    assert refined_result.status == RESEARCH_ONLY
    assert refined_seen == direct_seen == ["ETHUSDT"]


def test_holdout_requires_upstream_periods_and_hypotheses_and_labels_reuse():
    with pytest.raises(PortfolioIntegrationError, match="upstream_used_periods must be nonempty"):
        campaign(upstream_periods=())
    with pytest.raises(PortfolioIntegrationError, match="hypotheses must be nonempty"):
        campaign(hypothesis_values=())
    repeated = campaign(upstream_periods=((10, 20),))
    assert repeated.repeated_history_verification
    assert repeated.as_dict()["validation_label"] == "REPEATED_HISTORY_VERIFICATION"


def test_variant_identity_includes_both_directional_legs():
    long_leg = candidate(side="LONG")
    short_one = candidate(side="SHORT", pnl="5")
    short_two = candidate(side="SHORT", pnl="6")
    options = {"capability": capability(), "current_equity": {"amount": "100", "currency": "USDT", "timestamp_ms": 1, "expires_at_ms": 100}, "dd_cap_pct": "20", "sizing_grid": ("10",), "ranking_policy": {"version": "rank", "metrics": [{"field": "net_pnl", "direction": "DESC"}], "tie_breaker": "candidate_id"}, "now_ms": 10}
    one = next(variant for variant in search_portfolios([long_leg, short_one], **options).passing if variant.composition == "LONG+SHORT")
    two = next(variant for variant in search_portfolios([long_leg, short_two], **options).passing if variant.composition == "LONG+SHORT")
    assert _variant_id(one) != _variant_id(two)


def test_equity_freshness_and_actual_joint_witness_fail_closed():
    expired = evidence()
    expired["expires_at_ms"] = 9
    assert evaluate_research_risk(expired, profile="AGGRESSIVE", now_ms=10).checks[0].status == "UNKNOWN"
    future = evidence()
    future["timestamp_ms"] = 11
    assert evaluate_research_risk(future, profile="AGGRESSIVE", now_ms=10).checks[0].status == "UNKNOWN"
    nested_fresh = {"timestamp_ms": 1, "expires_at_ms": 9, "risk": {"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100}, "margin_states": evidence()["margin_states"]}
    assert evaluate_research_risk(nested_fresh, profile="AGGRESSIVE", now_ms=10).checks[0].status == "UNKNOWN"
    generic = {"drawdown_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": evidence()["margin_states"]}
    assert evaluate_research_risk(generic, profile="AGGRESSIVE", now_ms=10).checks[0].status == "UNKNOWN"


@pytest.mark.parametrize("clock", [None, "abc", True])
def test_invalid_evaluation_clock_fails_all_risk_checks_closed(clock):
    result = evaluate_research_risk(evidence(), profile="AGGRESSIVE", now_ms=clock)
    assert result.status == UNKNOWN
    assert all(check.status == UNKNOWN for check in result.checks)
    assert all(check.reason == "EVALUATION_CLOCK_UNAVAILABLE" for check in result.checks)


@pytest.mark.parametrize("contract", [{}, {"max_age_ms": 8}])
def test_missing_evaluation_clock_cannot_pass_margin_freshness(contract):
    state = {"timestamp": 1, "expires_at_ms": 100, "currency": "USDT", "margin_balance": "100", "calculated_total_im": "10", "calculated_total_mm": "10"}
    result = evaluate_research_risk(
        {"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": [state]},
        profile="AGGRESSIVE", now_ms=None, freshness_contract=contract,
    )
    assert result.checks[1].status == UNKNOWN
    assert result.checks[2].status == UNKNOWN
    assert result.checks[1].reason == result.checks[2].reason == "EVALUATION_CLOCK_UNAVAILABLE"


def test_metrics_freshness_derives_expiry_from_boundary_and_max_age():
    metrics = calculate_metrics(normalize_report(fixture_report()))
    boundary_ms = int(datetime.fromisoformat(metrics.boundary_end.replace("Z", "+00:00")).timestamp() * 1000)
    result = evaluate_research_risk(
        metrics,
        profile="AGGRESSIVE",
        now_ms=boundary_ms + 2,
        freshness_contract={"require_timestamp": True, "require_expiry": True, "max_age_ms": 2},
    )
    assert result.checks[0].status == PASS


def test_complete_policy_requires_a_valid_frozen_freshness_contract():
    incomplete = replace(campaign(), freshness_contract={})
    assert not PortfolioIntegration(incomplete)._complete_policy()


def test_refinement_round_is_idempotent_on_resume():
    frozen = campaign(budget=5, grid=("10",))
    calls: list[str] = []
    refined = candidate(symbol="ETHUSDT")
    first = run(frozen, [candidate()], test=lambda value: value.slot.symbol == "ETHUSDT", refine=lambda attempts: calls.append("refine") or [refined], rounds=1)
    assert calls == ["refine"]
    calls.clear()
    resumed = run(frozen, [candidate()], resume=first, test=lambda value: True, refine=lambda attempts: calls.append("duplicate") or [refined], rounds=1)
    assert calls == []
    assert resumed.attempts == first.attempts


def test_multi_account_margin_uses_worst_reserve_and_load_and_rejects_mismatch():
    states = [
        {"account_id": "a", "account_evidence_id": "ea", "evaluation_snapshot_id": "snap", "timestamp": 1, "expires_at_ms": 100, "currency": "USDT", "margin_balance": "100", "calculated_total_im": "10", "calculated_total_mm": "20"},
        {"account_id": "b", "account_evidence_id": "eb", "evaluation_snapshot_id": "snap", "timestamp": 1, "expires_at_ms": 100, "currency": "USDT", "margin_balance": "200", "calculated_total_im": "100", "calculated_total_mm": "100"},
    ]
    result = evaluate_research_risk({"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": states}, profile="AGGRESSIVE", now_ms=10)
    assert result.status == PASS
    assert result.checks[1].value == Decimal("50")
    assert result.checks[2].value == Decimal("50")
    mixed = [dict(states[0]), {**states[1], "currency": "EUR"}]
    mismatch = evaluate_research_risk({"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": mixed}, profile="AGGRESSIVE", now_ms=10)
    assert mismatch.checks[1].reason == "MARGIN_CURRENCY_MISMATCH"


def test_store_ids_include_run_and_evaluation_payload_and_are_idempotent(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    frozen = campaign(budget=4, grid=("10",))
    first = run(frozen, [candidate()], store=store, accounts={"a": {"deposit": "100"}})
    replay = run(frozen, [candidate()], store=store, accounts={"a": {"deposit": "100"}})
    other_account = run(frozen, [candidate()], store=store, accounts={"a": {"deposit": "200"}})
    failed = run(frozen, [candidate()], store=store, import_result=lambda value: evidence(mm="90"), accounts={"a": {"deposit": "100"}})
    assert first.decision_campaign_id == replay.decision_campaign_id
    assert first.execution_campaign_id == replay.execution_campaign_id
    assert first.run_id is not None
    assert store.get_trading_run(first.run_id) is not None
    assert first.evaluation_id == replay.evaluation_id
    assert other_account.run_id != first.run_id
    assert other_account.evaluation_id != first.evaluation_id
    assert failed.evaluation_id != first.evaluation_id
    assert store.get_evaluation(first.evaluation_id) is not None


def test_exact_budget_resume_replays_imported_refinement_and_store_evaluation(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    frozen = campaign(budget=4, grid=("10",))
    refined = candidate(symbol="ETHUSDT")

    def development(value):
        return value.slot.symbol == "ETHUSDT"

    first = run(
        frozen,
        [candidate()],
        test=development,
        refine=lambda attempts: [refined],
        store=store,
    )
    assert first.selected is not None
    assert first.exhausted
    assert max(attempt.budget_index or 0 for attempt in first.attempts) == 4

    def unexpected(*args):
        raise AssertionError("completed refinement or test was rerun")

    resumed = run(frozen, [candidate()], test=unexpected, refine=unexpected, store=store, resume=first)
    assert resumed.selected == first.selected
    assert resumed.attempts == first.attempts
    assert resumed.evaluation_id == first.evaluation_id


def test_resume_refined_search_failure_preserves_imported_refinement(monkeypatch):
    frozen = campaign(budget=6, grid=("10",))
    refined = candidate(symbol="ETHUSDT")
    first = run(frozen, [candidate()], test=lambda value: False, refine=lambda attempts: {"candidates": [refined]}, rounds=1)
    imported = next(attempt for attempt in first.attempts if attempt.identity == "refine-0" and attempt.phase == "refine")
    assert imported.status == "IMPORTED"

    import mrs3.portfolio.integration as integration_module

    search_calls = 0
    original_search = integration_module.search_portfolios

    def fail_refined_search(candidates, **options):
        nonlocal search_calls
        search_calls += 1
        if search_calls == 2:
            raise RuntimeError("derived search failed")
        return original_search(candidates, **options)

    monkeypatch.setattr(integration_module, "search_portfolios", fail_refined_search)
    resumed = run(frozen, [candidate()], resume=first, test=lambda value: False, refine=lambda attempts: {"candidates": [refined]}, rounds=1)
    imported_after = next(attempt for attempt in resumed.attempts if attempt.identity == "refine-0" and attempt.phase == "refine")
    derived = next(attempt for attempt in resumed.attempts if attempt.identity == "refine-0:search" and attempt.phase == "refine_search")
    assert imported_after.status == "IMPORTED"
    assert derived.status == "FAILED"
    assert derived.reason == "REFINED_SEARCH_FAILED"
    assert derived.budget_index is None


def test_search_failures_without_callbacks_do_not_consume_budget():
    original = campaign(budget=1, grid=("10", "20"))
    frozen = campaign(budget=2, grid=("10", "20"), resume_from=original)
    search = search_portfolios(
        [candidate(scalar="10")],
        capability=capability(),
        current_equity={"amount": "100", "currency": "USDT", "timestamp_ms": 1, "expires_at_ms": 100},
        dd_cap_pct="20",
        sizing_grid=("10", "20"),
        ranking_policy={"version": "rank-1", "metrics": [{"field": "net_pnl", "direction": "DESC"}], "tie_breaker": "candidate_id"},
        now_ms=10,
    )
    failed_variant = next(variant for variant in search.tried if variant.status != PASS)
    snapshot = ResumeSnapshot(
        original.canonical_digest,
        (Attempt(_variant_id(failed_variant), "development", "FAILED", _variant_id(failed_variant), failed_variant, "LIQUIDITY_QUALITY_INSUFFICIENT"),),
        _candidate_universe_digest([candidate(scalar="10")]),
    )
    calls: list[str] = []
    result = run(
        frozen,
        [candidate(scalar="10")],
        resume=snapshot,
        test=lambda value: calls.append("test") or True,
        validation_test=lambda value: calls.append("validation") or True,
    )
    assert result.selected is not None
    assert calls == ["test", "validation"]
    assert not any(attempt.status == "NOT_TESTED_BUDGET" for attempt in result.attempts)
    search_failed = [attempt for attempt in result.attempts if attempt.status == "FAILED" and attempt.phase == "development"]
    assert search_failed and all(attempt.budget_index is None for attempt in search_failed)


def test_unknown_search_option_is_rejected_before_search():
    with pytest.raises(PortfolioIntegrationError, match="unknown search option"):
        run(campaign(budget=2, grid=("10",)), [candidate()], search_options={"unexpected": True})


def test_single_account_expired_margin_state_has_generic_unknown_reason():
    state = {"timestamp": 1, "expires_at_ms": 9, "currency": "USDT", "margin_balance": "100", "calculated_total_im": "10", "calculated_total_mm": "10"}
    result = evaluate_research_risk(
        {"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": [state]},
        profile="AGGRESSIVE",
        now_ms=10,
    )
    assert result.checks[1].status == "UNKNOWN"
    assert result.checks[1].reason == "MARGIN_EVIDENCE_UNAVAILABLE"


def test_missing_global_liquidity_is_explicitly_unchecked():
    result = run(campaign(budget=2, grid=("10",)), [candidate()])
    assert result.status == RESEARCH_ONLY
    assert "GLOBAL_LIQUIDITY_NOT_EVALUATED" in result.reasons


def test_upstream_selection_period_marks_repeated_history_when_not_in_used_periods():
    repeated = campaign(upstream_periods=((0, 10),), selection_period=(10, 20))
    assert repeated.repeated_history_verification
    assert repeated.as_dict()["validation_label"] == "REPEATED_HISTORY_VERIFICATION"


def test_margin_freshness_requires_expiry_for_single_and_multi_account_states():
    single = {"timestamp": 1, "currency": "USDT", "margin_balance": "100", "calculated_total_im": "10", "calculated_total_mm": "10"}
    single_result = evaluate_research_risk(
        {"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": [single]},
        profile="AGGRESSIVE", now_ms=10, freshness_contract={"require_expiry": True},
    )
    assert single_result.checks[1].status == "UNKNOWN"
    assert single_result.checks[1].reason == "MARGIN_EVIDENCE_UNAVAILABLE"

    multi = [
        {"account_id": "a", "account_evidence_id": "ea", "evaluation_snapshot_id": "snap", "timestamp": 1, "expires_at_ms": 100, "currency": "USDT", "margin_balance": "100", "calculated_total_im": "10", "calculated_total_mm": "10"},
        {"account_id": "b", "account_evidence_id": "eb", "evaluation_snapshot_id": "snap", "timestamp": 1, "currency": "USDT", "margin_balance": "100", "calculated_total_im": "10", "calculated_total_mm": "10"},
    ]
    multi_result = evaluate_research_risk(
        {"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": multi},
        profile="AGGRESSIVE", now_ms=10, freshness_contract={"require_expiry": True},
    )
    assert multi_result.checks[1].status == "UNKNOWN"
    assert multi_result.checks[1].reason == "MARGIN_EVIDENCE_UNAVAILABLE"


def test_freshness_max_age_applies_to_margin_and_portfolio_metrics():
    state = {"timestamp": 1, "expires_at_ms": 100, "currency": "USDT", "margin_balance": "100", "calculated_total_im": "10", "calculated_total_mm": "10"}
    margin_result = evaluate_research_risk(
        {"actual_equity_dd_pct": "1", "timestamp_ms": 1, "expires_at_ms": 100, "margin_states": [state]},
        profile="AGGRESSIVE", now_ms=10, freshness_contract={"require_expiry": True, "max_age_ms": 8},
    )
    assert margin_result.checks[1].status == "UNKNOWN"
    assert margin_result.checks[1].reason == "MARGIN_EVIDENCE_UNAVAILABLE"

    metrics = calculate_metrics(normalize_report(fixture_report()))
    now_ms = int(datetime.fromisoformat(metrics.boundary_end.replace("Z", "+00:00")).timestamp() * 1000) + 2
    equity_result = evaluate_research_risk(
        metrics, profile="AGGRESSIVE", now_ms=now_ms, freshness_contract={"require_expiry": False, "max_age_ms": 1},
    )
    assert equity_result.checks[0].status == "UNKNOWN"
    assert equity_result.checks[0].reason == "EQUITY_EVIDENCE_UNAVAILABLE"


def test_integration_result_resume_rejects_changed_candidate_universe():
    frozen = campaign(budget=4, grid=("10",))
    first = run(frozen, [candidate()])
    with pytest.raises(PortfolioIntegrationError, match="candidate universe"):
        run(frozen, [candidate(symbol="ETHUSDT")], resume=first)


def test_linked_resume_snapshot_rejects_changed_candidate_universe():
    frozen = campaign(budget=4, grid=("10",))
    first = run(frozen, [candidate()])
    continuation = campaign(budget=4, grid=("10",), resume_from=frozen)
    with pytest.raises(PortfolioIntegrationError, match="candidate universe"):
        run(continuation, [candidate(symbol="ETHUSDT")], resume=first.resume_snapshot)


def test_different_candidate_universes_have_distinct_persisted_scopes(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    frozen = campaign(budget=4, grid=("10",))
    first = run(frozen, [candidate()], store=store)
    second = run(frozen, [candidate(symbol="ETHUSDT")], store=store)
    assert first.candidate_universe_digest != second.candidate_universe_digest
    assert first.decision_campaign_id != second.decision_campaign_id
    assert first.execution_campaign_id != second.execution_campaign_id


def test_candidate_universe_digest_preserves_input_order():
    first = _candidate_universe_digest([candidate(), candidate(symbol="ETHUSDT")])
    second = _candidate_universe_digest([candidate(symbol="ETHUSDT"), candidate()])
    assert first != second


def test_search_ledger_dedupes_by_identity_and_phase():
    original = campaign(budget=4, grid=("10", "20"))
    continuation = campaign(budget=4, grid=("10", "20"), resume_from=original)
    search = search_portfolios(
        [candidate(scalar="10")],
        capability=capability(),
        current_equity={"amount": "100", "currency": "USDT", "timestamp_ms": 1, "expires_at_ms": 100},
        dd_cap_pct="20",
        sizing_grid=("10", "20"),
        ranking_policy={"version": "rank-1", "metrics": [{"field": "net_pnl", "direction": "DESC"}], "tie_breaker": "candidate_id"},
        now_ms=10,
    )
    rejected = next(variant for variant in search.tried if variant.status != PASS)
    identity = _variant_id(rejected)
    snapshot = ResumeSnapshot(
        original.canonical_digest,
        (Attempt(identity, "validation", "VALIDATION_FAILED", identity, rejected, "prior"),),
        _candidate_universe_digest([candidate(scalar="10")]),
    )
    result = run(continuation, [candidate(scalar="10")], resume=snapshot)
    assert {(attempt.identity, attempt.phase) for attempt in result.attempts}.issuperset({(identity, "development"), (identity, "validation")})


def test_blocked_resume_preserves_prior_attempt_ledger_in_persistence(tmp_path):
    store = PortfolioStore(tmp_path / "portfolio.duckdb")
    frozen = campaign(budget=4, grid=("10",))
    first = run(frozen, [candidate()], store=store)
    blocked = run(frozen, [candidate()], resume=first, test=None, import_result=None, store=store)
    assert blocked.reason == "INJECTED_TEST_AND_IMPORT_REQUIRED"
    assert blocked.attempts == first.attempts
    persisted = store.get_evaluation(blocked.evaluation_id)
    assert persisted is not None
    assert len(json.loads(persisted["payload"])["attempts"]) == len(first.attempts)


@pytest.mark.parametrize("change", ["candidate", "campaign"])
def test_resume_binding_is_checked_before_missing_callbacks(change):
    frozen = campaign(budget=4, grid=("10",))
    first = run(frozen, [candidate()])
    if change == "candidate":
        resumed_campaign = frozen
        resumed_candidates = [candidate(symbol="ETHUSDT")]
    else:
        resumed_campaign = campaign(budget=4, grid=("10",), resume_from=frozen)
        resumed_candidates = [candidate()]
    with pytest.raises(PortfolioIntegrationError, match="digest"):
        run(resumed_campaign, resumed_candidates, resume=first, test=None, import_result=None)
