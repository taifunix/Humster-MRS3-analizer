"""Deterministic, fixture-only portfolio candidate composition search."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from numbers import Real
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import combinations, product
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .config import RANKING_METRICS


PASS = "PASS"
FAIL = "FAIL"
CANDIDATE_SCHEMA_VERSION = "portfolio_candidate_v1"

_METRIC_KEYS = {
    "net_pnl": "total_pnl",
    "max_dd_pct": "max_drawdown_pct",
    "recovery_factor": "recovery_factor",
}
_IDENTITY_FIELDS = (
    "user_status",
    "symbol",
    "side",
    "strategy_id",
    "result_id",
    "user_rank",
    "total_pnl",
    "max_drawdown_pct",
    "recovery_factor",
    "position_size_usdt",
    "sizing_digest",
    "reference_digest",
    "spread_status",
    "spread_mean_bps",
)

_PAYLOAD_FIELDS = frozenset({
    "equity", "equity_series", "equity_path", "minute_equity", "actions",
    "action_series", "strategy_actions", "minute_actions", "source_provenance",
})
_SOURCE_KEY = "_source_key"
_PROCESS_EVALUATOR: Callable[..., Any] | None = None
_PROCESS_CONTEXT: Any = None


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (int, str, Decimal)):
        raise TypeError(f"{field} requires an exact Decimal-compatible value")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    if positive and number <= 0:
        raise ValueError(f"{field} must be positive")
    return number


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(item) for item in value), key=repr)
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _compact_member(member: Mapping[str, Any]) -> dict[str, Any]:
    """Keep candidate identity and scalar sizing facts, never path payloads."""
    result = {key: value for key, value in member.items() if key not in _PAYLOAD_FIELDS}
    result[_SOURCE_KEY] = _source_key(member)
    return result


def _source_key(member: Mapping[str, Any]) -> str:
    return ":".join(
        str(member.get(field, ""))
        for field in ("symbol", "side", "strategy_id", "result_id")
    )


def _evaluation_member(member: Mapping[str, Any]) -> dict[str, Any]:
    """Build one shared evaluator row; retain daily equity, drop unused series."""
    result = {
        key: value
        for key, value in member.items()
        if key not in _PAYLOAD_FIELDS or key in {"equity", "equity_series"}
    }
    if "equity" not in result and "equity_series" in result:
        result["equity"] = result.pop("equity_series")
    result.pop("equity_series", None)
    result[_SOURCE_KEY] = _source_key(member)
    return result


def _pickle_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _pickle_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_pickle_plain(item) for item in value)
    if isinstance(value, list):
        return [_pickle_plain(item) for item in value]
    return value


def _compact_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Drop evaluator-owned bulk payloads before retaining a search result."""
    return {
        key: value
        for key, value in metrics.items()
        if key not in _PAYLOAD_FIELDS and key != "members"
    }


def _evaluator_arity(evaluator: Callable[..., Any] | None) -> int:
    if evaluator is None:
        return 1
    try:
        parameters = tuple(inspect.signature(evaluator).parameters.values())
    except (TypeError, ValueError):
        return 1
    return 2 if any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters) or len(parameters) > 1 else 1


def _process_initializer(evaluator: Callable[..., Any], context: Any) -> None:
    global _PROCESS_EVALUATOR, _PROCESS_CONTEXT
    _PROCESS_EVALUATOR = evaluator
    _PROCESS_CONTEXT = context


def _process_task(task: tuple[int, tuple[Mapping[str, Any], ...]]) -> tuple[int, Any]:
    index, members = task
    evaluator = _PROCESS_EVALUATOR
    if evaluator is None:
        return index, {"status": "UNKNOWN", "reason": "PROCESS_EVALUATOR_UNAVAILABLE"}
    try:
        return index, evaluator(members, _PROCESS_CONTEXT)
    except (ArithmeticError, KeyError, TypeError, ValueError, OSError):
        return index, {"status": "UNKNOWN", "reason": "PRETEST_EVALUATION_INVALID"}


class _ProcessBatchEvaluator:
    """Small bounded process bridge; the parent owns ordering and accounting."""

    def __init__(self, evaluator: Callable[..., Any], context: Any, workers: int, task_count: int) -> None:
        width = min(max(1, int(workers)), max(1, int(task_count)), os.cpu_count() or 1, 61 if os.name == "nt" else 2**31 - 1)
        self.width = width
        self.pool = ProcessPoolExecutor(
            max_workers=width,
            initializer=_process_initializer,
            initargs=(evaluator, context),
        )

    def __call__(self, tasks: Sequence[tuple[int, tuple[Mapping[str, Any], ...]]]) -> tuple[tuple[int, Any], ...]:
        futures = [self.pool.submit(_process_task, task) for task in tasks]
        try:
            return tuple(sorted((future.result() for future in as_completed(futures)), key=lambda item: item[0]))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.pool.shutdown(wait=True)


