"""Sampled tester runs from the MRS2 trade store: plan jobs, run them on the local tester, import tester segments.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 3 contract»).

BingX (`--exchange bingx`, Panel stage 5c): the tester and the strategy get the BingX API symbol, the BingX exchange
block and fees; the minute days of every job are ensured and verified before the tester starts.

Run:  .venv\\Scripts\\python.exe scripts\\tester_jobs.py plan   --store <folder> [--pairs XOMUSDT]
      .venv\\Scripts\\python.exe scripts\\tester_jobs.py run    --store <folder> [--limit 1] [--keep-html]
      .venv\\Scripts\\python.exe scripts\\tester_jobs.py import --store <folder> [--pairs XOMUSDT]
      .venv\\Scripts\\python.exe scripts\\tester_jobs.py retry  --store <folder> [--jobs id1,id2]
      .venv\\Scripts\\python.exe scripts\\tester_jobs.py recover --store <folder>

`run` stops between jobs when <store>/jobs/.stop appears (the Panel's Stop) and first recovers an interrupted
executor: the tester's files are restored from the job backup and the interrupted job is marked failed.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs3.minute_data import ensure_days  # noqa: E402
from mrs3.mrs2_simulator import RunSettings  # noqa: E402
from mrs3.tester_jobs import (  # noqa: E402
    JobQueueError, RestoreError, import_window, plan_jobs, recover, retry_failed, run_queue,
)
from mrs3.trade_history import exchange_run_settings, missing_days  # noqa: E402
from mrs3.trade_store import TradeStore  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
STRATEGY_TEMPLATE = REPO / "templates" / "strategies" / "source-v6-mrs2" / "long.json"
CONFIG_TEMPLATE = REPO / "templates" / "tester" / "mrs2" / "config_tester_long.json"
BINGX_STRATEGY = REPO / "templates" / "exchanges" / "BingX" / "mrs3-strategy.json"


def _pairs(store: TradeStore, text: str, exchange: str = "bybit") -> list[str]:
    if text:
        return [item.strip().upper() for item in text.split(",") if item.strip()]
    with store.connect(read_only=True) as con:
        return [row[0] for row in con.execute("select distinct symbol from segments where status = 'active' and "
                                              "source = 'simulator' and exchange = ? order by symbol",
                                              [exchange]).fetchall()]


def _bingx_api_symbols(config: Path, pairs: Sequence[str]) -> dict[str, str]:
    """The BingX API symbol of every pair, from the pair registry (read-only)."""
    from mrs3.pair_registry.history import _registry_connection

    con = _registry_connection(config, None)
    if con is None:
        raise JobQueueError("the BingX tester needs the pair registry (missing or held by a writer)")
    with con:
        rows = dict(con.execute("select symbol, api_symbol from pairs where exchange = 'bingx'").fetchall())
    missing = [symbol for symbol in pairs if not rows.get(symbol)]
    if missing:
        raise JobQueueError(f"BingX pairs without an api symbol in the registry: {', '.join(missing)}")
    return {symbol: rows[symbol] for symbol in pairs}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("plan", "run", "import", "retry", "recover"))
    parser.add_argument("--store", type=Path, default=None, help="store root; default: trade_store.root in the config")
    parser.add_argument("--config", type=Path, default=REPO / "config.local.json")
    parser.add_argument("--pairs", default="")
    parser.add_argument("--fraction", type=float, default=0.10, help="plan: share of the grid taken by UPI")
    parser.add_argument("--limit", type=int, default=0, help="run: at most this many jobs")
    parser.add_argument("--timeout", type=float, default=6 * 3600, help="run: seconds per tester run")
    parser.add_argument("--keep-html", action="store_true")
    parser.add_argument("--jobs", default="", help="retry: comma-separated job ids; default: every failed job")
    parser.add_argument("--exchange", choices=("bybit", "bingx"), default="bybit")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    store_root = args.store or Path(config.get("trade_store", {}).get("root", ""))
    if not str(store_root) or not (Path(store_root) / "catalog.duckdb").is_file():
        parser.error("a store with catalog.duckdb is required (--store or trade_store.root)")
    store = TradeStore(store_root)
    workers = int(config.get("duckdb_import", {}).get("workers", 4))
    bot_root = Path(config["tester_runner"]["bot_root"])
    pairs = _pairs(store, args.pairs, args.exchange)
    bingx = args.exchange == "bingx"
    settings = exchange_run_settings(REPO, args.exchange)

    if args.command == "plan":
        strategy = json.loads(STRATEGY_TEMPLATE.read_text(encoding="utf-8"))
        template = json.loads(CONFIG_TEMPLATE.read_text(encoding="utf-8"))
        extra: dict = {}
        try:
            api = _bingx_api_symbols(args.config, pairs) if bingx else {}
        except JobQueueError as error:
            print(json.dumps({"state": "failed", "error": str(error)}), flush=True)
            return 1
        exchange_block = json.loads(BINGX_STRATEGY.read_text(encoding="utf-8"))["exchange"] if bingx else None
        for symbol in pairs:
            if bingx:
                extra = {"exchange": "bingx", "settings": settings, "tester_symbol": api[symbol],
                         "strategy_exchange": exchange_block}
            print(json.dumps(plan_jobs(store, symbol, strategy_template=strategy, config_template=template,
                                       parallel_runs=workers, fraction=args.fraction, **extra)), flush=True)
        return 0
    if args.command == "retry":
        wanted = [item.strip() for item in args.jobs.split(",") if item.strip()] or None
        print(json.dumps({"requeued": retry_failed(store, wanted)}))
        return 0
    if args.command == "import":
        for symbol in pairs:
            print(json.dumps(import_window(store, symbol, settings=settings, exchange=args.exchange)), flush=True)
        return 0

    if args.command == "recover":
        print(json.dumps(recover(store, bot_root)), flush=True)
        return 0

    data_root = bot_root / "tester" / "data" / "bybit"
    warmup = RunSettings().warmup_days

    def prepare(job: Mapping) -> str | None:
        first, end = date.fromisoformat(job["start"]) - timedelta(days=warmup), date.fromisoformat(job["end"])
        if job.get("exchange", "bybit") == "bingx":
            return prepare_bingx(job, first, end)
        ensure_days(data_root, job["symbol"], first, end - timedelta(days=1), workers=max(1, min(8, workers)))
        absent = missing_days(data_root, job["symbol"], first, end)
        return f"MISSING_DAYS: {[day.isoformat() for day in absent][:10]}" if absent else None

    def prepare_bingx(job: Mapping, first: date, end: date) -> str | None:
        """Every day of warm-up and window ensured and verified in the folder the tester reads; the tester never
        downloads BingX data. A registry or file problem fails the job (no local path in the message)."""
        import duckdb

        from mrs3.pair_registry.minute_days import ensure_bingx_days_shared
        from mrs3.pair_registry.store import RegistryError, load_registry_settings

        try:
            registry = load_registry_settings(args.config)
            if registry.bingx_data_root is None or \
                    registry.bingx_data_root.resolve() != (bot_root / "tester" / "data" / "bingx").resolve():
                return "MISSING_DAYS: pair_registry.minute_data.bingx_data_root is not the tester's BingX folder"
            days = ensure_bingx_days_shared(registry, job["symbol"], first, end - timedelta(days=1),
                                            api_symbol=job.get("tester_symbol"))
        except (RegistryError, duckdb.Error, OSError) as error:
            return f"MISSING_DAYS: the minute days were not checked ({type(error).__name__})"
        accepted = set(days.accepted)
        absent = [first + timedelta(days=offset) for offset in range((end - first).days)
                  if first + timedelta(days=offset) not in accepted]
        return f"MISSING_DAYS: {[day.isoformat() for day in absent][:10]}" if absent else None

    try:
        summary = run_queue(store, bot_root, symbols=pairs if args.pairs else None, limit=args.limit,
                            exchange=args.exchange,
                            timeout_s=args.timeout, keep_html=args.keep_html, prepare=prepare,
                            report=lambda item: print(json.dumps(item), flush=True))
    except RestoreError as error:
        print(json.dumps({"state": "failed", "restore_error": str(error)}), flush=True)
        return 2                     # the tester's own files need attention: stop the executor
    except JobQueueError as error:
        print(json.dumps({"state": "failed", "error": str(error)}), flush=True)
        return 1
    print(json.dumps({"summary": summary}), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
