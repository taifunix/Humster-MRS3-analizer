from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json

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


@pytest.mark.parametrize(
    ("base_rate_tf", "message"),
    [
        (None, "base_rate_tf must be an object"),
        ({"2h": None}, "base_rate_tf.2h"),
        ([], "base_rate_tf must be an object"),
    ],
)
def test_from_json_rejects_null_or_non_object_base_rate_config(
    tmp_path, base_rate_tf: object, message: str
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_rate_tf": base_rate_tf}), encoding="utf-8")

    with pytest.raises(ValueError, match=message) as error:
        AlgorithmConfig.from_json(path)

    assert error.value.__cause__ is not None


@pytest.mark.parametrize("rate", [[], {}, True])
def test_from_json_rejects_non_scalar_base_rate_before_decimal(
    tmp_path, rate: object
) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_rate_tf": {"2h": rate}}), encoding="utf-8")

    with pytest.raises(ValueError, match="base_rate_tf.2h") as error:
        AlgorithmConfig.from_json(path)

    assert isinstance(error.value.__cause__, TypeError)
