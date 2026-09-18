"""Screener-specific verdict thresholds, loaded from config.local.json."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from mrs3.config import _local_config_object


def _optional_screener_path(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("screener.liquidity_registry_path must be a string or null")
    return Path(value)


@dataclass(frozen=True, slots=True)
class ScreenerConfig:
    stop_best_pnl30: Decimal = Decimal("8")
    go_min_good: int = 8
    big_min_good: int = 5
    good_pnl30: Decimal = Decimal("10")
    big_shift_bp: int = 110
    expected_combos_per_pair: int = 304
    liquidity_registry_path: Path | None = None

    def __post_init__(self) -> None:
        for name in ("stop_best_pnl30", "good_pnl30"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"screener.{name} must be a positive number")
        for name in ("go_min_good", "big_min_good", "big_shift_bp", "expected_combos_per_pair"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"screener.{name} must be a positive integer")
        if self.liquidity_registry_path is not None and not isinstance(self.liquidity_registry_path, Path):
            raise ValueError("screener.liquidity_registry_path must be a path or null")


def load_screener_config(path: Path) -> ScreenerConfig:
    raw = _local_config_object(path)
    section = raw.get("screener")
    if section is None:
        return ScreenerConfig()
    if not isinstance(section, dict):
        raise ValueError("screener must be an object")
    field_names = ScreenerConfig.__dataclass_fields__
    unknown = sorted(set(section).difference(field_names))
    if unknown:
        raise ValueError("screener contains unknown keys: " + ", ".join(unknown))
    defaults = ScreenerConfig()
    values: dict[str, object] = {}
    for name in field_names:
        if name not in section:
            values[name] = getattr(defaults, name)
            continue
        raw_value = section[name]
        if name in ("stop_best_pnl30", "good_pnl30"):
            try:
                values[name] = Decimal(str(raw_value))
            except InvalidOperation:
                raise ValueError(f"screener.{name} must be a positive number") from None
        elif name == "liquidity_registry_path":
            values[name] = _optional_screener_path(raw_value)
        else:
            values[name] = raw_value
    return ScreenerConfig(**values)
