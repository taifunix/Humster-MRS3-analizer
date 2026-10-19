"""Simulate MRS2 history of the registry pairs into the trade store (P2 segments + the latest 20-day window).

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 1 contract»).

Run:  .venv\\Scripts\\python.exe scripts\\simulate_history.py --store <folder> [--pairs SNOWUSDT,XOMUSDT] [--limit 5]
      [--exchange bingx]  (BingX: Panel stage 5b, docs/specs/2026-10-18-panel-simulation-workflow.md)
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs3.minute_data import instrument, last_archived_day  # noqa: E402
from mrs3.period_csv import TIMEFRAMES, grid  # noqa: E402
from mrs3.pair_registry.history import history_pairs, listing_days  # noqa: E402
from mrs3.trade_history import BingXMinutes, exchange_run_settings, pair_order, run_history  # noqa: E402
from mrs3.trade_store import TradeStore  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=None, help="store root; default: trade_store.root in the config")
    parser.add_argument("--config", type=Path, default=REPO / "config.local.json")
    parser.add_argument("--pairs", default="", help="comma-separated symbols; default: registry order")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    parser.add_argument("--end-day", type=date.fromisoformat, default=None,
                        help="exclusive end of the latest window; default: the day after the last archived day")
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--workers", type=int, default=0, help="default: duckdb_import.workers from the config")
    parser.add_argument("--exchange", choices=("bybit", "bingx"), default="bybit")
    args = parser.parse_args(argv)
    if args.exchange == "bingx":
        return _bingx(parser, args)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    store_root = args.store or Path(config.get("trade_store", {}).get("root", ""))
    if not str(store_root):
        parser.error("--store or trade_store.root in the config is required")
    data_root = Path(config["tester_runner"]["bot_root"]) / "tester" / "data" / "bybit"
    workers = args.workers or int(config.get("duckdb_import", {}).get("workers", 4))
    warn = lambda text: print(json.dumps({"warning": text}), flush=True)          # noqa: E731
    order = history_pairs(args.config, on_busy=warn)    # the pair registry; the workbook only without one
    if order is None:
        order = pair_order(Path(config["panel_workflow"]["listing_dates_path"]))
    if args.pairs:
        wanted = [item.strip().upper() for item in args.pairs.split(",") if item.strip()]
        known = {**dict(order), **listing_days(args.config, on_busy=warn)}    # the registry wins, as in the plan
        missing = [symbol for symbol in wanted if symbol not in known]
        if missing:
            parser.error(f"pairs without a listing day in the registry: {', '.join(missing)}")
        order = [(symbol, known[symbol]) for symbol in wanted]
    if args.limit:
        order = order[: args.limit]
    store = TradeStore(store_root)
    cache = store.root / "instruments.json"
    instruments = {}
    for symbol, _ in order:
        item = instrument(symbol, cache)
        instruments[symbol] = (item.tick_size, item.qty_step)
    end_day = args.end_day or last_archived_day(datetime.now(timezone.utc)) + timedelta(days=1)
    timeframes = tuple(item.strip() for item in args.timeframes.split(",") if item.strip())
    started = time.monotonic()
    summary = run_history(store, data_root=data_root, pairs=order, end_day=end_day, instruments=instruments,
                          points=grid(timeframes), workers=workers, download=not args.no_download,
                          run_args={"pairs": [symbol for symbol, _ in order], "timeframes": timeframes,
                                    "workers": workers, "download": not args.no_download},
                          progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    print(json.dumps({**summary, "pairs": len(order), "end_day": end_day.isoformat(),
                      "seconds": round(time.monotonic() - started, 1)}), flush=True)
    return 1 if summary["failed"] else 0


def _bingx(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """BingX history: pairs, tick and qty step from the registry, minute days verified (Panel stage 5b)."""
    import duckdb

    from mrs3.bingx_downloader import last_available_day
    from mrs3.pair_registry.history import _registry_connection
    from mrs3.pair_registry.minute_days import known_records, write_records
    from mrs3.pair_registry.store import RegistryError, connect, load_registry_settings

    config = json.loads(args.config.read_text(encoding="utf-8"))
    store_root = args.store or Path(config.get("trade_store", {}).get("root", ""))
    if not str(store_root):
        parser.error("--store or trade_store.root in the config is required")
    try:
        settings = load_registry_settings(args.config)
    except RegistryError as error:
        parser.error(str(error))
    if settings.bingx_data_root is None:
        parser.error("the BingX minute data folder is unknown: set tester_runner.bot_root")
    warn = lambda text: print(json.dumps({"warning": text}), flush=True)          # noqa: E731
    order = history_pairs(args.config, on_busy=warn, exchange="bingx")
    if order is None:
        parser.error("the BingX history needs the pair registry (missing or held by a writer)")
    if args.pairs:
        wanted = [item.strip().upper() for item in args.pairs.split(",") if item.strip()]
        known_days = listing_days(args.config, "bingx", on_busy=warn)
        missing = [symbol for symbol in wanted if symbol not in known_days]
        if missing:
            parser.error(f"BingX pairs without a listing day in the registry: {', '.join(missing)}")
        order = [(symbol, known_days[symbol]) for symbol in wanted]
    if args.limit:
        order = order[: args.limit]
    end_day = args.end_day or last_available_day(datetime.now(timezone.utc)) + timedelta(days=1)
    con = _registry_connection(args.config, warn)
    if con is None:
        parser.error("the BingX history needs the pair registry (missing or held by a writer)")
    with con:
        rows = {row[0]: row[1:] for row in con.execute(
            "select symbol, api_symbol, listing_ms, tick_size, qty_step from pairs where exchange = 'bingx'").fetchall()}
        tables = {row[0] for row in con.execute("select table_name from information_schema.tables").fetchall()}
        known = {symbol: known_records(con, "bingx", symbol, listing, end_day)       # plans start at the listing
                 for symbol, listing in order} if "minute_days" in tables else {}
    unusable = [symbol for symbol, _ in order
                if not rows.get(symbol) or not rows[symbol][0] or not rows[symbol][2] or not rows[symbol][3]]
    if unusable:            # one incomplete registry row never blocks the other pairs
        warn(f"skipped, no api symbol, tick size or qty step in the registry: {', '.join(unusable)}")
        order = [(symbol, listing) for symbol, listing in order if symbol not in unusable]
        if not order:
            parser.error("no BingX pair with api symbol, tick size and qty step in the registry")
    instruments = {symbol: (float(rows[symbol][2]), float(rows[symbol][3])) for symbol, _ in order}
    minutes = BingXMinutes(str(settings.bingx_data_root),
                           {symbol: (rows[symbol][0], int(rows[symbol][1] or 0)) for symbol, _ in order},
                           known, settings.bingx_download_mode)
    timeframes = tuple(item.strip() for item in args.timeframes.split(",") if item.strip())
    workers = args.workers or settings.workers
    collected: list[dict] = []
    started = time.monotonic()
    summary = run_history(TradeStore(store_root), data_root=settings.bingx_data_root, pairs=order, end_day=end_day,
                          instruments=instruments, points=grid(timeframes), workers=workers,
                          download=not args.no_download, settings=exchange_run_settings(REPO, "bingx"),
                          exchange="bingx", bingx=minutes, on_minute_records=collected.extend,
                          run_args={"pairs": [symbol for symbol, _ in order], "timeframes": timeframes,
                                    "workers": workers, "download": not args.no_download, "exchange": "bingx"},
                          progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    if collected:
        try:
            registry = connect(settings.root)
            try:
                write_records(registry, collected)
            finally:
                registry.close()
        except duckdb.Error:
            warn("the registry is held by a writer: the minute verification is not recorded and runs again next time")
    print(json.dumps({**summary, "exchange": "bingx", "pairs": len(order), "end_day": end_day.isoformat(),
                      "minute_days_verified": len(collected), "seconds": round(time.monotonic() - started, 1)}),
          flush=True)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
