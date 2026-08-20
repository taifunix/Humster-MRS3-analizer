from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def minimal_template() -> dict[str, object]:
    entry = {
        "id": 1,
        "side": "buy",
        "type": "SMA",
        "source": "ohlc4",
        "len": 3,
        "multiplier": 0.997,
        "lot_x": 1.0,
        "order_type": "limit",
        "post_only": True,
        "hidden": False,
        "value": None,
    }
    return {
        "name": "TEMPLATE",
        "is_runing": False,
        "basic": {
            "strategy": "mrs3",
            "symbol": "OLD",
            "time_frame": "1h",
            "use_long": True,
            "use_short": False,
        },
        "mrs3": {
            "ma_long": [entry],
            "ma_short": [{**entry, "side": "sell", "multiplier": 1.003}],
            "ma_close_long": {"len": 4, "multiplier": 1.003},
            "ma_close_short": {"len": 4, "multiplier": 0.997},
        },
    }


def write_selection_inputs(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    run_id = 0
    for shift_bp in (190, 230, 270):
        for open_ma in (2, 3):
            for close_ma in (3, 4):
                run_id += 1
                economic = shift_bp != 230
                pnl = 30.0 if shift_bp == 190 else (25.0 if shift_bp == 270 else -5.0)
                rows.append(
                    {
                        "StartDate": "2026-07-15 00:00:00",
                        "EndDate": "2026-08-06 00:00:00",
                        "TotalPnLPercent": pnl,
                        "TotalTrades": 30,
                        "Win": 27 if economic else 10,
                        "Los": 3 if economic else 20,
                        "WinRate": 90.0 if economic else 33.33,
                        "MaxDrawdownPercent": 5.0,
                        "ProfitFactor": 3.0 if economic else 0.5,
                        "Run id": run_id,
                        "settings[*].basic.symbol": "AAAUSDT",
                        "settings[*].basic.time_frame": "2h",
                        "settings[*].mrs2.ma_close_long.len": close_ma,
                        "settings[*].mrs2.ma_long.len": open_ma,
                        "settings[*].mrs2.ma_long.multiplier": float(
                            1 - shift_bp / 10000
                        ),
                    }
                )
    csv_path = root / "input.csv"
    dates_path = root / "dates.xlsx"
    template_path = root / "template.json"
    config_path = root / "config.json"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    pd.DataFrame([["AAAUSDT", "2026-07-01"]]).to_excel(
        dates_path, index=False, header=False
    )
    template_path.write_text(
        json.dumps(minimal_template(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    config_path.write_text(
        json.dumps(
            {
                "shift_domain": {"min_bp": 190, "max_bp": 270},
                "canonical_shifts_bp": [190, 230, 270],
                "gap_rules": [
                    {"lower_min_bp": 190, "lower_max_exclusive_bp": 230, "min_gap_bp": 30},
                    {"lower_min_bp": 230, "lower_max_exclusive_bp": 271, "min_gap_bp": 40},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "csv": csv_path,
        "dates": dates_path,
        "template": template_path,
        "config": config_path,
    }

