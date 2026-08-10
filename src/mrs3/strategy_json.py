from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import json
import re
from typing import Iterable, Mapping, Sequence

import pandas as pd

from .config import AlgorithmConfig
from .lots import LotMethod
from .models import Side
from .selection import validate_order_tuple


class StrategyValidationError(ValueError):
    """Raised when generated JSON cannot be traced to a READY source structure."""


def _side_keys(side: Side) -> tuple[str, str, Decimal]:
    if side is Side.LONG:
        return "ma_long", "ma_close_long", Decimal("-1")
    return "ma_short", "ma_close_short", Decimal("1")


def _entry_multiplier(side: Side, shift_bp: int) -> Decimal:
    change = Decimal(shift_bp) / Decimal("10000")
    return Decimal("1") - change if side is Side.LONG else Decimal("1") + change


def _safe_name(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")


def strategy_name(
    structure: Mapping[str, object],
    method: LotMethod,
) -> str:
    return "_".join(
        [
            _safe_name(structure["symbol"]),
            _safe_name(structure["timeframe"]),
            _safe_name(structure["side"]),
            f"{int(structure['order_count'])}ORD",
            f"CMA{int(structure['common_close_ma'])}",
            _safe_name(structure["structure_id"]),
            method.value,
        ]
    )


def generate_strategy(
    template: Mapping[str, object],
    structure: Mapping[str, object],
    lots: Sequence[Decimal],
    method: LotMethod,
    config: AlgorithmConfig,
) -> dict[str, object]:
    generated = deepcopy(dict(template))
    side = Side(str(structure["side"]))
    orders = tuple(structure["orders"])
    if len(orders) != int(structure["order_count"]) or len(lots) != len(orders):
        raise StrategyValidationError("order and lot counts do not match structure")
    active_key, close_key, _ = _side_keys(side)
    try:
        prototype = deepcopy(generated["mrs3"][active_key][0])
    except (KeyError, IndexError, TypeError) as exc:
        raise StrategyValidationError(f"template has no {active_key} prototype") from exc

    entries: list[dict[str, object]] = []
    for index, (order, lot) in enumerate(zip(orders, lots, strict=True), start=1):
        entry = deepcopy(prototype)
        entry["id"] = index
        entry["len"] = int(order["open_ma"])
        entry["multiplier"] = float(_entry_multiplier(side, int(order["shift_bp"])))
        entry["lot_x"] = float(lot)
        entries.append(entry)

    generated["name"] = strategy_name(structure, method)
    generated["is_runing"] = False
    generated["basic"]["strategy"] = "mrs3"
    generated["basic"]["symbol"] = str(structure["symbol"])
    generated["basic"]["time_frame"] = str(structure["timeframe"])
    generated["basic"]["use_long"] = side is Side.LONG
    generated["basic"]["use_short"] = side is Side.SHORT
    generated["mrs3"][active_key] = entries
    generated["mrs3"][close_key]["len"] = int(structure["common_close_ma"])
    generated["mrs3"][close_key]["multiplier"] = float(
        config.close_multiplier_long
        if side is Side.LONG
        else config.close_multiplier_short
    )
    json.dumps(generated, ensure_ascii=False, allow_nan=False)
    return generated


def validate_strategy(
    generated: Mapping[str, object],
    structure: Mapping[str, object],
    source_points: pd.DataFrame,
    config: AlgorithmConfig,
) -> None:
    if structure.get("status") != "READY_MRS3_STRUCTURE":
        raise StrategyValidationError("only READY structures may be exported")
    side = Side(str(structure["side"]))
    active_key, close_key, _ = _side_keys(side)
    orders = tuple(structure["orders"])
    if int(structure.get("order_count", -1)) != len(orders):
        raise StrategyValidationError("structure order_count does not match orders")
    order_sort_keys = [
        (int(order["shift_bp"]), str(order["point_id"])) for order in orders
    ]
    if order_sort_keys != sorted(order_sort_keys):
        raise StrategyValidationError("structure orders must be sorted by shift")
    try:
        entries = tuple(generated["mrs3"][active_key])
    except (KeyError, TypeError) as exc:
        raise StrategyValidationError("active order array is missing") from exc
    if generated.get("is_runing") is not False:
        raise StrategyValidationError("is_runing must be false")
    basic = generated.get("basic", {})
    if not isinstance(basic, Mapping) or (
        basic.get("strategy") != "mrs3"
        or str(basic.get("symbol")) != str(structure["symbol"])
        or str(basic.get("time_frame")) != str(structure["timeframe"])
    ):
        raise StrategyValidationError("generated basic metadata does not match structure")
    expected_names = {strategy_name(structure, method) for method in LotMethod}
    if generated.get("name") not in expected_names:
        raise StrategyValidationError("strategy name is not derived from the source structure")
    expected_flags = (side is Side.LONG, side is Side.SHORT)
    if (basic.get("use_long"), basic.get("use_short")) != expected_flags:
        raise StrategyValidationError("active-side flags are incorrect")
    if len(entries) != len(orders):
        raise StrategyValidationError("active order count does not match structure")
    if [int(entry["id"]) for entry in entries] != list(range(1, len(entries) + 1)):
        raise StrategyValidationError("order ids must be consecutive from one")
    lots = [Decimal(str(entry["lot_x"])) for entry in entries]
    if abs(sum(lots, Decimal("0")) - config.initial_lot_sum) > config.numeric_tolerance:
        raise StrategyValidationError("initial lot sum must equal one")
    for entry, order in zip(entries, orders, strict=True):
        if int(entry["len"]) != int(order["open_ma"]):
            raise StrategyValidationError("open MA does not match source point")
        expected_multiplier = _entry_multiplier(side, int(order["shift_bp"]))
        if abs(Decimal(str(entry["multiplier"])) - expected_multiplier) > config.numeric_tolerance:
            raise StrategyValidationError("entry multiplier does not match shift")
        if Decimal(str(order["close_support"])) < config.close_supported_min:
            raise StrategyValidationError("common close support is below threshold")
    expected_close = (
        config.close_multiplier_long if side is Side.LONG else config.close_multiplier_short
    )
    close = generated["mrs3"][close_key]
    if int(close["len"]) != int(structure["common_close_ma"]):
        raise StrategyValidationError("close MA does not match structure")
    if abs(Decimal(str(close["multiplier"])) - expected_close) > config.numeric_tolerance:
        raise StrategyValidationError("close multiplier is incorrect")

    source_id_series = source_points.get("point_id", pd.Series(dtype=str)).astype(str)
    source_ids = set(source_id_series)
    order_ids = {str(order["point_id"]) for order in orders}
    if len(order_ids) != len(orders):
        raise StrategyValidationError("a source point cannot be used more than once")
    if not order_ids.issubset(source_ids):
        raise StrategyValidationError("chosen point is absent from source audit")
    if source_id_series.duplicated().any():
        raise StrategyValidationError("source audit contains duplicate point_id values")

    required_source_fields = {
        "point_id",
        "plateau_id",
        "symbol",
        "side",
        "timeframe",
        "shift_bp",
        "shift_pct",
        "open_ma",
        "close_ma",
        "pnl_pct",
        "dd_pct",
        "efficiency",
        "trades",
        "economic_pass",
        "standalone_eligible",
        "depth_eligible",
        "refine_required",
    }
    missing_source_fields = sorted(required_source_fields.difference(source_points.columns))
    if missing_source_fields:
        raise StrategyValidationError(
            f"source audit lacks hard-validation fields: {missing_source_fields}"
        )
    indexed_source = source_points.copy()
    indexed_source.index = source_id_series

    for expected_id, order in enumerate(orders, start=1):
        if int(order.get("id", -1)) != expected_id:
            raise StrategyValidationError("structure order ids must be consecutive from one")
        source = indexed_source.loc[str(order["point_id"])]
        exact_pairs = (
            ("plateau_id", str(order["plateau_id"]), str(source["plateau_id"])),
            ("symbol", str(structure["symbol"]), str(source["symbol"])),
            ("side", str(structure["side"]), str(source["side"])),
            ("timeframe", str(structure["timeframe"]), str(source["timeframe"])),
            ("shift_bp", int(order["shift_bp"]), int(source["shift_bp"])),
            ("open_ma", int(order["open_ma"]), int(source["open_ma"])),
            (
                "close_ma",
                int(structure["common_close_ma"]),
                int(source["close_ma"]),
            ),
            ("trades", int(order["trades"]), int(source["trades"])),
            (
                "standalone_eligible",
                bool(order["standalone_eligible"]),
                bool(source["standalone_eligible"]),
            ),
            (
                "depth_eligible",
                bool(order["depth_eligible"]),
                bool(source["depth_eligible"]),
            ),
        )
        mismatched = [name for name, actual, expected in exact_pairs if actual != expected]
        numeric_pairs = (
            ("shift_pct", order["shift_pct"], source["shift_pct"]),
            ("source_pnl_pct", order["source_pnl_pct"], source["pnl_pct"]),
            ("source_dd_pct", order["source_dd_pct"], source["dd_pct"]),
            (
                "source_efficiency",
                order["source_efficiency"],
                source["efficiency"],
            ),
        )
        for name, actual, expected in numeric_pairs:
            actual_decimal = Decimal(str(actual))
            expected_decimal = Decimal(str(expected))
            if (
                not actual_decimal.is_finite()
                or not expected_decimal.is_finite()
                or abs(actual_decimal - expected_decimal) > config.numeric_tolerance
            ):
                mismatched.append(name)
        if mismatched:
            raise StrategyValidationError(
                "structure does not match source point fields: "
                + ", ".join(sorted(mismatched))
            )
        if not bool(source["economic_pass"]):
            raise StrategyValidationError("economically rejected point cannot be exported")
        if bool(source["refine_required"]):
            raise StrategyValidationError("REFINE_REQUIRED point cannot be exported")
    if validate_order_tuple(orders, config) != "READY_MRS3_STRUCTURE":
        raise StrategyValidationError("structure order tuple fails hard validation")


def validate_unique_names(strategies: Iterable[Mapping[str, object]]) -> None:
    names = [str(strategy.get("name", "")) for strategy in strategies]
    if any(not name for name in names):
        raise StrategyValidationError("strategy name must not be empty")
    if len(set(names)) != len(names):
        raise StrategyValidationError("duplicate strategy names")
