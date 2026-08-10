from __future__ import annotations

from decimal import Decimal
import hashlib
import itertools
from typing import Iterable, Mapping, Sequence

import pandas as pd

from .config import AlgorithmConfig


CLOSE_PROFILE_COLUMNS = [
    "plateau_id",
    "symbol",
    "side",
    "timeframe",
    "close_ma",
    "support",
    "status",
    "point_id",
    "refine_required",
]

STRUCTURE_COLUMNS = [
    "structure_id",
    "symbol",
    "side",
    "timeframe",
    "common_close_ma",
    "order_count",
    "orders",
    "plateau_ids",
    "min_close_support",
    "source_pnl_sum",
    "source_eff_mean",
    "low_sample_depth_count",
    "status",
]

STRUCTURE_DIAGNOSTIC_COLUMNS = [
    "symbol",
    "side",
    "timeframe",
    "common_close_ma",
    "order_count",
    "plateau_ids",
    "status",
    "reason",
    "orders",
]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _relative_difference(left: object, right: object) -> Decimal:
    a, b = _decimal(left), _decimal(right)
    denominator = max(a, b)
    if denominator <= 0:
        return Decimal("0") if a == b else Decimal("Infinity")
    return abs(a - b) / denominator


def equivalent(
    left: Mapping[str, object] | pd.Series,
    right: Mapping[str, object] | pd.Series,
    tolerance: Decimal,
) -> bool:
    return (
        _relative_difference(left["pnl_pct"], right["pnl_pct"]) <= tolerance
        and _relative_difference(left["efficiency"], right["efficiency"]) <= tolerance
    )


def _reference_row(points: pd.DataFrame) -> pd.Series:
    return points.sort_values(
        ["pnl_pct", "efficiency", "trades", "dd_pct", "point_id"],
        ascending=[False, False, False, True, True],
        kind="mergesort",
    ).iloc[0]


def _equivalent_rows(points: pd.DataFrame, config: AlgorithmConfig) -> pd.DataFrame:
    if points.empty:
        return points.copy()
    reference = _reference_row(points)
    mask = points.apply(
        lambda row: equivalent(row, reference, config.equivalent_tolerance), axis=1
    )
    return points.loc[mask].copy()


def choose_equivalent_default(
    points: pd.DataFrame,
    config: AlgorithmConfig,
) -> pd.Series:
    equivalents = _equivalent_rows(points, config)
    if equivalents.empty:
        raise ValueError("cannot choose representative from an empty point set")
    return equivalents.sort_values(
        ["shift_bp", "pnl_pct", "efficiency", "trades", "dd_pct", "point_id"],
        ascending=[False, False, False, False, True, True],
        kind="mergesort",
    ).iloc[0]


def close_status(support: Decimal, config: AlgorithmConfig) -> str:
    if support >= config.close_core_min:
        return "CORE_CLOSE"
    if support >= config.close_supported_min:
        return "SUPPORTED_CLOSE"
    return "UNSUPPORTED_CLOSE"


def _profile_alt(
    plateau_points: pd.DataFrame,
    primary: pd.Series,
    close_ma: int,
    open_ma_radius: int,
) -> pd.Series | None:
    candidates = plateau_points.loc[
        plateau_points["economic_pass"]
        & plateau_points["close_ma"].eq(close_ma)
        & plateau_points["shift_bp"].eq(int(primary["shift_bp"]))
        & plateau_points["open_ma"]
        .sub(int(primary["open_ma"]))
        .abs()
        .le(open_ma_radius)
    ]
    if candidates.empty:
        return None
    return _reference_row(candidates)


