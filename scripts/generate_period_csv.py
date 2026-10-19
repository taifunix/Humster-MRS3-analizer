"""Generate MRS2 period CSVs (tester `reports_history` format) with the simulator; the last period gets a tester config.

Contract: docs/specs/2026-10-16-mrs2-period-csv-generator.md.

Run:  .venv\\Scripts\\python.exe scripts\\generate_period_csv.py --out <folder> [--pairs SNOWUSDT,XOMUSDT] [--limit 5]
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs3.minute_data import ensure_days, instrument, last_archived_day  # noqa: E402
from mrs3.mrs2_simulator import RunSettings  # noqa: E402
from mrs3.pair_registry.history import history_pairs, listing_days  # noqa: E402
from mrs3.trade_history import pair_order  # noqa: E402
from mrs3.period_csv import (  # noqa: E402
    GENERATOR_VERSION, TIMEFRAMES, csv_bytes, first_period_day, grid, has_warmup_data, header, journal_append,
    load_minutes, ms_of, run_snapshot, sha256, simulate_rows, split_periods, strategy_document, build_tester_config,
    write_period_folder,
)

REPO = Path(__file__).resolve().parents[1]
STRATEGY_TEMPLATE = REPO / "templates" / "strategies" / "source-v6-mrs2" / "long.json"
CONFIG_TEMPLATE = REPO / "templates" / "tester" / "mrs2" / "config_tester_long.json"


def prepare_pair(symbol: str, listing: date, out: str, data_root: str, cache: str, end_day: date,
                 from_day: date | None, timeframes: Sequence[str]) -> tuple[list[tuple[date, date]], list[dict]]:
    """Download missing days, cut the periods, write the tester config of the last one; return the simulated periods."""
    settings = RunSettings()
    base = {"pair": symbol, "side": "LONG", "listing_date": listing.isoformat(), "generator": GENERATOR_VERSION,
            "limit_fill_plus_tick": settings.limit_fill_plus_tick}
    first = first_period_day(listing, settings.warmup_days, from_day)
    periods = split_periods(first, end_day)
    if not periods:
        return [], [{**base, "status": "NO_FULL_PERIOD", "start": first.isoformat(), "end": end_day.isoformat()}]
    warm_first = periods[0][0] - timedelta(days=settings.warmup_days)
    days = ensure_days(Path(data_root), symbol, warm_first, end_day - timedelta(days=1))
    records = [{**base, "status": "DATA", "days_created": len(days["created"]),
                "days_failed": [day.isoformat() for day in days["failed"]]}]
    strategy = strategy_document(json.loads(STRATEGY_TEMPLATE.read_text(encoding="utf-8")), symbol, settings)
    template = json.loads(CONFIG_TEMPLATE.read_text(encoding="utf-8"))
    start, end = periods[-1]
    name = f"tst_{symbol}_{start.isoformat()}_{end.isoformat()}"
    folder = Path(out) / symbol / f"{start.isoformat()}_{end.isoformat()}_tester"
    record = {**base, "start": start.isoformat(), "end": end.isoformat(), "source": "tester_pending"}
    if folder.exists():
        records.append({**record, "status": "EXISTS"})
    else:
        config = build_tester_config(template, symbol, start, end, name, timeframes, settings)
        write_period_folder(folder, {
            "config_tester.json": (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            f"{strategy['name']}.json": (json.dumps(strategy, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        })
        records.append({**record, "status": "TESTER_CONFIG"})
    return periods[:-1], records


def simulate_one(symbol: str, listing: date, start: date, end: date, out: str, data_root: str, cache: str,
                 timeframes: Sequence[str], report_utc: str) -> dict:
    """One pair × one period with the simulator: reports_history.csv + run_snapshot in the period folder."""
    settings = RunSettings()
    record = {"pair": symbol, "side": "LONG", "listing_date": listing.isoformat(), "generator": GENERATOR_VERSION,
              "limit_fill_plus_tick": settings.limit_fill_plus_tick,
              "start": start.isoformat(), "end": end.isoformat(), "source": "simulator"}
    folder = Path(out) / symbol / f"{start.isoformat()}_{end.isoformat()}"
    if folder.exists():
        return {**record, "status": "EXISTS"}
    inst = instrument(symbol, Path(cache))
    warm_first = start - timedelta(days=settings.warmup_days)
    minutes = load_minutes(Path(data_root) / symbol, symbol, ms_of(warm_first), ms_of(end))
    if not has_warmup_data(minutes, start, settings.warmup_days):
        return {**record, "status": "NO_WARMUP_DATA"}
    name = f"sim_{symbol}_{start.isoformat()}_{end.isoformat()}"
    rows = simulate_rows(minutes, symbol, start, end, tick=inst.tick_size, qty_step=inst.qty_step, name=name,
                         report_utc=report_utc, points=grid(timeframes), settings=settings)
    data = csv_bytes(header(ms_of(start), ms_of(end)), rows)
    strategy = strategy_document(json.loads(STRATEGY_TEMPLATE.read_text(encoding="utf-8")), symbol, settings)
    config = build_tester_config(json.loads(CONFIG_TEMPLATE.read_text(encoding="utf-8")), symbol, start, end, name,
                                 timeframes, settings)
    snapshot = json.dumps(run_snapshot(strategy, config), ensure_ascii=False, indent=2).encode("utf-8")
    write_period_folder(folder, {"reports_history.csv": data, f"run_snapshot_{name}.json": snapshot})
    return {**record, "status": "OK", "rows": len(rows), "csv_sha256": sha256(data), "tick_size": inst.tick_size,
            "qty_step": inst.qty_step}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=REPO / "config.local.json")
    parser.add_argument("--pairs", default="", help="comma-separated symbols; default: registry order")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--from-date", type=date.fromisoformat, default=None)
    parser.add_argument("--timeframes", default=",".join(TIMEFRAMES))
    parser.add_argument("--workers", type=int, default=0)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    bot_root = Path(config["tester_runner"]["bot_root"])
    data_root = bot_root / "tester" / "data" / "bybit"
    workers = args.workers or int(config.get("duckdb_import", {}).get("workers", 4))
    warn = lambda text: print(json.dumps({"warning": text}), flush=True)          # noqa: E731
    order = history_pairs(args.config, on_busy=warn)    # the pair registry; the workbook only without one
    if order is None:
        order = pair_order(Path(config["panel_workflow"]["listing_dates_path"]))
    if args.pairs:
        wanted = [item.strip().upper() for item in args.pairs.split(",") if item.strip()]
        known = {**dict(order), **listing_days(args.config, on_busy=warn)}      # any status, as simulate_history
        order = [(symbol, known[symbol]) for symbol in wanted if symbol in known]
    if args.limit:
        order = order[: args.limit]
    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.out / "instruments.json"
    for symbol, _ in order:      # fetched once here, so workers only read the cache
        instrument(symbol, cache)
    end_day = last_archived_day(datetime.now(timezone.utc)) + timedelta(days=1)
    report_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    journal = args.out / "journal.jsonl"
    timeframes = tuple(item.strip() for item in args.timeframes.split(",") if item.strip())
    failures = 0
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        # pairs are prepared in priority order; each simulated period is its own task
        pending = {pool.submit(prepare_pair, symbol, listing, str(args.out), str(data_root), str(cache), end_day,
                               args.from_date, timeframes): ("prepare", symbol, listing) for symbol, listing in order}
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                kind, symbol, listing = pending.pop(future)
                try:
                    result = future.result()
                except Exception as error:  # one pair or period never stops the others
                    failures += 1
                    journal_append(journal, {"pair": symbol, "status": "ERROR", "stage": kind,
                                             "error": f"{type(error).__name__}: {error}"})
                    print(symbol, kind, "ERROR", error, flush=True)
                    continue
                if kind == "prepare":
                    periods, records = result
                    for record in records:
                        journal_append(journal, record)
                    for start, end in periods:
                        pending[pool.submit(simulate_one, symbol, listing, start, end, str(args.out), str(data_root),
                                            str(cache), timeframes, report_utc)] = ("period", symbol, listing)
                    print(symbol, "prepared:", len(periods), "simulated periods", flush=True)
                else:
                    journal_append(journal, result)
                    print(symbol, result["start"], result["end"], result["status"], flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
