from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import re

import pytest

from mrs3.screener.config import ScreenerConfig, load_screener_config


def test_screener_config_defaults(tmp_path) -> None:
    defaults = ScreenerConfig()
    assert defaults == ScreenerConfig(
        stop_best_pnl30=Decimal("8"),
        go_min_good=8,
        big_min_good=5,
        good_pnl30=Decimal("10"),
        big_shift_bp=110,
        expected_combos_per_pair=304,
        liquidity_registry_path=None,
    )
    assert load_screener_config(tmp_path / "missing.json") == defaults


def test_load_screener_config_overrides_from_section(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(
        json.dumps(
            {
                "screener": {
                    "stop_best_pnl30": 7,
                    "go_min_good": 3,
                    "big_min_good": 2,
                    "good_pnl30": 10,
                    "big_shift_bp": 110,
                    "expected_combos_per_pair": 112,
                    "liquidity_registry_path": "input/bybit_tradfi_liquidity.xlsx",
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_screener_config(path) == ScreenerConfig(
        stop_best_pnl30=Decimal("7"),
        go_min_good=3,
        big_min_good=2,
        good_pnl30=Decimal("10"),
        big_shift_bp=110,
        expected_combos_per_pair=112,
        liquidity_registry_path=Path("input/bybit_tradfi_liquidity.xlsx"),
    )


def test_load_screener_config_rejects_non_string_liquidity_registry_path(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"screener": {"liquidity_registry_path": 5}}), encoding="utf-8")
    with pytest.raises(
        ValueError, match="screener.liquidity_registry_path must be a string or null"
    ):
        load_screener_config(path)


def test_screener_config_rejects_non_path_liquidity_registry_path() -> None:
    with pytest.raises(
        ValueError, match="screener.liquidity_registry_path must be a path or null"
    ):
        ScreenerConfig(liquidity_registry_path="input/bybit_tradfi_liquidity.xlsx")


def test_load_screener_config_partial_section_uses_dataclass_defaults(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"screener": {"go_min_good": 3}}), encoding="utf-8")
    assert load_screener_config(path) == replace(ScreenerConfig(), go_min_good=3)


def test_load_screener_config_rejects_unknown_keys(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"screener": {"go_min_god": 3}}), encoding="utf-8")
    with pytest.raises(
        ValueError, match=re.escape("screener contains unknown keys: go_min_god")
    ):
        load_screener_config(path)


def test_load_screener_config_rejects_non_object_section(tmp_path) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"screener": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="screener must be an object"):
        load_screener_config(path)


@pytest.mark.parametrize("value", [True, "abc", [1, 2], {"x": 1}, None])
def test_load_screener_config_rejects_non_decimal_pnl_fields(tmp_path, value: object) -> None:
    path = tmp_path / "config.local.json"
    path.write_text(json.dumps({"screener": {"stop_best_pnl30": value}}), encoding="utf-8")
    with pytest.raises(ValueError, match="screener.stop_best_pnl30 must be a positive number"):
        load_screener_config(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("stop_best_pnl30", Decimal("0")),
        ("stop_best_pnl30", Decimal("-1")),
        ("good_pnl30", Decimal("0")),
        ("go_min_good", 0),
        ("go_min_good", True),
        ("big_min_good", 0),
        ("big_shift_bp", 0),
        ("expected_combos_per_pair", 0),
    ],
)
def test_screener_config_rejects_invalid_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        ScreenerConfig(**{field: value})