def build_close_profiles(
    points: pd.DataFrame,
    plateaus: pd.DataFrame,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    updated = plateaus.copy()
    profile_rows: list[dict[str, object]] = []
    primary_by_plateau: dict[str, int] = {}
    refine_by_plateau: dict[str, bool] = {}

    for plateau in plateaus.sort_values("plateau_id", kind="mergesort").itertuples(index=False):
        point_ids = set(plateau.all_point_ids)
        plateau_points = points.loc[points["point_id"].isin(point_ids)].copy()
        plateau_points = plateau_points.loc[plateau_points["economic_pass"]]
        if plateau_points.empty:
            continue
        primary = choose_equivalent_default(plateau_points, config)
        primary_close = int(primary["close_ma"])
        primary_by_plateau[str(plateau.plateau_id)] = primary_close
        refine_required = False
        base = {
            "plateau_id": str(plateau.plateau_id),
            "symbol": str(plateau.symbol),
            "side": str(plateau.side),
            "timeframe": str(plateau.timeframe),
        }
        profile_rows.append(
            {
                **base,
                "close_ma": primary_close,
                "support": 1.0,
                "status": "PRIMARY_CLOSE",
                "point_id": str(primary["point_id"]),
                "refine_required": False,
            }
        )
        group_mask = (
            points["symbol"].eq(plateau.symbol)
            & points["side"].eq(plateau.side)
            & points["timeframe"].eq(plateau.timeframe)
        )
        close_min = int(points.loc[group_mask, "close_ma"].min())
        close_max = int(points.loc[group_mask, "close_ma"].max())
        for direction in (-1, 1):
            close_ma = primary_close + direction
            while close_min <= close_ma <= close_max:
                alternative = _profile_alt(
                    plateau_points,
                    primary,
                    close_ma,
                    config.ma_neighbor_radius,
                )
                if alternative is None:
                    source_slice = points.loc[
                        group_mask
                        & points["close_ma"].eq(close_ma)
                        & points["shift_bp"].eq(int(primary["shift_bp"]))
                        & points["open_ma"]
                        .sub(int(primary["open_ma"]))
                        .abs()
                        .le(config.ma_neighbor_radius)
                    ]
                    missing_source_cell = source_slice.empty
                    refine_required = refine_required or missing_source_cell
                    profile_rows.append(
                        {
                            **base,
                            "close_ma": close_ma,
                            "support": float("nan"),
                            "status": (
                                "REFINE_REQUIRED_CLOSE"
                                if missing_source_cell
                                else "UNSUPPORTED_CLOSE"
                            ),
                            "point_id": None,
                            "refine_required": missing_source_cell,
                        }
                    )
                    break
                support = min(
                    _decimal(alternative["pnl_pct"]) / _decimal(primary["pnl_pct"]),
                    _decimal(alternative["efficiency"])
                    / _decimal(primary["efficiency"]),
                )
                status = close_status(support, config)
                profile_rows.append(
                    {
                        **base,
                        "close_ma": close_ma,
                        "support": float(support),
                        "status": status,
                        "point_id": str(alternative["point_id"]),
                        "refine_required": False,
                    }
                )
                if status == "UNSUPPORTED_CLOSE":
                    break
                close_ma += direction
        refine_by_plateau[str(plateau.plateau_id)] = refine_required

    updated["primary_close_ma"] = updated["plateau_id"].map(primary_by_plateau)
    updated["close_refine_required"] = (
        updated["plateau_id"].map(refine_by_plateau).fillna(False).astype(bool)
    )
    profile = pd.DataFrame(profile_rows, columns=CLOSE_PROFILE_COLUMNS)
    if not profile.empty:
        profile = profile.sort_values(
            ["symbol", "side", "timeframe", "plateau_id", "close_ma"],
            kind="mergesort",
        ).reset_index(drop=True)
    return updated, profile


def select_base_one_order(
    points: pd.DataFrame,
    plateaus: pd.DataFrame,
    config: AlgorithmConfig,
) -> pd.DataFrame:
    ready_ids = set(plateaus.loc[plateaus["ready"], "plateau_id"])
    candidates = points.loc[
        points["plateau_id"].isin(ready_ids) & points["standalone_eligible"]
    ].copy()
    local_rows: list[pd.Series] = []
    for _, group in candidates.groupby("plateau_id", sort=True):
        local_rows.append(choose_equivalent_default(group, config))
    if not local_rows:
        return pd.DataFrame(columns=[*points.columns, "selection_type"])
    local = pd.DataFrame(local_rows)
    if "pnl_dd5_theoretical" not in local.columns:
        local["pnl_dd5_theoretical"] = (
            local["pnl_pct"] * float(config.target_dd_pct) / local["dd_pct"]
        )
    selected_rows: list[pd.Series] = []
    for _, group in local.groupby(["symbol", "side"], sort=True):
        selected_rows.append(
            group.sort_values(
                ["pnl_dd5_theoretical", "pnl_pct", "trades", "dd_pct", "point_id"],
                ascending=[False, False, False, True, True],
                kind="mergesort",
            ).iloc[0]
        )
    selected = pd.DataFrame(selected_rows).reset_index(drop=True)
    selected["selection_type"] = "BASE_1ORD"
    return selected


def validate_order_tuple(
    orders: Sequence[Mapping[str, object]],
    config: AlgorithmConfig,
) -> str:
    sorted_orders = sorted(orders, key=lambda order: (int(order["shift_bp"]), str(order.get("point_id", ""))))
    plateau_ids = [str(order["plateau_id"]) for order in sorted_orders]
    if len(set(plateau_ids)) != len(plateau_ids):
        return "SAME_PLATEAU_USED_TWICE"
    shifts = [int(order["shift_bp"]) for order in sorted_orders]
    if any(right <= left for left, right in zip(shifts, shifts[1:])):
        return "SHIFTS_NOT_STRICTLY_INCREASING"
    if not bool(sorted_orders[0]["standalone_eligible"]):
        return "NO_STANDALONE_ELIGIBLE_FIRST_ORDER"
    if any(not bool(order["depth_eligible"]) for order in sorted_orders[1:]):
        return "DEPTH_NOT_ELIGIBLE"
    deep_gap = False
    for left, right in zip(shifts, shifts[1:]):
        if left < config.gap_mid_start_bp:
            required = config.gap_lower_lt_150_bp
        elif left <= config.deep_gap_boundary_bp:
            required = config.gap_lower_150_to_400_bp
        else:
            deep_gap = True
            continue
        if right - left < required:
            return "GAP_TOO_SMALL"
    return "DEEP_GAP_RESEARCH" if deep_gap else "READY_MRS3_STRUCTURE"


def _structure_id(
    symbol: str,
    side: str,
    timeframe: str,
    close_ma: int,
    orders: Sequence[Mapping[str, object]],
) -> str:
    payload = "|".join(
        [
            symbol,
            side,
            timeframe,
            str(close_ma),
            *(str(order["plateau_id"]) for order in orders),
            *(str(order["point_id"]) for order in orders),
        ]
    )
    return f"STR_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _tuple_priority(orders: Sequence[Mapping[str, object]]) -> tuple[object, ...]:
    return (
        -sum(int(order["shift_bp"]) for order in orders),
        -sum(float(order["source_pnl_pct"]) for order in orders),
        -sum(float(order["source_efficiency"]) for order in orders) / len(orders),
        -sum(int(order["trades"]) for order in orders),
        tuple(str(order["point_id"]) for order in orders),
    )


def _order_from_point(point: pd.Series, close_support: float) -> dict[str, object]:
    return {
        "plateau_id": str(point["plateau_id"]),
        "point_id": str(point["point_id"]),
        "open_ma": int(point["open_ma"]),
        "shift_bp": int(point["shift_bp"]),
        "shift_pct": float(point["shift_pct"]),
        "source_pnl_pct": float(point["pnl_pct"]),
        "source_dd_pct": float(point["dd_pct"]),
        "source_efficiency": float(point["efficiency"]),
        "trades": int(point["trades"]),
        "close_support": float(close_support),
        "standalone_eligible": bool(point["standalone_eligible"]),
        "depth_eligible": bool(point["depth_eligible"]),
    }


def build_structures(
    points: pd.DataFrame,
    plateaus: pd.DataFrame,
    close_profiles: pd.DataFrame,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    usable_status = {"PRIMARY_CLOSE", "CORE_CLOSE", "SUPPORTED_CLOSE"}
    usable = close_profiles.loc[
        close_profiles["status"].isin(usable_status)
        & close_profiles["support"].ge(float(config.close_supported_min))
    ].copy()
    structure_rows: list[dict[str, object]] = []
    diagnostic_rows: list[dict[str, object]] = []

    for keys, family in usable.groupby(
        ["symbol", "side", "timeframe", "close_ma"], sort=True
    ):
        symbol, side, timeframe, close_ma = keys
        family = family.drop_duplicates("plateau_id", keep="first")
        if len(family) < 2:
            continue
        support_by_plateau = {
            str(row.plateau_id): float(row.support) for row in family.itertuples(index=False)
        }
        candidates_by_plateau: dict[str, list[pd.Series]] = {}
        for plateau_id in sorted(support_by_plateau):
            candidates = points.loc[
                points["plateau_id"].eq(plateau_id)
                & points["close_ma"].eq(int(close_ma))
                & points["economic_pass"]
            ].copy()
            equivalents = _equivalent_rows(candidates, config)
            candidates_by_plateau[plateau_id] = [
                row for _, row in equivalents.sort_values("point_id", kind="mergesort").iterrows()
            ]
        plateau_ids = [
            plateau_id
            for plateau_id in sorted(candidates_by_plateau)
            if candidates_by_plateau[plateau_id]
        ]
        for order_count in range(2, min(config.max_orders, len(plateau_ids)) + 1):
            for plateau_combo in itertools.combinations(plateau_ids, order_count):
                ready_tuples: list[list[dict[str, object]]] = []
                deep_tuples: list[list[dict[str, object]]] = []
                seen_reasons: set[str] = set()
                for product in itertools.product(
                    *(candidates_by_plateau[plateau_id] for plateau_id in plateau_combo)
                ):
                    orders = [
                        _order_from_point(point, support_by_plateau[str(point["plateau_id"])])
                        for point in product
                    ]
                    orders.sort(key=lambda order: (int(order["shift_bp"]), str(order["point_id"])))
                    status = validate_order_tuple(orders, config)
                    if status == "READY_MRS3_STRUCTURE":
                        ready_tuples.append(orders)
                    elif status == "DEEP_GAP_RESEARCH":
                        deep_tuples.append(orders)
                    else:
                        seen_reasons.add(status)
                for reason in sorted(seen_reasons):
                    diagnostic_rows.append(
                        {
                            "symbol": symbol,
                            "side": side,
                            "timeframe": timeframe,
                            "common_close_ma": int(close_ma),
                            "order_count": order_count,
                            "plateau_ids": tuple(plateau_combo),
                            "status": "REJECTED",
                            "reason": reason,
                        }
                    )
                chosen_pool = ready_tuples if ready_tuples else deep_tuples
                if not chosen_pool:
                    continue
                chosen_pool.sort(key=_tuple_priority)
                orders = chosen_pool[0]
                status = (
                    "READY_MRS3_STRUCTURE" if ready_tuples else "DEEP_GAP_RESEARCH"
                )
                if status == "DEEP_GAP_RESEARCH":
                    diagnostic_rows.append(
                        {
                            "symbol": symbol,
                            "side": side,
                            "timeframe": timeframe,
                            "common_close_ma": int(close_ma),
                            "order_count": order_count,
                            "plateau_ids": tuple(plateau_combo),
                            "status": status,
                            "reason": status,
                            "orders": tuple(orders),
                        }
                    )
                    continue
                numbered_orders = tuple(
                    {**order, "id": index}
                    for index, order in enumerate(orders, start=1)
                )
                structure_rows.append(
                    {
                        "structure_id": _structure_id(
                            str(symbol), str(side), str(timeframe), int(close_ma), numbered_orders
                        ),
                        "symbol": str(symbol),
                        "side": str(side),
                        "timeframe": str(timeframe),
                        "common_close_ma": int(close_ma),
                        "order_count": order_count,
                        "orders": numbered_orders,
                        "plateau_ids": tuple(order["plateau_id"] for order in numbered_orders),
                        "min_close_support": min(
                            float(order["close_support"]) for order in numbered_orders
                        ),
                        "source_pnl_sum": sum(
                            float(order["source_pnl_pct"]) for order in numbered_orders
                        ),
                        "source_eff_mean": sum(
                            float(order["source_efficiency"]) for order in numbered_orders
                        )
                        / order_count,
                        "low_sample_depth_count": sum(
                            not bool(order["standalone_eligible"])
                            for order in numbered_orders[1:]
                        ),
                        "status": status,
                    }
                )
    structures = pd.DataFrame(structure_rows, columns=STRUCTURE_COLUMNS)
    if not structures.empty:
        structures = structures.sort_values(
            [
                "symbol",
                "side",
                "timeframe",
                "order_count",
                "min_close_support",
                "source_pnl_sum",
                "source_eff_mean",
                "low_sample_depth_count",
                "structure_id",
            ],
            ascending=[True, True, True, True, False, False, False, True, True],
            kind="mergesort",
        ).reset_index(drop=True)
    diagnostics = pd.DataFrame(
        diagnostic_rows, columns=STRUCTURE_DIAGNOSTIC_COLUMNS
    )
    return structures, diagnostics
