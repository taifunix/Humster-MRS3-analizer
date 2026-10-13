"""Lot-sizing mode of a stored Performance v2 result (protocol v2 §4.5, §4.6)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

import duckdb

SIZING_DYNAMIC = "DYNAMIC"
SIZING_FIXED = "FIXED"

# Stored sizing facts that decide the FIXED base; the v12 columns are optional.
FIXED_SIZING_COLUMNS = (
    "sizing_use_upnl", "sizing_use_frozen_balance", "sizing_use_fix", "sizing_max_balance",
    "sizing_my_fix_balance", "sizing_balance_percentage_long", "sizing_risk_long",
    "sizing_balance_percentage_short", "sizing_risk_short",
)


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def fixed_lot_base(sizing: Mapping[str, object], side: object) -> Decimal | None:
    """Constant base B of a protocol-v2 FIXED result, or None for any other sizing.

    FIXED is the sizing part of the protocol v2 predicate (§4.2): use_upnl=false,
    use_frozen_balance=true, use_fix=true, max_balance=0, the active side at 100%
    and risk 1, and a positive my_fix_balance. B = my_fix_balance × percentage / 100.
    """
    suffix = {"LONG": "long", "SHORT": "short"}.get(str(side).strip().upper())
    if suffix is None:
        return None
    if (
        sizing.get("sizing_use_upnl") is not False
        or sizing.get("sizing_use_frozen_balance") is not True
        or sizing.get("sizing_use_fix") is not True
        or _number(sizing.get("sizing_max_balance")) != 0
    ):
        return None
    base = _number(sizing.get("sizing_my_fix_balance"))
    percentage = _number(sizing.get(f"sizing_balance_percentage_{suffix}"))
    risk = _number(sizing.get(f"sizing_risk_{suffix}"))
    if base is None or base <= 0 or percentage != 100 or risk != 1:
        return None
    # The active side is at 100%, so B = my_fix_balance × 100 / 100 = my_fix_balance.
    return base


def has_fixed_sizing_columns(connection: duckdb.DuckDBPyConnection) -> bool:
    """True for a schema-v12 catalog that stores the FIXED base."""
    found = connection.execute(
        """select count(*) from information_schema.columns
            where table_catalog = current_database() and table_schema = 'main'
              and table_name = 'strategy_results'
              and column_name in ('sizing_my_fix_balance', 'sizing_balance_percentage_short', 'sizing_risk_short')"""
    ).fetchone()[0]
    return int(found) == 3


def load_fixed_lot_bases(connection: duckdb.DuckDBPyConnection, result_ids: Iterable[int]) -> dict[int, Decimal]:
    """FIXED base B by result id; DYNAMIC, legacy and pre-v12 results are absent."""
    ids = sorted({int(value) for value in result_ids})
    if not ids or not has_fixed_sizing_columns(connection):
        return {}
    rows = connection.execute(
        "select r.result_id, s.side, " + ", ".join(f"r.{column}" for column in FIXED_SIZING_COLUMNS)
        + " from strategy_results r join strategies s on s.strategy_id = r.strategy_id"
        " where r.sizing_use_fix and r.result_id in (select unnest(?::bigint[]))",
        [ids],
    ).fetchall()
    bases = {int(row[0]): fixed_lot_base(dict(zip(FIXED_SIZING_COLUMNS, row[2:])), row[1]) for row in rows}
    return {result_id: base for result_id, base in bases.items() if base is not None}


def load_fixed_lot_base(connection: duckdb.DuckDBPyConnection, result_id: int) -> Decimal | None:
    """FIXED base B of one stored result; None for DYNAMIC, legacy or pre-v12 results."""
    return load_fixed_lot_bases(connection, (result_id,)).get(int(result_id))


__all__ = [
    "FIXED_SIZING_COLUMNS",
    "SIZING_DYNAMIC",
    "SIZING_FIXED",
    "fixed_lot_base",
    "has_fixed_sizing_columns",
    "load_fixed_lot_base",
    "load_fixed_lot_bases",
]
