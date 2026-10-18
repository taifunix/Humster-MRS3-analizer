"""Reference refresh of one exchange: upsert `pairs`, log `pair_changes`, snapshot `risk_tiers`, write `journal`."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Mapping, Sequence
from uuid import uuid4

import duckdb

from .store import FIELD_NAMES, RegistryError

__all__ = ["RegistryError", "record_failure", "refresh_exchange"]


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _journal(con: duckdb.DuckDBPyConnection, run_id: str, exchange: str, status: str, counts: Mapping | None,
             started: int, error: str | None) -> None:
    con.execute("insert into journal values (?, 'refresh', ?, ?, ?, ?, ?, ?)",
                [run_id, exchange, status, json.dumps(counts, sort_keys=True) if counts else None, started,
                 int(datetime.now(timezone.utc).timestamp() * 1000), error])


def record_failure(con: duckdb.DuckDBPyConnection, exchange: str, error: BaseException, started_ms: int) -> None:
    """Journal a refresh that failed before `refresh_exchange` (fetch or normalisation, e.g. a ticker collision)."""
    _journal(con, uuid4().hex, exchange, "FAILED", None, started_ms, f"{type(error).__name__}: {error}")


def refresh_exchange(con: duckdb.DuckDBPyConnection, exchange: str, records: Sequence[Mapping],
                     risk_tiers: Sequence[Mapping], *, now_ms: int) -> dict[str, int]:
    """Apply one exchange's normalised reference in one transaction; an empty list is refused."""
    run_id = uuid4().hex
    try:
        if not records:
            raise RegistryError(f"{exchange}: empty instrument list refused (nothing is delisted)")
        symbols = [str(record["symbol"]) for record in records]
        if len(set(symbols)) != len(symbols):
            raise RegistryError(f"{exchange}: duplicate symbols in the instrument list")
        con.begin()
        try:
            rows = con.execute(f"select symbol, {', '.join(FIELD_NAMES)} from pairs where exchange = ?",
                               [exchange]).fetchall()
            existing = {row[0]: dict(zip(FIELD_NAMES, row[1:])) for row in rows}
            listed = changed = delisted = 0
            columns = ("exchange", "symbol", *FIELD_NAMES, "first_seen_ms", "updated_ms")
            for record in records:
                symbol = str(record["symbol"])
                values = [record.get(name) for name in FIELD_NAMES]
                old = existing.get(symbol)
                if old is None:
                    con.execute(f"insert into pairs ({', '.join(columns)}) values ({', '.join('?' * len(columns))})",
                                [exchange, symbol, *values, now_ms, now_ms])
                    con.execute("insert into pair_changes (exchange, symbol, kind, changed_ms) values (?, ?, 'listed', ?)",
                                [exchange, symbol, now_ms])
                    listed += 1
                    continue
                diffs = [(name, old[name], value) for name, value in zip(FIELD_NAMES, values)
                         if _text(old[name]) != _text(value)]
                if not diffs:
                    continue
                for name, before, after in diffs:
                    con.execute("insert into pair_changes (exchange, symbol, kind, field, old_value, new_value, changed_ms) "
                                "values (?, ?, 'field', ?, ?, ?, ?)", [exchange, symbol, name, _text(before), _text(after), now_ms])
                con.execute(f"update pairs set {', '.join(f'{name} = ?' for name, _b, _a in diffs)}, updated_ms = ? "
                            "where exchange = ? and symbol = ?", [*(after for _n, _b, after in diffs), now_ms, exchange, symbol])
                changed += 1
            present = set(symbols)
            for symbol, old in existing.items():
                if symbol not in present and old["status"] != "DELISTED":
                    con.execute("update pairs set status = 'DELISTED', updated_ms = ? where exchange = ? and symbol = ?",
                                [now_ms, exchange, symbol])
                    con.execute("insert into pair_changes (exchange, symbol, kind, field, old_value, new_value, changed_ms) "
                                "values (?, ?, 'delisted', 'status', ?, 'DELISTED', ?)", [exchange, symbol, old["status"], now_ms])
                    delisted += 1
            day = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
            con.execute("delete from risk_tiers where exchange = ? and as_of_day = ?", [exchange, day])
            for tier in risk_tiers:
                con.execute("insert into risk_tiers values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [exchange, tier["symbol"], day, tier["tier_id"], tier["risk_limit_value"],
                             tier["initial_margin"], tier["maintenance_margin"], tier["max_leverage"],
                             tier["mm_deduction"], tier["is_lowest"]])
            con.commit()
        except BaseException:
            con.rollback()
            raise
    except Exception as error:
        try:
            _journal(con, run_id, exchange, "FAILED", None, now_ms, str(error))
        except Exception:      # never let a journal failure hide the refresh error
            pass
        raise
    counts = {"pairs": len(records), "listed": listed, "changed": changed, "delisted": delisted,
              "risk_tiers": len(risk_tiers)}
    _journal(con, run_id, exchange, "OK", counts, now_ms, None)
    return counts
