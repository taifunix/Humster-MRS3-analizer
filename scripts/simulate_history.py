"""Simulate MRS2 history of the registry pairs into the trade store (P2 segments + the latest 20-day window).

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 1 contract»).

Run:  .venv\\Scripts\\python.exe scripts\\simulate_history.py --store <folder> [--pairs SNOWUSDT,XOMUSDT] [--limit 5]
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
from mrs3.trade_history import pair_order, run_history  # noqa: E402
from mrs3.trade_store import TradeStore  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def registry_listing(config: Path) -> dict:
    """Listing days of the Bybit pairs in the pair registry (read-only); empty when there is no registry or a
    writer holds it."""
    from datetime import datetime, timezone

    import duckdb

    from mrs3.pair_registry.store import DATABASE, RegistryError, load_registry_settings

    try:
        settings = load_registry_settings(config)
    except RegistryError:
        return {}
    if not (settings.root / DATABASE).is_file():
        return {}
    try:
        con = duckdb.connect(str(settings.root / DATABASE), read_only=True)
    except duckdb.Error as error:          # the registry is held by a writer: fall back to the workbook dates
        print(json.dumps({"warning": f"pair registry unavailable: {type(error).__name__}"}), flush=True)
        return {}
    with con:
        rows = con.execute("select symbol, listing_ms from pairs where exchange = 'bybit' and listing_ms is not null"
                           ).fetchall()
    return {symbol: datetime.fromtimestamp(ms / 1000, timezone.utc).date() for symbol, ms in rows}


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
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    store_root = args.store or Path(config.get("trade_store", {}).get("root", ""))
    if not str(store_root):
        parser.error("--store or trade_store.root in the config is required")
    data_root = Path(config["tester_runner"]["bot_root"]) / "tester" / "data" / "bybit"
    workers = args.workers or int(config.get("duckdb_import", {}).get("workers", 4))
    order = pair_order(Path(config["panel_workflow"]["listing_dates_path"]))
    if args.pairs:
        wanted = [item.strip().upper() for item in args.pairs.split(",") if item.strip()]
        known = {**dict(order), **registry_listing(args.config)}    # the registry wins, as in the Panel plan
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


if __name__ == "__main__":
    raise SystemExit(main())
