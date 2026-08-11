from __future__ import annotations

from decimal import Decimal
import hashlib
from typing import Iterable, Mapping

import pandas as pd

from .config import AlgorithmConfig
from .refine import ShiftDomain, are_shift_neighbors


MetricPoint = Mapping[str, object]

PLATEAU_COLUMNS = [
    "plateau_id",
    "symbol",
    "side",
    "timeframe",
    "core_point_ids",
    "supported_point_ids",
    "all_point_ids",
    "core_size",
    "supported_size",
    "min_shift_bp",
    "max_shift_bp",
    "open_ma_min",
    "open_ma_max",
    "close_ma_min",
    "close_ma_max",
    "best_pnl_point_id",
    "best_eff_point_id",
    "standalone_eligible_point_ids",
    "depth_eligible_point_ids",
    "envelope",
    "plateau_event_count",
    "plateau_event_ids_hash",
    "status",
    "ready",
]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _retention(left: object, right: object) -> Decimal:
    a, b = _decimal(left), _decimal(right)
    high = max(a, b)
    if high <= 0:
        return Decimal("0")
    return min(a, b) / high


def core_link(left: MetricPoint, right: MetricPoint) -> Decimal:
    return min(
        _retention(left["pnl_pct"], right["pnl_pct"]),
        _retention(left["efficiency"], right["efficiency"]),
    )


def component_envelope(points: Iterable[MetricPoint]) -> Decimal:
    rows = tuple(points)
    if not rows:
        return Decimal("0")
    pnls = [_decimal(point["pnl_pct"]) for point in rows]
    efficiencies = [_decimal(point["efficiency"]) for point in rows]
    if max(pnls) <= 0 or max(efficiencies) <= 0:
        return Decimal("0")
    return min(min(pnls) / max(pnls), min(efficiencies) / max(efficiencies))


class _UnionFind:
    def __init__(self, ids: Iterable[str]) -> None:
        self.parent = {point_id: point_id for point_id in ids}
        self.members = {point_id: {point_id} for point_id in ids}

    def find(self, point_id: str) -> str:
        parent = self.parent[point_id]
        if parent != point_id:
            self.parent[point_id] = self.find(parent)
        return self.parent[point_id]

    def union(self, left: str, right: str) -> str:
        root_left, root_right = self.find(left), self.find(right)
        if root_left == root_right:
            return root_left
        keep, merge = sorted((root_left, root_right))
        self.parent[merge] = keep
        self.members[keep].update(self.members.pop(merge))
        return keep


def _shift_rule_kwargs(config: AlgorithmConfig) -> dict[str, int]:
    return {
        "fine_zone_max_exclusive_bp": config.fine_zone_max_exclusive_bp,
        "boundary_zone_max_bp": config.boundary_zone_max_bp,
        "fine_step_bp": config.fine_step_bp,
        "fine_radius_bp": config.fine_radius_bp,
        "boundary_down_radius_bp": config.boundary_down_radius_bp,
        "boundary_up_radius_bp": config.boundary_up_radius_bp,
        "coarse_radius_bp": config.coarse_radius_bp,
    }


def _geometric_neighbors(
    left: MetricPoint,
    right: MetricPoint,
    tested_shifts: tuple[int, ...],
    config: AlgorithmConfig,
) -> bool:
    if abs(int(left["open_ma"]) - int(right["open_ma"])) > config.ma_neighbor_radius:
        return False
    if abs(int(left["close_ma"]) - int(right["close_ma"])) > config.ma_neighbor_radius:
        return False
    return are_shift_neighbors(
        int(left["shift_bp"]),
        int(right["shift_bp"]),
        tested_shifts,
        ShiftDomain(config.shift_domain_min_bp, config.shift_domain_max_bp),
        **_shift_rule_kwargs(config),
    )


def _plateau_id(
    symbol: str,
    side: str,
    timeframe: str,
    core_ids: Iterable[str],
) -> str:
    payload = f"{symbol}|{side}|{timeframe}|{'|'.join(sorted(core_ids))}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"PLT_{digest}"


def _best_point_id(
    ids: Iterable[str], records: dict[str, dict[str, object]], metric: str
) -> str:
    return sorted(
        ids,
        key=lambda point_id: (
            -float(records[point_id][metric]),
            -float(records[point_id]["pnl_pct"]),
            point_id,
        ),
    )[0]


