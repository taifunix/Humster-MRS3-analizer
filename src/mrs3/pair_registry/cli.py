"""`python -m mrs3.pair_registry refresh [--exchange bybit|bingx|all] [--config config.local.json]`."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Sequence

import duckdb

from .bingx import fetch_bingx, normalize_contracts, unreadable_symbols
from .bybit import fetch_bybit, normalize_instruments, normalize_risk_limits
from .refresh import record_failure, refresh_exchange
from .store import RegistryError, connect, load_registry_settings


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mrs3.pair_registry")
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="refresh exchange reference data (pairs, risk tiers)")
    refresh.add_argument("--exchange", choices=("bybit", "bingx", "all"), default="all")
    refresh.add_argument("--config", type=Path, default=Path("config.local.json"))
    args = parser.parse_args(argv)

    try:
        settings = load_registry_settings(args.config)
        con = connect(settings.root)
    except (RegistryError, OSError, duckdb.Error) as error:   # e.g. the registry is locked by another process
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    summary: dict[str, object] = {}
    failed = False
    try:
        for exchange in (("bybit", "bingx") if args.exchange == "all" else (args.exchange,)):
            started = _now_ms()
            unreadable: list[str] = []
            try:                       # fetch and normalise: journaled here when they fail
                if exchange == "bybit":
                    instruments, risk = fetch_bybit()
                    records = normalize_instruments(instruments, settings)
                    tiers = normalize_risk_limits(risk, {record["symbol"] for record in records})
                else:
                    contracts = fetch_bingx()
                    unreadable = unreadable_symbols(contracts, settings)
                    records, tiers = normalize_contracts(contracts, settings), []
            except Exception as error:  # one exchange never stops the other
                try:
                    record_failure(con, exchange, error, started)
                except Exception:      # a journal failure never stops the other exchange
                    pass
                summary[exchange] = {"error": f"{type(error).__name__}: {error}"}
                failed = True
                continue
            try:                       # refresh_exchange journals its own outcome
                result: dict[str, object] = dict(refresh_exchange(con, exchange, records, tiers, now_ms=started))
            except Exception as error:
                summary[exchange] = {"error": f"{type(error).__name__}: {error}"}
                failed = True
                continue
            if unreadable:
                result["unreadable_symbols"] = unreadable
            summary[exchange] = result
    finally:
        con.close()
    print(json.dumps(summary, ensure_ascii=False))
    return 1 if failed else 0