def _semantic_facts(member: Mapping[str, Any]) -> dict[str, Any]:
    return {field: member.get(field) for field in _IDENTITY_FIELDS}


def _member_key(member: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        str(member["symbol"]),
        0 if member["side"] == "LONG" else 1,
        _canonical_json(_semantic_facts(member)),
    )


def _identity(profile_id: str, scenario_id: str, members: Sequence[Mapping[str, Any]]) -> str:
    facts = sorted((_semantic_facts(member) for member in members), key=lambda item: _canonical_json(item))
    payload = {"profile_id": profile_id, "scenario_id": scenario_id, "members": facts}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PortfolioCandidate:
    schema_version: str
    profile_id: str
    scenario_id: str
    identity: str
    members: tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Any] = MappingProxyType({})
    status: str = PASS

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(_freeze(member) for member in self.members))
        object.__setattr__(self, "metrics", _freeze(dict(self.metrics)))

    @property
    def proxy_pnl(self) -> Any:
        return self.metrics.get("proxy_pnl_usdt", self.metrics.get("total_pnl"))

    @property
    def proxy_max_drawdown_pct(self) -> Any:
        return self.metrics.get("proxy_max_drawdown_pct", self.metrics.get("max_drawdown_pct"))

    @property
    def proxy_recovery_factor(self) -> Any:
        return self.metrics.get("proxy_recovery_factor", self.metrics.get("recovery_factor"))


@dataclass(frozen=True, slots=True)
class Excluded:
    strategy: Any
    result: Any
    symbol: str
    side: str
    reason: str

    @property
    def strategy_id(self) -> Any:
        return self.strategy

    @property
    def result_id(self) -> Any:
        return self.result


@dataclass(frozen=True, slots=True)
class SearchResult:
    status: str
    reason: str | None = None
    total_combinations: int = 0
    candidates: tuple[PortfolioCandidate, ...] = ()
    excluded: tuple[Excluded, ...] = ()
    evaluated: int = 0
    required_budget: int = 0
    suggested_budget: int = 0
    warnings: tuple[str, ...] = ()
    mode: str = "LEGACY"


Candidate = PortfolioCandidate
ExcludedCandidate = Excluded


def _row_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None


def _excluded(row: Mapping[str, Any], reason: str) -> Excluded:
    return Excluded(
        _freeze(row.get("strategy_id")),
        _freeze(row.get("result_id")),
        row.get("symbol", ""),
        row.get("side", ""),
        reason,
    )


def _excluded_key(item: Excluded) -> tuple[str, str, str, str, str]:
    return (item.symbol, item.side, str(item.strategy), str(item.result), item.reason)


