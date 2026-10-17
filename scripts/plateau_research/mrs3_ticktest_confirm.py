"""Confirmatory MRS3 tick tests: run a handful of READY v4 candidate structures on the real tester and compare
their true combined outcome to the simulator-approximated forward outcome from store_threshold_study.py.

The study approximates a structure's forward curve by summing two INDEPENDENT 10,000-lot MRS2 runs (base 20,000).
A real MRS3 strategy (ADR-0076, EQUAL lots) splits ONE fixed balance (my_fix_balance) evenly between its orders.
Both are fixed-notional, no-compounding backtests over the same price action, so percentage outcomes (PnL%,
MaxDD%) are expected to scale-match even though the dollar bases differ; only the % figures are compared.

Usage: .venv\\Scripts\\python.exe scripts\\plateau_research\\mrs3_ticktest_confirm.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mrs3.config import AlgorithmConfig  # noqa: E402
from mrs3.lots import LotMethod, allocate_lots  # noqa: E402
from mrs3.mrs2_simulator import DAY_MS  # noqa: E402
from mrs3.performance import parse_performance_report  # noqa: E402
from mrs3.ready_store import ReadyRule, best_structures, common_points, period_features, structure_document  # noqa: E402
from mrs3.strategy_json import generate_strategy  # noqa: E402
from mrs3.tester_jobs import _backup, _restore, run_hb_tester  # noqa: E402
from mrs3.trade_store import TradeStore  # noqa: E402

PERMISSIVE = ReadyRule(admit_score=-1e18, max_dd=1e18)

# (pair, cut index, timeframe) picked from data/ready_threshold_study.parquet, spanning categories and the
# proposed admit_score boundary (25-35); cut is the forward-period index into store.segments(pair, "simulator").
CASES = [
    ("XLFUSDT", 2, "2h", "low, score 73.9"),
    ("REGNUSDT", 2, "15m", "moderate, score high"),
    ("MOUSDT", 2, "30m", "high, score high"),
    ("LLYUSDT", 2, "45m", "near new cutoff, won forward"),
    ("IONQUSDT", 2, "4h", "near new cutoff, lost forward"),
    ("PLTRUSDT", 2, "15m", "rejected (score<10), lost forward"),
]


def candidate(store: TradeStore, symbol: str, cut: int, timeframe: str) -> dict:
    segments = store.segments(symbol, source="simulator")
    loaded = [(segment, store.read_metrics(segment)) for segment in segments]
    features = [period_features(metrics, (segment["end_ms"] - segment["start_ms"]) / DAY_MS)
                for segment, metrics in loaded]
    train = common_points(features[max(0, cut - 3):cut][::-1])
    found = best_structures(train, PERMISSIVE)
    item = next(candidate for candidate in found if candidate["timeframe"] == timeframe)
    forward_segment, _ = loaded[cut]
    return {"symbol": symbol, "item": item, "start_ms": forward_segment["start_ms"],
            "end_ms": forward_segment["end_ms"]}


def build_strategy(symbol: str, item: dict) -> dict:
    config = AlgorithmConfig()
    structure = structure_document(symbol, item)
    lots = allocate_lots(structure["orders"], LotMethod.EQUAL, config)
    return generate_strategy(json.loads((ROOT / "templates/exchanges/Bybit/mrs3-strategy.json")
                                        .read_text(encoding="utf-8")), structure, lots, LotMethod.EQUAL, config)


def build_config(start_ms: int, end_ms: int, name: str) -> dict:
    import datetime
    document = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-tester.json").read_text(encoding="utf-8"))
    start = datetime.datetime.fromtimestamp(start_ms / 1000, datetime.timezone.utc).date()
    end = datetime.datetime.fromtimestamp(end_ms / 1000, datetime.timezone.utc).date()
    document.update(name_comment=name, StartDate=start.isoformat(), EndDate=end.isoformat(), WarmupDays=4,
                    UpdateData=False, order_limit_fill_plus_tick=True)
    return document


def main() -> int:
    config = json.loads((ROOT / "config.local.json").read_text(encoding="utf-8"))
    store = TradeStore(Path(config["trade_store"]["root"]))
    bot_root = Path(config["tester_runner"]["bot_root"])
    results = []
    for symbol, cut, timeframe, note in CASES:
        pick = candidate(store, symbol, cut, timeframe)
        item = pick["item"]
        strategy = build_strategy(symbol, item)
        name = f"mrs3tick_{symbol}_{timeframe}_{cut}"
        doc = build_config(pick["start_ms"], pick["end_ms"], name)
        backup = store.root / "jobs" / f"ticktest_{name}" / "backup"
        backup.parent.mkdir(parents=True, exist_ok=True)
        manifest = _backup(bot_root, backup)
        try:
            (bot_root / "config_tester.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                                                         encoding="utf-8")
            (bot_root / "tester" / "settings_strategy" / f"{strategy['name']}.json").write_text(
                json.dumps(strategy, ensure_ascii=False, indent=2), encoding="utf-8")
            started = time.monotonic()
            run_hb_tester(bot_root, 600.0, backup.parent / "tester.log")
            elapsed = time.monotonic() - started
        finally:
            _restore(bot_root, backup, manifest)
        try:
            report_dir = bot_root / "tester" / "report" / name
            paths = sorted(report_dir.glob("*.html")) if report_dir.is_dir() else []
            if not paths:
                raise RuntimeError("no report")
            report = parse_performance_report(paths[0].read_bytes(), allow_zero_activity=True,
                                              report_format_2026_10=True)
            results.append({
                "symbol": symbol, "cut": cut, "timeframe": timeframe, "note": note, "seconds": round(elapsed, 1),
                "proxy_score": item["score"], "proxy_dd": item["dd"], "orders": item["orders"],
                "real_pnl_pct": float(report.metrics.get("Total PnL, %", "nan")),
                "real_maxdd_pct": float(report.metrics.get("Max Drawdown, %", "nan")),
                "real_ulcer": report.metrics.get("Ulcer Index (hourly), %"),
                "real_trades": report.metrics.get("Total Trades (entry → full exit)"),
                "days": (pick["end_ms"] - pick["start_ms"]) / DAY_MS,
            })
        except Exception as error:  # the tester's files are already restored; one bad case keeps the rest
            results.append({"symbol": symbol, "cut": cut, "timeframe": timeframe, "note": note,
                            "error": f"{type(error).__name__}: {error}"})
        print(json.dumps(results[-1], ensure_ascii=False, default=str), flush=True)
        # written after every case, not only at the end: a later case's failure keeps the earlier results
        Path(ROOT / "data" / "mrs3_ticktest_confirm.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
