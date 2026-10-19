"""READY v4 (EQUAL MRS3 strategies + manifest) from the MRS2 trade store.

Contract: docs/specs/2026-10-17-mrs2-history-store-and-tester-sampling.md («Stage 2 contract»).

Run:  .venv\\Scripts\\python.exe scripts\\ready_from_store.py --store <folder> --out <folder> [--pairs SNOWUSDT,XOMUSDT]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mrs3.ready_store import build_ready  # noqa: E402
from mrs3.trade_store import TradeStore  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "templates" / "exchanges" / "Bybit" / "mrs3-strategy.json"
TEMPLATES = {"bybit": TEMPLATE, "bingx": REPO / "templates" / "exchanges" / "BingX" / "mrs3-strategy.json"}


def registry_rules(config: Path, exchange: str = "bybit") -> dict:
    """Per-pair READY rules from the pair registry (stage 3) when it is configured and exists; else none."""
    import duckdb

    from mrs3.pair_registry.records import effective_ready_rules
    from mrs3.pair_registry.store import DATABASE, RegistryError, load_registry_settings

    try:
        settings = load_registry_settings(config)
    except RegistryError:
        return {}
    if not (settings.root / DATABASE).is_file():
        return {}
    # read-only: a READY build never writes to the registry; a registry locked by a writer, or stored overrides
    # that no longer form a valid rule, stop the build loudly (registry stage 3 contract)
    con = duckdb.connect(str(settings.root / DATABASE), read_only=True)
    try:
        tables = {row[0] for row in con.execute("select table_name from information_schema.tables").fetchall()}
        return effective_ready_rules(con, exchange) if "pair_settings" in tables else {}
    finally:
        con.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=None, help="store root; default: trade_store.root in the config")
    parser.add_argument("--config", type=Path, default=REPO / "config.local.json")
    parser.add_argument("--out", type=Path, required=True, help="new READY folder (must not exist)")
    parser.add_argument("--pairs", default="", help="comma-separated symbols; default: every pair in the store")
    parser.add_argument("--latest-source", choices=("simulator", "tester", "auto"), default="simulator",
                        help="newest period from the simulator, from the imported tester segment, or (auto) from the "
                             "tester segment when it exists")
    parser.add_argument("--exchange", choices=("bybit", "bingx"), default="bybit")
    args = parser.parse_args(argv)
    store_root = args.store
    if store_root is None:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        store_root = Path(config.get("trade_store", {}).get("root", ""))
    if not str(store_root) or not (Path(store_root) / "catalog.duckdb").is_file():
        parser.error("a store with catalog.duckdb is required (--store or trade_store.root)")
    if args.out.exists():
        parser.error(f"output folder already exists: {args.out}")
    store = TradeStore(store_root)
    if args.pairs:
        symbols = [item.strip().upper() for item in args.pairs.split(",") if item.strip()]
    else:
        with store.connect(read_only=True) as con:
            symbols = [row[0] for row in con.execute(
                "select distinct symbol from segments where status = 'active' and exchange = ? order by symbol",
                [args.exchange]).fetchall()]
    template = json.loads(TEMPLATES[args.exchange].read_text(encoding="utf-8"))
    summary = build_ready(store, symbols, args.out, template, exchange=args.exchange, latest_source=args.latest_source,
                          rules=registry_rules(args.config, args.exchange),
                          progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
    print(json.dumps({**summary, "out": str(args.out)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
