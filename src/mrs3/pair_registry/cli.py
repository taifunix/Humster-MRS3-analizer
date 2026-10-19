"""Pair registry CLI: `python -m mrs3.pair_registry <command> [--config config.local.json]`.

Commands: refresh, liquidity, pipeline, sync (refresh + liquidity + pipeline, the Panel's «Синхронизировать»),
decide, setting. Contract: docs/specs/2026-10-18-pair-registry.md.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Sequence

import duckdb

from .bingx import fetch_bingx, normalize_contracts, unreadable_symbols
from .bybit import fetch_bybit, normalize_instruments, normalize_risk_limits
from .liquidity import refresh_liquidity
from .records import record_decision, record_setting
from .refresh import record_failure, refresh_exchange
from .store import RegistryError, RegistrySettings, connect, load_registry_settings


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _performance_db(config: Path) -> tuple[Path | None, str | None]:
    """The active PerformanceDB file (config.performance.json next to the local config), or None and the reason."""
    try:
        from ..performance_v2_store import load_performance_v2_config, performance_v2_database_path

        path = performance_v2_database_path(load_performance_v2_config(config.with_name("config.performance.json")))
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"
    return (path, None) if path.is_file() else (None, "PerformanceDB file not found")


def _refresh(con: duckdb.DuckDBPyConnection, settings: RegistrySettings, exchanges: Sequence[str]) -> tuple[dict, bool]:
    """Reference refresh per exchange; one exchange failing never stops the other."""
    summary: dict[str, object] = {}
    failed = False
    for exchange in exchanges:
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
        except Exception as error:
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
    return summary, failed


def _pipeline(con: duckdb.DuckDBPyConnection, config: Path, today: date, machine: str | None, skip_track: bool) -> dict:
    import os
    import socket

    from ..trade_store import TradeStore
    from .pipeline import compute_pipeline

    document = json.loads(Path(config).read_text(encoding="utf-8"))
    store_root = (document.get("trade_store") or {}).get("root")
    if not store_root:
        raise RegistryError("trade_store.root is not configured")
    performance_db, why = _performance_db(Path(config))
    machine = machine or os.environ.get("COMPUTERNAME") or socket.gethostname()
    outcome = compute_pipeline(con, TradeStore(Path(store_root)), "bybit", today, machine,
                               performance_db=performance_db, track=not skip_track)
    if performance_db is None:
        outcome = {**outcome, "performance_db": None, "performance_db_reason": why}
    return outcome


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mrs3.pair_registry")
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="refresh exchange reference data (pairs, risk tiers)")
    refresh.add_argument("--exchange", choices=("bybit", "bingx", "all"), default="all")
    liquidity = commands.add_parser("liquidity", help="weekday liquidity, LOT_MODEL_V2, for the TRADING Bybit pairs")
    pipeline = commands.add_parser("pipeline", help="import tests and finalists, recompute the pipeline state")
    sync = commands.add_parser("sync", help="refresh both exchanges, then liquidity, then the pipeline")
    for sub in (liquidity, pipeline, sync):
        sub.add_argument("--today", type=date.fromisoformat, default=None,
                         help="UTC day of the run (default: today); data up to the day before is used")
    for sub in (pipeline, sync):
        sub.add_argument("--machine", default=None, help="name of this machine (default: the computer name)")
        sub.add_argument("--skip-track", action="store_true", help="do not compute new track records")
    decide = commands.add_parser("decide", help="record a decision for a pair (append-only)")
    setting = commands.add_parser("setting", help="record a personal filter threshold for a pair (append-only)")
    for sub in (decide, setting):
        sub.add_argument("--exchange", choices=("bybit", "bingx"), default="bybit")
        sub.add_argument("--symbol", required=True)
        sub.add_argument("--side", choices=("LONG", "SHORT", "ALL"), default="ALL")
        sub.add_argument("--reason", required=True)
        sub.add_argument("--author", required=True)
    decide.add_argument("--decision", required=True, choices=("INCLUDE", "EXCLUDE", "WATCH"))
    setting.add_argument("--filter", default="ready_v4")
    setting.add_argument("--parameter", required=True)
    setting.add_argument("--value", default=None, help="empty or omitted: reset to the common default")
    for sub in (refresh, liquidity, pipeline, sync, decide, setting):
        sub.add_argument("--config", type=Path, default=Path("config.local.json"))
    args = parser.parse_args(argv)

    try:
        settings = load_registry_settings(args.config)
        con = connect(settings.root)
    except (RegistryError, OSError, duckdb.Error) as error:   # e.g. the registry is locked by another process
        _emit({"error": str(error)})
        return 1
    today = getattr(args, "today", None) or datetime.now(timezone.utc).date()
    try:
        if args.command in ("decide", "setting"):
            try:
                if args.command == "decide":
                    result = record_decision(con, args.exchange, args.symbol, args.side, args.decision, args.reason,
                                             args.author)
                else:
                    result = record_setting(con, args.exchange, args.symbol, args.side, args.filter, args.parameter,
                                            args.value, args.reason, args.author)
                code = 0
            except (RegistryError, duckdb.Error) as error:
                result, code = {"error": str(error)}, 1
            _emit(result)
            return code
        if args.command == "pipeline":
            try:
                result, code = {"bybit": _pipeline(con, args.config, today, args.machine, args.skip_track)}, 0
            except Exception as error:
                result, code = {"bybit": {"error": f"{type(error).__name__}: {error}"}}, 1
            _emit(result)
            return code
        if args.command == "liquidity":
            try:
                result, code = {"bybit": refresh_liquidity(con, "bybit", today, settings)}, 0
            except Exception as error:
                result, code = {"bybit": {"error": f"{type(error).__name__}: {error}"}}, 1
            _emit(result)
            return code
        if args.command == "sync":
            steps = ("refresh", "liquidity", "pipeline")
            summary: dict[str, object] = {}
            failed = False
            for done, step in enumerate(steps):
                _emit({"progress": {"done": done, "total": len(steps), "step": step, "pair": None}})
                try:
                    if step == "refresh":
                        summary[step], step_failed = _refresh(con, settings, ("bybit", "bingx"))
                        for exchange, value in summary[step].items():
                            if isinstance(value, dict) and "error" in value:
                                _emit({"error": value["error"], "step": f"refresh {exchange}", "pair": None})
                        failed = failed or step_failed
                    elif step == "liquidity":
                        summary[step] = refresh_liquidity(con, "bybit", today, settings)
                    else:
                        summary[step] = _pipeline(con, args.config, today, args.machine, args.skip_track)
                except Exception as error:          # one step failing never stops the next one
                    summary[step] = {"error": f"{type(error).__name__}: {error}"}
                    _emit({"error": summary[step]["error"], "step": step, "pair": None})
                    failed = True
            _emit({"progress": {"done": len(steps), "total": len(steps), "step": "done", "pair": None}})
            _emit({"sync": summary})
            return 1 if failed else 0
        summary, failed = _refresh(con, settings, ("bybit", "bingx") if args.exchange == "all" else (args.exchange,))
        _emit(summary)
        return 1 if failed else 0
    finally:
        con.close()