def _build_group_plateaus(
    group: pd.DataFrame,
    config: AlgorithmConfig,
) -> list[dict[str, object]]:
    candidate = group.loc[group["economic_pass"] & ~group["refine_required"]].copy()
    if len(candidate) < 2:
        return []
    records = {
        str(row["point_id"]): row.to_dict()
        for _, row in candidate.sort_values("point_id", kind="mergesort").iterrows()
    }
    tested_shifts = tuple(sorted(int(value) for value in group["shift_bp"].unique()))
    lookup = {
        (int(row["shift_bp"]), int(row["open_ma"]), int(row["close_ma"])): str(
            row["point_id"]
        )
        for _, row in candidate.iterrows()
    }
    shift_neighbors: dict[int, tuple[int, ...]] = {}
    for shift in tested_shifts:
        shift_neighbors[shift] = tuple(
            other
            for other in tested_shifts
            if are_shift_neighbors(
                shift,
                other,
                tested_shifts,
                ShiftDomain(config.shift_domain_min_bp, config.shift_domain_max_bp),
                **_shift_rule_kwargs(config),
            )
        )

    edges: list[tuple[Decimal, str, str]] = []
    for point_id, point in records.items():
        for shift in shift_neighbors[int(point["shift_bp"])]:
            for open_ma in range(
                int(point["open_ma"]) - config.ma_neighbor_radius,
                int(point["open_ma"]) + config.ma_neighbor_radius + 1,
            ):
                for close_ma in range(
                    int(point["close_ma"]) - config.ma_neighbor_radius,
                    int(point["close_ma"]) + config.ma_neighbor_radius + 1,
                ):
                    other_id = lookup.get((shift, open_ma, close_ma))
                    if other_id is None or point_id >= other_id:
                        continue
                    link = core_link(point, records[other_id])
                    if link >= config.core_link_min:
                        edges.append((link, point_id, other_id))
    edges.sort(key=lambda item: (-item[0], item[1], item[2]))

    union_find = _UnionFind(records)
    for _, left_id, right_id in edges:
        left_root, right_root = union_find.find(left_id), union_find.find(right_id)
        if left_root == right_root:
            continue
        combined = union_find.members[left_root] | union_find.members[right_root]
        if component_envelope(records[point_id] for point_id in combined) >= config.plateau_envelope_min:
            union_find.union(left_id, right_id)

    core_components = [
        set(members)
        for _, members in sorted(union_find.members.items())
        if len(members) >= 2
    ]
    if not core_components:
        return []
    first = candidate.iloc[0]
    plateaus: list[dict[str, object]] = []
    for core_ids in core_components:
        plateau_id = _plateau_id(
            str(first["symbol"]), str(first["side"]), str(first["timeframe"]), core_ids
        )
        plateaus.append(
            {"plateau_id": plateau_id, "core": set(core_ids), "supported": set()}
        )
    plateaus.sort(key=lambda plateau: str(plateau["plateau_id"]))

    core_member_ids = set().union(*(plateau["core"] for plateau in plateaus))
    border_ids = sorted(set(records).difference(core_member_ids))
    border_order: list[tuple[Decimal, str]] = []
    for point_id in border_ids:
        supports = [
            core_link(records[point_id], records[core_id])
            for plateau in plateaus
            for core_id in plateau["core"]
            if _geometric_neighbors(records[point_id], records[core_id], tested_shifts, config)
        ]
        border_order.append((max(supports, default=Decimal("0")), point_id))
    border_order.sort(key=lambda item: (-item[0], item[1]))

    for _, point_id in border_order:
        proposals: list[tuple[Decimal, Decimal, str, dict[str, object]]] = []
        for plateau in plateaus:
            local_support = max(
                (
                    core_link(records[point_id], records[core_id])
                    for core_id in plateau["core"]
                    if _geometric_neighbors(
                        records[point_id], records[core_id], tested_shifts, config
                    )
                ),
                default=Decimal("0"),
            )
            all_ids = plateau["core"] | plateau["supported"] | {point_id}
            if local_support < config.supported_link_min:
                continue
            if component_envelope(records[member] for member in all_ids) < config.plateau_envelope_min:
                continue
            max_pnl = max(_decimal(records[member]["pnl_pct"]) for member in plateau["core"])
            proposals.append((local_support, max_pnl, str(plateau["plateau_id"]), plateau))
        if proposals:
            proposals.sort(key=lambda item: (-item[0], -item[1], item[2]))
            proposals[0][3]["supported"].add(point_id)

    output: list[dict[str, object]] = []
    for plateau in plateaus:
        core_ids = tuple(sorted(plateau["core"]))
        supported_ids = tuple(sorted(plateau["supported"]))
        all_ids = tuple(sorted((*core_ids, *supported_ids)))
        rows = [records[point_id] for point_id in all_ids]
        standalone_ids = tuple(
            point_id
            for point_id in all_ids
            if bool(records[point_id]["economic_pass"])
            and bool(records[point_id]["standalone_sample_pass"])
            and bool(records[point_id]["history_pass"])
            and bool(records[point_id].get("event_eligible", True))
        )
        depth_ids = tuple(
            point_id
            for point_id in all_ids
            if bool(records[point_id]["economic_pass"])
            and bool(records[point_id].get("event_eligible", True))
        )
        mrs3_usable = any(bool(records[point_id].get("event_eligible", True)) and bool(records[point_id].get("economic_pass", False)) for point_id in all_ids)
        output.append(
            {
                "plateau_id": plateau["plateau_id"],
                "symbol": str(first["symbol"]),
                "side": str(first["side"]),
                "timeframe": str(first["timeframe"]),
                "core_point_ids": core_ids,
                "supported_point_ids": supported_ids,
                "all_point_ids": all_ids,
                "core_size": len(core_ids),
                "supported_size": len(supported_ids),
                "min_shift_bp": min(int(row["shift_bp"]) for row in rows),
                "max_shift_bp": max(int(row["shift_bp"]) for row in rows),
                "open_ma_min": min(int(row["open_ma"]) for row in rows),
                "open_ma_max": max(int(row["open_ma"]) for row in rows),
                "close_ma_min": min(int(row["close_ma"]) for row in rows),
                "close_ma_max": max(int(row["close_ma"]) for row in rows),
                "best_pnl_point_id": _best_point_id(all_ids, records, "pnl_pct"),
                "best_eff_point_id": _best_point_id(all_ids, records, "efficiency"),
                "standalone_eligible_point_ids": standalone_ids,
                "depth_eligible_point_ids": depth_ids,
                "envelope": float(component_envelope(rows)),
                "plateau_event_count": None,
                "plateau_event_ids_hash": None,
                "status": "MRS3_USABLE" if mrs3_usable else "INSUFFICIENT_INDEPENDENT_EVENTS",
                "ready": mrs3_usable,
            }
        )
    return output


