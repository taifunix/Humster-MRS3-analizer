"""Pair registry storage: settings, schema and connection (docs/specs/2026-10-18-pair-registry.md, stage 1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import socket

import duckdb

from ..config import DuckDBImportSettings
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
    # stage 2, LOT_MODEL_V2 (ADR-0079): window of N weekdays, K and the USDT rounding step of the base lot
    liquidity_window_weekdays: int = 7
    liquidity_k: Decimal = Decimal(9)
    liquidity_round_down: Decimal = Decimal(10)
    liquidity_data_root: Path | None = None
    # stage 5, git sync (ADR-0081): this machine's name and the local sync repository
    machine: str = ""
    sync_path: Path | None = None
    # Panel stage 5a, the minute data layer: the BingX tester data folder and the download mode
    bingx_data_root: Path | None = None
    bingx_download_mode: str = "incremental"
    # the shared limit for resource-heavy work (`duckdb_import.workers`, CLAUDE.md)
    workers: int = DuckDBImportSettings().workers


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


def _positive_decimal(value: object, key: str, default: Decimal, *, whole: bool = False) -> Decimal:
    if value is None:
        return default
    if isinstance(value, (bool, float)):
        raise RegistryError(f"pair_registry.liquidity.{key} must be an exact number (int or decimal text)")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RegistryError(f"pair_registry.liquidity.{key} must be a number") from error
    if not number.is_finite() or number <= 0 or (whole and number != number.to_integral_value()):
        raise RegistryError(f"pair_registry.liquidity.{key} has an invalid value")
    return number


def _coefficient(value: object, default: Decimal) -> Decimal:
    """K of the lot model: the optimizer accepts 1..20 (position_sizing.calculate_lot_capacity)."""
    number = _positive_decimal(value, "k", default)
    if not Decimal(1) <= number <= Decimal(20):
        raise RegistryError("pair_registry.liquidity.k must be from 1 to 20")
    return number


MACHINE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _machine_name(configured: object) -> str:
    """This machine's name in the sync files (ADR-0081). A configured name must already be safe; a name taken from
    the system is made safe (other characters become '_', plus a short hash of the original so two such names never
    collide), so a non-ASCII computer name never stops the registry."""
    if configured not in (None, ""):
        if not isinstance(configured, str) or not MACHINE_NAME.fullmatch(configured.strip()):
            raise RegistryError("pair_registry.machine must be a name of letters, digits, '.', '_' or '-'")
        return configured.strip()
    raw = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "").strip()
    if MACHINE_NAME.fullmatch(raw):
        return raw
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:48] or "machine"
    return f"{safe}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]}"


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
    liquidity = _object(section.get("liquidity"), "pair_registry.liquidity")
    weekdays = liquidity.get("window_weekdays", defaults.liquidity_window_weekdays)
    if type(weekdays) is not int or not 3 <= weekdays <= 20:
        raise RegistryError("pair_registry.liquidity.window_weekdays must be a whole number from 3 to 20")
    raw_data = liquidity.get("data_root")
    if raw_data is not None and not isinstance(raw_data, str):
        raise RegistryError("pair_registry.liquidity.data_root must be a path")
    runner = document.get("tester_runner") if isinstance(document.get("tester_runner"), dict) else {}
    bot_root = runner.get("bot_root") if isinstance(runner.get("bot_root"), str) and runner.get("bot_root") else None
    if raw_data:
        data_root: Path | None = Path(raw_data) if Path(raw_data).is_absolute() else config_path.parent / raw_data
    else:
        data_root = Path(bot_root) / "tester" / "data" / "bybit" if bot_root else None
    minute = _object(section.get("minute_data"), "pair_registry.minute_data")
    raw_bingx = minute.get("bingx_data_root")
    if raw_bingx is not None and not isinstance(raw_bingx, str):
        raise RegistryError("pair_registry.minute_data.bingx_data_root must be a path")
    if raw_bingx:
        bingx_root: Path | None = Path(raw_bingx) if Path(raw_bingx).is_absolute() else config_path.parent / raw_bingx
    else:
        bingx_root = Path(bot_root) / "tester" / "data" / "bingx" if bot_root else None
    bingx_mode = minute.get("bingx_download_mode", defaults.bingx_download_mode)
    if bingx_mode not in ("incremental", "overwrite"):
        raise RegistryError("pair_registry.minute_data.bingx_download_mode must be incremental or overwrite")
    importer = document.get("duckdb_import") if isinstance(document.get("duckdb_import"), dict) else {}
    workers = importer.get("workers", defaults.workers)
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise RegistryError("duckdb_import.workers must be a positive integer")
    machine = _machine_name(section.get("machine"))
    sync = _object(section.get("sync"), "pair_registry.sync")
    raw_sync = sync.get("path")
    if raw_sync is not None and (not isinstance(raw_sync, str) or not raw_sync.strip()):
        raise RegistryError("pair_registry.sync.path must be a path")
    sync_path = (Path(raw_sync) if Path(raw_sync).is_absolute() else config_path.parent / raw_sync) if raw_sync \
        else root / "sync"
    return RegistrySettings(
        machine=machine,
        sync_path=sync_path.resolve(),
        liquidity_window_weekdays=weekdays,
        liquidity_k=_coefficient(liquidity.get("k"), defaults.liquidity_k),
        liquidity_round_down=_positive_decimal(liquidity.get("round_down_usdt"), "round_down_usdt",
                                               defaults.liquidity_round_down, whole=True),
        liquidity_data_root=data_root.resolve() if data_root is not None else None,
        bingx_data_root=bingx_root.resolve() if bingx_root is not None else None,
        bingx_download_mode=bingx_mode,
        workers=workers,
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
    # stage 2: weekday liquidity, LOT_MODEL_V2 (ADR-0079)
    "create table if not exists liquidity_days (exchange varchar not null, symbol varchar not null, day date not null, "
    "weekend boolean not null, turnover varchar not null, positive_minutes integer not null, active_bins integer not null, "
    "file_sha256 varchar not null, computed_ms bigint not null, primary key (exchange, symbol, day))",
    "create table if not exists liquidity (exchange varchar not null, symbol varchar not null, as_of_day date not null, "
    "model varchar not null, status varchar not null, window_weekdays integer not null, window_days varchar not null, "
    "missing_days varchar not null, positive_minutes integer, active_bins integer, total_bins integer, v25 varchar, "
    "a15 varchar, l varchar, k varchar, step varchar, base_lot varchar, computed_ms bigint not null, "
    "reason varchar, window_files varchar, inputs_sha256 varchar, primary key (exchange, symbol, as_of_day))",
    # Panel stage 5a: the verified minute days (local, never synced)
    "create table if not exists minute_days (exchange varchar not null, symbol varchar not null, day date not null, "
    "candles integer not null, expected_minutes integer not null, zero_volume_minutes integer not null, "
    "missing_minutes integer not null, gap_ranges varchar not null, differing_hours varchar not null, "
    "status varchar not null, verified_ms bigint not null, file_sha256 varchar, primary key (exchange, symbol, day))",
)
# columns added after a table was first created (the stage 2 draft): added in place, nulls for old rows
LATE_COLUMNS = (("liquidity", "reason", "varchar"), ("liquidity", "window_files", "varchar"),
                ("liquidity", "inputs_sha256", "varchar"))


def connect(root: Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) `<root>/registry.duckdb` with the stage 1 schema."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(root / DATABASE))
    for statement in SCHEMA:
        con.execute(statement)
    for table, column, kind in LATE_COLUMNS:
        present = {row[0] for row in con.execute(
            "select column_name from information_schema.columns where table_name = ?", [table]).fetchall()}
        if column not in present:
            con.execute(f"alter table {table} add column {column} {kind}")
    from .records import ensure_schema          # stage 3 tables (decisions, settings, finalists, tests, pipeline)

    ensure_schema(con)
    return con
