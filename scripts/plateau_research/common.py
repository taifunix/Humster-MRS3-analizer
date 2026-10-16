"""Shared helpers for the plateau walk-forward study (read-only research).

Nothing here changes analyzer code or databases: Source v6 databases are opened
read-only through the project's own storage functions.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
WORK = Path(os.environ["PLATEAU_WORK"])
SOURCE_ROOT = Path(os.environ["PLATEAU_SOURCE_ROOT"])
FINGERPRINT = "source-v6-fresh-compact-v2"
DAY_MS = 24 * 60 * 60 * 1000

TRAIN_MIN_DAYS = 42
FORWARD_DAYS = 28
FORWARD_DAYS_SENS = 14
STEP_DAYS = 14
ROLLING_TRAIN_DAYS = 56
SHIFT_BUCKETS = ((30, 70, "30-70"), (90, 200, "90-200"), (230, 390, "230-390"), (430, 550, "430-550"))


def shift_bucket(shift_bp: int) -> str:
    for low, high, name in SHIFT_BUCKETS:
        if low <= shift_bp <= high:
            return name
    raise ValueError(shift_bp)


def ms(day: str | datetime) -> int:
    if isinstance(day, str):
        day = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int(day.timestamp() * 1000)


def ms_to_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%d")


def symbol_half(symbol: str) -> str:
    """Fixed discovery/confirmation split, decided before any computation."""
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return "A" if int(digest[:8], 16) % 2 == 0 else "B"


def load_config():
    from mrs3.config import AlgorithmConfig

    return AlgorithmConfig.from_json(REPO / "config.local.json")


def config_dict(config) -> dict:
    def clean(value):
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if isinstance(value, (int, float, str, bool)) or value is None:
            return value
        return str(value)

    return clean(asdict(config))


def listing_dates() -> dict:
    from mrs3.loader import load_listing_dates

    raw = json.loads((REPO / "config.local.json").read_text(encoding="utf-8"))
    path = Path(raw["panel_workflow"]["listing_dates_path"])
    return load_listing_dates(path), path


def write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """pyarrow is not installed in .venv; DuckDB writes the parquet instead."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    con = duckdb.connect()
    try:
        con.register("frame", frame)
        con.execute(f"copy frame to '{tmp.as_posix()}' (format parquet, compression zstd)")
    finally:
        con.close()
    os.replace(tmp, path)


def read_parquet(path: Path | str) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        return con.execute(f"select * from read_parquet('{Path(path).as_posix()}')").df()
    finally:
        con.close()