def _validate_options(
    selected_symbols: Sequence[str],
    profile_id: str,
    scenario_id: str,
    individual_max_dd_pct: Any,
    individual_net_pnl_min_exclusive: Any,
    top_n_per_direction: int,
    max_enumerated_combinations: int,
) -> tuple[tuple[str, ...], Decimal, Decimal]:
    if isinstance(selected_symbols, (str, bytes)):
        raise TypeError("selected_symbols must be a sequence of unique non-empty strings")
    symbols = tuple(selected_symbols)
    if not symbols:
        raise ValueError("selected_symbols must be non-empty")
    if any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
        raise ValueError("selected_symbols must contain only non-empty strings")
    if len(set(symbols)) != len(symbols):
        raise ValueError("selected_symbols must be unique")
    if not isinstance(profile_id, str) or profile_id not in RANKING_METRICS:
        raise ValueError(f"unsupported profile_id: {profile_id}")
    _required_text(scenario_id, "scenario_id")
    for value, field in (
        (top_n_per_direction, "top_n_per_direction"),
        (max_enumerated_combinations, "max_enumerated_combinations"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
    return (
        tuple(sorted(symbols)),
        _decimal(individual_max_dd_pct, "individual_max_dd_pct", positive=True),
        _decimal(individual_net_pnl_min_exclusive, "individual_net_pnl_min_exclusive"),
    )


def _ranking_key(row: Mapping[str, Any], profile_id: str) -> tuple[Any, ...]:
    key: list[Any] = []
    for descriptor in RANKING_METRICS[profile_id]:
        value = row[_METRIC_KEYS[descriptor["field"]]]
        key.append(-value if descriptor["direction"] == "DESC" else value)
    key.append(_canonical_json(_semantic_facts(row)))
    return tuple(key)


def _option_key(option: tuple[Mapping[str, Any], ...]) -> tuple[Any, ...]:
    return tuple(_member_key(member) for member in option)


def _legacy_search_portfolio_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    selected_symbols: Sequence[str],
    profile_id: str,
    scenario_id: str,
    individual_max_dd_pct: Any,
    individual_net_pnl_min_exclusive: Any,
    top_n_per_direction: int,
    max_enumerated_combinations: int,
) -> SearchResult:
    """Filter exact FINALIST rows and enumerate the full bounded composition universe."""

    symbols, dd_limit, pnl_floor = _validate_options(
        selected_symbols,
        profile_id,
        scenario_id,
        individual_max_dd_pct,
        individual_net_pnl_min_exclusive,
        top_n_per_direction,
        max_enumerated_combinations,
    )
    by_symbol: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        symbol: {"LONG": [], "SHORT": []} for symbol in symbols
    }
    excluded: list[Excluded] = []
    runtime_missing = False

    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        symbol = raw.get("symbol")
        if not isinstance(symbol, str) or symbol not in by_symbol:
            continue
        side = raw.get("side")
        side = side.upper() if isinstance(side, str) else ""
        row = dict(raw)
        row["side"] = side
        if raw.get("user_status") != "FINALIST":
            excluded.append(_excluded(row, "USER_STATUS_NOT_FINALIST"))
            continue
        if side not in {"LONG", "SHORT"}:
            excluded.append(_excluded(row, "INVALID_DIRECTION"))
            continue
        try:
            row["position_size_usdt"] = _decimal(raw.get("position_size_usdt"), "position_size_usdt", positive=True)
            row["sizing_digest"] = _required_text(raw.get("sizing_digest"), "sizing_digest")
            row["reference_digest"] = _required_text(raw.get("reference_digest"), "reference_digest")
        except (TypeError, ValueError):
            runtime_missing = True
            excluded.append(_excluded(row, "MISSING_RUNTIME_FACTS"))
            continue
        try:
            row["total_pnl"] = _decimal(_row_value(raw, "total_pnl", "net_pnl"), "total_pnl")
            row["max_drawdown_pct"] = _decimal(_row_value(raw, "max_drawdown_pct", "max_dd_pct"), "max_drawdown_pct")
            if row["max_drawdown_pct"] < 0:
                raise ValueError("max_drawdown_pct must be non-negative")
            row["recovery_factor"] = _decimal(raw.get("recovery_factor"), "recovery_factor")
        except (TypeError, ValueError):
            excluded.append(_excluded(row, "INVALID_RANKING_FACTS"))
            continue
        if row["total_pnl"] <= pnl_floor:
            excluded.append(_excluded(row, "INDIVIDUAL_PNL_BELOW_FLOOR"))
        elif row["max_drawdown_pct"] > dd_limit:
            excluded.append(_excluded(row, "INDIVIDUAL_DD_EXCEEDED"))
        else:
            by_symbol[symbol][side].append(_freeze(row))

    if runtime_missing:
        return SearchResult(FAIL, "MISSING_RUNTIME_FACTS", 0, (), tuple(sorted(excluded, key=_excluded_key)))

    for symbol in symbols:
        for side in ("LONG", "SHORT"):
            values = sorted(by_symbol[symbol][side], key=lambda row: _ranking_key(row, profile_id))
            by_symbol[symbol][side] = values[:top_n_per_direction]
            excluded.extend(_excluded(row, "DIRECTION_TOP_N") for row in values[top_n_per_direction:])

    options_by_symbol: list[tuple[tuple[Mapping[str, Any], ...], ...]] = []
    for symbol in symbols:
        longs = by_symbol[symbol]["LONG"]
        shorts = by_symbol[symbol]["SHORT"]
        options = [*( (row,) for row in longs), *((row,) for row in shorts)]
        options.extend((long, short) for long in longs for short in shorts)
        options_by_symbol.append(((), *sorted(options, key=_option_key)))

    if not any(options[1:] for options in options_by_symbol):
        return SearchResult(
            FAIL,
            "INSUFFICIENT_DIRECTIONAL_UNIVERSE",
            0,
            (),
            tuple(sorted(excluded, key=_excluded_key)),
        )

    total = 1
    for options in options_by_symbol:
        total *= len(options)
    total -= 1
    if total > max_enumerated_combinations:
        return SearchResult(
            FAIL,
            "COMBINATION_LIMIT_EXCEEDED",
            total,
            (),
            tuple(sorted(excluded, key=_excluded_key)),
        )

    combinations = product(*options_by_symbol)
    candidates: list[PortfolioCandidate] = []
    for selected in combinations:
        members = tuple(member for option in selected for member in option)
        if not members:
            continue
        members = tuple(sorted(members, key=_member_key))
        candidates.append(
            PortfolioCandidate(
                CANDIDATE_SCHEMA_VERSION,
                profile_id,
                scenario_id,
                _identity(profile_id, scenario_id, members),
                members,
            )
        )
    return SearchResult(
        PASS,
        None,
        total,
        tuple(candidates),
        tuple(sorted(excluded, key=_excluded_key)),
    )


