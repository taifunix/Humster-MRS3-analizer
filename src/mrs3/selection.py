from __future__ import annotations

from decimal import Decimal, InvalidOperation
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
    "continuity_status",
    "usable",
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
    "source_eff_mean",
    "low_sample_depth_count",
    "Order1EventCount",
    "Order2EventCount",
    "Order3EventCount",
    "Order4EventCount",
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


OPERATIONAL_FACTS_VERSION = "cma_representatives_v1"

FROZEN_OPERATIONAL_FACT_FIELDS = (
    "operational_facts_version",
    "primary_close_ma",
    "cma_representatives",
    "base_1ord_point_id",
)

REPRESENTATIVE_FACT_FIELDS = frozenset(
    {"close_ma", "point_id", "support", "support_status", "continuity_status", "usable"}
)

SUPPORT_STATUS_VALUES = (
    "PRIMARY_CLOSE",
    "CORE_CLOSE",
    "SUPPORTED_CLOSE",
    "UNSUPPORTED_CLOSE",
)

CONTINUITY_STATUS_VALUES = (
    "USABLE",
    "BREAK_UNSUPPORTED",
    "BLOCKED_BY_CONTINUITY",
)

_CLOSE_CORE_MIN = Decimal("0.90")
_CLOSE_SUPPORTED_MIN = Decimal("0.60")


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
    if "point_event_count" not in equivalents:
        if "event_mode" in equivalents and equivalents["event_mode"].eq("real_independent_events").any():
            raise ValueError("point_event_count is required for real_independent_events")
        equivalents["point_event_count"] = equivalents["trades"]
    return equivalents.sort_values(
        ["point_event_count", "shift_bp", "pnl_pct", "efficiency", "trades", "dd_pct", "point_id"],
        ascending=[False, False, False, False, False, True, True],
        kind="mergesort",
    ).iloc[0]


def close_status(support: Decimal, config: AlgorithmConfig) -> str:
    if support >= config.close_core_min:
        return "CORE_CLOSE"
    if support >= config.close_supported_min:
        return "SUPPORTED_CLOSE"
    return "UNSUPPORTED_CLOSE"


def _finite_positive_decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite positive number, got {value!r}") from error
    if not number.is_finite():
        raise ValueError(f"{label} must be finite, got {value!r}")
    if number <= 0:
        raise ValueError(f"{label} must be positive, got {value!r}")
    return number


def choose_primary_representative(representatives: Mapping[int, pd.Series]) -> pd.Series:
    """Choose the single primary among existing representatives by economic ordering.

    PnL DESC, efficiency/PnL-DD DESC, trades DESC, dd_pct ASC, point_id ASC.
    """
    if not representatives:
        raise ValueError("cannot choose a primary without representatives")
    frame = pd.DataFrame(list(representatives.values()))
    return frame.sort_values(
        ["pnl_pct", "efficiency", "trades", "dd_pct", "point_id"],
        ascending=[False, False, False, True, True],
        kind="mergesort",
    ).iloc[0]


def compute_close_support(
    primary: Mapping[str, object],
    representative: Mapping[str, object],
) -> Decimal:
    """Fail-closed CloseSupport = min(CMA_PnL / Primary_PnL, CMA_Eff / Primary_Eff)."""
    primary_pnl = _finite_positive_decimal(primary["pnl_pct"], "Primary PnL")
    primary_efficiency = _finite_positive_decimal(primary["efficiency"], "Primary efficiency")
    cma_pnl = _finite_positive_decimal(representative["pnl_pct"], "CMA PnL")
    cma_efficiency = _finite_positive_decimal(representative["efficiency"], "CMA efficiency")
    support_pnl = cma_pnl / primary_pnl
    support_efficiency = cma_efficiency / primary_efficiency
    if not support_pnl.is_finite() or not support_efficiency.is_finite():
        raise ValueError("CloseSupport division produced a non-finite value")
    if support_pnl <= 0 or support_efficiency <= 0:
        raise ValueError("CloseSupport division produced a non-positive value")
    support = min(support_pnl, support_efficiency)
    if not support.is_finite() or support <= 0 or support > 1:
        raise ValueError(f"CloseSupport outside (0, 1]: {support}")
    return support


