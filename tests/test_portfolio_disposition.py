import pytest

from mrs3.portfolio.canonical import (
    PORTFOLIO_DISPOSITION_V1,
    PORTFOLIO_EVIDENCE_CLASS_V1,
    PORTFOLIO_GATE_RESULT_V1,
    PORTFOLIO_REASON_V1,
)
from mrs3.portfolio.disposition import (
    OPEN_POLICY_CONDITIONS_V1,
    PORTFOLIO_DISPOSITION_RULES_V1,
    applicable_reasons,
    campaign_ready_allowed,
    dependent_evaluation_allowed,
    rules_for,
)


EXPECTED_RULES = {
    "owner_unverifiable_or_operator_manual_clear": (
        "requested protected action / ownership audit",
        "stop without write / durable audit event",
        (),
        ("LOCK_OWNER_UNVERIFIABLE", "LOCK_MANUAL_CLEAR"),
    ),
    "turnover_missing_stale_or_request_failed": (
        "Evaluation / PortfolioSet",
        "INSUFFICIENT_EVIDENCE",
        ("INSUFFICIENT_EVIDENCE",),
        ("TURNOVER_MISSING", "TURNOVER_STALE", "TURNOVER_REQUEST_FAILED"),
    ),
    "liquidity_missing_stale_or_quality_insufficient": (
        "candidate Evaluation",
        "INSUFFICIENT_EVIDENCE",
        ("INSUFFICIENT_EVIDENCE",),
        ("LIQUIDITY_MISSING", "LIQUIDITY_STALE", "LIQUIDITY_QUALITY_INSUFFICIENT"),
    ),
    "fee_unknown_without_approved_bound": (
        "Evaluation",
        "INSUFFICIENT_EVIDENCE",
        ("INSUFFICIENT_EVIDENCE",),
        ("FEE_RATE_UNKNOWN",),
    ),
    "sizing_upper_bound_not_finite": (
        "candidate Evaluation",
        "INSUFFICIENT_EVIDENCE",
        ("INSUFFICIENT_EVIDENCE",),
        ("SIZING_ENVELOPE_UNBOUNDED",),
    ),
    "equity_denominator_path_or_coverage_insufficient": (
        "TradingRun and dependent Evaluation",
        "INSUFFICIENT_EVIDENCE",
        ("INSUFFICIENT_EVIDENCE",),
        ("EQUITY_DENOMINATOR_INVALID", "EQUITY_PATH_MISSING", "EQUITY_COVERAGE_INSUFFICIENT"),
    ),
    "actual_leverage_differs_from_manifest": (
        "TradingRun",
        "NEEDS_RETEST",
        ("NEEDS_RETEST",),
        ("LEVERAGE_MISMATCH",),
    ),
    "post_rounding_minimum_or_geometry_violation": (
        "PortfolioCandidate",
        "FAIL",
        (),
        ("POST_ROUNDING_MINIMUM", "POST_ROUNDING_GEOMETRY"),
        ("FAIL",),
    ),
    "enumeration_limit_exceeded_with_approved_bound": (
        "guard evidence",
        "not an automatic failure",
        (),
        ("ENUMERATION_FALLBACK_USED",),
    ),
    "margin_bound_failed_or_unavailable": (
        "candidate / Evaluation",
        "FAIL / INSUFFICIENT_EVIDENCE",
        ("INSUFFICIENT_EVIDENCE",),
        ("MARGIN_BOUND_FAILED", "MARGIN_BOUND_UNAVAILABLE"),
        ("FAIL",),
    ),
    "frozen_finalist_failed_validation": (
        "finalist Evaluation",
        "FAIL",
        (),
        ("VALIDATION_FAILED",),
        ("FAIL",),
    ),
    "semantic_divergence_at_exact_execution_identity": (
        "TradingRun",
        "NONDETERMINISTIC_RESULT",
        ("NONDETERMINISTIC_RESULT",),
        ("SEMANTIC_RESULT_DIVERGENCE",),
    ),
    "executable_payload_changed": (
        "Evaluation / export",
        "NEEDS_RETEST",
        ("NEEDS_RETEST",),
        ("EXECUTABLE_PAYLOAD_CHANGED",),
    ),
    "portfolio_set_member_or_load_changed": (
        "PortfolioSet Evaluation / export",
        "NEEDS_RESCREEN",
        ("NEEDS_RESCREEN",),
        ("PORTFOLIO_SET_CHANGED",),
    ),
    "mandatory_policy_open": (
        "Evaluation / export",
        "RESEARCH_ONLY",
        ("RESEARCH_ONLY",),
        ("OPEN_POLICY",),
    ),
    "no_frozen_finalist_passed_validation": (
        "profile/decision Campaign",
        "INSUFFICIENT_EVIDENCE",
        ("INSUFFICIENT_EVIDENCE",),
        ("NO_VALIDATION_PASS",),
    ),
}


