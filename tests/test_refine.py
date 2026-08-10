from __future__ import annotations

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.refine import (
    ShiftDomain,
    annotate_refine,
    are_shift_neighbors,
    required_shift_neighbors,
)


DOMAIN = ShiftDomain(min_bp=30, max_bp=470)


def test_fine_center_1_0_requires_point_one_grid() -> None:
    tested = tuple(range(30, 131, 10))
    assert required_shift_neighbors(100, tested, DOMAIN) == (
        70,
        80,
        90,
        100,
        110,
        120,
        130,
    )


@pytest.mark.parametrize(
    ("center", "required"),
    [
        (150, (120, 130, 140, 150, 190)),
        (160, (130, 140, 150, 160, 190)),
        (170, (140, 150, 160, 170, 190)),
    ],
)
def test_boundary_zone_is_asymmetric(center: int, required: tuple[int, ...]) -> None:
    tested = (30, 70, 110, 150, 190, 230)
    assert required_shift_neighbors(center, tested, DOMAIN) == required


def test_shift_point_three_is_one_sided() -> None:
    assert required_shift_neighbors(30, (30, 40, 50, 60), DOMAIN) == (
        30,
        40,
        50,
        60,
    )


@pytest.mark.parametrize(
    ("tested", "required"),
    [
        ((190, 210, 230, 250, 270), (190, 210, 230, 250, 270)),
        ((170, 200, 230, 260, 290), (200, 230, 260)),
        ((150, 190, 230, 270, 310), (190, 230, 270)),
        ((130, 180, 230, 280, 330), (180, 230, 280)),
    ],
)
def test_coarse_zone_uses_all_actual_neighbors_within_point_five(
    tested: tuple[int, ...], required: tuple[int, ...]
) -> None:
    assert required_shift_neighbors(230, tested, DOMAIN) == required


def test_shift_neighbor_relation_is_mutual() -> None:
    tested = (110, 150, 190, 230)
    assert are_shift_neighbors(190, 230, tested, DOMAIN)
    assert not are_shift_neighbors(150, 110, tested, DOMAIN)


def _grid_points(shifts: tuple[int, ...]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for shift in shifts:
        rows.append(
            {
                "point_id": f"AAAUSDT|LONG|2h|{shift}|3|4",
                "symbol": "AAAUSDT",
                "side": "LONG",
                "timeframe": "2h",
                "shift_bp": shift,
                "shift_pct": shift / 100,
                "open_ma": 3,
                "close_ma": 4,
            }
        )
    return pd.DataFrame(rows)


def test_missing_fine_cells_mark_center_1_5_refine_required() -> None:
    points = _grid_points((30, 70, 110, 150, 190, 230))

    annotated, missing = annotate_refine(points, AlgorithmConfig.defaults())

    center = annotated.loc[annotated["shift_bp"].eq(150)].iloc[0]
    assert center["refine_required"]
    center_missing = missing.loc[missing["center_point_id"].eq(center["point_id"])]
    assert {120, 130, 140}.issubset(set(center_missing["shift_bp"]))


def test_complete_coarse_cube_is_ready_for_refine_stage() -> None:
    rows: list[dict[str, object]] = []
    for shift in (190, 230, 270):
        for open_ma in (2, 3, 4):
            for close_ma in (3, 4, 5):
                rows.append(
                    {
                        "point_id": f"AAAUSDT|LONG|2h|{shift}|{open_ma}|{close_ma}",
                        "symbol": "AAAUSDT",
                        "side": "LONG",
                        "timeframe": "2h",
                        "shift_bp": shift,
                        "shift_pct": shift / 100,
                        "open_ma": open_ma,
                        "close_ma": close_ma,
                    }
                )
    points = pd.DataFrame(rows)

    annotated, missing = annotate_refine(points, AlgorithmConfig.defaults())

    center = annotated.query("shift_bp == 230 and open_ma == 3 and close_ma == 4").iloc[0]
    assert not center["refine_required"]
    assert center["missing_test_count"] == 0
    assert missing.loc[missing["center_point_id"].eq(center["point_id"])].empty


def test_missing_ma_neighbor_is_recorded_without_interpolation() -> None:
    points = _grid_points((190, 230, 270))
    extra = points.iloc[[0]].copy()
    extra["point_id"] = "AAAUSDT|LONG|2h|230|4|4"
    extra["shift_bp"] = 230
    extra["shift_pct"] = 2.3
    extra["open_ma"] = 4
    points = pd.concat([points, extra], ignore_index=True)

    annotated, missing = annotate_refine(points, AlgorithmConfig.defaults())

    center = annotated.query("shift_bp == 230 and open_ma == 3").iloc[0]
    assert center["refine_required"]
    assert (
        (missing["center_point_id"] == center["point_id"])
        & (missing["shift_bp"] == 190)
        & (missing["open_ma"] == 4)
        & (missing["close_ma"] == 4)
    ).any()

