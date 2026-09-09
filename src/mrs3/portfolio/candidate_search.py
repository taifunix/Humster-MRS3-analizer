"""Deterministic, fixture-only portfolio candidate composition search."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import product
from types import MappingProxyType
from typing import Any, Mapping, Sequence

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(_freeze(member) for member in self.members))


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


def search_portfolio_candidates(
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


__all__ = [
    "PASS",
    "FAIL",
    "CANDIDATE_SCHEMA_VERSION",
    "Candidate",
    "Excluded",
    "ExcludedCandidate",
    "PortfolioCandidate",
    "SearchResult",
    "search_portfolio_candidates",
]
