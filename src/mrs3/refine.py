from __future__ import annotations

import pandas as pd

from .config import AlgorithmConfig


def canonical_shift_neighbors(
    center_bp: int,
    canonical_shifts_bp: tuple[int, ...],
) -> tuple[int, ...]:
    """Return the immediate left/right neighbors of center_bp in the canonical tuple.

    Adjacency is defined only by neighboring elements of the canonical shift
    tuple; numeric distance or tested sparse values never infer neighbors.
    Non-canonical centers are rejected deterministically.
    """
    canonical = tuple(int(value) for value in canonical_shifts_bp)
    try:
        index = canonical.index(center_bp)
    except ValueError as error:
        raise ValueError(f"center shift {center_bp} is not a canonical shift") from error
    left = canonical[index - 1] if index > 0 else None
    right = canonical[index + 1] if index + 1 < len(canonical) else None
    return tuple(value for value in (left, right) if value is not None)


def required_shift_neighbors(
    center_bp: int,
    canonical_shifts_bp: tuple[int, ...],
) -> tuple[int, ...]:
    """Current shift plus its immediate canonical left/right neighbors."""
    return (center_bp, *canonical_shift_neighbors(center_bp, canonical_shifts_bp))


def are_shift_neighbors(
    left_bp: int,
    right_bp: int,
    canonical_shifts_bp: tuple[int, ...],
) -> bool:
    return (
        right_bp in canonical_shift_neighbors(left_bp, canonical_shifts_bp)
        and left_bp in canonical_shift_neighbors(right_bp, canonical_shifts_bp)
    )


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
        open_min, open_max = int(group["open_ma"].min()), int(group["open_ma"].max())
        close_min, close_max = int(group["close_ma"].min()), int(group["close_ma"].max())
        existing = {
            (int(row.shift_bp), int(row.open_ma), int(row.close_ma))
            for row in group.itertuples(index=False)
        }

        for row in group.sort_values("point_id", kind="mergesort").itertuples(index=False):
            required_shifts = required_shift_neighbors(
                int(row.shift_bp), config.canonical_shifts_bp
            )
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