def recompute_continuity(
    support_status_by_close_ma: Mapping[int, str],
    primary_close_ma: int,
) -> dict[int, tuple[str, bool]]:
    """Recompute continuity independently downward/upward from the primary CloseMA.

    Only adjacent integer CloseMAs are walked. A missing representative or an
    UNSUPPORTED_CLOSE row breaks that direction; every outer representative is
    BLOCKED_BY_CONTINUITY regardless of its raw support status.
    """
    if primary_close_ma not in support_status_by_close_ma:
        raise ValueError("primary CloseMA is missing from representatives")
    result: dict[int, tuple[str, bool]] = {primary_close_ma: ("USABLE", True)}
    for step in (-1, 1):
        current = primary_close_ma
        while True:
            next_close_ma = current + step
            if next_close_ma not in support_status_by_close_ma:
                break
            if support_status_by_close_ma[next_close_ma] == "UNSUPPORTED_CLOSE":
                result[next_close_ma] = ("BREAK_UNSUPPORTED", False)
                break
            result[next_close_ma] = ("USABLE", True)
            current = next_close_ma
    for close_ma in support_status_by_close_ma:
        if close_ma not in result:
            result[close_ma] = ("BLOCKED_BY_CONTINUITY", False)
    return result


def has_frozen_operational_facts(metrics: Mapping[str, object]) -> bool:
    for key in (
        "operational_facts_version",
        "primary_close_ma",
        "cma_representatives",
        "base_1ord_point_id",
    ):
        if metrics.get(key) is not None:
            return True
    return False


def missing_frozen_operational_facts(metrics: Mapping[str, object]) -> tuple[str, ...]:
    """Return the frozen operational fact fields absent from the metrics."""
    return tuple(key for key in FROZEN_OPERATIONAL_FACT_FIELDS if key not in metrics)


def require_complete_operational_facts(metrics: Mapping[str, object]) -> None:
    """Fail closed when a canonical/COMPUTED plateau lacks any frozen fact field.

    All four logical fields are mandatory; ``base_1ord_point_id`` may be null
    but the key itself must be present. Explicitly legacy runs bypass this gate
    at their call sites (facts_state ``UNAVAILABLE_LEGACY``).
    """
    missing = missing_frozen_operational_facts(metrics)
    if missing:
        raise ValueError(
            "canonical/COMPUTED READY plateau lacks mandatory frozen operational facts: "
            + ", ".join(missing)
        )


def _expected_close_status(support: Decimal) -> str:
    if support >= _CLOSE_CORE_MIN:
        return "CORE_CLOSE"
    if support >= _CLOSE_SUPPORTED_MIN:
        return "SUPPORTED_CLOSE"
    return "UNSUPPORTED_CLOSE"


