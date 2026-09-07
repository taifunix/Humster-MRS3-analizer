"""Fixture-only M7 orchestration around the deterministic M4 search.

The module intentionally has no tester, network, or database discovery.  A
caller supplies immutable research facts and small fake callbacks for test and
import.  The returned attempt ledger is the replay surface; a PortfolioStore
is optional and only receives the already-built immutable payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import inspect
import json
import math
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import POLICY_VERSION, PROFILE_NAMES, RESEARCH_RISK_POLICY
from .metrics import PortfolioMetrics, calculate_metrics
from .reports import NormalizedReport
from .search import Variant, canonical_candidate_identity, search_portfolios


PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
RESEARCH_ONLY = "RESEARCH_ONLY"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
_MISSING = object()
_UNSET = object()


class PortfolioIntegrationError(ValueError):
    """Frozen M7 input or callback contract cannot be trusted."""


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise PortfolioIntegrationError(f"{name} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PortfolioIntegrationError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise PortfolioIntegrationError(f"{name} must be a finite decimal")
    return result


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"__decimal__": format(value, "f")}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise PortfolioIntegrationError("canonical mappings require string keys")
        return {key: _plain(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise PortfolioIntegrationError("unordered values are not canonical")
    if is_dataclass(value):
        return _plain({item.name: getattr(value, item.name) for item in fields(value)})
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PortfolioIntegrationError("canonical value must be finite")
        return {"__float__": repr(value)}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise PortfolioIntegrationError(f"unsupported canonical value: {type(value).__name__}")


def _encoded(value: Any) -> str:
    try:
        return json.dumps(_plain(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PortfolioIntegrationError("canonical value is not serializable") from error


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value).encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _first(value: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        item = _get(value, key, None)
        if item is not None:
            return item
    return default


def _timestamp(value: Any) -> tuple[str, Any]:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), parsed.timestamp()
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        number = _decimal(value, "window timestamp")
        return format(number, "f"), number
    if isinstance(value, str):
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as error:
            try:
                numeric = _decimal(raw, "window timestamp")
            except PortfolioIntegrationError:
                raise PortfolioIntegrationError("window timestamp is malformed") from error
            return format(numeric, "f"), numeric
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z"), parsed.timestamp()
    raise PortfolioIntegrationError("window timestamp is malformed")


def _clock_ms(value: Any, numeric: Any) -> int:
    """Convert the frozen evaluation clock to the evidence clock units.

    Integer/decimal clocks are already treated as the caller's millisecond
    unit (which also keeps fixture clocks compact).  ISO clocks are converted
    from epoch seconds to milliseconds so they can be compared with report
    freshness witnesses.
    """
    if isinstance(value, datetime):
        return int(Decimal(str(numeric)) * Decimal("1000"))
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return int(numeric)
        return int(Decimal(str(numeric)) * Decimal("1000"))
    return int(numeric)


def _window(value: Any, name: str) -> tuple[Mapping[str, Any], tuple[str, Any], tuple[str, Any]]:
    if isinstance(value, Mapping):
        start = _first(value, "start", "from", "start_at", "start_ms")
        end = _first(value, "end", "to", "end_at", "end_ms")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        raise PortfolioIntegrationError(f"{name} must contain start and end")
    if start is None or end is None:
        raise PortfolioIntegrationError(f"{name} must contain start and end")
    start_c = _timestamp(start)
    end_c = _timestamp(end)
    if start_c[1] >= end_c[1]:
        raise PortfolioIntegrationError(f"{name} must have start before end")
    normalized = {"start": start_c[0], "end": end_c[0]}
    return MappingProxyType(normalized), start_c, end_c


def _required_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioIntegrationError(f"{name} is required")
    return value.strip()


def _policy_identifier(value: Any) -> str:
    if isinstance(value, Mapping):
        value = _first(value, "policy_id", "id", "version")
    return str(value) if value is not None else ""


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (int, str, Decimal)):
        raise PortfolioIntegrationError(f"{name} must be a non-negative integer")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        parsed = int(decimal_value)
    except (InvalidOperation, OverflowError, TypeError, ValueError):
        raise PortfolioIntegrationError(f"{name} must be a non-negative integer")
    if decimal_value != Decimal(parsed) or parsed < 0:
        raise PortfolioIntegrationError(f"{name} must be a non-negative integer")
    return parsed


@dataclass(frozen=True, slots=True)
class FrozenCampaign:
    """All facts that can influence proposal or validation, frozen up front."""

    development_window: Mapping[str, Any]
    validation_window: Mapping[str, Any]
    upstream_selection: Any
    upstream_used_periods: tuple[Any, ...]
    hypotheses: tuple[Any, ...]
    warmup: Any
    state_boundary: Any
    initial_wallet: Any
    initial_equity: Any
    initial_positions: Any
    attribution: Any
    evidence_minimum: Any
    profile_id: str
    config_id: str
    policy_ids: Mapping[str, Any]
    sizing_grid: tuple[Decimal, ...]
    sizing_grid_order: tuple[int, ...]
    ranking_policy: Mapping[str, Any] | None
    total_test_budget: int
    identity: str
    canonical_digest: str
    repeated_history_verification: bool = False
    evaluation_clock: Any = None
    freshness_contract: Mapping[str, Any] = field(default_factory=dict)
    search_facts: Mapping[str, Any] = field(default_factory=dict)
    refinement_rounds: int = 0
    parent_campaign_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "development_window", _freeze(self.development_window))
        object.__setattr__(self, "validation_window", _freeze(self.validation_window))
        for name in ("upstream_selection", "warmup", "state_boundary", "initial_wallet", "initial_equity", "initial_positions", "attribution", "evidence_minimum"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))
        object.__setattr__(self, "upstream_used_periods", tuple(_freeze(item) for item in self.upstream_used_periods))
        object.__setattr__(self, "hypotheses", tuple(_freeze(item) for item in self.hypotheses))
        object.__setattr__(self, "policy_ids", _freeze(self.policy_ids))
        object.__setattr__(self, "freshness_contract", _freeze(self.freshness_contract))
        object.__setattr__(self, "search_facts", _freeze(self.search_facts))
        object.__setattr__(self, "sizing_grid", tuple(self.sizing_grid))
        object.__setattr__(self, "sizing_grid_order", tuple(self.sizing_grid_order))
        if self.ranking_policy is not None:
            object.__setattr__(self, "ranking_policy", _freeze(self.ranking_policy))

    @property
    def digest(self) -> str:
        return self.canonical_digest

    @property
    def evaluation_clock_ms(self) -> int | None:
        try:
            _, numeric = _timestamp(self.evaluation_clock)
            return _clock_ms(self.evaluation_clock, numeric)
        except PortfolioIntegrationError:
            return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "development_window": self.development_window,
            "validation_window": self.validation_window,
            "upstream_selection": self.upstream_selection,
            "upstream_used_periods": self.upstream_used_periods,
            "hypotheses": self.hypotheses,
            "warmup": self.warmup,
            "state_boundary": self.state_boundary,
            "initial_wallet": self.initial_wallet,
            "initial_equity": self.initial_equity,
            "initial_positions": self.initial_positions,
            "attribution": self.attribution,
            "evidence_minimum": self.evidence_minimum,
            "profile_id": self.profile_id,
            "config_id": self.config_id,
            "policy_ids": self.policy_ids,
            "sizing_grid": self.sizing_grid,
            "sizing_grid_order": self.sizing_grid_order,
            "ranking_policy": self.ranking_policy,
            "total_test_budget": self.total_test_budget,
            "repeated_history_verification": self.repeated_history_verification,
            "validation_label": "REPEATED_HISTORY_VERIFICATION" if self.repeated_history_verification else "INDEPENDENT_HOLDOUT",
            "identity": self.identity,
            "canonical_digest": self.canonical_digest,
            "evaluation_clock": self.evaluation_clock,
            "freshness_contract": self.freshness_contract,
            "search_facts": self.search_facts,
            "refinement_rounds": self.refinement_rounds,
            "parent_campaign_digest": self.parent_campaign_digest,
        }


def freeze_campaign(
    development_window: Any,
    validation_window: Any,
    *,
    upstream_selection: Any = None,
    upstream_selection_provenance: Any = None,
    upstream_used_periods: Iterable[Any] = (),
    hypotheses: Iterable[Any] = (),
    warmup: Any = None,
    state_boundary: Any = None,
    initial_wallet: Any = None,
    initial_equity: Any = None,
    initial_positions: Any = None,
    attribution: Any = None,
    initial_state: Mapping[str, Any] | None = None,
    evidence_minimum: Any = None,
    profile_id: str | None = None,
    profile: str | None = None,
    config_id: str | None = None,
    config_version: str | None = None,
    policy_ids: Mapping[str, Any] | None = None,
    policy_id: str | None = None,
    sizing_grid: Sequence[Any] = (),
    sizing_order: Sequence[int] | None = None,
    ranking_policy: Mapping[str, Any] | None = None,
    total_test_budget: int | None = None,
    budget: int | None = None,
    repeated_history_verification: bool = False,
    evaluation_clock: Any = None,
    evaluation_clock_ms: Any = None,
    freshness_contract: Mapping[str, Any] | None = None,
    capability: Mapping[str, Any] | None = None,
    current_equity: Any = None,
    dd_cap_pct: Any = None,
    composition: Any = None,
    priorities: Sequence[Any] | None = None,
    priority: Any = _UNSET,
    opposite_policy: Any = _UNSET,
    limiter: Any = _UNSET,
    grid_version: str | object = _UNSET,
    gate_policy_ids: Mapping[str, Any] | None = None,
    search_facts: Mapping[str, Any] | None = None,
    refinement_rounds: int = 0,
    resume_from: FrozenCampaign | str | None = None,
    continuation_of: FrozenCampaign | str | None = None,
) -> FrozenCampaign:
    """Validate and freeze all pre-search facts into one canonical identity."""
    development, dev_start, dev_end = _window(development_window, "development_window")
    validation, val_start, val_end = _window(validation_window, "validation_window")
    if max(dev_start[1], val_start[1]) < min(dev_end[1], val_end[1]):
        raise PortfolioIntegrationError("development and validation windows overlap")
    if upstream_selection is None:
        upstream_selection = upstream_selection_provenance
    if upstream_selection is None:
        raise PortfolioIntegrationError("upstream selection provenance is required")
    if initial_state is not None:
        initial_wallet = _first(initial_state, "wallet", "initial_wallet", default=initial_wallet)
        initial_equity = _first(initial_state, "equity", "initial_equity", default=initial_equity)
        initial_positions = _first(initial_state, "positions", "initial_positions", default=initial_positions)
        attribution = _first(initial_state, "attribution", default=attribution)
    if any(item is None for item in (warmup, state_boundary, initial_wallet, initial_equity, initial_positions, attribution, evidence_minimum)):
        raise PortfolioIntegrationError("warmup, state, attribution, and evidence minimum are required")
    if evaluation_clock is not None and evaluation_clock_ms is not None:
        _, first_clock = _timestamp(evaluation_clock)
        _, second_clock = _timestamp(evaluation_clock_ms)
        if _clock_ms(evaluation_clock, first_clock) != _clock_ms(evaluation_clock_ms, second_clock):
            raise PortfolioIntegrationError("evaluation_clock and evaluation_clock_ms differ")
    if evaluation_clock is None:
        evaluation_clock = evaluation_clock_ms
    if evaluation_clock is None:
        raise PortfolioIntegrationError("evaluation_clock is required")
    clock_canonical, clock_numeric = _timestamp(evaluation_clock)
    frozen_clock = clock_canonical
    for value, name in ((freshness_contract, "freshness_contract"), (policy_ids, "policy_ids"), (ranking_policy, "ranking_policy"), (gate_policy_ids, "gate_policy_ids"), (search_facts, "search_facts")):
        if value is not None and not isinstance(value, Mapping):
            raise PortfolioIntegrationError(f"{name} must be a mapping")
    freshness = dict(freshness_contract or {})
    freshness.setdefault("require_timestamp", True)
    freshness.setdefault("require_expiry", True)
    if freshness.get("require_timestamp") is not True or freshness.get("require_expiry") is not True:
        raise PortfolioIntegrationError("freshness contract must require timestamp and expiry")
    selected_profile = profile_id or profile
    selected_config = config_id or config_version
    selected_policy = dict(policy_ids or {})
    if policy_id is not None:
        selected_policy.setdefault("policy", policy_id)
    selected_profile = _required_id(selected_profile, "profile_id")
    selected_config = _required_id(selected_config, "config_id")
    if not selected_policy:
        raise PortfolioIntegrationError("policy_ids are required")
    if isinstance(sizing_grid, (str, bytes)):
        raise PortfolioIntegrationError("sizing_grid must be a finite sequence")
    try:
        points = tuple(_decimal(value, "sizing_grid") for value in sizing_grid)
    except TypeError as error:
        raise PortfolioIntegrationError("sizing_grid must be a finite sequence") from error
    if not points or any(point <= 0 for point in points) or len(set(points)) != len(points):
        raise PortfolioIntegrationError("sizing_grid must contain unique positive finite values")
    if sizing_order is not None and isinstance(sizing_order, (str, bytes)):
        raise PortfolioIntegrationError("sizing_grid_order must be a finite sequence")
    try:
        order = tuple(range(len(points))) if sizing_order is None else tuple(sizing_order)
    except TypeError as error:
        raise PortfolioIntegrationError("sizing_grid_order must be a finite sequence") from error
    if any(isinstance(index, bool) or not isinstance(index, int) for index in order) or len(order) != len(points) or tuple(sorted(order)) != tuple(range(len(points))):
        raise PortfolioIntegrationError("sizing_grid_order must be a permutation of the finite grid")
    if total_test_budget is None:
        total_test_budget = budget
    if isinstance(total_test_budget, bool) or not isinstance(total_test_budget, int) or total_test_budget <= 0:
        raise PortfolioIntegrationError("total_test_budget must be a positive integer")
    if isinstance(refinement_rounds, bool) or not isinstance(refinement_rounds, int) or refinement_rounds < 0:
        raise PortfolioIntegrationError("refinement_rounds must be a non-negative integer")
    if not isinstance(hypotheses, Iterable) or isinstance(hypotheses, (str, bytes)):
        raise PortfolioIntegrationError("hypotheses must be an iterable")
    hypothesis_values = tuple(hypotheses)
    if not isinstance(upstream_used_periods, Iterable) or isinstance(upstream_used_periods, (str, bytes)):
        raise PortfolioIntegrationError("upstream_used_periods must be an iterable")
    used_periods = tuple(upstream_used_periods)
    if not used_periods:
        raise PortfolioIntegrationError("upstream_used_periods must be nonempty")
    if not hypothesis_values:
        raise PortfolioIntegrationError("hypotheses must be nonempty")
    if priorities is not None:
        if isinstance(priorities, (str, bytes)):
            raise PortfolioIntegrationError("priorities must be an iterable")
        try:
            priorities = tuple(_nonnegative_int(value, "priorities") for value in priorities)
        except TypeError as error:
            raise PortfolioIntegrationError("priorities must be an iterable") from error
        if not priorities:
            raise PortfolioIntegrationError("priorities must be nonempty")
    repeated_history = bool(repeated_history_verification)
    periods_to_check = list(used_periods)
    # Selection provenance may itself name the history window used to choose
    # the upstream surface.  Include that witness when it is recognizable,
    # while avoiding a duplicate of the explicit used-period list.
    if isinstance(upstream_selection, Mapping):
        for key in ("period", "window"):
            selection_period = upstream_selection.get(key)
            if selection_period is None:
                continue
            try:
                _, selection_start, selection_end = _window(selection_period, "upstream_selection_period")
            except PortfolioIntegrationError:
                continue
            selection_identity = (selection_start[1], selection_end[1])
            explicit_identities = set()
            for explicit_period in used_periods:
                try:
                    _, explicit_start, explicit_end = _window(explicit_period, "upstream_used_period")
                except PortfolioIntegrationError:
                    continue
                explicit_identities.add((explicit_start[1], explicit_end[1]))
            if selection_identity not in explicit_identities:
                periods_to_check.append(selection_period)
            break
    for period in periods_to_check:
        _, used_start, used_end = _window(period, "upstream_used_period")
        if max(used_start[1], val_start[1]) < min(used_end[1], val_end[1]):
            repeated_history = True
    if ranking_policy is not None:
        if not isinstance(ranking_policy.get("version"), str) or not ranking_policy.get("version"):
            raise PortfolioIntegrationError("ranking policy version is required")
        metrics = ranking_policy.get("metrics")
        if not isinstance(metrics, (list, tuple)) or not metrics:
            raise PortfolioIntegrationError("ranking policy metrics are required")
        for metric in metrics:
            if not isinstance(metric, Mapping) or not metric.get("field") or str(metric.get("direction", "")).upper() not in {"ASC", "DESC"}:
                raise PortfolioIntegrationError("ranking policy metric is malformed")
        tie_break = ranking_policy.get("tie_breaker", ranking_policy.get("candidate_id_tie_break"))
        if tie_break not in {"candidate_id", "canonical_candidate_identity"}:
            raise PortfolioIntegrationError("ranking policy candidate-id tie-break is required")
    frozen_facts = dict(search_facts or {})
    if priority is _UNSET:
        priority = frozen_facts.get("priority", 1)
    if limiter is _UNSET:
        limiter = frozen_facts.get("limiter", 0)
    if opposite_policy is _UNSET:
        opposite_policy = frozen_facts.get("opposite_policy", "KEEP_OPPOSITE")
    if grid_version is _UNSET:
        grid_version = frozen_facts.get("grid_version", "m4-grid-v1")
    grid_version = _required_id(grid_version, "grid_version")
    priority = _nonnegative_int(priority, "priority")
    limiter = _nonnegative_int(limiter, "limiter")
    frozen_facts.update({
        "capability": capability if capability is not None else frozen_facts.get("capability"),
        "current_equity": current_equity if current_equity is not None else frozen_facts.get("current_equity"),
        "dd_cap_pct": dd_cap_pct if dd_cap_pct is not None else frozen_facts.get("dd_cap_pct"),
        "composition": composition if composition is not None else frozen_facts.get("composition"),
        "priorities": priorities if priorities is not None else frozen_facts.get("priorities"),
        "priority": priority,
        "opposite_policy": opposite_policy,
        "limiter": limiter,
        "grid_version": grid_version,
        "gate_policy_ids": gate_policy_ids if gate_policy_ids is not None else frozen_facts.get("gate_policy_ids"),
        "evaluation_clock": clock_canonical,
    })
    if resume_from is not None and continuation_of is not None:
        first_parent = resume_from.canonical_digest if isinstance(resume_from, FrozenCampaign) else str(resume_from).strip()
        second_parent = continuation_of.canonical_digest if isinstance(continuation_of, FrozenCampaign) else str(continuation_of).strip()
        if first_parent != second_parent:
            raise PortfolioIntegrationError("resume parent campaign identities differ")
    parent = resume_from if resume_from is not None else continuation_of
    if isinstance(parent, FrozenCampaign):
        parent_digest = parent.canonical_digest
    elif parent is None:
        parent_digest = None
    elif isinstance(parent, str) and parent.strip():
        parent_digest = parent.strip()
    else:
        raise PortfolioIntegrationError("resume parent campaign digest is malformed")
    payload = {
        "development_window": development, "validation_window": validation,
        "upstream_selection": upstream_selection, "upstream_used_periods": used_periods,
        "hypotheses": hypothesis_values, "warmup": warmup, "state_boundary": state_boundary,
        "initial_wallet": initial_wallet, "initial_equity": initial_equity,
        "initial_positions": initial_positions, "attribution": attribution,
        "evidence_minimum": evidence_minimum, "profile_id": selected_profile,
        "config_id": selected_config, "policy_ids": selected_policy,
        "sizing_grid": points, "sizing_grid_order": order,
        "ranking_policy": ranking_policy, "total_test_budget": total_test_budget,
        "repeated_history_verification": repeated_history,
        "evaluation_clock": clock_canonical, "freshness_contract": freshness,
        "search_facts": frozen_facts, "refinement_rounds": refinement_rounds,
        "parent_campaign_digest": parent_digest,
    }
    digest = _digest(payload)
    return FrozenCampaign(
        development, validation, upstream_selection, used_periods, hypothesis_values,
        warmup, state_boundary, initial_wallet, initial_equity, initial_positions, attribution,
        evidence_minimum, selected_profile, selected_config, selected_policy, points, order,
        ranking_policy, total_test_budget, digest, digest, repeated_history,
        frozen_clock, freshness, frozen_facts, refinement_rounds, parent_digest,
    )


freeze_search = freeze_campaign
freeze_m7_campaign = freeze_campaign


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    status: str
    value: Decimal | None
    threshold: Decimal | None
    reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _freeze(self.evidence))

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "value": self.value, "threshold": self.threshold, "reason": self.reason, "evidence": self.evidence}


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    policy_id: str
    profile: str
    status: str
    checks: tuple[RiskCheck, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checks", tuple(self.checks))

    @property
    def passed(self) -> bool:
        return self.status == PASS

    def as_dict(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id, "profile": self.profile, "status": self.status, "reason": self.reason, "checks": [check.as_dict() for check in self.checks]}


def _metrics(value: Any) -> Any:
    if isinstance(value, PortfolioMetrics):
        return value
    if isinstance(value, NormalizedReport):
        return calculate_metrics(value)
    return value


def _risk_number(value: Any, *keys: str) -> Decimal | None:
    candidate = _first(value, *keys)
    if candidate is None:
        return None
    try:
        return _decimal(candidate, "risk evidence")
    except PortfolioIntegrationError:
        return None


def _fresh(value: Any, now_ms: int | None, freshness_contract: Mapping[str, Any] | None = None) -> tuple[bool, str | None, Mapping[str, Any]]:
    """Require an explicit, clock-checked timestamp/expiry freshness witness."""
    if not isinstance(value, Mapping):
        return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    if value.get("fresh") is False or value.get("stale") is True or value.get("valid") is False:
        return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    bound = _first(value, "freshness_bound", "equity_freshness")
    source = bound if isinstance(bound, Mapping) else value
    timestamp = _first(source, "timestamp_ms", "timestamp", "evidence_timestamp_ms")
    expiry = _first(source, "expires_at_ms", "expires_at", "evidence_expires_at_ms")
    contract = freshness_contract or {}
    require_timestamp = _first(contract, "require_timestamp", default=True)
    require_expiry = _first(contract, "require_expiry", default=True)
    if require_timestamp is not True or require_expiry not in (True, False):
        return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    if (require_timestamp and timestamp is None) or (require_expiry and expiry is None):
        return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    if now_ms is None:
        return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    try:
        _, timestamp_numeric = _timestamp(timestamp)
        timestamp_i = _clock_ms(timestamp, timestamp_numeric)
        expiry_i = None
        if expiry is not None:
            _, expiry_numeric = _timestamp(expiry)
            expiry_i = _clock_ms(expiry, expiry_numeric)
    except PortfolioIntegrationError:
        return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    if timestamp_i > now_ms or (expiry_i is not None and (expiry_i < now_ms or timestamp_i > expiry_i)):
        return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    maximum_age = _first(freshness_contract, "max_age_ms", "maximum_age_ms")
    if maximum_age is not None:
        try:
            maximum_age_i = _nonnegative_int(maximum_age, "max_age_ms")
        except PortfolioIntegrationError:
            return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
        if now_ms - timestamp_i > maximum_age_i:
            return False, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    witness = {"timestamp_ms": timestamp_i}
    if expiry_i is not None:
        witness["expires_at_ms"] = expiry_i
    return True, None, witness


def _equity_dd(evidence: Any, now_ms: int | None = None, freshness_contract: Mapping[str, Any] | None = None) -> tuple[Decimal | None, str | None, Mapping[str, Any]]:
    try:
        metrics = _metrics(evidence)
    except Exception:
        return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    if isinstance(metrics, PortfolioMetrics):
        if metrics.status != "COMPLETE" or metrics.coverage != "COMPLETE" or metrics.actual_drawdown_pct is None:
            return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
        try:
            metric_dd = _decimal(metrics.actual_drawdown_pct, "actual_equity_dd_pct")
        except PortfolioIntegrationError:
            return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
        if metric_dd < 0:
            return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
        if metrics.boundary_start is None or metrics.boundary_end is None:
            return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
        metric_witness = {"timestamp": metrics.boundary_end}
        metric_expiry = _first(metrics, "expires_at_ms", "expires_at", "evidence_expires_at_ms")
        if metric_expiry is not None:
            metric_witness["expires_at_ms"] = metric_expiry
        else:
            maximum_age = _first(freshness_contract, "max_age_ms", "maximum_age_ms")
            if maximum_age is not None:
                try:
                    _, boundary_numeric = _timestamp(metrics.boundary_end)
                    boundary_ms = _clock_ms(metrics.boundary_end, boundary_numeric)
                    metric_witness["expires_at_ms"] = boundary_ms + _nonnegative_int(maximum_age, "max_age_ms")
                except PortfolioIntegrationError:
                    pass
        fresh, reason, witness = _fresh(metric_witness, now_ms, freshness_contract)
        if not fresh:
            return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
        return metric_dd, None, {"source": "actual_joint_equity", "boundary_end": metrics.boundary_end, **witness}
    if isinstance(metrics, Mapping):
        outer_fresh, outer_reason, outer_witness = _fresh(metrics, now_ms, freshness_contract)
        if not outer_fresh:
            return None, outer_reason, {}
        nested = _first(metrics, "risk", "metrics")
        if isinstance(nested, Mapping):
            # An inner fresh marker cannot override stale/missing outer facts.
            if metrics.get("fresh") is False or metrics.get("stale") is True or metrics.get("valid") is False:
                return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
            if any(key in nested for key in ("timestamp_ms", "timestamp", "expires_at_ms", "expires_at", "freshness_bound", "equity_freshness")):
                nested_fresh, nested_reason, _ = _fresh(nested, now_ms, freshness_contract)
                if not nested_fresh:
                    return None, nested_reason, {}
            metrics = nested
        if metrics.get("fresh") is False or metrics.get("stale") is True or metrics.get("valid") is False or metrics.get("joint") is False or metrics.get("truncated") is True:
            return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
        value = _risk_number(metrics, "actual_equity_dd_pct")
        if value is not None:
            if value < 0:
                return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
            return value, None, {"source": "actual_joint_equity", **outer_witness}
    else:
        value = None
    provenance = _first(evidence, "equity_provenance", "joint_equity_provenance")
    if not isinstance(provenance, Mapping) or provenance.get("joint") is not True or provenance.get("full_series") is not True:
        return None, "EQUITY_EVIDENCE_UNAVAILABLE", {}
    fresh, reason, witness = _fresh({**provenance, **({"timestamp_ms": _first(evidence, "timestamp_ms", "timestamp"), "expires_at_ms": _first(evidence, "expires_at_ms", "expires_at")} if _first(provenance, "timestamp_ms", "timestamp") is None else {})}, now_ms, freshness_contract)
    if not fresh:
        return None, reason, {}
    series = _first(evidence, "equity_series")
    if isinstance(series, (list, tuple)) and series:
        values: list[Decimal] = []
        try:
            for point in series:
                item = point.get("value") if isinstance(point, Mapping) else point[1] if isinstance(point, (list, tuple)) and len(point) > 1 else point
                point_timestamp = _first(point, "timestamp_ms", "timestamp") if isinstance(point, Mapping) else point[0] if isinstance(point, (list, tuple)) and point else None
                if point_timestamp is not None and now_ms is not None:
                    try:
                        _, point_numeric = _timestamp(point_timestamp)
                        if _clock_ms(point_timestamp, point_numeric) > now_ms:
                            raise PortfolioIntegrationError("equity series point is in the future")
                    except PortfolioIntegrationError:
                        raise
                values.append(_decimal(item, "equity series"))
        except (PortfolioIntegrationError, TypeError, IndexError):
            values = []
        if values and all(value > 0 for value in values):
            peak = values[0]
            maximum = Decimal("0")
            for current in values:
                peak = max(peak, current)
                maximum = max(maximum, (peak - current) / peak * Decimal("100"))
            return maximum, None, {"source": "actual_joint_equity", "samples": len(values), **witness}
    reason = _first(evidence, "equity_reason", default="EQUITY_EVIDENCE_UNAVAILABLE")
    return None, str(reason), {}


def _margin_states(evidence: Any, now_ms: int | None = None, freshness_contract: Mapping[str, Any] | None = None) -> tuple[tuple[Decimal, Decimal, Decimal, Mapping[str, Any]], ...] | None:
    if isinstance(evidence, Mapping) and (evidence.get("fresh") is False or evidence.get("stale") is True or evidence.get("valid") is False):
        return None
    source = evidence
    if isinstance(evidence, Mapping):
        nested = _first(evidence, "risk", "metrics", "margin")
        if isinstance(nested, Mapping) and any(key in nested for key in ("margin_states", "margin_envelope_states", "states", "margin_balance", "calculated_total_im", "calculated_total_mm", "total_im", "total_mm")):
            if nested.get("fresh") is False or nested.get("stale") is True or nested.get("valid") is False:
                return None
            source = nested
    states = _first(source, "margin_states", "margin_envelope_states", "states")
    if states is None:
        single = source if isinstance(source, Mapping) else None
        if single is not None and all(_first(single, key) is not None for key in ("margin_balance", "calculated_total_im", "calculated_total_mm")):
            states = (single,)
    if not isinstance(states, (list, tuple)) or not states:
        return None
    parsed = []
    currency: str | None = None
    source_identity: Any = None
    snapshot_identity: Any = None
    snapshot_ids: list[str] = []
    account_ids: list[str] = []
    state_evidence_ids: list[str] = []
    for state in states:
        if not isinstance(state, Mapping):
            return None
        if state.get("fresh") is False or state.get("stale") is True or state.get("valid") is False:
            return None
        timestamp = _first(state, "timestamp", "timestamp_ms")
        state_currency = _first(state, "currency", "account_currency")
        if timestamp is None or state_currency is None or not str(state_currency).strip():
            return None
        try:
            _, timestamp_numeric = _timestamp(timestamp)
            numeric_timestamp = _clock_ms(timestamp, timestamp_numeric)
        except PortfolioIntegrationError:
            return None
        expiry = _first(state, "expires_at", "expires_at_ms")
        contract = freshness_contract or {}
        require_expiry = _first(contract, "require_expiry", default=True)
        if require_expiry is not True and require_expiry is not False:
            return None
        if require_expiry and expiry is None:
            return None
        if now_ms is not None:
            fresh, _, _ = _fresh(state, now_ms, freshness_contract)
            if not fresh:
                return None
        elif expiry is not None:
            try:
                _timestamp(expiry)
            except PortfolioIntegrationError:
                return None
        current_currency = str(state_currency)
        if currency is None:
            currency = current_currency
        elif currency != current_currency:
            return None
        identity = _first(state, "source_digest")
        if source_identity is None:
            source_identity = identity
        elif identity is not None and source_identity != identity:
            return None
        account_id = _first(state, "account_id", "account")
        snapshot = _first(state, "evaluation_snapshot_id", "evaluation_id", "snapshot_id")
        if account_id is not None and str(account_id).strip():
            account_ids.append(str(account_id))
        if snapshot is not None and str(snapshot).strip():
            snapshot_ids.append(str(snapshot))
            if snapshot_identity is None:
                snapshot_identity = str(snapshot)
            elif snapshot_identity != str(snapshot):
                return None
        state_evidence = _first(state, "account_evidence_id", "evidence_id")
        if state_evidence is not None and str(state_evidence).strip():
            state_evidence_ids.append(str(state_evidence))
        try:
            balance = _decimal(_first(state, "margin_balance"), "margin_balance")
            total_im = _decimal(_first(state, "calculated_total_im", "total_im"), "calculated_total_im")
            total_mm = _decimal(_first(state, "calculated_total_mm", "total_mm"), "calculated_total_mm")
        except PortfolioIntegrationError:
            return None
        if balance <= 0 or total_im < 0 or total_mm < 0:
            return None
        parsed.append((balance, total_im, total_mm, {"timestamp_ms": numeric_timestamp, "currency": current_currency}))
    if len(parsed) > 1:
        if len(account_ids) != len(parsed) or len(set(account_ids)) != len(account_ids):
            return None
        if snapshot_identity is None or len(snapshot_ids) != len(parsed) or len(state_evidence_ids) != len(parsed):
            return None
    return tuple(parsed)


def _margin_unknown_reason(evidence: Any) -> str:
    source = evidence
    if isinstance(evidence, Mapping):
        nested = _first(evidence, "risk", "metrics", "margin")
        if isinstance(nested, Mapping) and any(key in nested for key in ("margin_states", "margin_envelope_states", "states", "margin_balance", "calculated_total_im", "calculated_total_mm", "total_im", "total_mm")):
            source = nested
    states = _first(source, "margin_states", "margin_envelope_states", "states")
    if not isinstance(states, (list, tuple)):
        return "MARGIN_EVIDENCE_UNAVAILABLE"
    currencies = {str(_first(item, "currency", "account_currency")) for item in states if isinstance(item, Mapping)}
    if len(currencies) > 1:
        return "MARGIN_CURRENCY_MISMATCH"
    if len(states) > 1:
        snapshots = [(_first(item, "evaluation_snapshot_id", "evaluation_id", "snapshot_id") if isinstance(item, Mapping) else None) for item in states]
        if any(value is None for value in snapshots):
            return "MARGIN_EVALUATION_SNAPSHOT_MISSING"
        if len({str(value) for value in snapshots}) > 1:
            return "MARGIN_EVALUATION_SNAPSHOT_MISMATCH"
        if (len([item for item in states if isinstance(item, Mapping) and _first(item, "account_id", "account") is not None]) != len(states) or len([item for item in states if isinstance(item, Mapping) and _first(item, "account_evidence_id", "evidence_id") is not None]) != len(states)):
            return "MARGIN_ACCOUNT_EVIDENCE_ID_MISSING"
    return "MARGIN_EVIDENCE_UNAVAILABLE"


def evaluate_research_risk(evidence: Any, *, profile: str, policy_id: str = POLICY_VERSION, now_ms: int | None = None, freshness_contract: Mapping[str, Any] | None = None) -> RiskEvaluation:
    """Evaluate DD, reserve, and MM jointly with fixed v1 thresholds."""
    if profile not in PROFILE_NAMES or policy_id != POLICY_VERSION:
        checks = tuple(RiskCheck(name, UNKNOWN, None, None, "RISK_POLICY_UNAVAILABLE") for name in ("actual_equity_dd", "free_margin_reserve", "account_mm_load"))
        return RiskEvaluation(policy_id, profile, UNKNOWN, checks, "RISK_POLICY_UNAVAILABLE")
    thresholds = RESEARCH_RISK_POLICY[profile]
    clock_available = now_ms is not None and not isinstance(now_ms, bool)
    if clock_available:
        try:
            parsed_clock = _decimal(now_ms, "evaluation clock")
            integer_clock = int(parsed_clock)
            if parsed_clock != Decimal(integer_clock):
                raise PortfolioIntegrationError("evaluation clock must be an integer")
            now_ms = integer_clock
        except (PortfolioIntegrationError, TypeError, ValueError, OverflowError):
            clock_available = False
    if not clock_available:
        checks = tuple(
            RiskCheck(name, UNKNOWN, None, thresholds[threshold], "EVALUATION_CLOCK_UNAVAILABLE")
            for name, threshold in (
                ("actual_equity_dd", "max_actual_equity_dd_pct"),
                ("free_margin_reserve", "min_calculated_free_margin_reserve_pct"),
                ("account_mm_load", "max_calculated_account_mm_load_pct"),
            )
        )
        return RiskEvaluation(policy_id, profile, UNKNOWN, checks, "EVALUATION_CLOCK_UNAVAILABLE")
    dd, dd_reason, dd_witness = _equity_dd(evidence, now_ms, freshness_contract)
    dd_check = RiskCheck("actual_equity_dd", UNKNOWN if dd is None else (PASS if dd <= thresholds["max_actual_equity_dd_pct"] else FAIL), dd, thresholds["max_actual_equity_dd_pct"], dd_reason, dd_witness)
    states = _margin_states(evidence, now_ms, freshness_contract)
    if states is None:
        unknown_reason = _margin_unknown_reason(evidence)
        reserve_check = RiskCheck("free_margin_reserve", UNKNOWN, None, thresholds["min_calculated_free_margin_reserve_pct"], unknown_reason)
        mm_check = RiskCheck("account_mm_load", UNKNOWN, None, thresholds["max_calculated_account_mm_load_pct"], unknown_reason)
    else:
        reserves = tuple((balance - total_im) / balance * Decimal("100") for balance, total_im, _, _ in states)
        loads = tuple(total_mm / balance * Decimal("100") for balance, _, total_mm, _ in states)
        reserve = min(reserves)
        load = max(loads)
        reserve_check = RiskCheck("free_margin_reserve", PASS if reserve >= thresholds["min_calculated_free_margin_reserve_pct"] else FAIL, reserve, thresholds["min_calculated_free_margin_reserve_pct"], None, {"states": len(states), "formula": "(MarginBalance-calculated_total_IM)/MarginBalance*100"})
        mm_check = RiskCheck("account_mm_load", PASS if load <= thresholds["max_calculated_account_mm_load_pct"] else FAIL, load, thresholds["max_calculated_account_mm_load_pct"], None, {"states": len(states), "formula": "calculated_total_MM/MarginBalance*100"})
    checks = (dd_check, reserve_check, mm_check)
    if any(check.status == FAIL for check in checks):
        status, reason = FAIL, next(check.name.upper() + "_FAILED" for check in checks if check.status == FAIL)
    elif any(check.status == UNKNOWN for check in checks):
        status, reason = UNKNOWN, next(check.reason or (check.name.upper() + "_UNKNOWN") for check in checks if check.status == UNKNOWN)
    else:
        status, reason = PASS, None
    return RiskEvaluation(policy_id, profile, status, checks, reason)


research_risk = evaluate_research_risk
evaluate_risk = evaluate_research_risk


_TERMINAL = frozenset({"PRECHECK_REJECTED", "FAILED", "IMPORTED", "VALIDATION_FAILED", "VALIDATION_PASS"})


@dataclass(frozen=True, slots=True)
class Attempt:
    identity: str
    phase: str
    status: str
    candidate_id: str
    variant: Any = None
    reason: str | None = None
    detail: Any = None
    risk: RiskEvaluation | None = None
    budget_index: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "variant", _freeze(self.variant))
        object.__setattr__(self, "detail", _freeze(self.detail))

    def as_dict(self) -> dict[str, Any]:
        return {"identity": self.identity, "phase": self.phase, "status": self.status, "candidate_id": self.candidate_id, "variant": self.variant, "reason": self.reason, "detail": self.detail, "risk": self.risk.as_dict() if self.risk else None, "budget_index": self.budget_index}


@dataclass(frozen=True, slots=True)
class ResumeSnapshot:
    """Typed continuation token for a new explicitly linked campaign."""

    campaign_digest: str
    attempts: tuple[Attempt, ...]
    candidate_universe_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_digest, str) or not self.campaign_digest.strip():
            raise PortfolioIntegrationError("resume snapshot campaign digest is required")
        object.__setattr__(self, "attempts", tuple(self.attempts))
        if self.candidate_universe_digest is not None and (not isinstance(self.candidate_universe_digest, str) or not self.candidate_universe_digest.strip()):
            raise PortfolioIntegrationError("resume snapshot candidate universe digest is malformed")


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    status: str
    reason: str | None
    reasons: tuple[str, ...]
    campaign: FrozenCampaign
    attempts: tuple[Attempt, ...]
    development_order: tuple[str, ...] = ()
    finalists: tuple[str, ...] = ()
    validation_passes: tuple[str, ...] = ()
    selected: str | None = None
    risk: Mapping[str, RiskEvaluation] = field(default_factory=dict)
    accounts: Mapping[str, Any] = field(default_factory=dict)
    decision_campaign_id: str | None = None
    execution_campaign_id: str | None = None
    evaluation_id: str | None = None
    exhausted: bool = False
    trading_run_id: str | None = None
    candidate_universe_digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(self, "risk", MappingProxyType(dict(self.risk)))
        object.__setattr__(self, "accounts", _freeze(self.accounts))
        if self.candidate_universe_digest is not None and (not isinstance(self.candidate_universe_digest, str) or not self.candidate_universe_digest.strip()):
            raise PortfolioIntegrationError("candidate universe digest is malformed")

    @property
    def recommendation_status(self) -> str:
        return self.status

    @property
    def repeated_history_verification(self) -> bool:
        return self.campaign.repeated_history_verification

    @property
    def validation_label(self) -> str:
        return "REPEATED_HISTORY_VERIFICATION" if self.repeated_history_verification else "INDEPENDENT_HOLDOUT"

    @property
    def run_id(self) -> str | None:
        return self.trading_run_id

    @property
    def resume_snapshot(self) -> ResumeSnapshot:
        return ResumeSnapshot(self.campaign.canonical_digest, self.attempts, self.candidate_universe_digest)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "reasons": self.reasons, "campaign": self.campaign.as_dict(), "candidate_universe_digest": self.candidate_universe_digest, "attempts": [attempt.as_dict() for attempt in self.attempts], "development_order": self.development_order, "finalists": self.finalists, "validation_passes": self.validation_passes, "selected": self.selected, "risk": {key: value.as_dict() for key, value in self.risk.items()}, "accounts": self.accounts, "decision_campaign_id": self.decision_campaign_id, "execution_campaign_id": self.execution_campaign_id, "evaluation_id": self.evaluation_id, "exhausted": self.exhausted, "trading_run_id": self.trading_run_id}


def _invoke(callback: Callable[..., Any], value: Any, campaign: FrozenCampaign, attempt: Attempt | None, phase: str) -> Any:
    try:
        signature = inspect.signature(callback)
        positional = [parameter for parameter in signature.parameters.values() if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)]
        if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values()):
            return callback(value, campaign, attempt, phase)
        count = len(positional)
    except (TypeError, ValueError):
        count = 1
    if count >= 4:
        return callback(value, campaign, attempt, phase)
    if count == 3:
        return callback(value, campaign, attempt)
    if count == 2:
        return callback(value, campaign)
    return callback(value)


def _invoke_global(callback: Callable[..., Any], variants: Sequence[Any], accounts: Mapping[str, Any] | None, campaign: FrozenCampaign) -> Any:
    """Global PortfolioSet screens receive all variants and account states."""
    try:
        signature = inspect.signature(callback)
        positional = [parameter for parameter in signature.parameters.values() if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)]
        count = len(positional)
        if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values()):
            count = 3
    except (TypeError, ValueError):
        count = 1
    if count >= 3:
        return callback(variants, accounts or {}, campaign)
    if count == 2:
        return callback(variants, accounts or {})
    return callback(variants)


def _callback_passed(value: Any) -> bool:
    if value is True or (isinstance(value, str) and value == PASS):
        return True
    if isinstance(value, Mapping):
        marker = value.get("status", value.get("validation"))
        return isinstance(marker, str) and marker == PASS
    if isinstance(value, NormalizedReport):
        return bool(value.schema and value.version > 0 and value.run_id and value.attempt_id and value.member and value.semantic_digest and value.raw_digest and not value.blocking)
    if isinstance(value, PortfolioMetrics):
        return bool(value.complete)
    return False


def _callback_reason(value: Any, fallback: str) -> str:
    if isinstance(value, Mapping):
        reason = value.get("reason", value.get("code"))
        if reason not in (None, ""):
            return str(reason)
        marker = value.get("status", value.get("validation"))
        if marker not in (None, ""):
            return str(marker)
    if isinstance(value, str) and value not in {PASS, FAIL}:
        return value
    return fallback


def _variant_id(variant: Any) -> str:
    if not isinstance(variant, Variant):
        return _digest({"variant": variant})
    legs = {
        "LONG": canonical_candidate_identity(variant.slot.long) if variant.slot.long is not None else None,
        "SHORT": canonical_candidate_identity(variant.slot.short) if variant.slot.short is not None else None,
    }
    return _digest({"legs": legs, "directions": tuple(variant.directions), "symbol": variant.slot.symbol, "composition": variant.composition, "scalar": variant.scalar, "priority": variant.priority, "limiter": variant.limiter})


def _candidate_universe_digest(candidates: Sequence[Any]) -> str:
    try:
        identities = tuple(canonical_candidate_identity(candidate) for candidate in candidates)
    except Exception as error:
        raise PortfolioIntegrationError("candidate universe identity is malformed") from error
    return _digest({"ordered_candidate_identities": identities})


class PortfolioIntegration:
    """Run M7 using only injected test/import/refinement functions."""

    def __init__(self, campaign: FrozenCampaign, *, store: Any = None) -> None:
        if not isinstance(campaign, FrozenCampaign):
            raise PortfolioIntegrationError("frozen campaign is required")
        self.campaign = campaign
        self.store = store

    def run(
        self,
        candidates: Sequence[Any],
        *,
        search_options: Mapping[str, Any] | None = None,
        precheck: Callable[..., Any] | None = None,
        test: Callable[..., Any] | None = None,
        import_result: Callable[..., Any] | None = None,
        validation_test: Callable[..., Any] | None = None,
        validation_import: Callable[..., Any] | None = None,
        refine: Callable[..., Any] | None = None,
        resume: IntegrationResult | ResumeSnapshot | Sequence[Attempt] | None = None,
        rounds: int | None = None,
        accounts: Mapping[str, Any] | None = None,
        global_liquidity_gate: Callable[..., Any] | None = None,
    ) -> IntegrationResult:
        # Scenario deposits and account state are inputs to the frozen run;
        # callbacks receive an immutable view and cannot change the ledger
        # or persistence identity midway through the loop.
        candidate_values = tuple(candidates)
        candidate_universe_digest = _candidate_universe_digest(candidate_values)
        accounts = _freeze(accounts or {})
        if resume is not None and not isinstance(resume, (IntegrationResult, ResumeSnapshot)):
            raise PortfolioIntegrationError("raw attempt sequences cannot be resumed without campaign binding")
        attempts = list(resume.attempts if isinstance(resume, (IntegrationResult, ResumeSnapshot)) else ())
        if any(not isinstance(attempt, Attempt) for attempt in attempts):
            raise PortfolioIntegrationError("resume attempt ledger is malformed")
        if len({(attempt.identity, attempt.phase) for attempt in attempts}) != len(attempts):
            raise PortfolioIntegrationError("resume attempt ledger contains duplicates")
        if isinstance(resume, IntegrationResult) and resume.campaign.canonical_digest != self.campaign.canonical_digest:
            raise PortfolioIntegrationError("resume campaign digest mismatch")
        if isinstance(resume, IntegrationResult) and resume.candidate_universe_digest != candidate_universe_digest:
            raise PortfolioIntegrationError("resume candidate universe digest mismatch")
        if isinstance(resume, ResumeSnapshot):
            if self.campaign.parent_campaign_digest != resume.campaign_digest:
                raise PortfolioIntegrationError("resume snapshot is not linked to campaign")
            if resume.candidate_universe_digest != candidate_universe_digest:
                raise PortfolioIntegrationError("resume candidate universe digest mismatch")
        if test is None or import_result is None:
            return self._blocked("INJECTED_TEST_AND_IMPORT_REQUIRED", attempts=attempts, accounts=accounts, candidate_universe_digest=candidate_universe_digest)
        if rounds is None:
            rounds = self.campaign.refinement_rounds
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
            return self._blocked("REFINEMENT_ROUNDS_INVALID", attempts=attempts, accounts=accounts, candidate_universe_digest=candidate_universe_digest)
        if rounds != self.campaign.refinement_rounds:
            raise PortfolioIntegrationError("refinement round schedule differs from frozen campaign")
        options = dict(search_options or {})
        allowed_search_keys = {
            "capability", "current_equity", "dd_cap_pct", "sizing_grid", "grid_version",
            "max_pretest_variant_count", "ranking_policy", "limiter", "priority", "priorities",
            "composition", "opposite_policy", "now_ms", "structural_gate", "liquidity_gate",
            "margin_gate", "sink", "profile", "current_portfolio_equity", "gate_policy_ids",
        }
        unknown_search_keys = [key for key in options if not isinstance(key, str) or key not in allowed_search_keys]
        if unknown_search_keys:
            raise PortfolioIntegrationError(f"unknown search option: {unknown_search_keys[0]!r}")
        if self.campaign.ranking_policy is None:
            return self._blocked("OPEN_POLICY", attempts=attempts, accounts=accounts, candidate_universe_digest=candidate_universe_digest)
        # The frozen campaign owns these values; a caller cannot silently
        # retune the search by supplying a second grid or ranking descriptor.
        frozen_grid = tuple(self.campaign.sizing_grid[index] for index in self.campaign.sizing_grid_order)
        if "sizing_grid" in options and _encoded(options["sizing_grid"]) != _encoded(frozen_grid):
            raise PortfolioIntegrationError("runtime sizing grid differs from frozen campaign")
        if "ranking_policy" in options and _encoded(options["ranking_policy"]) != _encoded(self.campaign.ranking_policy):
            raise PortfolioIntegrationError("runtime ranking policy differs from frozen campaign")
        if "max_pretest_variant_count" in options and options["max_pretest_variant_count"] is not None:
            raise PortfolioIntegrationError("runtime pretest budget differs from frozen campaign")
        if "profile" in options or "current_portfolio_equity" in options:
            raise PortfolioIntegrationError("runtime search alias is not frozen")
        options["sizing_grid"] = frozen_grid
        options["ranking_policy"] = self.campaign.ranking_policy
        options["max_pretest_variant_count"] = None
        frozen = self.campaign.search_facts
        for key in ("capability", "current_equity", "dd_cap_pct", "composition", "priorities", "priority", "opposite_policy", "limiter", "grid_version"):
            expected = frozen.get(key)
            if key in options:
                if _encoded(options[key]) != _encoded(expected):
                    raise PortfolioIntegrationError(f"runtime search fact differs from frozen campaign: {key}")
            options[key] = expected
        if "now_ms" in options:
            if _encoded(options["now_ms"]) != _encoded(self.campaign.evaluation_clock_ms):
                raise PortfolioIntegrationError("runtime evaluation clock differs from frozen campaign")
        else:
            options["now_ms"] = self.campaign.evaluation_clock_ms
        if "gate_policy_ids" in options and _encoded(options["gate_policy_ids"]) != _encoded(frozen.get("gate_policy_ids")):
            raise PortfolioIntegrationError("runtime gate policy identities differ from frozen campaign")
        gate_ids = frozen.get("gate_policy_ids")
        for gate_name in ("structural_gate", "liquidity_gate", "margin_gate", "sink"):
            if gate_name in options:
                descriptor = gate_ids.get(gate_name, gate_ids.get(gate_name.removesuffix("_gate"))) if isinstance(gate_ids, Mapping) else None
                if descriptor in (None, ""):
                    raise PortfolioIntegrationError(f"runtime {gate_name} lacks a frozen policy identity")
        options.pop("gate_policy_ids", None)
        try:
            search = search_portfolios(candidate_values, **options)
        except Exception as error:
            return self._blocked("SEARCH_FAILED", detail=str(error), attempts=attempts, accounts=accounts, candidate_universe_digest=candidate_universe_digest)
        if search.status not in {PASS}:
            blocked_reason = "NO_FEASIBLE_CANDIDATE" if not search.slots or search.detail in {"NO_FINALIST", "COMPOSITION_EMPTY"} else (search.reason or "NO_FEASIBLE_CANDIDATE")
            rejected = tuple(
                Attempt(
                    _variant_id(variant),
                    "development",
                    "PRECHECK_REJECTED" if variant.gate else "FAILED",
                    _variant_id(variant),
                    variant,
                    variant.reason or blocked_reason,
                    variant.detail,
                )
                for variant in search.tried
            )
            existing = {(attempt.identity, attempt.phase) for attempt in attempts}
            for attempt in rejected:
                key = (attempt.identity, attempt.phase)
                if key not in existing:
                    attempts.append(attempt)
                    existing.add(key)
            return self._blocked(blocked_reason, attempts=attempts, accounts=accounts, reasons=search.reasons, candidate_universe_digest=candidate_universe_digest)
        variants = tuple(search.passing)
        # The joint screen runs against the actual final PortfolioSet.  A
        # variant that failed injected testing is not a portfolio member.
        screen_variants: list[Variant] = []
        screen_ids: set[str] = set()
        def add_screen(variants_to_add: Iterable[Variant]) -> None:
            for item in variants_to_add:
                item_id = _variant_id(item)
                if item_id not in screen_ids:
                    screen_variants.append(item)
                    screen_ids.add(item_id)
        def record(attempt: Attempt) -> None:
            """Keep one immutable outcome per attempt identity and phase."""
            for index, prior_attempt in enumerate(attempts):
                if prior_attempt.identity == attempt.identity and prior_attempt.phase == attempt.phase:
                    attempts[index] = attempt
                    return
            attempts.append(attempt)

        # Search itself is the first precheck stage.  Retain every rejected
        # finite variant in the ledger even though only passing variants reach
        # the injected tester.
        existing_keys = {(attempt.identity, attempt.phase) for attempt in attempts}
        for variant in search.tried:
            if variant.status == PASS:
                continue
            identity = _variant_id(variant)
            key = (identity, "development")
            if key in existing_keys:
                continue
            attempts.append(Attempt(identity, "development", "PRECHECK_REJECTED" if variant.gate else "FAILED", identity, variant, variant.reason or "PRECHECK_REJECTED", variant.detail))
            existing_keys.add(key)
        done = {attempt.identity for attempt in attempts if attempt.status in _TERMINAL}
        budget_indexes: list[int] = []
        for attempt in attempts:
            if attempt.budget_index is None:
                continue
            if isinstance(attempt.budget_index, bool) or not isinstance(attempt.budget_index, int) or attempt.budget_index <= 0:
                raise PortfolioIntegrationError("resume attempt budget index is invalid")
            budget_indexes.append(attempt.budget_index)
        # Callback consumption is authoritative.  Search-origin failures,
        # precheck rejections, and budget markers have no budget index.
        budget_used = max(budget_indexes, default=0)
        development_passes: list[Variant] = []
        risk_values: dict[str, RiskEvaluation] = {}
        def execute_development(to_test: Sequence[Variant]) -> list[Variant]:
            nonlocal budget_used
            passed: list[Variant] = []
            for variant in to_test:
                identity = _variant_id(variant)
                existing = next((item for item in attempts if item.identity == identity and item.phase == "development"), None)
                if existing is not None and existing.status != "NOT_TESTED_BUDGET":
                    if existing.status == "IMPORTED":
                        passed.append(variant)
                    if existing.risk:
                        risk_values[identity] = existing.risk
                    done.add(identity)
                    continue
                # A budget exhaustion marker is a durable fact for this
                # campaign.  It is not terminal, but replaying the same
                # exhausted campaign must not append duplicate markers.  A
                # linked campaign with a larger budget is allowed to test it.
                if existing is not None and budget_used >= self.campaign.total_test_budget:
                    continue
                if identity in done:
                    continue
                if budget_used >= self.campaign.total_test_budget:
                    attempts.append(Attempt(identity, "development", "NOT_TESTED_BUDGET", identity, variant, "NOT_TESTED_BUDGET", "TOTAL_TEST_REFINEMENT_BUDGET_EXHAUSTED"))
                    continue
                if precheck is not None:
                    try:
                        precheck_result = _invoke(precheck, variant, self.campaign, None, "development_precheck")
                    except Exception as error:
                        precheck_result = {"status": FAIL, "reason": "PRECHECK_EXCEPTION", "detail": str(error)}
                    if not _callback_passed(precheck_result):
                        record(Attempt(identity, "development", "PRECHECK_REJECTED", identity, variant, _callback_reason(precheck_result, "PRECHECK_REJECTED"), precheck_result))
                        done.add(identity)
                        continue
                budget_used += 1
                base = Attempt(identity, "development", "TRIED", identity, variant, budget_index=budget_used)
                try:
                    tested = _invoke(test, variant, self.campaign, base, "development")
                    if not _callback_passed(tested):
                        record(Attempt(identity, "development", "FAILED", identity, variant, _callback_reason(tested, "TEST_FAILED"), tested, budget_index=budget_used))
                        done.add(identity)
                        continue
                    imported = _invoke(import_result, tested, self.campaign, base, "development")
                    if imported is None:
                        record(Attempt(identity, "development", "FAILED", identity, variant, "IMPORT_FAILED", None, budget_index=budget_used))
                        done.add(identity)
                        continue
                    risk = evaluate_research_risk(imported, profile=self.campaign.profile_id, policy_id=_policy_identifier(_first(self.campaign.policy_ids, "risk", "policy", default="")), now_ms=self.campaign.evaluation_clock_ms, freshness_contract=self.campaign.freshness_contract)
                    risk_values[identity] = risk
                    if risk.status != PASS:
                        record(Attempt(identity, "development", "FAILED", identity, variant, risk.reason or "RISK_GATE_FAILED", imported, risk, budget_used))
                        done.add(identity)
                        continue
                    record(Attempt(identity, "development", "IMPORTED", identity, variant, None, imported, risk, budget_used))
                    done.add(identity)
                    passed.append(variant)
                except Exception as error:
                    record(Attempt(identity, "development", "FAILED", identity, variant, "CALLBACK_FAILED", str(error), budget_index=budget_used))
                    done.add(identity)
            return passed

        development_passes.extend(execute_development(variants))
        if not development_passes and refine is not None:
            for round_number in range(rounds):
                refine_identity = f"refine-{round_number}"
                prior_refine = next((item for item in attempts if item.identity == refine_identity and item.phase == "refine"), None)
                if prior_refine is not None and prior_refine.status != "NOT_TESTED_BUDGET":
                    if prior_refine.status == "IMPORTED":
                        refined = prior_refine.detail
                    else:
                        continue
                else:
                    refined = _MISSING
                # A persisted IMPORTED refinement is replayable even when
                # the frozen budget is fully consumed.  Only a new callback
                # invocation needs a remaining budget slot.
                if refined is _MISSING and budget_used >= self.campaign.total_test_budget:
                    break
                if refined is _MISSING:
                    budget_used += 1
                    refine_attempt = Attempt(refine_identity, "refine", "TRIED", refine_identity, None, budget_index=budget_used)
                    try:
                        refined = _invoke(refine, tuple(attempts), self.campaign, refine_attempt, "refine")
                    except Exception as error:
                        record(Attempt(refine_identity, "refine", "FAILED", refine_identity, None, "REFINE_FAILED", str(error), budget_index=budget_used))
                        continue
                    if refined is None:
                        record(Attempt(refine_identity, "refine", "FAILED", refine_identity, None, "REFINE_EMPTY", None, budget_index=budget_used))
                        continue
                    record(Attempt(refine_identity, "refine", "IMPORTED", refine_identity, None, None, refined, budget_index=budget_used))
                refined_candidates = refined.get("candidates") if isinstance(refined, Mapping) else refined
                refined_candidates = _thaw(refined_candidates)
                refined_search = None
                if isinstance(refined_candidates, (list, tuple)) and refined_candidates and all(isinstance(item, Mapping) for item in refined_candidates):
                    try:
                        refined_search = search_portfolios(refined_candidates, **options)
                    except Exception as error:
                        refined_search = None
                        record(Attempt(f"{refine_identity}:search", "refine_search", "FAILED", refine_identity, None, "REFINED_SEARCH_FAILED", str(error)))
                elif refined_candidates is not _MISSING:
                    record(Attempt(f"{refine_identity}:search", "refine_search", "FAILED", refine_identity, None, "REFINED_CANDIDATES_INVALID", refined_candidates))
                if refined_search is not None:
                    for rejected in refined_search.tried:
                        if rejected.status != PASS:
                            rejected_id = _variant_id(rejected)
                            rejected_key = (rejected_id, "development")
                            if rejected_key not in existing_keys:
                                attempts.append(Attempt(rejected_id, "development", "PRECHECK_REJECTED", rejected_id, rejected, rejected.reason or "PRECHECK_REJECTED", rejected.detail))
                                existing_keys.add(rejected_key)
                                done.add(rejected_id)
                    development_passes.extend(execute_development(refined_search.passing))
                if development_passes:
                    break
        development_order = tuple(_variant_id(variant) for variant in variants) + tuple(_variant_id(variant) for variant in development_passes if _variant_id(variant) not in {_variant_id(item) for item in variants})
        finalists = tuple(_variant_id(variant) for variant in development_passes)
        if not screen_variants:
            add_screen(development_passes)
        liquidity_reasons: tuple[str, ...] = ()
        if global_liquidity_gate is not None:
            try:
                if not _callback_passed(_invoke_global(global_liquidity_gate, tuple(screen_variants), accounts, self.campaign)):
                    return self._finish(INSUFFICIENT_EVIDENCE, "GLOBAL_LIQUIDITY_FAILED", tuple(attempts), development_order, finalists, (), None, risk_values, accounts, budget_used >= self.campaign.total_test_budget, candidate_universe_digest=candidate_universe_digest)
            except Exception as error:
                return self._finish(INSUFFICIENT_EVIDENCE, "GLOBAL_LIQUIDITY_UNKNOWN", tuple(attempts), development_order, finalists, (), None, risk_values, accounts, budget_used >= self.campaign.total_test_budget, candidate_universe_digest=candidate_universe_digest, detail=str(error))
        else:
            liquidity_reasons = ("GLOBAL_LIQUIDITY_NOT_EVALUATED",)
        if validation_import is None:
            validation_import = import_result
        validation_passes: list[str] = []
        validation_callback = validation_test or test
        for variant in development_passes:
            identity = _variant_id(variant)
            prior = next((item for item in attempts if item.identity == f"validation:{identity}"), None)
            if prior is not None:
                if prior.status == "VALIDATION_PASS":
                    validation_passes.append(identity)
                    continue
                if prior.status != "NOT_TESTED_BUDGET":
                    continue
            if budget_used >= self.campaign.total_test_budget:
                if prior is None or prior.status != "NOT_TESTED_BUDGET":
                    attempts.append(Attempt(f"validation:{identity}", "validation", "NOT_TESTED_BUDGET", identity, variant, "NOT_TESTED_BUDGET", "TOTAL_TEST_REFINEMENT_BUDGET_EXHAUSTED"))
                continue
            budget_used += 1
            validation_identity = f"validation:{identity}"
            base = Attempt(validation_identity, "validation", "TRIED", identity, variant, budget_index=budget_used)
            try:
                tested = _invoke(validation_callback, variant, self.campaign, base, "validation")
                if not _callback_passed(tested):
                    record(Attempt(validation_identity, "validation", "VALIDATION_FAILED", identity, variant, _callback_reason(tested, "VALIDATION_FAILED"), tested, budget_index=budget_used))
                    continue
                imported = _invoke(validation_import, tested, self.campaign, base, "validation") if validation_import else tested
                if imported is None:
                    record(Attempt(validation_identity, "validation", "VALIDATION_FAILED", identity, variant, "IMPORT_FAILED", None, budget_index=budget_used))
                    continue
                risk = evaluate_research_risk(imported, profile=self.campaign.profile_id, policy_id=_policy_identifier(_first(self.campaign.policy_ids, "risk", "policy", default="")), now_ms=self.campaign.evaluation_clock_ms, freshness_contract=self.campaign.freshness_contract)
                risk_values[identity] = risk
                if risk.status != PASS:
                    record(Attempt(validation_identity, "validation", "VALIDATION_FAILED", identity, variant, risk.reason or "RISK_GATE_FAILED", imported, risk, budget_used))
                    continue
                record(Attempt(validation_identity, "validation", "VALIDATION_PASS", identity, variant, None, imported, risk, budget_used))
                validation_passes.append(identity)
            except Exception as error:
                record(Attempt(validation_identity, "validation", "VALIDATION_FAILED", identity, variant, "CALLBACK_FAILED", str(error), budget_index=budget_used))
        if not validation_passes:
            return self._finish(INSUFFICIENT_EVIDENCE, "NO_VALIDATION_PASS", tuple(attempts), development_order, finalists, (), None, risk_values, accounts, budget_used >= self.campaign.total_test_budget, liquidity_reasons, candidate_universe_digest=candidate_universe_digest)
        selected = next(identity for identity in finalists if identity in validation_passes)
        reasons: list[str] = list(liquidity_reasons)
        policy_open = not self._complete_policy()
        if policy_open:
            reasons.append("OPEN_POLICY")
        # M7 remains fixture-only: a passed fixture loop is research evidence.
        status = RESEARCH_ONLY
        reason = "OPEN_POLICY" if policy_open else "FIXTURE_ONLY_RESEARCH"
        return self._finish(status, reason, tuple(attempts), development_order, finalists, tuple(validation_passes), selected, risk_values, accounts, budget_used >= self.campaign.total_test_budget, reasons, candidate_universe_digest=candidate_universe_digest)

    def _complete_policy(self) -> bool:
        policy = self.campaign.policy_ids
        freshness = self.campaign.freshness_contract
        if not isinstance(freshness, Mapping) or freshness.get("require_timestamp") is not True or freshness.get("require_expiry") is not True:
            return False
        maximum_age = _first(freshness, "max_age_ms", "maximum_age_ms")
        if maximum_age is not None:
            try:
                _nonnegative_int(maximum_age, "max_age_ms")
            except PortfolioIntegrationError:
                return False
        return bool(_first(policy, "pnl", "pnl_policy") and _first(policy, "liquidity", "liquidity_policy") and _first(policy, "ranking", "ranking_policy"))

    def _blocked(self, reason: str, *, candidate_universe_digest: str, detail: Any = None, attempts: Sequence[Attempt] = (), accounts: Mapping[str, Any] | None = None, reasons: Sequence[str] = ()) -> IntegrationResult:
        return self._finish(RESEARCH_ONLY if reason == "OPEN_POLICY" else INSUFFICIENT_EVIDENCE, reason, tuple(attempts), (), (), (), None, {}, accounts, False, tuple(reasons) + (reason,), candidate_universe_digest=candidate_universe_digest, detail=detail)

    def _finish(self, status: str, reason: str, attempts: tuple[Attempt, ...], development_order: tuple[str, ...], finalists: tuple[str, ...], validation_passes: tuple[str, ...], selected: str | None, risk: Mapping[str, RiskEvaluation], accounts: Mapping[str, Any] | None, exhausted: bool, reasons: Sequence[str] = (), *, candidate_universe_digest: str, detail: Any = None) -> IntegrationResult:
        decision_id = execution_id = evaluation_id = run_id = None
        payload = {
            "status": status,
            "reason": reason,
            "reasons": tuple(dict.fromkeys((*reasons, reason))),
            "attempts": [attempt.as_dict() for attempt in attempts],
            "campaign_digest": self.campaign.canonical_digest,
            "candidate_universe_digest": candidate_universe_digest,
            "development_order": development_order,
            "finalists": finalists,
            "validation_passes": validation_passes,
            "selected": selected,
            "exhausted": exhausted,
            "detail": detail,
        }
        if self.store is not None:
            decision_payload = {"campaign": self.campaign.as_dict(), "candidate_universe_digest": candidate_universe_digest}
            decision_digest = _digest(decision_payload)
            decision_id = f"decision-{decision_digest[:32]}"
            execution_payload = {"campaign_digest": self.campaign.canonical_digest, "candidate_universe_digest": candidate_universe_digest, "kind": "execution"}
            execution_digest = _digest(execution_payload)
            execution_id = f"execution-{execution_digest[:32]}"
            run_payload = {"campaign_digest": self.campaign.canonical_digest, "candidate_universe_digest": candidate_universe_digest, "accounts": accounts or {}, "state": self.campaign.initial_positions, "attempts": attempts}
            run_id = f"run-{_digest(run_payload)[:32]}"
            evaluation_payload = {"campaign_digest": self.campaign.canonical_digest, "run_id": run_id, "result": payload}
            evaluation_id = f"evaluation-{_digest(evaluation_payload)[:32]}"
            try:
                self.store.create_campaign(decision_id, decision_digest, _plain(decision_payload))
                self.store.create_campaign(execution_id, execution_digest, _plain(execution_payload))
                self.store.create_trading_run(run_id, execution_id, _plain(run_payload))
                self.store.create_evaluation(evaluation_id, run_id, decision_id, _plain(payload), execution_campaign_id=execution_id)
            except Exception as error:
                raise PortfolioIntegrationError("M7 persistence failed") from error
        return IntegrationResult(status, reason, tuple(dict.fromkeys((*reasons, reason))), self.campaign, attempts, development_order, finalists, validation_passes, selected, risk, accounts or {}, decision_id, execution_id, evaluation_id, exhausted, run_id, candidate_universe_digest)


def run_integration(campaign: FrozenCampaign, candidates: Sequence[Any], **kwargs: Any) -> IntegrationResult:
    return PortfolioIntegration(campaign, store=kwargs.pop("store", None)).run(candidates, **kwargs)


run_campaign = run_integration
run_portfolio_integration = run_integration
PortfolioOptimizerIntegration = PortfolioIntegration
M7Integration = PortfolioIntegration


__all__ = [
    "PASS", "FAIL", "UNKNOWN", "RESEARCH_ONLY", "INSUFFICIENT_EVIDENCE", "PortfolioIntegrationError",
    "FrozenCampaign", "freeze_campaign", "freeze_search", "freeze_m7_campaign", "RiskCheck", "RiskEvaluation",
    "evaluate_research_risk", "research_risk", "evaluate_risk", "Attempt", "ResumeSnapshot", "IntegrationResult", "PortfolioIntegration",
    "PortfolioOptimizerIntegration", "M7Integration", "run_integration", "run_campaign", "run_portfolio_integration",
]
