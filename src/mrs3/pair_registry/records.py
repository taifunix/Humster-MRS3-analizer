"""Hand and imported rows of the pair registry: decisions, per-pair filter settings, finalists, pair tests.

Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 3 contract». Decisions, settings and finalists are
append-only (the latest row is in force, history is never edited); tests are keyed by their source.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Mapping

import duckdb

from ..ready_store import ReadyRule
from .store import RegistryError

SIDES = ("LONG", "SHORT", "ALL")
DECISIONS = ("INCLUDE", "EXCLUDE", "WATCH")
FINALIST_STATUSES = ("FINALIST", "RESERVE", "REJECTED", "NONE")
# Declared filter parameters: (python type, minimum, maximum). Only these can be overridden per pair.
FILTERS: dict[str, dict[str, tuple[type, float, float]]] = {
    "ready_v4": {
        "admit_score": (float, 0, 1000), "max_dd": (float, 0.1, 50), "low_dd": (float, 0.1, 50),
        "moderate_dd": (float, 0.1, 50), "min_trades": (int, 1, 1000), "min_shift_bp": (int, 0, 550),
        "pool": (int, 2, 20), "ratio": (float, 1, 10),
    },
}

SCHEMA = (
    "create sequence if not exists decisions_seq",
    "create table if not exists decisions (seq bigint primary key default nextval('decisions_seq'), "
    "exchange varchar not null, symbol varchar not null, side varchar not null, decision varchar not null, "
    "reason varchar not null, author varchar not null, created_ms bigint not null)",
    "create sequence if not exists pair_settings_seq",
    "create table if not exists pair_settings (seq bigint primary key default nextval('pair_settings_seq'), "
    "exchange varchar not null, symbol varchar not null, side varchar not null, filter varchar not null, "
    "parameter varchar not null, value varchar, reason varchar not null, author varchar not null, "
    "created_ms bigint not null)",
    "create sequence if not exists finalists_seq",
    "create table if not exists finalists (seq bigint primary key default nextval('finalists_seq'), "
    "exchange varchar not null, symbol varchar not null, side varchar not null, strategy varchar not null, "
    "status varchar not null, user_rank integer, source varchar not null, created_ms bigint not null)",
    "create table if not exists pair_tests (source varchar primary key, exchange varchar not null, "
    "symbol varchar not null, side varchar not null, kind varchar not null, window_start varchar, "
    "window_end varchar, structure varchar, pnl_pct varchar, maxdd_pct varchar, trades integer, "
    "created_ms bigint not null)",
    "create table if not exists pipeline (exchange varchar not null, symbol varchar not null, side varchar not null, "
    "as_of_day date not null, machine varchar not null, computed_ms bigint not null, stage varchar not null, "
    "periods integer not null, first_period varchar, last_period varchar, ready_status varchar, ready_rule varchar, "
    "admitted integer, best_score varchar, best_dd varchar, best_category varchar, tester_window varchar, "
    "tick_tests integer not null, finalist_strategies integer not null, decision varchar, history_status varchar, "
    "track_cuts integer, track_admitted_cuts integer, track_loss_rate varchar, track_inputs_sha256 varchar, "
    "primary key (exchange, symbol, side, as_of_day))",
)


# stage 5, git sync (ADR-0081): where a row was created and its global identity; nulls are stamped on export
SYNC_COLUMNS = (("decisions", "origin"), ("decisions", "row_id"), ("pair_settings", "origin"),
                ("pair_settings", "row_id"), ("finalists", "origin"), ("finalists", "row_id"), ("pair_tests", "origin"))


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    for statement in SCHEMA:
        con.execute(statement)
    for table, column in SYNC_COLUMNS:
        present = {row[0] for row in con.execute(
            "select column_name from information_schema.columns where table_name = ?", [table]).fetchall()}
        if column not in present:
            con.execute(f"alter table {table} add column {column} varchar")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{name} is required")
    return value.strip()


def _pair(con: duckdb.DuckDBPyConnection, exchange: str, symbol: object, side: object) -> tuple[str, str]:
    symbol = _text(symbol, "symbol").upper()
    if side not in SIDES:
        raise RegistryError(f"side must be one of {', '.join(SIDES)}")
    if con.execute("select 1 from pairs where exchange = ? and symbol = ?", [exchange, symbol]).fetchone() is None:
        raise RegistryError(f"unknown pair {exchange}/{symbol}")
    return symbol, str(side)


# -- decisions ----------------------------------------------------------------------------------------


def record_decision(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, side: str, decision: str,
                    reason: str, author: str, *, now_ms: int | None = None) -> dict[str, object]:
    symbol, side = _pair(con, exchange, symbol, side)
    if decision not in DECISIONS:
        raise RegistryError(f"decision must be one of {', '.join(DECISIONS)}")
    row = [exchange, symbol, side, decision, _text(reason, "reason"), _text(author, "author"), now_ms or _now_ms()]
    con.execute("insert into decisions (exchange, symbol, side, decision, reason, author, created_ms) "
                "values (?, ?, ?, ?, ?, ?, ?)", row)
    return {"exchange": exchange, "symbol": symbol, "side": side, "decision": decision}


def current_decision(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, side: str = "LONG") -> str | None:
    row = con.execute("select decision from decisions where exchange = ? and symbol = ? and side in (?, 'ALL') "
                      "order by created_ms desc, seq desc limit 1", [exchange, symbol, side]).fetchone()
    return row[0] if row else None


# -- per-pair filter settings ------------------------------------------------------------------------


def _value(filter_name: str, parameter: str, value: object) -> str | None:
    if value is None or value == "":
        return None
    kind, low, high = FILTERS[filter_name][parameter]
    if isinstance(value, bool):
        raise RegistryError(f"{filter_name}.{parameter} must be a number")
    try:
        number = Decimal(str(value))
    except ArithmeticError as error:
        raise RegistryError(f"{filter_name}.{parameter} must be a number") from error
    if not number.is_finite() or (kind is int and number != number.to_integral_value()):
        raise RegistryError(f"{filter_name}.{parameter} must be a {'whole number' if kind is int else 'number'}")
    if not Decimal(str(low)) <= number <= Decimal(str(high)):
        raise RegistryError(f"{filter_name}.{parameter} must be from {low} to {high}")
    return format(number.normalize(), "f") if kind is float else str(int(number))


def _overrides(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, filter_name: str,
               side: str = "LONG") -> dict[str, str | None]:
    rows = con.execute("select parameter, value from pair_settings where exchange = ? and symbol = ? and filter = ? "
                       "and side in (?, 'ALL') order by created_ms, seq", [exchange, symbol, filter_name, side]).fetchall()
    latest: dict[str, str | None] = {}
    for parameter, value in rows:
        latest[parameter] = value
    return {key: value for key, value in latest.items() if value is not None}


def _apply(base: ReadyRule, overrides: Mapping[str, str]) -> ReadyRule:
    values = {key: (int(value) if FILTERS["ready_v4"][key][0] is int else float(value)) for key, value in overrides.items()}
    rule = replace(base, **values)
    if not rule.low_dd <= rule.moderate_dd <= rule.max_dd:
        raise RegistryError("ready_v4 needs low_dd <= moderate_dd <= max_dd")
    return rule


def record_setting(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, side: str, filter_name: str,
                   parameter: str, value: object, reason: str, author: str, *, now_ms: int | None = None) -> dict:
    symbol, side = _pair(con, exchange, symbol, side)
    if filter_name not in FILTERS:
        raise RegistryError(f"unknown filter {filter_name!r}")
    if parameter not in FILTERS[filter_name]:
        raise RegistryError(f"unknown parameter {filter_name}.{parameter}")
    text = _value(filter_name, parameter, value)
    reason, author = _text(reason, "reason"), _text(author, "author")
    for check_side in (("LONG", "SHORT") if side == "ALL" else (side,)):
        overrides = _overrides(con, exchange, symbol, filter_name, check_side)
        if text is None:
            overrides.pop(parameter, None)
        else:
            overrides[parameter] = text
        _apply(ReadyRule(), overrides)
    con.execute("insert into pair_settings (exchange, symbol, side, filter, parameter, value, reason, author, "
                "created_ms) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [exchange, symbol, side, filter_name, parameter, text, reason, author, now_ms or _now_ms()])
    return {"symbol": symbol, "side": side, "filter": filter_name, "parameter": parameter, "value": text}


def effective_ready_rule(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str) -> tuple[ReadyRule, dict]:
    """The READY v4 rule of a pair: the common default with its overrides; returns (rule, {parameter: value})."""
    overrides = _overrides(con, exchange, symbol, "ready_v4")
    rule = _apply(ReadyRule(), overrides)
    return rule, {key: getattr(rule, key) for key in sorted(overrides)}


def effective_ready_rules(con: duckdb.DuckDBPyConnection, exchange: str) -> dict[str, ReadyRule]:
    """Rules of the pairs that have at least one override (others use the common default)."""
    symbols = [row[0] for row in con.execute(
        "select distinct symbol from pair_settings where exchange = ? and filter = 'ready_v4'", [exchange]).fetchall()]
    return {symbol: rule for symbol in symbols for rule, overridden in [effective_ready_rule(con, exchange, symbol)]
            if overridden}


# -- finalists ------------------------------------------------------------------------------------------


def record_finalist(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str, side: str, strategy: str,
                    status: str, source: str, *, user_rank: int | None = None, now_ms: int | None = None) -> bool:
    """Append a status change; returns False when the status in force is already the same."""
    if status not in FINALIST_STATUSES:
        raise RegistryError(f"status must be one of {', '.join(FINALIST_STATUSES)}")
    strategy = _text(strategy, "strategy")
    current = con.execute("select status, user_rank from finalists where exchange = ? and strategy = ? "
                          "order by created_ms desc, seq desc limit 1", [exchange, strategy]).fetchone()
    if current is not None and (current[0], current[1]) == (status, user_rank):
        return False
    con.execute("insert into finalists (exchange, symbol, side, strategy, status, user_rank, source, created_ms) "
                "values (?, ?, ?, ?, ?, ?, ?, ?)",
                [exchange, symbol.upper(), side, strategy, status, user_rank, _text(source, "source"),
                 now_ms or _now_ms()])
    return True


def current_finalists(con: duckdb.DuckDBPyConnection, exchange: str, symbol: str | None = None) -> list[dict]:
    query = ("select symbol, side, strategy, status, user_rank from (select *, row_number() over "
             "(partition by strategy order by created_ms desc, seq desc) as latest from finalists where exchange = ?"
             + (" and symbol = ?" if symbol else "") + ") where latest = 1 order by symbol, strategy")
    rows = con.execute(query, [exchange, *([symbol] if symbol else [])]).fetchall()
    return [dict(zip(("symbol", "side", "strategy", "status", "user_rank"), row)) for row in rows]


def registry_decides(con: duckdb.DuckDBPyConnection, exchange: str) -> bool:
    """The registry decides the finalists of an exchange once it holds any finalist row of it (ADR-0080; before the
    one-time seed the PerformanceDB reviews still decide)."""
    return con.execute("select count(*) from finalists where exchange = ?", [exchange]).fetchone()[0] > 0


def finalist_marks(con: duckdb.DuckDBPyConnection, exchange: str) -> dict[str, dict]:
    """The marks in force by strategy name (status other than NONE) with their row: seq, source and time are the
    provenance the finalist retest and the optimizer bind to (ADR-0080)."""
    rows = con.execute("select seq, symbol, side, strategy, status, user_rank, source, created_ms from (select *, "
                       "row_number() over (partition by strategy order by created_ms desc, seq desc) as latest from finalists "
                       "where exchange = ?) where latest = 1 and status <> 'NONE' order by strategy",
                       [exchange]).fetchall()
    return {row[3]: dict(zip(("seq", "symbol", "side", "strategy", "status", "user_rank", "source", "created_ms"), row))
            for row in rows}


def _performance(database: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(database), read_only=True)


def database_exchange(source: duckdb.DuckDBPyConnection) -> str:
    """Exchange of a PerformanceDB: `schema_info.exchange` (protocol v2); a protocol v1 database has none and holds
    Bybit results only (its `strategy_results.exchange` column says `tester`)."""
    try:
        row = source.execute("select value from schema_info where key = 'exchange'").fetchone()
    except duckdb.Error:
        row = None
    return str(row[0]).lower() if row and row[0] else "bybit"


def import_finalists(con: duckdb.DuckDBPyConnection, performance_db: Path, exchange: str,
                     *, now_ms: int | None = None, skip_marked: bool = False) -> int:
    """Each strategy's latest User Status (FINALIST / RESERVE / REJECTED) from the PerformanceDB selection reviews.
    `skip_marked` leaves every strategy that already has a registry row alone (the ADR-0080 one-time seed)."""
    with _performance(performance_db) as source:
        tables = {row[0] for row in source.execute("select table_name from information_schema.tables").fetchall()}
        if not {"selection_review_rows", "selection_review_imports", "strategies"} <= tables:
            return 0
        if database_exchange(source) != exchange.lower():
            return 0
        rows = source.execute(
            "select s.symbol, s.side, s.strategy_name, r.user_status, r.user_rank from ("
            "select r.*, row_number() over (partition by r.strategy_id order by i.imported_at_utc desc, "
            "r.review_import_id desc) as latest "
            "from selection_review_rows r join selection_review_imports i using (review_import_id)) r "
            "join strategies s using (strategy_id) where r.latest = 1 order by s.strategy_name").fetchall()
    in_force = {row["strategy"]: row["status"] for row in current_finalists(con, exchange)}
    changes = 0
    for symbol, side, name, status, rank in rows:
        if skip_marked and name in in_force:
            continue
        if status in ("FINALIST", "RESERVE", "REJECTED"):
            changes += record_finalist(con, exchange, symbol, side, name, status, "performance_db", user_rank=rank,
                                       now_ms=now_ms)
        elif in_force.get(name) not in (None, "NONE"):     # the newest review dropped the mark (ANALOG, FILTERED)
            changes += record_finalist(con, exchange, symbol, side, name, "NONE", "performance_db", now_ms=now_ms)
    return changes


# -- pair tests ----------------------------------------------------------------------------------------


def _add_test(con: duckdb.DuckDBPyConnection, row: Mapping[str, object], now_ms: int) -> bool:
    if con.execute("select 1 from pair_tests where source = ?", [row["source"]]).fetchone() is not None:
        return False
    con.execute("insert into pair_tests (source, exchange, symbol, side, kind, window_start, window_end, structure, "
                "pnl_pct, maxdd_pct, trades, created_ms) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [row["source"], row["exchange"], row["symbol"], row["side"], row["kind"], row.get("window_start"),
                 row.get("window_end"), row.get("structure"), row.get("pnl_pct"), row.get("maxdd_pct"),
                 row.get("trades"), now_ms])
    return True


def import_tester_samples(con: duckdb.DuckDBPyConnection, store, exchange: str, *, now_ms: int | None = None) -> int:
    """Every tester segment of the trade store becomes one `tester_sample` test (keyed by the segment)."""
    now_ms = now_ms or _now_ms()
    added = 0
    with store.connect(read_only=True) as source:
        rows = source.execute("select segment_id, symbol, start_ms, end_ms from segments where exchange = ? "
                              "and source = 'tester' order by segment_id", [exchange]).fetchall()
    for segment_id, symbol, start_ms, end_ms in rows:
        added += _add_test(con, {"source": f"store:{segment_id}", "exchange": exchange, "symbol": symbol,
                                 "side": "LONG", "kind": "tester_sample",
                                 "window_start": datetime.fromtimestamp(start_ms / 1000, timezone.utc).date().isoformat(),
                                 "window_end": datetime.fromtimestamp(end_ms / 1000, timezone.utc).date().isoformat()},
                           now_ms)
    return added


def import_tick_tests(con: duckdb.DuckDBPyConnection, performance_db: Path, exchange: str,
                      *, now_ms: int | None = None) -> int:
    """Every PerformanceDB strategy result of the exchange becomes one `mrs3_tick` test (keyed by the result)."""
    now_ms = now_ms or _now_ms()
    with _performance(performance_db) as source:
        tables = {row[0] for row in source.execute("select table_name from information_schema.tables").fetchall()}
        if not {"strategies", "strategy_results"} <= tables or database_exchange(source) != exchange.lower():
            return 0
        rows = source.execute(
            "select r.result_id, s.symbol, s.side, s.strategy_name, r.report_start_utc, r.report_end_utc, "
            "r.total_pnl_pct, r.max_drawdown_pct, r.total_trades from strategy_results r join strategies s "
            "using (strategy_id) order by r.result_id").fetchall()
    added = 0
    for result_id, symbol, side, name, start, end, pnl, maxdd, trades in rows:
        added += _add_test(con, {"source": f"perfdb:{result_id}", "exchange": exchange, "symbol": symbol,
                                 "side": side, "kind": "mrs3_tick", "structure": name,
                                 "window_start": start.date().isoformat() if start else None,
                                 "window_end": end.date().isoformat() if end else None,
                                 "pnl_pct": None if pnl is None else format(pnl, "f"),
                                 "maxdd_pct": None if maxdd is None else format(maxdd, "f"), "trades": trades},
                           now_ms)
    return added


def rule_parameters(rule: ReadyRule) -> dict[str, object]:
    return asdict(rule)
