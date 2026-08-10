from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import AlgorithmConfig


@dataclass(frozen=True, slots=True)
class ShiftDomain:
    min_bp: int
    max_bp: int

    def __post_init__(self) -> None:
        if self.min_bp > self.max_bp:
            raise ValueError("shift domain minimum exceeds maximum")


def _fine_values(start: int, stop: int, step: int) -> tuple[int, ...]:
    if start > stop:
        return ()
    first = ((start + step - 1) // step) * step
    return tuple(range(first, stop + 1, step))


def required_shift_neighbors(
    center_bp: int,
    tested_bp: tuple[int, ...],
    domain: ShiftDomain,
    *,
    fine_zone_max_exclusive_bp: int = 150,
    boundary_zone_max_bp: int = 170,
    fine_step_bp: int = 10,
    fine_radius_bp: int = 30,
    boundary_down_radius_bp: int = 30,
    boundary_up_radius_bp: int = 50,
    coarse_radius_bp: int = 50,
) -> tuple[int, ...]:
    if not domain.min_bp <= center_bp <= domain.max_bp:
        raise ValueError(f"center shift {center_bp} is outside declared domain")
    tested = tuple(sorted(set(int(value) for value in tested_bp)))

    if center_bp < fine_zone_max_exclusive_bp:
        start = max(domain.min_bp, center_bp - fine_radius_bp)
        stop = min(domain.max_bp, center_bp + fine_radius_bp)
        return _fine_values(start, stop, fine_step_bp)

    if center_bp <= boundary_zone_max_bp:
        lower = _fine_values(
            max(domain.min_bp, center_bp - boundary_down_radius_bp),
            center_bp - fine_step_bp,
            fine_step_bp,
        )
        upper = tuple(
            value
            for value in tested
            if center_bp < value <= min(domain.max_bp, center_bp + boundary_up_radius_bp)
        )
        if not upper and center_bp < domain.max_bp:
            upper = (min(domain.max_bp, center_bp + boundary_up_radius_bp),)
        return tuple(sorted(set((*lower, center_bp, *upper))))

    actual = {
        value
        for value in tested
        if abs(value - center_bp) <= coarse_radius_bp
    }
    actual.add(center_bp)
    if center_bp > domain.min_bp and not any(value < center_bp for value in actual):
        actual.add(max(domain.min_bp, center_bp - coarse_radius_bp))
    if center_bp < domain.max_bp and not any(value > center_bp for value in actual):
        actual.add(min(domain.max_bp, center_bp + coarse_radius_bp))
    return tuple(sorted(actual))


def _required_for_config(
    center_bp: int,
    tested_bp: tuple[int, ...],
    config: AlgorithmConfig,
) -> tuple[int, ...]:
    return required_shift_neighbors(
        center_bp,
        tested_bp,
        ShiftDomain(config.shift_domain_min_bp, config.shift_domain_max_bp),
        fine_zone_max_exclusive_bp=config.fine_zone_max_exclusive_bp,
        boundary_zone_max_bp=config.boundary_zone_max_bp,
        fine_step_bp=config.fine_step_bp,
        fine_radius_bp=config.fine_radius_bp,
        boundary_down_radius_bp=config.boundary_down_radius_bp,
        boundary_up_radius_bp=config.boundary_up_radius_bp,
        coarse_radius_bp=config.coarse_radius_bp,
    )


def are_shift_neighbors(
    left_bp: int,
    right_bp: int,
    tested_bp: tuple[int, ...],
    domain: ShiftDomain,
    **rules: int,
) -> bool:
    left_required = required_shift_neighbors(left_bp, tested_bp, domain, **rules)
    right_required = required_shift_neighbors(right_bp, tested_bp, domain, **rules)
    return right_bp in left_required and left_bp in right_required


def _ma_neighbors(center: int, minimum: int, maximum: int, radius: int) -> tuple[int, ...]:
    return tuple(range(max(minimum, center - radius), min(maximum, center + radius) + 1))


def annotate_refine(
    points: pd.DataFrame,
    config: AlgorithmConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    required_columns = {
        "point_id",
        "symbol",
        "side",
        "timeframe",
        "shift_bp",
        "open_ma",
        "close_ma",
    }
    missing_columns = sorted(required_columns.difference(points.columns))
    if missing_columns:
        raise ValueError(f"refine input missing columns: {missing_columns}")

    out = points.copy()
    missing_rows: list[dict[str, object]] = []
    missing_by_point: dict[str, tuple[str, ...]] = {}
    group_columns = ["symbol", "side", "timeframe"]

    for keys, group in out.groupby(group_columns, sort=True):
        symbol, side, timeframe = (str(value) for value in keys)
        tested_shifts = tuple(sorted(int(value) for value in group["shift_bp"].unique()))
        open_min, open_max = int(group["open_ma"].min()), int(group["open_ma"].max())
        close_min, close_max = int(group["close_ma"].min()), int(group["close_ma"].max())
        existing = {
            (int(row.shift_bp), int(row.open_ma), int(row.close_ma))
            for row in group.itertuples(index=False)
        }

        for row in group.sort_values("point_id", kind="mergesort").itertuples(index=False):
            required_shifts = _required_for_config(int(row.shift_bp), tested_shifts, config)
            required_open = _ma_neighbors(
                int(row.open_ma), open_min, open_max, config.ma_neighbor_radius
            )
            required_close = _ma_neighbors(
                int(row.close_ma), close_min, close_max, config.ma_neighbor_radius
            )
            point_missing: list[str] = []
            for shift_bp in required_shifts:
                for open_ma in required_open:
                    for close_ma in required_close:
                        cell = (shift_bp, open_ma, close_ma)
                        if cell in existing:
                            continue
                        target_id = (
                            f"{symbol}|{side}|{timeframe}|{shift_bp}|{open_ma}|{close_ma}"
                        )
                        point_missing.append(target_id)
                        missing_rows.append(
                            {
                                "center_point_id": str(row.point_id),
                                "target_point_id": target_id,
                                "symbol": symbol,
                                "side": side,
                                "timeframe": timeframe,
                                "shift_bp": shift_bp,
                                "shift_pct": shift_bp / 100.0,
                                "open_ma": open_ma,
                                "close_ma": close_ma,
                                "reason": "SHIFT_REFINE_REQUIRED",
                            }
                        )
            missing_by_point[str(row.point_id)] = tuple(sorted(point_missing))

    out["missing_tests"] = out["point_id"].map(missing_by_point)
    out["missing_test_count"] = out["missing_tests"].map(len)
    out["refine_required"] = out["missing_test_count"].gt(0)
    missing = pd.DataFrame(
        missing_rows,
        columns=[
            "center_point_id",
            "target_point_id",
            "symbol",
            "side",
            "timeframe",
            "shift_bp",
            "shift_pct",
            "open_ma",
            "close_ma",
            "reason",
        ],
    )
    if not missing.empty:
        missing = missing.sort_values(
            ["symbol", "side", "timeframe", "center_point_id", "shift_bp", "open_ma", "close_ma"],
            kind="mergesort",
        ).reset_index(drop=True)
    return out, missing