def _validated_support(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number, got {value!r}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite positive number, got {value!r}") from error
    if not number.is_finite():
        raise ValueError(f"{label} must be finite, got {value!r}")
    if number <= 0:
        raise ValueError(f"{label} must be positive, got {value!r}")
    return number


def validate_frozen_operational_facts(
    metrics: Mapping[str, object],
    *,
    surface_point_ids: Iterable[str],
    plateau_all_point_ids: Iterable[str],
    standalone_eligible_point_ids: Iterable[str],
    numeric_tolerance: Decimal = Decimal("1e-9"),
) -> None:
    """Structural and semantic validation of frozen Plateau operational facts.

    Shared by publication and read-time consumers. Continuity is recomputed
    from the sorted representative list instead of trusting stored flags.
    """
    if not has_frozen_operational_facts(metrics):
        return
    require_complete_operational_facts(metrics)
    if metrics.get("operational_facts_version") != OPERATIONAL_FACTS_VERSION:
        raise ValueError(
            f"unknown operational facts version: {metrics.get('operational_facts_version')!r}"
        )
    symbol, side, timeframe = (
        metrics.get("symbol"),
        metrics.get("side"),
        metrics.get("timeframe"),
    )
    if not symbol or not side or not timeframe:
        raise ValueError("plateau operational facts are missing symbol/side/timeframe scope")
    primary_close_ma = metrics.get("primary_close_ma")
    if isinstance(primary_close_ma, bool) or not isinstance(primary_close_ma, int):
        raise ValueError("primary_close_ma must be an integer")
    representatives = metrics.get("cma_representatives")
    if not isinstance(representatives, list) or not representatives:
        raise ValueError("cma_representatives must be a non-empty list")
    surface_points = set(surface_point_ids)
    plateau_points = set(plateau_all_point_ids)
    standalone_points = set(standalone_eligible_point_ids)
    close_mas: list[int] = []
    point_ids: list[str] = []
    primary_index: int | None = None
    support_status_by_close_ma: dict[int, str] = {}
    expected_status_by_close_ma: dict[int, str] = {}
    for index, row in enumerate(representatives):
        if not isinstance(row, dict):
            raise ValueError("cma_representative entries must be objects")
        extra_keys = sorted(set(row) - REPRESENTATIVE_FACT_FIELDS)
        if extra_keys:
            raise ValueError(
                "cma_representative entry has unexpected fields: " + ", ".join(extra_keys)
            )
        missing_keys = sorted(REPRESENTATIVE_FACT_FIELDS - set(row))
        if missing_keys:
            raise ValueError(
                "cma_representative entry is missing fields: " + ", ".join(missing_keys)
            )
        close_ma = row.get("close_ma")
        if isinstance(close_ma, bool) or not isinstance(close_ma, int):
            raise ValueError("representative close_ma must be an integer")
        point_id = row.get("point_id")
        if not isinstance(point_id, str) or not point_id:
            raise ValueError("representative point_id must be a non-empty string")
        if close_ma in support_status_by_close_ma:
            raise ValueError(f"duplicate representative CloseMA: {close_ma}")
        if point_id in point_ids:
            raise ValueError(f"duplicate representative point_id: {point_id}")
        support = _validated_support(
            row.get("support"), f"representative close_ma {close_ma} support"
        )
        support_status = row.get("support_status")
        if support_status not in SUPPORT_STATUS_VALUES:
            raise ValueError(f"unknown support_status: {support_status!r}")
        continuity_status = row.get("continuity_status")
        if continuity_status not in CONTINUITY_STATUS_VALUES:
            raise ValueError(f"unknown continuity_status: {continuity_status!r}")
        usable = row.get("usable")
        if not isinstance(usable, bool):
            raise ValueError("representative usable must be a boolean")
        parts = point_id.split("|")
        if len(parts) != 6:
            raise ValueError(f"representative point_id has invalid shape: {point_id!r}")
        point_symbol, point_side, point_timeframe, _shift, _open_ma, point_close = parts
        if (point_symbol, point_side, point_timeframe) != (
            str(symbol),
            str(side),
            str(timeframe),
        ):
            raise ValueError(f"representative point {point_id} is outside the Plateau scope")
        try:
            parsed_close_ma = int(point_close)
        except ValueError as error:
            raise ValueError(f"representative point {point_id} has an invalid CloseMA") from error
        if parsed_close_ma != close_ma:
            raise ValueError(f"representative point {point_id} CloseMA does not match {close_ma}")
        if point_id not in surface_points:
            raise ValueError(f"representative point {point_id} is outside the surface")
        if point_id not in plateau_points:
            raise ValueError(f"representative point {point_id} is outside the Plateau")
        close_mas.append(close_ma)
        point_ids.append(point_id)
        support_status_by_close_ma[close_ma] = support_status
        expected_status_by_close_ma[close_ma] = _expected_close_status(support)
        if support_status == "PRIMARY_CLOSE":
            if primary_index is not None:
                raise ValueError("more than one PRIMARY_CLOSE representative")
            if abs(support - Decimal("1")) > numeric_tolerance:
                raise ValueError("primary support must equal 1.0")
            primary_index = index
        elif support > 1:
            raise ValueError(f"non-primary support outside (0, 1]: {support}")
    if primary_index is None:
        raise ValueError("frozen representatives have no PRIMARY_CLOSE row")
    if close_mas != sorted(close_mas):
        raise ValueError("representatives are not strictly ordered by close_ma")
    primary_row = representatives[primary_index]
    if primary_row["close_ma"] != primary_close_ma:
        raise ValueError("primary_close_ma does not match the PRIMARY_CLOSE row")
    if primary_row["continuity_status"] != "USABLE" or primary_row["usable"] is not True:
        raise ValueError("primary representative must be continuity USABLE")
    for close_ma, expected_status in expected_status_by_close_ma.items():
        if close_ma == primary_close_ma:
            continue
        if support_status_by_close_ma[close_ma] != expected_status:
            raise ValueError(
                f"support_status does not match numeric support for CloseMA {close_ma}"
            )
    recomputed = recompute_continuity(support_status_by_close_ma, primary_close_ma)
    for row in representatives:
        close_ma = row["close_ma"]
        expected_status, expected_usable = recomputed[close_ma]
        if row["continuity_status"] != expected_status or row["usable"] != expected_usable:
            raise ValueError(
                f"continuity_status/usable contradict recomputed continuity for CloseMA {close_ma}"
            )
    base_id = metrics.get("base_1ord_point_id")
    if base_id is not None:
        if not isinstance(base_id, str) or not base_id:
            raise ValueError("base_1ord_point_id must be a non-empty string or null")
        if base_id not in surface_points:
            raise ValueError(f"base point {base_id} is outside the surface")
        if base_id not in plateau_points:
            raise ValueError(f"base point {base_id} is outside the Plateau")
        matches = [index for index, point_id in enumerate(point_ids) if point_id == base_id]
        if len(matches) != 1:
            raise ValueError(f"base point {base_id} must appear exactly once among representatives")
        base_row = representatives[matches[0]]
        if base_row["continuity_status"] != "USABLE" or base_row["usable"] is not True:
            raise ValueError(f"base point {base_id} is not a continuity-usable representative")
        if base_id not in standalone_points:
            raise ValueError(f"base point {base_id} is not standalone-eligible")


def choose_cma_representative(
    plateau_points: pd.DataFrame,
    close_ma: int,
    config: AlgorithmConfig,
) -> pd.Series | None:
    """Choose exactly one frozen representative for one Plateau + exact CloseMA.

    Candidate pool is every Plateau member point with that exact CloseMA; the
    global-primary Shift and OpenMA radius never restrict the pool. The
    economic reference uses `_reference_row()` ordering only, the 5% equivalent
    group is built before the event filter, and the final ranking is
    deterministic. Returns None when no representative survives.
    """
    candidates = plateau_points.loc[
        plateau_points["economic_pass"] & plateau_points["close_ma"].eq(close_ma)
    ]
    if candidates.empty:
        return None
    equivalents = _equivalent_rows(candidates, config)
    if "event_eligible" not in equivalents:
        equivalents["event_eligible"] = equivalents["economic_pass"]
    equivalents = equivalents.loc[equivalents["event_eligible"]].copy()
    if equivalents.empty:
        return None
    real_events = (
        "event_mode" in equivalents
        and equivalents["event_mode"].eq("real_independent_events").any()
    )
    if real_events:
        if "point_event_count" not in equivalents:
            raise ValueError("point_event_count is required for real_independent_events")
        equivalents = equivalents.loc[
            equivalents["point_event_count"].ge(config.min_point_events)
        ].copy()
        if equivalents.empty:
            return None
    if "point_event_count" not in equivalents:
        equivalents["point_event_count"] = equivalents["trades"]
    return equivalents.sort_values(
        ["point_event_count", "shift_bp", "pnl_pct", "efficiency", "trades", "dd_pct", "point_id"],
        ascending=[False, False, False, False, False, True, True],
        kind="mergesort",
    ).iloc[0]


def _plateau_fact(
    facts_by_plateau: Mapping[str, Mapping[str, object]],
    plateau_id: object,
    key: str,
) -> object:
    facts = facts_by_plateau.get(str(plateau_id))
    return facts.get(key) if facts is not None else None


def _frozen_base_point_id(
    representatives: Mapping[int, pd.Series],
    continuity: Mapping[int, tuple[str, bool]],
    config: AlgorithmConfig,
) -> str | None:
    """Freeze at most one local BASE from continuity-usable standalone reps."""
    candidates = [
        representative
        for close_ma, representative in representatives.items()
        if continuity[close_ma][1] and bool(representative["standalone_eligible"])
    ]
    if not candidates:
        return None
    chosen = choose_equivalent_default(pd.DataFrame(candidates), config)
    return str(chosen["point_id"])


def build_close_profiles(
    points: pd.DataFrame,
    plateaus: pd.DataFrame,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    points = points.copy()
    if "event_eligible" not in points:
        points["event_eligible"] = points["economic_pass"]
    updated = plateaus.copy()
    profile_rows: list[dict[str, object]] = []
    facts_by_plateau: dict[str, dict[str, object]] = {}

    for plateau in plateaus.sort_values("plateau_id", kind="mergesort").itertuples(index=False):
        if not bool(getattr(plateau, "ready", True)):
            continue
        point_ids = set(plateau.all_point_ids)
        plateau_points = points.loc[points["point_id"].isin(point_ids)].copy()
        representatives: dict[int, pd.Series] = {}
        for close_ma in sorted({int(value) for value in plateau_points["close_ma"]}):
            representative = choose_cma_representative(plateau_points, close_ma, config)
            if representative is not None:
                representatives[close_ma] = representative
        if not representatives:
            continue
        primary = choose_primary_representative(representatives)
        primary_close = int(primary["close_ma"])
        _finite_positive_decimal(primary["pnl_pct"], "Primary PnL")
        _finite_positive_decimal(primary["efficiency"], "Primary efficiency")
        support_by_close_ma: dict[int, tuple[Decimal, str]] = {}
        for close_ma, representative in representatives.items():
            if close_ma == primary_close:
                support = Decimal("1.0")
                status = "PRIMARY_CLOSE"
            else:
                support = compute_close_support(primary, representative)
                status = close_status(support, config)
            support_by_close_ma[close_ma] = (support, status)
        continuity = recompute_continuity(
            {close_ma: status for close_ma, (_, status) in support_by_close_ma.items()},
            primary_close,
        )
        cma_representatives = [
            {
                "close_ma": close_ma,
                "point_id": str(representatives[close_ma]["point_id"]),
                "support": float(support),
                "support_status": status,
                "continuity_status": continuity[close_ma][0],
                "usable": continuity[close_ma][1],
            }
            for close_ma, (support, status) in sorted(support_by_close_ma.items())
        ]
        facts_by_plateau[str(plateau.plateau_id)] = {
            "operational_facts_version": OPERATIONAL_FACTS_VERSION,
            "primary_close_ma": primary_close,
            "cma_representatives": cma_representatives,
            "base_1ord_point_id": _frozen_base_point_id(representatives, continuity, config),
        }
        base = {
            "plateau_id": str(plateau.plateau_id),
            "symbol": str(plateau.symbol),
            "side": str(plateau.side),
            "timeframe": str(plateau.timeframe),
        }
        for close_ma in sorted(support_by_close_ma):
            support, status = support_by_close_ma[close_ma]
            cont_status, usable = continuity[close_ma]
            profile_rows.append(
                {
                    **base,
                    "close_ma": close_ma,
                    "support": float(support),
                    "status": status,
                    "point_id": str(representatives[close_ma]["point_id"]),
                    "refine_required": False,
                    "continuity_status": cont_status,
                    "usable": usable,
                }
            )

    updated["operational_facts_version"] = updated["plateau_id"].map(
        lambda plateau_id: _plateau_fact(facts_by_plateau, plateau_id, "operational_facts_version")
    )
    updated["primary_close_ma"] = updated["plateau_id"].map(
        lambda plateau_id: _plateau_fact(facts_by_plateau, plateau_id, "primary_close_ma")
    )
    updated["cma_representatives"] = updated["plateau_id"].map(
        lambda plateau_id: _plateau_fact(facts_by_plateau, plateau_id, "cma_representatives")
    )
    updated["base_1ord_point_id"] = updated["plateau_id"].map(
        lambda plateau_id: _plateau_fact(facts_by_plateau, plateau_id, "base_1ord_point_id")
    )
    updated["close_refine_required"] = False
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
    """Replay at most one frozen BASE per pair+side from per-Plateau facts.

    Each Plateau BASE was frozen during representative selection from
    continuity-usable representatives whose source point is standalone-eligible;
    this replay never re-enumerates arbitrary Plateau standalone points.
    """
    empty = pd.DataFrame(columns=[*points.columns, "selection_type"])
    if plateaus.empty:
        return empty
    if "base_1ord_point_id" not in plateaus.columns:
        raise ValueError("plateaus are missing frozen operational facts")
    base_rows = plateaus.loc[
        plateaus["ready"] & plateaus["base_1ord_point_id"].notna()
    ]
    local_rows: list[pd.Series] = []
    for row in base_rows.itertuples(index=False):
        point_id = str(row.base_1ord_point_id)
        matches = points.loc[points["point_id"].eq(point_id)]
        if len(matches) != 1:
            raise ValueError(f"frozen base point {point_id} is missing or duplicated")
        local_rows.append(matches.iloc[0].copy())
    if not local_rows:
        return empty
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


def required_gap_bp(left_shift_bp: int, config: AlgorithmConfig) -> int:
    """Resolve the minimum required gap for the smaller/left sorted Shift."""
    for lower_min_bp, lower_max_exclusive_bp, min_gap_bp in config.gap_rules:
        if lower_min_bp <= left_shift_bp < lower_max_exclusive_bp:
            return int(min_gap_bp)
    raise ValueError(
        f"left shift {left_shift_bp} is not covered by configured gap rules"
    )


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
    for left, right in zip(shifts, shifts[1:]):
        if right - left < required_gap_bp(left, config):
            return "GAP_TOO_SMALL"
    return "READY_MRS3_STRUCTURE"


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


def _order_from_point(point: pd.Series, close_support: float) -> dict[str, object]:
    point_event_count = point.get("point_event_count")
    if pd.isna(point_event_count):
        if point.get("event_mode") == "real_independent_events":
            raise ValueError("point_event_count is required for real_independent_events")
        point_event_count = point["trades"]
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
        "point_event_count": int(point_event_count),
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
    points = points.copy()
    if "event_eligible" not in points:
        points["event_eligible"] = points["economic_pass"]
    usable_status = {"PRIMARY_CLOSE", "CORE_CLOSE", "SUPPORTED_CLOSE"}
    usable = close_profiles.loc[
        close_profiles["status"].isin(usable_status)
        & close_profiles["support"].ge(float(config.close_supported_min))
        & close_profiles["continuity_status"].eq("USABLE")
        & close_profiles["usable"].eq(True)
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
        point_by_plateau = {
            str(row.plateau_id): str(row.point_id) for row in family.itertuples(index=False)
        }
        candidates_by_plateau: dict[str, list[pd.Series]] = {}
        for plateau_id in sorted(support_by_plateau):
            candidates = points.loc[
                points["point_id"].eq(point_by_plateau[plateau_id])
                & points["economic_pass"]
                & points["event_eligible"]
            ].copy()
            candidates_by_plateau[plateau_id] = [] if candidates.empty else [candidates.iloc[0]]
        plateau_ids = [
            plateau_id
            for plateau_id in sorted(candidates_by_plateau)
            if candidates_by_plateau[plateau_id]
        ]
        for order_count in range(2, min(config.max_orders, len(plateau_ids)) + 1):
            for plateau_combo in itertools.combinations(plateau_ids, order_count):
                ready_tuples: list[list[dict[str, object]]] = []
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
                if not ready_tuples:
                    continue
                orders = ready_tuples[0]
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
                        "source_eff_mean": sum(
                            float(order["source_efficiency"]) for order in numbered_orders
                        )
                        / order_count,
                        "low_sample_depth_count": sum(
                            not bool(order["standalone_eligible"])
                            for order in numbered_orders[1:]
                        ),
                        "Order1EventCount": int(numbered_orders[0]["point_event_count"]) if len(numbered_orders) >= 1 else None,
                        "Order2EventCount": int(numbered_orders[1]["point_event_count"]) if len(numbered_orders) >= 2 else None,
                        "Order3EventCount": int(numbered_orders[2]["point_event_count"]) if len(numbered_orders) >= 3 else None,
                        "Order4EventCount": int(numbered_orders[3]["point_event_count"]) if len(numbered_orders) >= 4 else None,
                        "status": "READY_MRS3_STRUCTURE",
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
                "source_eff_mean",
                "low_sample_depth_count",
                "structure_id",
            ],
            ascending=[True, True, True, True, False, False, True, True],
            kind="mergesort",
        ).reset_index(drop=True)
    diagnostics = pd.DataFrame(
        diagnostic_rows, columns=STRUCTURE_DIAGNOSTIC_COLUMNS
    )
    return structures, diagnostics
