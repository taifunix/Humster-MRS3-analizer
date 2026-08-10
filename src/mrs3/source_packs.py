from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import shutil
import tempfile
from typing import Iterable

import pandas as pd


LEGACY_TRADES_PROXY = "legacy_trades_proxy"
REAL_INDEPENDENT_EVENTS = "real_independent_events"
EVENT_MODES = frozenset({LEGACY_TRADES_PROXY, REAL_INDEPENDENT_EVENTS})
LEGACY_EVENT_IDS_HASH = "LEGACY_PROXY_NO_EVENT_IDS"


class SourcePackError(ValueError):
    """Raised when source rows cannot form one auditable event package."""


@dataclass(frozen=True, slots=True)
class SourcePackage:
    directory: Path
    points_csv: Path
    audit_csv: Path
    manifest_path: Path
    manifest: dict[str, object]


def _utc_timestamp(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_single_event_mode(points: pd.DataFrame) -> str:
    if "event_mode" not in points:
        raise SourcePackError("event_mode is required")
    values = points["event_mode"]
    if values.isna().any() or values.astype(str).str.strip().eq("").any():
        raise SourcePackError("missing event mode")
    modes = sorted(set(values.astype(str)))
    if len(modes) != 1:
        raise SourcePackError(f"mixed event modes: {modes}")
    mode = modes[0]
    if mode not in EVENT_MODES:
        raise SourcePackError(f"unknown event mode: {mode}")
    return mode


def _source_rows(paths: Iterable[Path]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in paths:
        if not path.is_file():
            raise SourcePackError(f"CSV source is not a file: {path}")
        frame = pd.read_csv(path).copy()
        frame["source_file"] = path.name
        frame["source_sha256"] = _sha256(path)
        frames.append(frame)
    if not frames:
        raise SourcePackError("at least one CSV source is required")
    return pd.concat(frames, ignore_index=True, sort=False)


def build_csv_package(
    csv_paths: Iterable[Path],
    window_start: str,
    window_end: str,
    output_dir: Path,
) -> SourcePackage:
    """Build an exact-window legacy-trades package without changing its sources."""
    start = _utc_timestamp(window_start)
    end = _utc_timestamp(window_end)
    if end <= start:
        raise SourcePackError("window end must be later than start")
    paths = tuple(csv_paths)
    raw = _source_rows(paths)
    required = {"StartDate", "EndDate", "TotalTrades"}
    missing = sorted(required.difference(raw.columns))
    if missing:
        raise SourcePackError(f"CSV sources are missing columns: {missing}")
    starts = pd.to_datetime(raw["StartDate"], utc=True, errors="raise")
    ends = pd.to_datetime(raw["EndDate"], utc=True, errors="raise")
    exact = starts.eq(start) & ends.eq(end)
    accepted = raw.loc[exact].copy()
    accepted["event_mode"] = LEGACY_TRADES_PROXY
    trades = pd.to_numeric(accepted["TotalTrades"], errors="raise")
    if not trades.map(lambda value: math.isfinite(float(value)) and float(value).is_integer() and value >= 0).all():
        raise SourcePackError("TotalTrades must be finite non-negative integers")
    accepted["point_event_count"] = trades.astype("int64")
    accepted["event_ids_hash"] = LEGACY_EVENT_IDS_HASH
    audit = raw[["source_file", "source_sha256", "StartDate", "EndDate"]].copy()
    audit["status"] = exact.map({True: "ACCEPTED", False: "REJECTED"})
    audit["reason"] = exact.map({True: "", False: "PERIOD_NOT_EXACT"})
    target = output_dir.resolve()
    if target.exists() and any(target.iterdir()):
        raise SourcePackError(f"package output directory is not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    points_csv = target / "points.csv"
    audit_csv = target / "source_audit.csv"
    manifest: dict[str, object] = {
        "package_version": 1,
        "event_mode": LEGACY_TRADES_PROXY,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "source_files": [
            {"name": path.name, "sha256": _sha256(path)} for path in paths
        ],
        "source_rows": int(len(raw)),
        "accepted_rows": int(len(accepted)),
        "rejected_rows": int((~exact).sum()),
    }
    manifest_path = target / "package_manifest.json"
    try:
        accepted.to_csv(staging / points_csv.name, index=False, lineterminator="\n")
        audit.to_csv(staging / audit_csv.name, index=False, lineterminator="\n")
        (staging / manifest_path.name).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target.exists():
            target.rmdir()
        staging.replace(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return SourcePackage(target, points_csv, audit_csv, manifest_path, manifest)
