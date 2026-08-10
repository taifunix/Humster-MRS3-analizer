from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from mrs3.config import AlgorithmConfig


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"shift_domain_min_bp": 470, "shift_domain_max_bp": 30},
            "shift domain",
        ),
        ({"max_orders": 5}, "max_orders"),
        (
            {
                "shift_factors": (
                    (200, Decimal("0.9")),
                    (150, Decimal("1.0")),
                )
            },
            "shift factor boundaries",
        ),
        (
            {
                "core_link_min": Decimal("0.70"),
                "plateau_envelope_min": Decimal("0.75"),
            },
            "plateau thresholds",
        ),
        (
            {
                "close_core_min": Decimal("0.70"),
                "close_supported_min": Decimal("0.75"),
            },
            "close support",
        ),
        (
            {"gap_mid_start_bp": 410, "deep_gap_boundary_bp": 400},
            "gap boundaries",
        ),
        ({"initial_lot_sum": Decimal("0")}, "initial_lot_sum"),
        ({"base_columns": {}}, "base column mappings"),
    ],
)
def test_algorithm_config_rejects_invalid_semantics(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(AlgorithmConfig.defaults(), **changes)


def test_shift_domain_can_extend_beyond_calibrated_shift_factors() -> None:
    config = replace(AlgorithmConfig.defaults(), shift_domain_max_bp=700)

    assert config.shift_domain_max_bp == 700
