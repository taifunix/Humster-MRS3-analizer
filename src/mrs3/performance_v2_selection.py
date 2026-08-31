"""Typed, closed request/config contract for Performance v2 finalist selection."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Literal, Mapping


StageScope = Literal["pair_side", "pair_side_timeframe"]

_STAGE_IDS = frozenset((
    "filter_holding_outlier",
    "filter_low_trades",
    "ab_deterioration",
    "pareto_dd5_balanced",
    "pareto_plateau_points_per_order",
    "pareto_plateau_points_total",
    "pareto_efficiency_shift",
    "pareto_dd5_holding",
    "pareto_dd5_close_ma",
    "pareto_dd5_first_shift",
    "pareto_conditional_close_ma",
    "pareto_primary",
    "pareto_dd5_capital",
))
_SCOPES = frozenset(("pair_side", "pair_side_timeframe"))


class PerformanceV2SelectionError(ValueError):
    """Stable error code for an invalid finalist-selection request/config."""


@dataclass(frozen=True, slots=True)
class SelectionStage:
    id: str
    enabled: bool
    scope: StageScope


@dataclass(frozen=True, slots=True)
class SelectionRequest:
    symbol: str
    side: Literal["LONG", "SHORT"]
    stages: tuple[SelectionStage, ...]


@dataclass(frozen=True, slots=True)
class SelectionConfig:
    ab_final_days: int = 14
    ab_return_floor_pct: Decimal = Decimal("5")
    ab_return_divisor: Decimal = Decimal("10")
    ab_win_rate_floor_pct: Decimal = Decimal("58")
    ab_trade_rate_divisor: Decimal = Decimal("7")
    plateau_points_pareto_pnl_multiplier: Decimal = Decimal("2")


def _error(code: str) -> PerformanceV2SelectionError:
    return PerformanceV2SelectionError(code)


def parse_selection_request(payload: Mapping[str, object]) -> SelectionRequest:
    if set(payload) != {"symbol", "side", "stages"}:
        raise _error("INVALID_REQUEST")
    symbol = payload["symbol"]
    side = payload["side"]
    stages = payload["stages"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise _error("INVALID_SYMBOL")
    if side not in {"LONG", "SHORT"}:
        raise _error("INVALID_SIDE")
    if not isinstance(stages, list):
        raise _error("INVALID_STAGES")

    parsed: list[SelectionStage] = []
    seen: set[str] = set()
    for raw in stages:
        if not isinstance(raw, Mapping) or set(raw) != {"id", "enabled", "scope"}:
            raise _error("INVALID_STAGE")
        stage_id, enabled, scope = raw["id"], raw["enabled"], raw["scope"]
        if not isinstance(stage_id, str) or stage_id not in _STAGE_IDS:
            raise _error("UNKNOWN_STAGE")
        if stage_id in seen:
            raise _error("DUPLICATE_STAGE")
        if not isinstance(enabled, bool):
            raise _error("INVALID_STAGE")
        if scope not in _SCOPES:
            raise _error("INVALID_SCOPE")
        seen.add(stage_id)
        parsed.append(SelectionStage(stage_id, enabled, scope))
    return SelectionRequest(symbol.strip(), side, tuple(parsed))


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise _error(f"INVALID_CONFIG_{name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error(f"INVALID_CONFIG_{name}") from None
    if not parsed.is_finite() or parsed <= 0:
        raise _error(f"INVALID_CONFIG_{name}")
    return parsed


def load_selection_config(path: Path) -> SelectionConfig:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        section = raw["unified_performance_v2"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise _error("INVALID_CONFIG") from None
    if not isinstance(section, Mapping):
        raise _error("INVALID_CONFIG")
    selected = section.get("finalist_selection", {})
    if not isinstance(selected, Mapping):
        raise _error("INVALID_CONFIG")
    final_days = selected.get("ab_final_days", 14)
    if isinstance(final_days, bool) or not isinstance(final_days, int) or final_days < 1:
        raise _error("INVALID_CONFIG_ab_final_days")
    return SelectionConfig(
        ab_final_days=final_days,
        ab_return_floor_pct=_positive_decimal(selected.get("ab_return_floor_pct", 5), "ab_return_floor_pct"),
        ab_return_divisor=_positive_decimal(selected.get("ab_return_divisor", 10), "ab_return_divisor"),
        ab_win_rate_floor_pct=_positive_decimal(selected.get("ab_win_rate_floor_pct", 58), "ab_win_rate_floor_pct"),
        ab_trade_rate_divisor=_positive_decimal(selected.get("ab_trade_rate_divisor", 7), "ab_trade_rate_divisor"),
        plateau_points_pareto_pnl_multiplier=_positive_decimal(
            selected.get("plateau_points_pareto_pnl_multiplier", 2),
            "plateau_points_pareto_pnl_multiplier",
        ),
    )
