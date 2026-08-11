"""Оркестрация слоя A: чтение входа, отсев, запись артефактов и манифеста."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .layer_a import (
    Candidate,
    LayerAError,
    candidates_report,
    screen_all,
    screen_report,
)
from .trade_logs import TradeLogError, coverage_report, load_stats

REQUIRED_CSV_COLUMNS = (
    "strategy_id",
    "pair",
    "side",
    "timeframe",
    "trades",
    "lot_x_base",
    "pnl_pct",
    "dd_pct",
    "turnover_24h",
)
# Эти три можно не задавать в CSV, если подан журнал сделок: они выводятся из него.
DERIVABLE_COLUMNS = ("median_hold_min", "window_start", "window_end")


@dataclass(frozen=True)
class LayerAInputs:
    candidates_csv: Path
    output_dir: Path
    trades_db: Path | None = None
    trades_table: str = "trades"
    limiters: tuple[int, ...] = (2, 3, 4)
    max_size_factor: int = 3


def _parse_ts(raw: str, field: str, sid: str) -> datetime:
    text = (raw or "").strip().replace("Z", "+00:00")
    if not text:
        raise LayerAError(f"{sid}: пустое поле {field}")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LayerAError(f"{sid}: {field} не в формате ISO 8601: {raw!r}") from exc
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def read_candidates(
    csv_path: Path,
    derived: dict[str, object] | None = None,
) -> list[Candidate]:
    """Прочитать кандидатов. `derived` — данные из журналов сделок, если поданы."""
    derived = derived or {}
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        header = reader.fieldnames or []
        missing = [c for c in REQUIRED_CSV_COLUMNS if c not in header]
        if missing:
            raise LayerAError(f"в CSV нет обязательных колонок: {', '.join(missing)}")
        rows = list(reader)

    out: list[Candidate] = []
    for row in rows:
        sid = (row.get("strategy_id") or "").strip()
        if not sid:
            continue
        stats = derived.get(sid)

        def pick(column: str):
            raw = (row.get(column) or "").strip()
            if raw:
                return raw
            if stats is None:
                raise LayerAError(
                    f"{sid}: нет {column} ни в CSV, ни в журнале сделок. "
                    "Задайте колонку или подайте --trades-db."
                )
            return getattr(stats, column)

        median_hold = pick("median_hold_min")
        window_start = pick("window_start")
        window_end = pick("window_end")

        try:
            candidate = Candidate(
                strategy_id=sid,
                pair=row["pair"].strip(),
                side=row["side"].strip().upper(),
                timeframe=row["timeframe"].strip(),
                trades=int(row["trades"]),
                median_hold_min=float(median_hold),
                lot_x_base=float(row["lot_x_base"]),
                pnl_pct=float(row["pnl_pct"]),
                dd_pct=float(row["dd_pct"]),
                window_start=(
                    window_start
                    if isinstance(window_start, datetime)
                    else _parse_ts(str(window_start), "window_start", sid)
                ),
                window_end=(
                    window_end
                    if isinstance(window_end, datetime)
                    else _parse_ts(str(window_end), "window_end", sid)
                ),
                turnover_24h=float(row["turnover_24h"]),
                target_share=float(row.get("target_share") or 0.115),
                target_share_source=(row.get("target_share_source") or "ESTIMATED")
                .strip()
                .upper(),
            )
        except (ValueError, TypeError) as exc:
            if isinstance(exc, LayerAError):
                raise
            raise LayerAError(f"{sid}: {exc}") from exc
        out.append(candidate)
    if not out:
        raise LayerAError("во входном CSV нет ни одного кандидата")
    return out


def _write_csv(rows: Sequence[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def run_layer_a(inputs: LayerAInputs) -> dict:
    """Полный прогон слоя A. Возвращает манифест."""
    derived: dict[str, object] = {}
    coverage: list[dict] = []
    trade_source: dict | None = None

    if inputs.trades_db is not None:
        stats = load_stats(inputs.trades_db, inputs.trades_table)
        derived = {s.strategy_id: s for s in stats}
        trade_source = {
            "path": str(inputs.trades_db),
            "table": inputs.trades_table,
            "strategies": len(stats),
            "trades": sum(s.trades for s in stats),
        }

    candidates = read_candidates(inputs.candidates_csv, derived)

    if inputs.trades_db is not None:
        coverage = coverage_report(
            list(derived.values()), [c.strategy_id for c in candidates]
        )

    screens = screen_all(candidates, inputs.limiters, inputs.max_size_factor)

    out = Path(inputs.output_dir)
    _write_csv(candidates_report(candidates), out / "02_Candidates.csv")
    _write_csv(screen_report(screens), out / "03_Layer_A_Screen.csv")
    if coverage:
        _write_csv(coverage, out / "00_Trade_Log_Coverage.csv")

    accepted = [s for s in screens if s.accepted]
    reasons: dict[str, int] = {}
    for s in screens:
        for reason in s.reject_reasons:
            reasons[reason] = reasons.get(reason, 0) + 1

    manifest = {
        "module": "portfolio-layer-a",
        "spec": "docs/specs/2026-08-09-portfolio-analyzer-v04.md",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": len(candidates),
        "limiters": list(inputs.limiters),
        "max_size_factor": inputs.max_size_factor,
        "combinations_screened": len(screens),
        "combinations_accepted": len(accepted),
        "reject_reasons": dict(sorted(reasons.items())),
        "estimated_target_share": sum(
            1 for c in candidates if c.target_share_source == "ESTIMATED"
        ),
        "trade_log_source": trade_source,
        "artifacts": sorted(p.name for p in out.glob("*.csv")),
        "not_implemented": [
            "set simulation",
            "lot multiplier search",
            "margin and liquidation",
            "pareto and recommendations",
        ],
        "blocked_on": [
            "trade timestamps for every candidate",
            "limiter contract (positions or orders; LONG/SHORT slots; hedge or one-way)",
            "L2 order book for measured target_share",
            "margin data (MMR/IMR tiers)",
        ],
    }
    (out / "portfolio_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


__all__ = [
    "LayerAInputs",
    "LayerAError",
    "TradeLogError",
    "read_candidates",
    "run_layer_a",
]
