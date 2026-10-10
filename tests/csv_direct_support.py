"""Synthetic tester exports for CSV Direct tests (no real tester reports in git).

The column set mirrors the tester's ``reports_history.csv`` header; the
snapshot mirrors the parts of ``run_snapshot_*.json`` that CSV Direct reads.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import zipfile

COLUMNS = (
    "ReportUtc", "NameComment", "StartDate", "EndDate", "PairsCount", "InitialBalance",
    "FinalBalance", "TotalPnL", "TotalPnLPercent", "TotalTrades", "Win", "Los", "WinRate",
    "Fills", "MaxDrawdown", "MaxDrawdownPercent", "TotalFees", "PositionAvgPercent",
    "PositionMaxPercent", "ProfitFactor", "Run id", "RunTag",
    "settings[*].basic.symbol", "settings[*].basic.time_frame",
    "settings[*].mrs2.ma_close_long.len", "settings[*].mrs2.ma_long.len",
    "settings[*].mrs2.ma_long.multiplier",
)
WINDOW = ("2026-07-14 00:00:00", "2026-09-12 00:00:00")  # 60 days


def snapshot(symbol: str = "XYZUSDT", *, use_fix: bool = True, use_upnl: bool = False) -> dict[str, object]:
    ma = {"type": "SMA", "source": "ohlc4"}
    return {"settings": [{
        "basic": {"symbol": symbol, "use_fix": use_fix, "my_fix_balance": 1000.0},
        "exchange": {"use_upnl": use_upnl},
        "mrs2": {"ma_long": dict(ma), "ma_close_long": dict(ma), "ma_short": dict(ma), "ma_close_short": dict(ma)},
    }]}


def report_row(
    *, symbol: str = "XYZUSDT", timeframe: str = "30m", shift_bp: int = 30, open_ma: int = 2,
    close_ma: int = 3, pnl: str = "500.00", dd_pct: str = "5.00", trades: int = 240,
    wins: int = 220, losses: int = 20, fills: int | None = None, profit_factor: str = "4.00",
    window: tuple[str, str] = WINDOW, initial: str = "1000.00", run: int = 1,
) -> dict[str, str]:
    initial_value = Decimal(initial)
    pnl_value = Decimal(pnl)
    classified = wins + losses
    win_rate = Decimal(100) * wins / classified if classified else Decimal(0)
    return {
        "ReportUtc": "2026-10-10 12:00:00", "NameComment": "synthetic",
        "StartDate": window[0], "EndDate": window[1], "PairsCount": "1",
        "InitialBalance": initial, "FinalBalance": str(initial_value + pnl_value),
        "TotalPnL": pnl, "TotalPnLPercent": str((pnl_value / initial_value * 100).quantize(Decimal("0.01"))),
        "TotalTrades": str(trades), "Win": str(wins), "Los": str(losses),
        "WinRate": str(win_rate.quantize(Decimal("0.01"))),
        "Fills": str(trades if fills is None else fills), "MaxDrawdown": "50.00",
        "MaxDrawdownPercent": dd_pct, "TotalFees": "1.00", "PositionAvgPercent": "10.00",
        "PositionMaxPercent": "20.00", "ProfitFactor": profit_factor, "Run id": str(run),
        "RunTag": f"run_{run:03d}", "settings[*].basic.symbol": symbol,
        "settings[*].basic.time_frame": timeframe, "settings[*].mrs2.ma_close_long.len": str(close_ma),
        "settings[*].mrs2.ma_long.len": str(open_ma),
        "settings[*].mrs2.ma_long.multiplier": str((Decimal(10000) - shift_bp) / Decimal(10000)),
    }


def canonical_grid(
    symbol: str = "XYZUSDT", timeframe: str = "30m", *, trades: int = 240, window: tuple[str, str] = WINDOW,
) -> list[dict[str, str]]:
    """4 shifts x 6 open x 6 close: one broad profitable plateau (READY BASE)."""
    rows = []
    for shift_bp in (30, 40, 50, 60):
        for open_ma in range(2, 8):
            for close_ma in range(2, 8):
                pnl = Decimal(500) - Decimal("0.10") * (shift_bp - 30)
                rows.append(report_row(
                    symbol=symbol, timeframe=timeframe, shift_bp=shift_bp, open_ma=open_ma,
                    close_ma=close_ma, pnl=str(pnl.quantize(Decimal("0.01"))), trades=trades,
                    wins=trades * 11 // 12, losses=trades - trades * 11 // 12, window=window, run=len(rows) + 1,
                ))
    return rows


def csv_text(rows: list[dict[str, str]]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]) if rows else list(COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def write_tester_zip(path: Path, rows: list[dict[str, str]], snap: dict[str, object] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("run/reports_history.csv", csv_text(rows))
        archive.writestr("run/run_snapshot_run.json", json.dumps(snapshot() if snap is None else snap))
        # A tester bundle may still contain heavy HTML; CSV Direct must never read it.
        archive.writestr("run/run_001_report.html", "<html>NOT READ</html>")
    return path