def test_exact_versioned_table_and_enum_valid_values():
    assert set(PORTFOLIO_DISPOSITION_RULES_V1) == set(EXPECTED_RULES)
    for key, expected in EXPECTED_RULES.items():
        scope, result, dispositions, reasons, gates = expected if len(expected) == 5 else (*expected, ())
        rule = PORTFOLIO_DISPOSITION_RULES_V1[key]
        assert (rule.scope, rule.result) == (scope, result)
        assert tuple(item.value for item in rule.dispositions) == dispositions
        assert tuple(item.value for item in rule.gate_results) == gates
        assert tuple(item.value for item in rule.reasons) == reasons
        assert all(item.value in PORTFOLIO_DISPOSITION_V1 for item in rule.dispositions)
        assert all(item.value in PORTFOLIO_GATE_RESULT_V1 for item in rule.gate_results)
        assert rule.evidence_class is None or rule.evidence_class.value in PORTFOLIO_EVIDENCE_CLASS_V1
        assert all(item.value in PORTFOLIO_REASON_V1 for item in rule.reasons)


def test_rule_mapping_is_immutable():
    with pytest.raises(TypeError):
        PORTFOLIO_DISPOSITION_RULES_V1["new"] = PORTFOLIO_DISPOSITION_RULES_V1["mandatory_policy_open"]


def test_multiple_applicable_reasons_are_retained_in_rule_order():
    rules = rules_for(("turnover_missing_stale_or_request_failed", "liquidity_missing_stale_or_quality_insufficient"))
    assert tuple(item.value for item in applicable_reasons(rules)) == (
        "TURNOVER_MISSING", "TURNOVER_STALE", "TURNOVER_REQUEST_FAILED",
        "LIQUIDITY_MISSING", "LIQUIDITY_STALE", "LIQUIDITY_QUALITY_INSUFFICIENT",
    )


def test_candidate_local_fail_does_not_add_campaign_block():
    rule = PORTFOLIO_DISPOSITION_RULES_V1["post_rounding_minimum_or_geometry_violation"]
    assert rule.candidate_local
    assert not rule.blocks_campaign_ready
    assert not campaign_ready_allowed((rule,))


def test_conservative_bound_does_not_auto_fail_or_add_campaign_block():
    rule = PORTFOLIO_DISPOSITION_RULES_V1["enumeration_limit_exceeded_with_approved_bound"]
    assert rule.evidence_class.value == "CONSERVATIVE_BOUND"
    assert tuple(item.value for item in rule.reasons) == ("ENUMERATION_FALLBACK_USED",)
    assert not rule.blocks_campaign_ready
    assert not campaign_ready_allowed((rule,))


def test_ready_requires_no_blocking_rows_and_open_policy_is_research_only():
    open_policy = PORTFOLIO_DISPOSITION_RULES_V1["mandatory_policy_open"]
    assert OPEN_POLICY_CONDITIONS_V1 == ("mandatory_policy_open",)
    assert open_policy.dispositions[0].value == "RESEARCH_ONLY"
    assert tuple(item.value for item in applicable_reasons((open_policy,))) == ("OPEN_POLICY",)
    assert not campaign_ready_allowed(())
    assert not campaign_ready_allowed((open_policy,))


def test_execution_reasons_block_dependent_evaluations_without_adding_campaign_block():
    run_rules = rules_for(("actual_leverage_differs_from_manifest", "semantic_divergence_at_exact_execution_identity"))
    assert not dependent_evaluation_allowed(run_rules)
    assert not campaign_ready_allowed(run_rules)
    assert tuple(item.value for item in applicable_reasons(run_rules)) == (
        "LEVERAGE_MISMATCH", "SEMANTIC_RESULT_DIVERGENCE",
    )


def test_no_validation_pass_is_added_to_specific_invalid_trading_run_reason():
    rules = rules_for(("semantic_divergence_at_exact_execution_identity", "no_frozen_finalist_passed_validation"))
    assert tuple(item.value for item in applicable_reasons(rules)) == (
        "SEMANTIC_RESULT_DIVERGENCE", "NO_VALIDATION_PASS",
    )