_PRETEST_PROFILE_ORDER = {
    "AGGRESSIVE": ("proxy_pnl_usdt", "proxy_recovery_factor", "proxy_max_drawdown_pct"),
    "BALANCED": ("proxy_recovery_factor", "proxy_pnl_usdt", "proxy_max_drawdown_pct"),
    "CONSERVATIVE": ("proxy_recovery_factor", "proxy_max_drawdown_pct", "proxy_pnl_usdt"),
}


def _pretest_decimal(value: Any, field: str, *, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal, Real)):
        return default
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default
    return result if result.is_finite() else default


def _pretest_metric(value: Any, *keys: str, default: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                parsed = _pretest_decimal(value[key], key)
                if parsed is not None:
                    return parsed
    else:
        for key in keys:
            parsed = _pretest_decimal(getattr(value, key, None), key)
            if parsed is not None:
                return parsed
    return default


def _pretest_option_key(option: tuple[Mapping[str, Any], ...]) -> tuple[Any, ...]:
    return tuple(_member_key(member) for member in option)


def _pretest_candidate_key(candidate: PortfolioCandidate) -> tuple[Any, ...]:
    return tuple(sorted((_member_key(member) for member in candidate.members)))


def _pretest_profile_key(candidate: PortfolioCandidate, profile_id: str) -> tuple[Any, ...]:
    metrics = candidate.metrics
    key: list[Any] = []
    for field in _PRETEST_PROFILE_ORDER.get(profile_id, _PRETEST_PROFILE_ORDER["BALANCED"]):
        value = _pretest_metric(metrics, field, {"proxy_pnl_usdt": "total_pnl", "proxy_recovery_factor": "recovery_factor", "proxy_max_drawdown_pct": "max_drawdown_pct"}.get(field, field))
        # Larger PnL/recovery is better; DD is lower-is-better.
        key.append(-value if field != "proxy_max_drawdown_pct" else value)
    key.append(-_pretest_metric(metrics, "proxy_reserve_usdt", "reserve_usdt"))
    def member_tie(item: Mapping[str, Any]) -> tuple[Any, ...]:
        rank = _pretest_decimal(item.get("user_rank"), "user_rank")
        rank_key = (0, rank) if rank is not None else (1, Decimal(0))
        return (
            rank_key,
            _pretest_decimal(item.get("total_pnl", item.get("net_pnl")), "total_pnl", default=Decimal("0")),
            _pretest_decimal(item.get("max_drawdown_pct", item.get("max_dd_pct")), "max_drawdown_pct", default=Decimal("0")),
            _pretest_decimal(item.get("recovery_factor"), "recovery_factor", default=Decimal("0")),
            str(item.get("strategy_id", "")),
            str(item.get("result_id", "")),
        )
    key.append(tuple(member_tie(item) for item in candidate.members))
    key.append(candidate.identity)
    return tuple(key)


def _pretest_dominates(left: PortfolioCandidate, right: PortfolioCandidate) -> bool:
    values = (
        (_pretest_metric(left.metrics, "proxy_pnl_usdt", "total_pnl"), _pretest_metric(right.metrics, "proxy_pnl_usdt", "total_pnl"), True),
        (_pretest_metric(left.metrics, "proxy_recovery_factor", "recovery_factor"), _pretest_metric(right.metrics, "proxy_recovery_factor", "recovery_factor"), True),
        (_pretest_metric(left.metrics, "proxy_reserve_usdt", "reserve_usdt"), _pretest_metric(right.metrics, "proxy_reserve_usdt", "reserve_usdt"), True),
        (_pretest_metric(left.metrics, "proxy_max_drawdown_pct", "max_drawdown_pct"), _pretest_metric(right.metrics, "proxy_max_drawdown_pct", "max_drawdown_pct"), False),
    )
    no_worse = all(a >= b if larger else a <= b for a, b, larger in values)
    strictly = any(a > b if larger else a < b for a, b, larger in values)
    return no_worse and strictly


def _invoke_evaluator(evaluator: Callable[..., Any] | None, members: tuple[Mapping[str, Any], ...], candidate: PortfolioCandidate, arity: int = 1) -> Mapping[str, Any]:
    if evaluator is None:
        # A missing path is an invalid pretest evaluation, and still consumes
        # one budget unit.  Callers that have source paths inject the proxy
        # evaluator through the explicit seam below.
        return {"status": "UNKNOWN", "reason": "EQUITY_PATH_UNAVAILABLE"}
    try:
        if arity > 1:
            result = evaluator(members, candidate)
        else:
            result = evaluator(members)
    except (ArithmeticError, KeyError, TypeError, ValueError, OSError):
        return {"status": "UNKNOWN", "reason": "PRETEST_EVALUATION_INVALID"}
    if isinstance(result, Mapping):
        return dict(result)
    if hasattr(result, "as_dict"):
        value = result.as_dict()
        return dict(value) if isinstance(value, Mapping) else {"status": "UNKNOWN", "reason": "INVALID_EVALUATION"}
    return {"status": "UNKNOWN", "reason": "INVALID_EVALUATION"}


def _default_pretest_evaluator(members: Sequence[Mapping[str, Any]], *, campaign_equity: Any = 1) -> Mapping[str, Any]:
    """Evaluate source paths when the caller did not inject a test seam."""
    from .pretest_proxy import compute_proxy_metrics

    try:
        campaign = _pretest_decimal(campaign_equity, "campaign_equity")
        if campaign is None or campaign <= 0:
            return {"status": "UNKNOWN", "reason": "INVALID_CAMPAIGN_EQUITY"}
        paths: list[tuple[Mapping[str, Any], ...]] = []
        for member in members:
            path = member.get("equity", member.get("equity_series", member.get("equity_path", ())))
            initial = member.get("initial_balance", member.get("source_initial_balance"))
            orders = member.get("strategy_orders", member.get("orders", member.get("opening_orders", ())))
            tested = None
            if orders:
                lot_sum = sum((_pretest_decimal(item.get("lot_x"), "lot_x", default=Decimal(0)) or Decimal(0) for item in orders if isinstance(item, Mapping)), Decimal(0))
                tested = (initial or 0) * lot_sum
            if tested is None:
                tested = member.get("tested_size_usdt")
            actual = member.get("actual_size_usdt", member.get("position_size_usdt"))
            metrics = compute_proxy_metrics(path, campaign_equity=campaign, source_initial_balance=initial, tested_size_usdt=tested, actual_size_usdt=actual)
            if metrics.status != "PASS":
                return metrics.as_dict()
            paths.append(metrics.equity_path)
        if not paths or any(not path for path in paths):
            return {"status": "UNKNOWN", "reason": "EQUITY_PATH_UNAVAILABLE"}
        timestamps = tuple(point["timestamp_utc"] for point in paths[0])
        if any(tuple(point["timestamp_utc"] for point in path) != timestamps for path in paths[1:]):
            return {"status": "UNKNOWN", "reason": "EQUITY_PERIOD_MISMATCH"}
        combined = tuple({"timestamp_utc": timestamp, "equity": campaign + sum((path[index]["equity"] - campaign for path in paths), Decimal(0))} for index, timestamp in enumerate(timestamps))
        return compute_proxy_metrics(combined, campaign_equity=campaign).as_dict()
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return {"status": "UNKNOWN", "reason": "EQUITY_PATH_UNAVAILABLE"}


def search_pretest_proxy(
    rows: Sequence[Mapping[str, Any]],
    *,
    selected_symbols: Sequence[str],
    profile_id: str,
    scenario_id: str,
    max_candidates: int,
    max_enumerated_combinations: int = 100000,
    evaluator: Callable[..., Any] | None = None,
    evaluate: Callable[..., Any] | None = None,
    source_total_pnl_floor: Any | None = None,
    campaign_equity: Any = 1,
    evaluation_budget: int | None = None,
    budget: int | None = None,
    workers: int = 1,
    batch_evaluator: Callable[..., Any] | None = None,
    process_evaluator: Callable[..., Any] | None = None,
    process_context: Any = None,
) -> SearchResult:
    """Bounded PRETEST_PROXY composition search.

    The mandatory singleton and two-symbol evaluations establish a useful
    lower bound before the optional higher-cardinality beam search.
    """
    if isinstance(selected_symbols, (str, bytes)) or not selected_symbols:
        raise ValueError("selected_symbols must be a non-empty sequence")
    symbols = tuple(sorted(set(str(symbol) for symbol in selected_symbols)))
    if len(symbols) != len(tuple(selected_symbols)) or any(not symbol for symbol in symbols):
        raise ValueError("selected_symbols must contain unique non-empty strings")
    if profile_id not in _PRETEST_PROFILE_ORDER:
        raise ValueError("unsupported profile_id")
    if not isinstance(max_candidates, int) or isinstance(max_candidates, bool) or max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    if evaluation_budget is not None:
        max_enumerated_combinations = evaluation_budget
    if budget is not None:
        max_enumerated_combinations = budget
    if not isinstance(max_enumerated_combinations, int) or isinstance(max_enumerated_combinations, bool) or max_enumerated_combinations <= 0:
        raise ValueError("max_enumerated_combinations must be positive")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    floor = _pretest_decimal(source_total_pnl_floor, "source_total_pnl_floor")
    by_symbol: dict[str, list[Mapping[str, Any]]] = {symbol: [] for symbol in symbols}
    source_by_key: dict[str, Mapping[str, Any]] = {}
    excluded: list[Excluded] = []
    for raw in rows:
        if not isinstance(raw, Mapping) or raw.get("symbol") not in by_symbol:
            continue
        row = dict(raw)
        side = str(row.get("side", "")).upper()
        if row.get("user_status") != "FINALIST":
            excluded.append(_excluded(row, "USER_STATUS_NOT_FINALIST"))
            continue
        if side not in {"LONG", "SHORT"}:
            excluded.append(_excluded(row, "INVALID_DIRECTION"))
            continue
        row["side"] = side
        if floor is not None:
            pnl = _pretest_decimal(row.get("total_pnl", row.get("net_pnl")), "total_pnl")
            if pnl is None or pnl <= floor:
                excluded.append(_excluded(row, "SOURCE_TOTAL_PNL_BELOW_FLOOR"))
                continue
        frozen = _freeze(row)
        key = _source_key(frozen)
        source_by_key[key] = _freeze(_evaluation_member(frozen))
        by_symbol[str(row["symbol"])].append(_freeze(_compact_member(frozen)))
    by_symbol = {
        symbol: tuple(sorted({ _canonical_json(_semantic_facts(value)): value for value in values }.values(), key=_member_key))
        for symbol, values in by_symbol.items()
        if values
    }
    if not by_symbol:
        return SearchResult(FAIL, "INSUFFICIENT_DIRECTIONAL_UNIVERSE", 0, (), tuple(sorted(excluded, key=_excluded_key)), 0, 0, 0, (), "PRETEST_PROXY")

    options: dict[str, tuple[tuple[Mapping[str, Any], ...], ...]] = {}
    for symbol, values in by_symbol.items():
        longs = tuple(row for row in values if row["side"] == "LONG")
        shorts = tuple(row for row in values if row["side"] == "SHORT")
        choices = [*( (row,) for row in longs), *((row,) for row in shorts), *((long, short) for long in longs for short in shorts)]
        options[symbol] = tuple(sorted(choices, key=_pretest_option_key))
    k_values = {symbol: len(value) for symbol, value in options.items()}
    required = sum(k_values.values()) + sum(k_values[left] * k_values[right] for index, left in enumerate(k_values) for right in tuple(k_values)[index + 1:])
    suggested = max(required, math.ceil(required * 1.25))
    if max_enumerated_combinations < required:
        return SearchResult(FAIL, "PRETEST_BUDGET_TOO_SMALL", required, (), tuple(sorted(excluded, key=_excluded_key)), 0, required, suggested, (f"configured={max_enumerated_combinations}", f"required={required}", f"suggested={suggested}"), "PRETEST_PROXY")

    evaluator = evaluator or evaluate
    if evaluator is None:
        evaluator = lambda members: _default_pretest_evaluator(members, campaign_equity=campaign_equity)
    evaluator_arity = _evaluator_arity(evaluator)
    seen: set[str] = set()
    top_candidates: list[PortfolioCandidate] = []
    level_best: dict[int, dict[frozenset[str], PortfolioCandidate]] = {}
    charged = 0

    def hydrate(members: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
        return tuple(source_by_key.get(str(member.get(_SOURCE_KEY)), member) for member in members)

    def reserve_members(members: Sequence[Mapping[str, Any]]) -> tuple[int, tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]] | None:
        nonlocal charged
        ordered = tuple(sorted(members, key=_member_key))
        if not ordered:
            return None
        identity = _identity(profile_id, scenario_id, ordered)
        if identity in seen or charged >= max_enumerated_combinations:
            return None
        seen.add(identity)
        charged += 1
        return charged, ordered, hydrate(ordered)

    def commit(reservation: tuple[int, tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]], raw_metrics: Any) -> PortfolioCandidate:
        _generation, ordered, evaluation_rows = reservation
        candidate = PortfolioCandidate(CANDIDATE_SCHEMA_VERSION, profile_id, scenario_id, _identity(profile_id, scenario_id, ordered), ordered)
        if raw_metrics is None:
            metrics = _invoke_evaluator(evaluator, evaluation_rows, candidate, evaluator_arity)
        elif isinstance(raw_metrics, Mapping):
            metrics = dict(raw_metrics)
        else:
            metrics = {"status": "UNKNOWN", "reason": "PRETEST_EVALUATION_INVALID"}
        status = str(metrics.get("status", "PASS"))
        sized_members = metrics.get("members")
        if isinstance(sized_members, Sequence) and not isinstance(sized_members, (str, bytes)) and all(isinstance(item, Mapping) for item in sized_members):
            final_members = tuple(sorted((_compact_member(item) for item in sized_members), key=_member_key))
        else:
            final_members = ordered
        final_identity = _identity(profile_id, scenario_id, final_members)
        candidate = PortfolioCandidate(CANDIDATE_SCHEMA_VERSION, profile_id, scenario_id, final_identity, final_members, _compact_metrics(metrics), status)
        if status in {"PASS", "OK"}:
            level = len({str(member["symbol"]) for member in final_members})
            bucket = level_best.setdefault(level, {})
            symbol_set = frozenset(str(member["symbol"]) for member in final_members)
            previous = bucket.get(symbol_set)
            if previous is None or _pretest_profile_key(candidate, profile_id) < _pretest_profile_key(previous, profile_id):
                bucket[symbol_set] = candidate
            top_candidates.append(candidate)
            top_candidates.sort(key=lambda item: _pretest_profile_key(item, profile_id))
            del top_candidates[max_candidates:]
        return candidate

    def evaluate_serial(members: Sequence[Mapping[str, Any]]) -> PortfolioCandidate | None:
        reservation = reserve_members(members)
        if reservation is None:
            return None
        return commit(reservation, None)

    def evaluate_batch(proposals: Sequence[Sequence[Mapping[str, Any]]], generation: int, callback: Callable[..., Any]) -> None:
        reservations: list[tuple[int, tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]] = []
        tasks: list[tuple[int, tuple[Mapping[str, Any], ...]]] = []
        for proposal in proposals:
            reservation = reserve_members(proposal)
            if reservation is not None:
                reservations.append(reservation)
                tasks.append((reservation[0] + generation, tuple(_pickle_plain(item) for item in reservation[2])))
        if not reservations:
            return
        raw_results = callback(tuple(tasks))
        by_index: dict[int, Any] = {}
        positional: list[Any] = []
        for item in raw_results or ():
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], int):
                by_index[item[0]] = item[1]
            else:
                positional.append(item)
        if by_index and len(by_index) == len(tasks):
            results = tuple(by_index[index] for index, _ in tasks)
        else:
            results = tuple(positional or raw_results or ())
        for index, reservation in enumerate(reservations):
            commit(reservation, results[index] if index < len(results) else None)

    usable_symbols = tuple(sorted(options))
    full_count = 1
    for symbol in usable_symbols:
        full_count *= 1 + len(options[symbol])
    full_count -= 1
    batch_limit = max(1, min(4 * workers, 64))
    process_batch = None
    if process_evaluator is not None and workers > 1:
        process_batch = _ProcessBatchEvaluator(
            process_evaluator,
            _pickle_plain(process_context),
            workers,
            min(batch_limit, required),
        )
    batch_callback = batch_evaluator or process_batch

    def dispatch(proposals: Sequence[Sequence[Mapping[str, Any]]]) -> None:
        if not proposals:
            return
        if batch_callback is None:
            for proposal in proposals:
                evaluate_serial(proposal)
        else:
            for start in range(0, len(proposals), batch_limit):
                evaluate_batch(proposals[start:start + batch_limit], 0, batch_callback)

    # Every singleton and every two-symbol composition is mandatory.
    pending: list[tuple[Mapping[str, Any], ...]] = []
    for symbol in usable_symbols:
        for option in options[symbol]:
            if batch_callback is None:
                dispatch((option,))
            else:
                pending.append(option)
                if len(pending) >= batch_limit:
                    dispatch(tuple(pending))
                    pending.clear()
    for index, left in enumerate(usable_symbols):
        for right in usable_symbols[index + 1:]:
            for left_option in options[left]:
                for right_option in options[right]:
                    proposal = (*left_option, *right_option)
                    if batch_callback is None:
                        dispatch((proposal,))
                    else:
                        pending.append(proposal)
                        if len(pending) >= batch_limit:
                            dispatch(tuple(pending))
                            pending.clear()
    dispatch(tuple(pending))

    def cardinality(candidate: PortfolioCandidate) -> int:
        return len({str(member["symbol"]) for member in candidate.members})

    def beam_for(level: int) -> list[PortfolioCandidate]:
        # Diversity is for expansion only: keep one profile-best parent per
        # symbol set, while final output retains every non-dominated variant.
        by_set = level_best.get(level, {})
        ordered = sorted(by_set.values(), key=lambda candidate: _pretest_profile_key(candidate, profile_id))
        beam: list[PortfolioCandidate] = []
        for candidate in ordered:
            if any(_pretest_dominates(other, candidate) for other in beam):
                continue
            beam.append(candidate)
            if len(beam) >= beam_width:
                break
        return beam

    beam_width = min(256, max(16, 4 * max_candidates))

    def source_for(candidate: PortfolioCandidate) -> tuple[Mapping[str, Any], ...]:
        return hydrate(candidate.members)

    def option_for(source: Sequence[Mapping[str, Any]], symbol: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(member for member in source if str(member.get("symbol")) == symbol)

    def add_neighbors(parent: PortfolioCandidate):
        source = source_for(parent)
        included = {str(member["symbol"]) for member in source}
        for symbol in usable_symbols:
            if symbol in included:
                continue
            for option in options[symbol]:
                yield tuple(member for member in source if str(member.get("symbol")) != symbol) + option

    def local_neighbors(source: Sequence[Mapping[str, Any]]):
        included = tuple(sorted({str(member["symbol"]) for member in source}))
        # Swap one included symbol for one unused symbol.
        for old_symbol in included:
            for new_symbol in usable_symbols:
                if new_symbol in included:
                    continue
                for option in options[new_symbol]:
                    yield tuple(member for member in source if str(member.get("symbol")) != old_symbol) + option
        # Replace an included symbol's option without changing cardinality.
        for symbol in included:
            current = option_for(source, symbol)
            for option in options[symbol]:
                if _pretest_option_key(option) == _pretest_option_key(current):
                    continue
                yield tuple(member for member in source if str(member.get("symbol")) != symbol) + option

    # Higher-cardinality search is a bounded beam. Every parent contributes
    # one proposal per round, and each add is followed by its local
    # replace/swap neighbors before the next round, so local moves remain
    # reachable under a tight budget without reverting to a Cartesian walk.
    for level in range(3, len(usable_symbols) + 1):
        if charged >= max_enumerated_combinations:
            break
        parents = beam_for(level - 1)[:beam_width]
        if not parents:
            break
        def operations_for(parent: PortfolioCandidate):
            for proposal in add_neighbors(parent):
                yield proposal
                yield from local_neighbors(proposal)

        operations = [iter(operations_for(parent)) for parent in parents]
        while charged < max_enumerated_combinations:
            proposals = []
            for operation in operations:
                try:
                    proposals.append(next(operation))
                except StopIteration:
                    pass
            if not proposals:
                break
            dispatch(proposals)

    if process_batch is not None:
        process_batch.close()
    selected = tuple(top_candidates)
    warnings = ("PRETEST_BUDGET_EXHAUSTED",) if charged >= max_enumerated_combinations and full_count > charged else ()
    return SearchResult(
        PASS if selected else FAIL,
        None if selected else "NO_VALID_PRETEST_CANDIDATES",
        charged,
        selected,
        tuple(sorted(excluded, key=_excluded_key)),
        charged,
        required,
        suggested,
        warnings,
        "PRETEST_PROXY",
    )


def search_portfolio_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    selected_symbols: Sequence[str],
    profile_id: str,
    scenario_id: str = "PRETEST",
    individual_max_dd_pct: Any = "1",
    individual_net_pnl_min_exclusive: Any = "0",
    top_n_per_direction: int = 1,
    max_enumerated_combinations: int = 100000,
    max_candidates: int | None = None,
    evaluator: Callable[..., Any] | None = None,
    evaluate: Callable[..., Any] | None = None,
    source_total_pnl_floor: Any | None = None,
    mode: str | None = None,
    workers: int = 1,
    batch_evaluator: Callable[..., Any] | None = None,
    process_evaluator: Callable[..., Any] | None = None,
    process_context: Any = None,
    **kwargs: Any,
) -> SearchResult:
    """Compatibility entry point with PRETEST_PROXY selected by max_candidates."""
    evaluator = evaluator or evaluate or kwargs.get("evaluation") or kwargs.get("evaluate_composition") or kwargs.get("proxy_evaluator")
    max_candidates = max_candidates if max_candidates is not None else kwargs.get("top_n_candidates")
    budget = kwargs.get("budget", kwargs.get("evaluation_budget", max_enumerated_combinations))
    try:
        max_enumerated_combinations = int(budget)
    except (TypeError, ValueError):
        pass
    source_total_pnl_floor = source_total_pnl_floor if source_total_pnl_floor is not None else kwargs.get("pnl_floor", kwargs.get("source_pnl_floor"))
    if max_candidates is not None or mode == "PRETEST_PROXY" or evaluator is not None:
        return search_pretest_proxy(
            rows,
            selected_symbols=selected_symbols,
            profile_id=profile_id,
            scenario_id=scenario_id,
            max_candidates=max_candidates or 1,
            max_enumerated_combinations=max_enumerated_combinations,
            evaluator=evaluator,
            source_total_pnl_floor=source_total_pnl_floor if source_total_pnl_floor is not None else individual_net_pnl_min_exclusive,
            campaign_equity=kwargs.get("campaign_equity", kwargs.get("portfolio_equity", 1)),
            workers=workers,
            batch_evaluator=batch_evaluator,
            process_evaluator=process_evaluator,
            process_context=process_context,
        )
    return _legacy_search_portfolio_candidates(
        rows,
        selected_symbols=selected_symbols,
        profile_id=profile_id,
        scenario_id=scenario_id,
        individual_max_dd_pct=individual_max_dd_pct,
        individual_net_pnl_min_exclusive=individual_net_pnl_min_exclusive,
        top_n_per_direction=top_n_per_direction,
        max_enumerated_combinations=max_enumerated_combinations,
    )


__all__ = [
    "PASS",
    "FAIL",
    "CANDIDATE_SCHEMA_VERSION",
    "Candidate",
    "Excluded",
    "ExcludedCandidate",
    "PortfolioCandidate",
    "SearchResult",
    "search_pretest_proxy",
    "search_portfolio_candidates",
]
