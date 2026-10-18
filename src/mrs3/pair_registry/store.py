"""Pair registry storage: settings, schema and connection (docs/specs/2026-10-18-pair-registry.md, stage 1)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import duckdb

DEFAULT_ROOT = Path("data") / "pair_registry"
DATABASE = "registry.duckdb"


class RegistryError(ValueError):
    """A registry input or refresh is refused."""


@dataclass(frozen=True, slots=True)
class RegistrySettings:
    root: Path = field(default=DEFAULT_ROOT)
    bybit_symbol_types: tuple[str, ...] = ("stock", "ETF", "commodity")
    bybit_exclude: tuple[str, ...] = ("SPXUSDT", "XAUTUSDT")
    bingx_prefixes: tuple[str, ...] = ("NCSK", "NCSI", "NCCO")
    bingx_exclude: tuple[str, ...] = ()


def _strings(value: object, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise RegistryError(f"pair_registry.{key} must be a list of names")
    return tuple(item.strip() for item in value)


def _object(value: object, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegistryError(f"{key} must be an object")
    return value


def load_registry_settings(config_path: Path) -> RegistrySettings:
    """`pair_registry` of the local config; every key has a default, `root` is relative to the config folder."""
    config_path = Path(config_path)
    if not config_path.is_file():
        raise RegistryError(f"config not found: {config_path}")
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegistryError("config is unreadable") from error
    if not isinstance(document, dict):
        raise RegistryError("config must be a JSON object")
    section = _object(document.get("pair_registry"), "pair_registry")
    universe = _object(section.get("universe"), "pair_registry.universe")
    bybit = _object(universe.get("bybit"), "pair_registry.universe.bybit")
    bingx = _object(universe.get("bingx"), "pair_registry.universe.bingx")
    defaults = RegistrySettings()
    raw_root = section.get("root") or str(DEFAULT_ROOT)
    if not isinstance(raw_root, str):
        raise RegistryError("pair_registry.root must be a path")
    root = Path(raw_root)
    if not root.is_absolute():
        root = config_path.parent / root
    return RegistrySettings(
        root=root.resolve(),
        bybit_symbol_types=_strings(bybit.get("symbol_types"), "universe.bybit.symbol_types", defaults.bybit_symbol_types),
        bybit_exclude=_strings(bybit.get("exclude"), "universe.bybit.exclude", defaults.bybit_exclude),
        bingx_prefixes=_strings(bingx.get("prefixes"), "universe.bingx.prefixes", defaults.bingx_prefixes),
        bingx_exclude=_strings(bingx.get("exclude"), "universe.bingx.exclude", defaults.bingx_exclude),
    )


# Reference fields of `pairs` (numbers as canonical decimal text, so change detection is exact).
REFERENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("api_symbol", "varchar"), ("display_name", "varchar"), ("base_asset", "varchar"), ("symbol_type", "varchar"),
    ("contract_type", "varchar"), ("settle_coin", "varchar"), ("status", "varchar"), ("exchange_status", "varchar"),
    ("listing_ms", "bigint"), ("delivery_ms", "bigint"), ("api_open", "boolean"), ("api_close", "boolean"),
    ("tick_size", "varchar"), ("min_price", "varchar"), ("max_price", "varchar"), ("price_scale", "integer"),
    ("qty_step", "varchar"), ("min_order_qty", "varchar"), ("max_order_qty", "varchar"),
    ("max_market_order_qty", "varchar"), ("min_notional", "varchar"), ("min_leverage", "varchar"),
    ("max_leverage", "varchar"), ("leverage_step", "varchar"), ("funding_interval_min", "integer"),
    ("upper_funding_rate", "varchar"), ("lower_funding_rate", "varchar"), ("maker_fee", "varchar"),
    ("taker_fee", "varchar"), ("full_name", "varchar"),
)
FIELD_NAMES = tuple(name for name, _type in REFERENCE_FIELDS)

SCHEMA = (
    "create table if not exists pairs (exchange varchar not null, symbol varchar not null, "
    + ", ".join(f"{name} {kind}" for name, kind in REFERENCE_FIELDS)
    + ", first_seen_ms bigint not null, updated_ms bigint not null, primary key (exchange, symbol))",
    "create sequence if not exists pair_changes_seq",
    "create table if not exists pair_changes (seq bigint primary key default nextval('pair_changes_seq'), "
    "exchange varchar not null, symbol varchar not null, kind varchar not null, field varchar, "
    "old_value varchar, new_value varchar, changed_ms bigint not null)",
    "create table if not exists risk_tiers (exchange varchar not null, symbol varchar not null, "
    "as_of_day date not null, tier_id integer not null, risk_limit_value varchar, initial_margin varchar, "
    "maintenance_margin varchar, max_leverage varchar, mm_deduction varchar, is_lowest boolean, "
    "primary key (exchange, symbol, as_of_day, tier_id))",
    "create table if not exists journal (run_id varchar primary key, step varchar not null, exchange varchar, "
    "status varchar not null, counts varchar, started_at bigint not null, finished_at bigint, error varchar)",
)


def connect(root: Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) `<root>/registry.duckdb` with the stage 1 schema."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(root / DATABASE))
    for statement in SCHEMA:
        con.execute(statement)
    return con
