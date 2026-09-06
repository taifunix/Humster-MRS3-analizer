"""Versioned portfolio disposition conditions from optimizer spec section 5.6."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .canonical import PORTFOLIO_REASON_V1, PORTFOLIO_REASON_V2, TypedValue, enum_value


@dataclass(frozen=True, slots=True)
class DispositionRule:
    """One normative condition row; this is data, not a workflow state."""

    condition: str
    scope: str
    result: str
    dispositions: tuple[TypedValue, ...]
    reasons: tuple[TypedValue, ...]
    gate_results: tuple[TypedValue, ...] = ()
    evidence_class: TypedValue | None = None
    blocks_campaign_ready: bool = True
    blocks_dependent_evaluations: bool = False
    candidate_local: bool = False

    @property
    def result_values(self) -> tuple[TypedValue, ...]:
        values = self.dispositions + self.gate_results
        return values if self.evidence_class is None else values + (self.evidence_class,)


def _enum(enum_name: str, *values: str) -> tuple[TypedValue, ...]:
    return tuple(enum_value(enum_name, value) for value in values)


_DISPOSITION = "portfolio_disposition_v1"
_GATE = "portfolio_gate_result_v1"
_EVIDENCE = "portfolio_evidence_class_v1"
_REASON = "portfolio_reason_v1"
_REASON_V2 = "portfolio_reason_v2"

REASON_ENUM_VERSION_V1 = _REASON
REASON_ENUM_VERSION_V2 = _REASON_V2
PORTFOLIO_REASON_CODES_V1 = PORTFOLIO_REASON_V1
PORTFOLIO_REASON_CODES_V2 = PORTFOLIO_REASON_V2


def reason_value(code: str, *, version: str = _REASON) -> TypedValue:
    """Return a stable reason enum without changing the immutable v1 values."""

    if version == _REASON:
        if code not in PORTFOLIO_REASON_CODES_V1:
            raise ValueError(f"unknown portfolio_reason_v1 code: {code}")
        return enum_value(version, code)
    if version == _REASON_V2 and code in PORTFOLIO_REASON_CODES_V2:
        return enum_value(version, code)
    raise ValueError(f"unknown portfolio reason code: {code}")

OPEN_POLICY_CONDITIONS_V1 = ("mandatory_policy_open",)

_UNKNOWN_REASONS = frozenset(
    {"INDIVIDUAL_DD_UNAVAILABLE", "MARGIN_BOUND_UNAVAILABLE", "LIQUIDITY_MISSING", "NO_VALIDATION_PASS"}
)


def status_for_reasons(reasons: Iterable[str]) -> str:
    """Return the deterministic gate status for all applicable reasons."""

    values = tuple(reasons)
    if "OPEN_POLICY" in values:
        return "OPEN_POLICY"
    if any(reason in (_UNKNOWN_REASONS - {"NO_VALIDATION_PASS"}) for reason in values):
        return "UNKNOWN"
    if "NO_VALIDATION_PASS" in values:
        return "UNKNOWN" if len(values) == 1 else "FAIL"
    return "FAIL"


def primary_reason(reasons: Iterable[str]) -> str | None:
    """Choose one stable representative without discarding other reasons."""

    values = tuple(reasons)
    if "OPEN_POLICY" in values:
        return "OPEN_POLICY"
    for reason in values:
        if reason != "NO_VALIDATION_PASS":
            return reason
    return values[0] if values else None


PORTFOLIO_DISPOSITION_RULES_V1: Mapping[str, DispositionRule] = MappingProxyType(
    {
        "owner_unverifiable_or_operator_manual_clear": DispositionRule(
            "owner_unverifiable_or_operator_manual_clear",
            "requested protected action / ownership audit",
            "stop without write / durable audit event",
            (),
            _enum(_REASON, "LOCK_OWNER_UNVERIFIABLE", "LOCK_MANUAL_CLEAR"),
            blocks_campaign_ready=False,
        ),
        "turnover_missing_stale_or_request_failed": DispositionRule(
            "turnover_missing_stale_or_request_failed",
            "Evaluation / PortfolioSet",
            "INSUFFICIENT_EVIDENCE",
            _enum(_DISPOSITION, "INSUFFICIENT_EVIDENCE"),
            _enum(_REASON, "TURNOVER_MISSING", "TURNOVER_STALE", "TURNOVER_REQUEST_FAILED"),
        ),
        "liquidity_missing_stale_or_quality_insufficient": DispositionRule(
            "liquidity_missing_stale_or_quality_insufficient",
            "candidate Evaluation",
            "INSUFFICIENT_EVIDENCE",
            _enum(_DISPOSITION, "INSUFFICIENT_EVIDENCE"),
            _enum(_REASON, "LIQUIDITY_MISSING", "LIQUIDITY_STALE", "LIQUIDITY_QUALITY_INSUFFICIENT"),
            blocks_campaign_ready=False,
            candidate_local=True,
        ),
        "fee_unknown_without_approved_bound": DispositionRule(
            "fee_unknown_without_approved_bound",
            "Evaluation",
            "INSUFFICIENT_EVIDENCE",
            _enum(_DISPOSITION, "INSUFFICIENT_EVIDENCE"),
            _enum(_REASON, "FEE_RATE_UNKNOWN"),
        ),
        "sizing_upper_bound_not_finite": DispositionRule(
            "sizing_upper_bound_not_finite",
            "candidate Evaluation",
            "INSUFFICIENT_EVIDENCE",
            _enum(_DISPOSITION, "INSUFFICIENT_EVIDENCE"),
            _enum(_REASON, "SIZING_ENVELOPE_UNBOUNDED"),
            blocks_campaign_ready=False,
            candidate_local=True,
        ),
        "equity_denominator_path_or_coverage_insufficient": DispositionRule(
            "equity_denominator_path_or_coverage_insufficient",
            "TradingRun and dependent Evaluation",
            "INSUFFICIENT_EVIDENCE",
            _enum(_DISPOSITION, "INSUFFICIENT_EVIDENCE"),
            _enum(_REASON, "EQUITY_DENOMINATOR_INVALID", "EQUITY_PATH_MISSING", "EQUITY_COVERAGE_INSUFFICIENT"),
            blocks_dependent_evaluations=True,
        ),
        "actual_leverage_differs_from_manifest": DispositionRule(
            "actual_leverage_differs_from_manifest",
            "TradingRun",
            "NEEDS_RETEST",
            _enum(_DISPOSITION, "NEEDS_RETEST"),
            _enum(_REASON, "LEVERAGE_MISMATCH"),
            blocks_campaign_ready=False,
            blocks_dependent_evaluations=True,
        ),
        "post_rounding_minimum_or_geometry_violation": DispositionRule(
            "post_rounding_minimum_or_geometry_violation",
            "PortfolioCandidate",
            "FAIL",
            (),
            _enum(_REASON, "POST_ROUNDING_MINIMUM", "POST_ROUNDING_GEOMETRY"),
            gate_results=_enum(_GATE, "FAIL"),
            blocks_campaign_ready=False,
            candidate_local=True,
        ),
        "enumeration_limit_exceeded_with_approved_bound": DispositionRule(
            "enumeration_limit_exceeded_with_approved_bound",
            "guard evidence",
            "not an automatic failure",
            (),
            _enum(_REASON, "ENUMERATION_FALLBACK_USED"),
            evidence_class=enum_value(_EVIDENCE, "CONSERVATIVE_BOUND"),
            blocks_campaign_ready=False,
        ),
        "margin_bound_failed_or_unavailable": DispositionRule(
            "margin_bound_failed_or_unavailable",
            "candidate / Evaluation",
            "FAIL / INSUFFICIENT_EVIDENCE",
            _enum(_DISPOSITION, "INSUFFICIENT_EVIDENCE"),
            _enum(_REASON, "MARGIN_BOUND_FAILED", "MARGIN_BOUND_UNAVAILABLE"),
            gate_results=_enum(_GATE, "FAIL"),
            blocks_campaign_ready=False,
            candidate_local=True,
        ),
        "frozen_finalist_failed_validation": DispositionRule(
            "frozen_finalist_failed_validation",
            "finalist Evaluation",
            "FAIL",
            (),
            _enum(_REASON, "VALIDATION_FAILED"),
            gate_results=_enum(_GATE, "FAIL"),
            blocks_campaign_ready=False,
            candidate_local=True,
        ),
        "semantic_divergence_at_exact_execution_identity": DispositionRule(
            "semantic_divergence_at_exact_execution_identity",
            "TradingRun",
            "NONDETERMINISTIC_RESULT",
            _enum(_DISPOSITION, "NONDETERMINISTIC_RESULT"),
            _enum(_REASON, "SEMANTIC_RESULT_DIVERGENCE"),
            blocks_campaign_ready=False,
            blocks_dependent_evaluations=True,
        ),
        "executable_payload_changed": DispositionRule(
            "executable_payload_changed",
            "Evaluation / export",
            "NEEDS_RETEST",
            _enum(_DISPOSITION, "NEEDS_RETEST"),
            _enum(_REASON, "EXECUTABLE_PAYLOAD_CHANGED"),
        ),
        "portfolio_set_member_or_load_changed": DispositionRule(
            "portfolio_set_member_or_load_changed",
            "PortfolioSet Evaluation / export",
            "NEEDS_RESCREEN",
            _enum(_DISPOSITION, "NEEDS_RESCREEN"),
            _enum(_REASON, "PORTFOLIO_SET_CHANGED"),
        ),
        "mandatory_policy_open": DispositionRule(
            "mandatory_policy_open",
            "Evaluation / export",
            "RESEARCH_ONLY",
            _enum(_DISPOSITION, "RESEARCH_ONLY"),
            _enum(_REASON, "OPEN_POLICY"),
        ),
        "no_frozen_finalist_passed_validation": DispositionRule(
            "no_frozen_finalist_passed_validation",
            "profile/decision Campaign",
            "INSUFFICIENT_EVIDENCE",
            _enum(_DISPOSITION, "INSUFFICIENT_EVIDENCE"),
            _enum(_REASON, "NO_VALIDATION_PASS"),
        ),
    }
)

# Short alias for callers that do not need the expanded contract name.
DISPOSITION_RULES_V1 = PORTFOLIO_DISPOSITION_RULES_V1


def rules_for(conditions: Iterable[str]) -> tuple[DispositionRule, ...]:
    """Resolve condition IDs while retaining input order and duplicate rows."""
    return tuple(PORTFOLIO_DISPOSITION_RULES_V1[condition] for condition in conditions)


def applicable_reasons(rows: Iterable[DispositionRule]) -> tuple[TypedValue, ...]:
    """Return every applicable reason; no row or reason is collapsed."""
    return tuple(reason for row in rows for reason in row.reasons)


def campaign_ready_allowed(rows: Iterable[DispositionRule]) -> bool:
    """READY is blocked by supplied rows or the versioned open-policy rows."""
    applicable = tuple(rows) + rules_for(OPEN_POLICY_CONDITIONS_V1)
    return not any(row.blocks_campaign_ready for row in applicable)


def dependent_evaluation_allowed(rows: Iterable[DispositionRule]) -> bool:
    """Execution evidence may block dependent Evaluations without changing Campaign."""
    return not any(row.blocks_dependent_evaluations for row in rows)
