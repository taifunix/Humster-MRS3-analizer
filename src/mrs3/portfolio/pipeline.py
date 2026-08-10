"""Оркестрация полного прогона анализатора портфеля."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .models import RunConfig, SetResult, StrategyInput, TradeRecord
from .search import correlation_matrix, enumerate_sets, pareto_front, split_validation
from .trade_logs import load_trades

__all__ = ["PortfolioInputs", "PortfolioError", "run_portfolio", "read_strategies"]


class PortfolioError(ValueError):
    """Нарушен контракт входных данных."""


REQUIRED_COLUMNS = (
    "strategy_id", "pair", "side", "timeframe",
    "lot_x_base", "pnl_pct", "dd_pct", "turnover_24h",
)


@dataclass(frozen=True)
class PortfolioInputs:
    strategies_csv: Path
    trades_db: Path
    output_dir: Path
    trades_table: str = "trades"
    config: RunConfig = RunConfig()


def read_strategies(csv_path: Path) -> list[StrategyInput]:
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise PortfolioError(f"в CSV нет обязательных колонок: {', '.join(missing)}")
        rows = list(reader)

    out: list[StrategyInput] = []
    for row in rows:
        sid = (row.get("strategy_id") or "").strip()
        if not sid:
            continue
        try:
            out.append(
                StrategyInput(
                    strategy_id=sid,
                    pair=row["pair"].strip(),
                    side=row["side"].strip().upper(),
                    timeframe=row["timeframe"].strip(),
                    lot_x_base=float(row["lot_x_base"]),
                    pnl_pct=float(row["pnl_pct"]),
                    dd_pct=float(row["dd_pct"]),
                    turnover_24h=float(row["turnover_24h"]),
                    target_share=float(row.get("target_share") or 0.115),
                    target_share_source=(row.get("target_share_source") or "ESTIMATED").strip().upper(),
                    mmr=float(row.get("mmr") or 0.02),
                    imr=float(row.get("imr") or 0.05),
                    orders=int(row.get("orders") or 1),
                )
            )
        except ValueError as exc:
            raise PortfolioError(f"{sid}: {exc}") from exc
    if not out:
        raise PortfolioError("во входном CSV нет ни одной стратегии")
    return out


def attach_trades(
    strategies: Sequence[StrategyInput],
    trades: dict[str, list[TradeRecord]],
) -> tuple[list[StrategyInput], list[dict]]:
    """Пришить журналы. Стратегии без журнала выбывают с явной причиной."""
    ready: list[StrategyInput] = []
    coverage: list[dict] = []
    for s in sorted(strategies, key=lambda x: x.strategy_id):
        log = trades.get(s.strategy_id, [])
        if len(log) < 2:
            coverage.append({
                "strategy_id": s.strategy_id,
                "status": "MISSING_TRADE_LOG" if not log else "TOO_FEW_TRADES",
                "trades": len(log), "window_start": "", "window_end": "",
                "median_hold_min": "",
            })
            continue
        filled = replace(s, trades=log)
        ready.append(filled)
        coverage.append({
            "strategy_id": s.strategy_id, "status": "OK", "trades": len(log),
            "window_start": filled.window_start.isoformat(),
            "window_end": filled.window_end.isoformat(),
            "median_hold_min": round(filled.median_hold_min, 3),
        })
    for sid in sorted(set(trades) - {s.strategy_id for s in strategies}):
        coverage.append({
            "strategy_id": sid, "status": "EXTRA_IN_DB", "trades": len(trades[sid]),
            "window_start": "", "window_end": "", "median_hold_min": "",
        })
    return ready, coverage


def _write_csv(rows: Sequence[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def _set_row(result: SetResult) -> dict:
    return {
        "strategy_ids": "|".join(result.strategy_ids),
        "limiter": result.limiter,
        "size": len(result.strategy_ids),
        "weights": "|".join(str(w) for w in result.weights),
        "g": round(result.g, 5),
        "lots": "|".join(f"{v:.5f}" for v in result.lots),
        "pnl30_pct": round(result.pnl30_pct, 4),
        "pnl_pct": round(result.pnl_pct, 4),
        "pnl_abs": round(result.pnl_abs, 2),
        "max_dd_pct": round(result.max_dd_pct, 4),
        "max_margin_ratio": round(result.max_margin_ratio, 5),
        "max_occupancy_margin": round(result.max_occupancy_margin, 5),
        "min_buffer": round(result.min_buffer, 5),
        "capital_requirement": round(result.capital_requirement, 5),
        "efficiency": round(result.efficiency, 4),
        "d_eff_common_days": round(result.d_eff_common_days, 3),
        "min_acceptance": round(min(o.acceptance for o in result.outcomes), 4),
        "flags": "|".join(result.flags),
    }


def run_portfolio(inputs: PortfolioInputs) -> dict:
    cfg = inputs.config
    out = Path(inputs.output_dir)
    strategies = read_strategies(inputs.strategies_csv)
    trades = load_trades(inputs.trades_db, inputs.trades_table)
    ready, coverage = attach_trades(strategies, trades)
    _write_csv(coverage, out / "00_Trade_Log_Coverage.csv")

    if len(ready) < min(cfg.limiters):
        raise PortfolioError(
            f"стратегий с журналами {len(ready)}, для лимита {min(cfg.limiters)} нужно минимум столько же"
        )

    _write_csv([{
        "strategy_id": s.strategy_id, "pair": s.pair, "side": s.side,
        "timeframe": s.timeframe, "trades": len(s.trades),
        "d_eff_days": round(s.d_eff_days, 3),
        "trades_per_day": round(s.trades_per_day, 4),
        "median_hold_min": round(s.median_hold_min, 3),
        "occupancy": round(s.occupancy, 6), "lot_x_base": s.lot_x_base,
        "capacity": round(s.capacity, 2), "mmr": s.mmr, "imr": s.imr,
        "target_share": s.target_share, "target_share_source": s.target_share_source,
    } for s in ready], out / "02_Candidates.csv")

    results, screened = enumerate_sets(ready, cfg)
    _write_csv(screened, out / "03_Layer_A_Screen.csv")
    _write_csv([_set_row(r) for r in results], out / "05_All_Sets.csv")

    front = pareto_front(results)
    _write_csv([_set_row(r) for r in front], out / "06_Pareto_Front.csv")

    _write_csv([{
        "strategy_ids": "|".join(r.strategy_ids), "limiter": r.limiter,
        "strategy_id": o.strategy_id, "accepted": o.accepted,
        "blocked_slot": o.blocked_slot, "blocked_margin": o.blocked_margin,
        "acceptance": round(o.acceptance, 4), "pnl_abs": round(o.pnl_abs, 2),
    } for r in front for o in r.outcomes], out / "07_Contention.csv")

    corr = correlation_matrix(ready)
    _write_csv(corr["pairs"], out / "09_Correlation.csv")

    oos = split_validation(ready, cfg)
    if oos.get("rows"):
        _write_csv(oos["rows"], out / "10_OOS_Validation.csv")

    manifest = {
        "module": "portfolio-analyzer",
        "spec": "docs/specs/2026-08-09-portfolio-analyzer-v04.md",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "deposit": cfg.deposit, "dd_target_pct": cfg.dd_target_pct,
            "margin_limit": cfg.margin_limit, "limiters": list(cfg.limiters),
            "max_size_factor": cfg.max_size_factor,
            "cancel_opposite": cfg.cancel_opposite,
            "long_short_same_slot": cfg.long_short_same_slot,
            "weight_levels": list(cfg.weight_levels),
        },
        "strategies_in_csv": len(strategies),
        "strategies_with_trades": len(ready),
        "combinations_screened": len(screened),
        "sets_simulated": len(results),
        "pareto_size": len(front),
        "best_by_efficiency": _set_row(results[0]) if results else None,
        "correlation": {
            "mean_abs_corr": corr["mean_abs_corr"],
            "worst_day_frac": corr["worst_day_frac"],
            "flag": "HIGH_CORRELATION" if corr["mean_abs_corr"] > cfg.correlation_flag else "",
        },
        "oos": {k: v for k, v in oos.items() if k != "rows"},
        "unverified": [
            "очередь в стакане не моделируется: результат — верхняя граница",
            "MMR/IMR взяты из CSV, сверить с /v5/market/risk-limit",
            "target_share ESTIMATED, пока не измерен по стакану L2"
            if any(s.target_share_source == "ESTIMATED" for s in ready) else "",
        ],
        "artifacts": sorted(p.name for p in out.glob("*")),
    }
    (out / "portfolio_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