def build_plateaus(
    points: pd.DataFrame,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    points = points.copy()
    if "event_eligible" not in points:
        points["event_eligible"] = True
    required = {
        "point_id",
        "symbol",
        "side",
        "timeframe",
        "shift_bp",
        "open_ma",
        "close_ma",
        "pnl_pct",
        "efficiency",
        "economic_pass",
        "refine_required",
        "standalone_sample_pass",
        "history_pass",
    }
    missing = sorted(required.difference(points.columns))
    if missing:
        raise ValueError(f"plateau input missing columns: {missing}")

    plateau_rows: list[dict[str, object]] = []
    for _, group in points.groupby(["symbol", "side", "timeframe"], sort=True):
        plateau_rows.extend(_build_group_plateaus(group, config))
    plateaus = pd.DataFrame(plateau_rows, columns=PLATEAU_COLUMNS)
    if not plateaus.empty:
        plateaus = plateaus.sort_values(
            ["symbol", "side", "timeframe", "plateau_id"], kind="mergesort"
        ).reset_index(drop=True)

    annotated = points.copy()
    point_to_plateau: dict[str, str] = {}
    point_to_role: dict[str, str] = {}
    if not plateaus.empty:
        for row in plateaus.itertuples(index=False):
            for point_id in row.core_point_ids:
                point_to_plateau[point_id] = row.plateau_id
                point_to_role[point_id] = "CORE"
            for point_id in row.supported_point_ids:
                point_to_plateau[point_id] = row.plateau_id
                point_to_role[point_id] = "SUPPORTED"
    annotated["plateau_id"] = annotated["point_id"].map(point_to_plateau)
    annotated["plateau_role"] = annotated["point_id"].map(point_to_role)
    in_plateau = annotated["plateau_id"].notna()
    annotated["standalone_eligible"] = (
        in_plateau
        & annotated["economic_pass"]
        & annotated["standalone_sample_pass"]
        & annotated["history_pass"]
        & annotated["event_eligible"]
    )
    annotated["depth_eligible"] = in_plateau & annotated["economic_pass"] & annotated["event_eligible"]
    return annotated, plateaus


def find_isolated_peaks(
    points: pd.DataFrame,
    config: AlgorithmConfig,
) -> pd.DataFrame:
    reference = points.loc[
        points["economic_pass"]
        & points["standalone_sample_pass"]
        & points["history_pass"]
    ].copy()
    eligible = reference.loc[reference["plateau_id"].isna()].copy()
    rows: list[pd.DataFrame] = []
    for _, group in eligible.groupby(["symbol", "side", "timeframe"], sort=True):
        first = group.iloc[0]
        peers = reference.loc[
            reference["symbol"].eq(first["symbol"])
            & reference["side"].eq(first["side"])
            & reference["timeframe"].eq(first["timeframe"])
        ]
        best_pnl = float(peers["pnl_pct"].max())
        best_eff = float(peers["efficiency"].max())
        threshold = float(config.isolated_peak_relative)
        selected = group.loc[
            (group["pnl_pct"] >= threshold * best_pnl)
            | (group["efficiency"] >= threshold * best_eff)
        ].copy()
        selected["status"] = "ISOLATED_PEAK"
        rows.append(selected)
    if not rows:
        return pd.DataFrame(columns=[*points.columns, "status"])
    return pd.concat(rows, ignore_index=True).sort_values(
        ["symbol", "side", "timeframe", "point_id"], kind="mergesort"
    ).reset_index(drop=True)
