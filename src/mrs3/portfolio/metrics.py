"""Deterministic metrics over normalized, fixture-only portfolio reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

from .margin import NEEDS_RETEST, PASS, UNKNOWN
from .reports import METRICS_VERSION, NormalizedReport, _summary_value


SAMPLING_RESOLUTION_MODEL = "minimum_positive_observed_interval_v1"


def _d(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("metric value must be Decimal/integer/string")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("metric value is not decimal") from error
    if not number.is_finite():
        raise ValueError("metric value must be finite")
    return number


def _seconds(left: str, right: str) -> Decimal:
    first = datetime.fromisoformat(left.replace("Z", "+00:00"))
    second = datetime.fromisoformat(right.replace("Z", "+00:00"))
    delta = second - first
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal("1000000")


@dataclass(frozen=True, slots=True)
class LeverageCheck:
    status: str
    reason: str | None
    planned: Mapping[str, Decimal] = field(default_factory=dict)
    applied: Mapping[str, Decimal] = field(default_factory=dict)
    missing_symbols: tuple[str, ...] = ()
    mismatched_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MarginGuard:
    status: str
    reason: str | None
    maximum_notional: Decimal | None
    minimum_margin_balance: Decimal | None
    witness: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    status: str
    metrics_version: str
    initial_equity: Decimal | None
    final_equity: Decimal | None
    primary_result: Decimal | None
    realized_pnl: Decimal | None
    actual_drawdown: Decimal | None
    actual_drawdown_pct: Decimal | None
    sampling_resolution_seconds: Decimal | None
    coverage: str
    boundary_start: str | None
    boundary_end: str | None
    gaps: tuple[Mapping[str, Any], ...]
    censoring: tuple[str, ...]
    actual_concurrency: int | None
    margin_guard: MarginGuard
    leverage: LeverageCheck
    diagnostics: tuple[str, ...]
    blocking_diagnostics: tuple[str, ...]
    availability: Mapping[str, str]

    @property
    def sampling_resolution_model(self) -> str:
        return SAMPLING_RESOLUTION_MODEL

    @property
    def complete(self) -> bool:
        return self.status == "COMPLETE" and not self.blocking_diagnostics

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "metrics_version": self.metrics_version,
            "initial_equity": self.initial_equity,
            "final_equity": self.final_equity,
            "primary_result": self.primary_result,
            "realized_pnl": self.realized_pnl,
            "actual_drawdown": self.actual_drawdown,
            "actual_drawdown_pct": self.actual_drawdown_pct,
            "sampling_resolution_seconds": self.sampling_resolution_seconds,
            "sampling_resolution_model": self.sampling_resolution_model,
            "coverage": self.coverage,
            "boundary_start": self.boundary_start,
            "boundary_end": self.boundary_end,
            "gaps": list(self.gaps),
            "censoring": list(self.censoring),
            "actual_concurrency": self.actual_concurrency,
            "margin_guard": self.margin_guard,
            "leverage": self.leverage,
            "diagnostics": list(self.diagnostics),
            "blocking_diagnostics": list(self.blocking_diagnostics),
            "availability": dict(self.availability),
        }


def check_applied_leverage(
    planned: Mapping[str, Any] | None,
    applied: Mapping[str, Any] | None,
) -> LeverageCheck:
    """Compare report readback to the planned map without inventing missing data."""
    planned_values = {str(key): _d(value) for key, value in (planned or {}).items()}
    applied_values: dict[str, Decimal] = {}
    for key, value in (applied or {}).items():
        if value is not None:
            try:
                applied_values[str(key)] = _d(value)
            except ValueError:
                continue
    if not planned_values:
        return LeverageCheck(UNKNOWN, "LEVERAGE_UNVERIFIED", planned_values, applied_values)
    missing = tuple(sorted(name for name in planned_values if name not in applied_values))
    mismatch = tuple(sorted(name for name, value in planned_values.items() if name in applied_values and applied_values[name] != value))
    if mismatch:
        return LeverageCheck(NEEDS_RETEST, "LEVERAGE_MISMATCH", planned_values, applied_values, missing, mismatch)
    if missing:
        return LeverageCheck(UNKNOWN, "LEVERAGE_UNVERIFIED", planned_values, applied_values, missing, ())
    return LeverageCheck(PASS, None, planned_values, applied_values)


def _concurrency(report: NormalizedReport) -> int | None:
    if any(action.size is None and action.post_size is None and action.qty_delta is None for action in report.actions if action.symbol):
        return None
    from .reports import _CLOSING_ACTIONS, _signed_size  # shared cycle transition semantics
    positions: dict[str, Decimal] = {}
    maximum = 0
    for action in report.actions:
        if not action.symbol:
            continue
        old = positions.get(action.symbol, Decimal("0"))
        kind = action.action.casefold()
        if (
            old == 0
            and action.pre_size is None
            and action.post_size is None
            and action.qty_delta is None
            and kind in _CLOSING_ACTIONS
        ):
            # A leading close proves no post-close position; it does not prove
            # a phantom short position existed before the report began.
            continue
        base = action.pre_size
        if base is not None and action.pre_side:
            base = abs(base) if action.pre_side != "SHORT" else -abs(base)
        if base is None:
            base = old
        new = action.post_size
        if new is None:
            new = base + _signed_size(action, base)
        if action.post_side and new:
            new = abs(new) if action.post_side != "SHORT" else -abs(new)
        positions[action.symbol] = new
        maximum = max(maximum, sum(value != 0 for value in positions.values()))
    return maximum


def calculate_margin_guard(report: NormalizedReport) -> MarginGuard:
    """Expose a separate calculated guard; absent margin facts stay UNKNOWN."""
    notional = tuple(point.value for point in report.series.get("notional", ()) if point.value is not None)
    margin = tuple(point.value for point in report.series.get("margin_balance", ()) if point.value is not None)
    if not notional or not margin:
        return MarginGuard(UNKNOWN, "MARGIN_BOUND_UNAVAILABLE", max(notional) if notional else None, min(margin) if margin else None)
    maximum = max(notional)
    minimum = min(margin)
    status = PASS if minimum >= maximum else "FAIL"
    return MarginGuard(status, None if status == PASS else "MARGIN_BOUND_FAILED", maximum, minimum, {"maximum_notional": maximum, "minimum_margin_balance": minimum})


def _equity(report: NormalizedReport) -> tuple[tuple[str, Decimal], ...]:
    return tuple((point.timestamp_utc, point.value) for point in report.series.get("equity", ()) if point.value is not None and point.availability == "AVAILABLE")


def calculate_metrics(
    report: NormalizedReport,
    *,
    planned_leverage: Mapping[str, Any] | None = None,
    actual_leverage: Mapping[str, Any] | None = None,
    applied_leverage: Mapping[str, Any] | None = None,
) -> PortfolioMetrics:
    if not isinstance(report, NormalizedReport):
        raise TypeError("calculate_metrics requires NormalizedReport")
    equity = _equity(report)
    diagnostics: list[str] = list(report.diagnostics)
    blocking: list[str] = []
    if len(equity) < 2:
        blocking.append("EQUITY_PATH_MISSING" if not equity else "EQUITY_COVERAGE_INSUFFICIENT")
    initial = equity[0][1] if equity else None
    final = equity[-1][1] if equity else None
    # Summary boundaries are corroboration only; they never replace the path.
    declared_initial = _summary_value(report.portfolio, "initial_equity")
    declared_final = _summary_value(report.portfolio, "final_equity")
    if (declared_initial is None) != (declared_final is None):
        blocking.append("FINANCIAL_RECONCILIATION_FAILED")
    for key, actual in (("initial_equity", initial), ("final_equity", final)):
        declared = _summary_value(report.portfolio, key)
        if declared is not None and actual is not None:
            try:
                if _d(declared) != actual:
                    blocking.append("FINANCIAL_RECONCILIATION_FAILED")
            except ValueError:
                blocking.append("FINANCIAL_RECONCILIATION_FAILED")
    primary = final - initial if initial is not None and final is not None else None
    drawdown: Decimal | None = None
    drawdown_pct: Decimal | None = None
    if equity:
        peak = equity[0][1]
        for _, value in equity:
            peak = max(peak, value)
            current_drawdown = peak - value
            drawdown = max(drawdown or Decimal("0"), current_drawdown)
            if peak <= 0:
                blocking.append("EQUITY_DENOMINATOR_INVALID")
            else:
                with localcontext() as context:
                    context.prec = 50
                    current_pct = current_drawdown / peak * Decimal("100")
                drawdown_pct = max(drawdown_pct or Decimal("0"), current_pct)
    intervals = tuple(_seconds(equity[index][0], equity[index + 1][0]) for index in range(len(equity) - 1))
    # Resolution is descriptive: irregular spacing is not evidence of a
    # missing tick. Coverage still requires exact declared boundaries and no
    # explicitly unavailable equity points.
    resolution = min((value for value in intervals if value > 0), default=None)
    gaps: list[Mapping[str, Any]] = []
    if resolution is not None:
        for index, value in enumerate(intervals):
            if value > resolution:
                gaps.append({"start": equity[index][0], "end": equity[index + 1][0], "seconds": value})
    unavailable_equity = any(
        point.availability != "AVAILABLE" or point.value is None
        for point in report.series.get("equity", ())
    )
    coverage = "UNKNOWN"
    if report.report_start and report.report_end and equity:
        coverage = "COMPLETE" if equity[0][0] == report.report_start and equity[-1][0] == report.report_end and not unavailable_equity else "PARTIAL"
        if coverage != "COMPLETE":
            blocking.append("EQUITY_COVERAGE_INSUFFICIENT")
    elif equity:
        coverage = "OBSERVED_BOUNDARIES_UNKNOWN"
        blocking.append("EQUITY_COVERAGE_INSUFFICIENT")
    else:
        blocking.append("EQUITY_PATH_MISSING")
    cycle_realized = tuple(cycle.realized_pnl for cycle in report.cycles)
    known_realized = tuple(value for value in cycle_realized if value is not None)
    realized = sum(known_realized, Decimal("0")) if known_realized else None
    realized_partial = bool(known_realized) and len(known_realized) != len(cycle_realized)
    if realized_partial:
        diagnostics.append("REALIZED_PNL_PARTIAL")
    closed_cycles = tuple(cycle for cycle in report.cycles if not cycle.censored)
    declared_realized = _summary_value(report.portfolio, "realized_pnl", "realised_pnl")
    if declared_realized is not None:
        try:
            declared_value = _d(declared_realized)
            if realized is None or realized_partial:
                diagnostics.extend(
                    code
                    for cycle in report.cycles
                    for code in cycle.diagnostics
                    if code == "REALIZED_PNL_UNAVAILABLE"
                )
                blocking.append("FINANCIAL_RECONCILIATION_UNVERIFIED")
            elif declared_value != realized:
                blocking.append("FINANCIAL_RECONCILIATION_FAILED")
        except ValueError:
            blocking.append("FINANCIAL_RECONCILIATION_FAILED")
    for field_name, aliases, attribute in (
        ("fees", ("fees", "fee"), "fee"),
        ("funding", ("funding", "funding_fee"), "funding"),
    ):
        declared = _summary_value(report.portfolio, *aliases)
        values = [getattr(action, attribute) for action in report.actions]
        if declared is not None:
            try:
                declared_value = _d(declared)
            except ValueError:
                blocking.append("FINANCIAL_RECONCILIATION_FAILED")
                continue
            if not values or any(value is None for value in values):
                if attribute == "fee":
                    diagnostics.extend(
                        code
                        for cycle in closed_cycles
                        for code in cycle.diagnostics
                        if code == "FEES_UNAVAILABLE"
                    )
                blocking.append("FINANCIAL_RECONCILIATION_UNVERIFIED")
            else:
                try:
                    actual_value = sum((_d(value) for value in values), Decimal("0"))
                except ValueError:
                    blocking.append("FINANCIAL_RECONCILIATION_FAILED")
                else:
                    if actual_value != declared_value:
                        blocking.append("FINANCIAL_RECONCILIATION_FAILED")
    declared_net = _summary_value(report.portfolio, "net_pnl", "total_pnl")
    if declared_net is not None:
        try:
            declared_value = _d(declared_net)
        except ValueError:
            blocking.append("FINANCIAL_RECONCILIATION_FAILED")
        else:
            if primary is None:
                blocking.append("FINANCIAL_RECONCILIATION_UNVERIFIED")
            elif declared_value != primary:
                blocking.append("FINANCIAL_RECONCILIATION_FAILED")
    censoring = tuple(sorted({"OPEN_AT_END" for cycle in report.cycles if cycle.censored}))
    actual_readback = report.actual_leverage if actual_leverage is None and applied_leverage is None else (actual_leverage if actual_leverage is not None else applied_leverage)
    leverage = check_applied_leverage(planned_leverage, actual_readback)
    if leverage.reason == "LEVERAGE_MISMATCH":
        diagnostics.append("LEVERAGE_MISMATCH")
    margin = calculate_margin_guard(report)
    if margin.status == "FAIL":
        # A calculated margin bound is a safety gate.  Keep the typed witness
        # and make the result ineligible for complete publication.
        blocking.append("MARGIN_BOUND_FAILED")
    diagnostics.extend(censoring)
    diagnostics.extend(gaps and ["EQUITY_GAPS"] or [])
    diagnostics.extend(blocking)
    if planned_leverage and leverage.status in {NEEDS_RETEST, UNKNOWN}:
        status = "NEEDS_RETEST"
    else:
        status = "COMPLETE" if not blocking else "INCOMPLETE"
    return PortfolioMetrics(
        status, report.metrics_version, initial, final, primary, realized,
        drawdown, drawdown_pct, resolution, coverage, report.report_start, report.report_end,
        tuple(gaps), censoring, _concurrency(report), margin, leverage,
        tuple(dict.fromkeys(diagnostics)), tuple(dict.fromkeys(blocking)),
        {name: ("AVAILABLE" if any(point.value is not None for point in points) else "UNAVAILABLE") for name, points in report.series.items()},
    )


compute_metrics = calculate_metrics
portfolio_metrics = calculate_metrics
calculate_portfolio_metrics = calculate_metrics
compute_portfolio_metrics = calculate_metrics


__all__ = [
    "METRICS_VERSION", "SAMPLING_RESOLUTION_MODEL", "LeverageCheck", "MarginGuard", "PortfolioMetrics",
    "check_applied_leverage", "calculate_margin_guard", "calculate_metrics", "compute_metrics", "portfolio_metrics", "calculate_portfolio_metrics", "compute_portfolio_metrics",
]
