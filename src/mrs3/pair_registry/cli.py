"""Pair registry CLI: `python -m mrs3.pair_registry <command> [--config config.local.json]`.

Commands: refresh, liquidity, pipeline, sync (pull + refresh + liquidity + pipeline + listing workbook + push, the
Panel's «Синхронизировать»), sync-init, workbook, decide, setting. Contract: docs/specs/2026-10-18-pair-registry.md.
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
from .sync import SyncError, init_repo, sync_pull, sync_push
from .workbook import WORKBOOK_NAME, write_listing_workbook


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


def _push(con: duckdb.DuckDBPyConnection, settings: RegistrySettings, message: str) -> dict:
    """Export the synced tables after a write (ADR-0081); a sync failure never fails the write itself."""
    try:
        return sync_push(con, settings.sync_path, settings.machine, message)
    except (SyncError, OSError, duckdb.Error) as error:
        return {"error": f"{type(error).__name__}: {error}"}


def _pipeline(con: duckdb.DuckDBPyConnection, config: Path, today: date, machine: str | None, skip_track: bool,
              exchange: str = "bybit") -> dict:
    import os
    import socket

    from ..trade_store import TradeStore
    from .pipeline import compute_pipeline

    document = json.loads(Path(config).read_text(encoding="utf-8"))
    store_root = (document.get("trade_store") or {}).get("root")
    if not store_root:
        raise RegistryError("trade_store.root is not configured")
    # the PerformanceDB holds Bybit tick tests; BingX reads none until Panel stage 5c
    performance_db, why = _performance_db(Path(config)) if exchange == "bybit" else (None, "BingX: Panel stage 5c")
    machine = machine or os.environ.get("COMPUTERNAME") or socket.gethostname()
    outcome = compute_pipeline(con, TradeStore(Path(store_root)), exchange, today, machine,
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
    liquidity.add_argument("--days", type=int, default=1,
                           help="also the windows of the N-1 run days before, oldest first (1-31; e.g. 14 to backfill "
                                "the base-lot history)")
    pipeline = commands.add_parser("pipeline", help="import tests and finalists, recompute the pipeline state")
    sync = commands.add_parser("sync", help="pull the sync repository, refresh both exchanges, liquidity, the "
                                            "pipeline, the workbook, then push")
    sync_init = commands.add_parser("sync-init", help="create the local sync repository (git); with --remote, pull "
                                                      "and push go to that repository")
    sync_init.add_argument("--remote", default=None, help="URL of the private sync repository (created by the user)")
    pipeline.add_argument("--exchange", choices=("bybit", "bingx"), default="bybit")
    workbook = commands.add_parser("workbook", help="write the listing workbook (sheets «Пары», «Пары_BingX»)")
    workbook.add_argument("--out", type=Path, default=None, help=f"default: <registry root>/{WORKBOOK_NAME}")
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
    for sub in (refresh, liquidity, pipeline, sync, sync_init, workbook, decide, setting):
        sub.add_argument("--config", type=Path, default=Path("config.local.json"))
    args = parser.parse_args(argv)

    try:
        settings = load_registry_settings(args.config)
        if args.command == "sync-init":
            _emit({"sync": init_repo(settings.sync_path, settings.machine, args.remote), "machine": settings.machine})
            return 0
        if args.command == "workbook" and not (settings.root / "registry.duckdb").is_file():
            raise RegistryError("the pair registry does not exist yet: run sync first")
        con = connect(settings.root)
    except SyncError as error:
        _emit({"error": str(error)})
        return 1
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
            if code == 0:
                result = {**result, "sync": _push(con, settings, args.command)}
            _emit(result)
            return code
        if args.command == "pipeline":
            try:
                result, code = {args.exchange: _pipeline(con, args.config, today, args.machine or settings.machine,
                                                         args.skip_track, args.exchange)}, 0
            except Exception as error:
                result, code = {args.exchange: {"error": f"{type(error).__name__}: {error}"}}, 1
            if code == 0:
                result["sync"] = _push(con, settings, "pipeline")
            _emit(result)
            return code
        if args.command == "workbook":
            try:
                result, code = {"workbook": write_listing_workbook(con, args.out or settings.root / WORKBOOK_NAME)}, 0
            except (OSError, duckdb.Error) as error:
                result, code = {"error": f"{type(error).__name__}: listing workbook not written"}, 1
            _emit(result)
            return code
        if args.command == "liquidity":
            if not 1 <= args.days <= 31:
                _emit({"error": "--days: a whole number from 1 to 31"})
                return 1
            try:
                from datetime import timedelta

                runs = [refresh_liquidity(con, "bybit", today - timedelta(days=offset), settings)
                        for offset in range(args.days - 1, -1, -1)]          # oldest first: today's window is newest
                result, code = {"bybit": runs[-1] if args.days == 1 else {"days": args.days, "runs": runs}}, 0
            except Exception as error:
                result, code = {"bybit": {"error": f"{type(error).__name__}: {error}"}}, 1
            _emit(result)
            return code
        if args.command == "sync":
            steps = ("pull", "refresh", "liquidity", "pipeline", "workbook", "push")
            summary: dict[str, object] = {}
            failed = False
            for done, step in enumerate(steps):
                _emit({"progress": {"done": done, "total": len(steps), "step": step, "pair": None}})
                try:
                    if step == "pull":
                        summary[step] = sync_pull(con, settings.sync_path, settings.machine)
                    elif step == "push":
                        summary[step] = sync_push(con, settings.sync_path, settings.machine, "sync")
                    elif step == "refresh":
                        summary[step], step_failed = _refresh(con, settings, ("bybit", "bingx"))
                        for exchange, value in summary[step].items():
                            if isinstance(value, dict) and "error" in value:
                                _emit({"error": value["error"], "step": f"refresh {exchange}", "pair": None})
                        failed = failed or step_failed
                    elif step == "liquidity":
                        summary[step] = refresh_liquidity(con, "bybit", today, settings)
                    elif step == "workbook":
                        try:
                            summary[step] = write_listing_workbook(con, settings.root / WORKBOOK_NAME)
                        except OSError as error:          # e.g. the workbook is open in Excel; no local path
                            raise RegistryError(f"{type(error).__name__}: listing workbook not written") from None
                    else:
                        summary[step] = _pipeline(con, args.config, today, args.machine or settings.machine, args.skip_track)
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
