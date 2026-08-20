from __future__ import annotations

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.refine import (
    annotate_refine,
    are_shift_neighbors,
    canonical_shift_neighbors,
    required_shift_neighbors,
)


CANONICAL = AlgorithmConfig.defaults().canonical_shifts_bp


@pytest.mark.parametrize(
    ("center", "expected"),
    [
        (30, (40,)),
        (40, (30, 50)),
        (70, (60, 90)),
        (90, (70, 110)),
        (110, (90, 140)),
        (470, (430, 510)),
        (510, (470, 550)),
        (550, (510,)),
    ],
)
def test_canonical_shift_neighbors_are_immediate_tuple_elements(
    center: int, expected: tuple[int, ...]
) -> None:
    assert canonical_shift_neighbors(center, CANONICAL) == expected


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (30, 40, True),
        (70, 90, True),
        (90, 110, True),
        (470, 510, True),
        (510, 550, True),
        (70, 110, False),
        (140, 200, False),
        (430, 510, False),
    ],
)
def test_canonical_adjacency_examples(left: int, right: int, expected: bool) -> None:
    assert are_shift_neighbors(left, right, CANONICAL) is expected
    assert are_shift_neighbors(right, left, CANONICAL) is expected


def test_required_shift_neighbors_are_current_plus_immediate_canonical_neighbors() -> None:
    assert required_shift_neighbors(90, CANONICAL) == (90, 70, 110)
    assert required_shift_neighbors(30, CANONICAL) == (30, 40)
    assert required_shift_neighbors(550, CANONICAL) == (550, 510)


def test_required_shifts_never_exceed_canonical_domain() -> None:
    for center in CANONICAL:
        required = required_shift_neighbors(center, CANONICAL)
        assert required
        assert all(30 <= shift <= 550 for shift in required)
        assert set(required).issubset(set(CANONICAL))


@pytest.mark.parametrize("center", [15, 80, 150, 555])
def test_non_canonical_center_is_rejected(center: int) -> None:
    with pytest.raises(ValueError):
        canonical_shift_neighbors(center, CANONICAL)
    with pytest.raises(ValueError):
        required_shift_neighbors(center, CANONICAL)


def test_shift_neighbor_relation_is_mutual() -> None:
    assert are_shift_neighbors(90, 110, CANONICAL)
    assert not are_shift_neighbors(70, 110, CANONICAL)


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


def test_complete_canonical_cube_is_ready_for_refine_stage() -> None:
    rows: list[dict[str, object]] = []
    for shift in (200, 230, 270):
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
    points = _grid_points((200, 230, 270))
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
        & (missing["shift_bp"] == 200)
        & (missing["open_ma"] == 4)
        & (missing["close_ma"] == 4)
    ).any()


def test_refine_never_requests_shifts_outside_canonical_domain() -> None:
    points = _grid_points((30, 90, 550))

    _, missing = annotate_refine(points, AlgorithmConfig.defaults())

    assert not missing.empty
    assert missing["shift_bp"].between(30, 550).all()
    assert set(missing["shift_bp"]).issubset(set(CANONICAL))
