from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd

from .config import AlgorithmConfig
from .loader import load_points
from .models import InputAudit, Side
from .source_packs import (
    EVENT_MODES,
    LEGACY_TRADES_PROXY,
    REAL_INDEPENDENT_EVENTS,
    require_single_event_mode,
)


class PackageInputError(ValueError):
    """Raised when a source package is unsafe to select."""


@dataclass(frozen=True, slots=True)
class PackageInput:
    directory: Path
    points_csv: Path
    source_audit_csv: Path
    manifest_path: Path
    manifest: dict[str, object]
    manifest_sha256: str
    event_mode: str
    points: pd.DataFrame
    input_audit: InputAudit


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(path: Path) -> tuple[dict[str, object], str, pd.Timestamp, pd.Timestamp]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageInputError(f"invalid package_manifest.json: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageInputError("package_manifest.json must contain an object")
    if value.get("package_version") != 1:
        raise PackageInputError("package_version must be 1")
    mode = value.get("event_mode")
    if mode not in EVENT_MODES:
        raise PackageInputError(f"manifest event_mode is unknown: {mode!r}")
    try:
        start = pd.Timestamp(value["window_start"])
        end = pd.Timestamp(value["window_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PackageInputError("manifest must declare a valid window") from exc
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise PackageInputError("manifest window must be UTC-aware with end after start")
    return value, str(mode), start.tz_convert("UTC"), end.tz_convert("UTC")


def _required_file(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.is_file():
        raise PackageInputError(f"package requires regular file {name}")
    return path


def _validate_real_events(
    points: pd.DataFrame, events_path: Path
) -> dict[str, tuple[str, ...]]:
    events = pd.read_csv(events_path, dtype=str)
    if list(events.columns) != ["point_id", "event_id"]:
        raise PackageInputError("point_events.csv columns must be (point_id,event_id)")
    empty = events.apply(lambda column: column.str.strip().eq(""))
    if events.isna().any().any() or empty.any().any():
        raise PackageInputError("point_events.csv entries must be non-empty")
    records = list(events.itertuples(index=False, name=None))
    if records != sorted(records):
        raise PackageInputError("point_events.csv must be sorted by point_id,event_id")
    if events.duplicated(["point_id", "event_id"]).any():
        raise PackageInputError("point_events.csv entries must be unique")

    point_ids = set(points["point_id"].astype(str))
    mapped_ids = set(events["point_id"])
    if not mapped_ids.issubset(point_ids):
        raise PackageInputError("point_events.csv contains an unknown point mapping")
    grouped = {
        str(point_id): tuple(group["event_id"])
        for point_id, group in events.groupby("point_id", sort=False)
    }
    result: dict[str, tuple[str, ...]] = {}
    for point in points.itertuples(index=False):
        point_id = str(point.point_id)
        event_ids = grouped.get(point_id, ())
        if len(event_ids) != int(point.point_event_count):
            raise PackageInputError(f"point event count mismatch: {point_id}")
        expected_hash = sha256("|".join(event_ids).encode("utf-8")).hexdigest()
        if expected_hash != str(point.event_ids_hash):
            raise PackageInputError(f"point event hash mismatch: {point_id}")
        result[point_id] = event_ids
    return result


def load_package(
    package_dir: Path,
    dates_path: Path,
    side: Side,
    config: AlgorithmConfig,
) -> PackageInput:
    directory = package_dir.resolve()
    if not directory.is_dir():
        raise PackageInputError(f"source package is not a directory: {directory}")
    manifest_path = _required_file(directory, "package_manifest.json")
    points_csv = _required_file(directory, "points.csv")
    source_audit_csv = _required_file(directory, "source_audit.csv")
    manifest, mode, window_start, window_end = _manifest(manifest_path)

    raw_points = pd.read_csv(points_csv)
    if raw_points.empty:
        raise PackageInputError("points.csv must contain at least one point")
    if require_single_event_mode(raw_points) != mode:
        raise PackageInputError("manifest event_mode does not match points.csv")
    count_key = "point_count" if mode == REAL_INDEPENDENT_EVENTS else "accepted_rows"
    if manifest.get(count_key) != len(raw_points):
        raise PackageInputError(f"manifest {count_key} does not match points.csv")

    points, input_audit = load_points(points_csv, dates_path, side, config)
    starts = pd.to_datetime(points["report_start"], utc=True)
    ends = pd.to_datetime(points["report_end"], utc=True)
    if not starts.eq(window_start).all() or not ends.eq(window_end).all():
        raise PackageInputError("points.csv rows must match the declared package window")

    events_path = directory / "point_events.csv"
    points = points.copy()
    if mode == LEGACY_TRADES_PROXY:
        if events_path.exists():
            raise PackageInputError("legacy package must not contain point_events.csv")
        points["_event_ids"] = [()] * len(points)
    else:
        if "metric_status" not in raw_points or not raw_points["metric_status"].eq(
            "VERIFIED"
        ).all():
            raise PackageInputError("every real package point must have metric_status=VERIFIED")
        if "point_id" not in raw_points:
            raise PackageInputError("real package points.csv requires point_id")
        raw_ids = raw_points["point_id"]
        if (
            raw_ids.isna().any()
            or raw_ids.astype(str).str.strip().eq("").any()
            or raw_ids.duplicated().any()
        ):
            raise PackageInputError("real package point_id values must be non-empty and unique")
        if set(raw_ids.astype(str)) != set(points["point_id"]):
            raise PackageInputError("points.csv point_id values do not match normalized points")
        events_by_point = _validate_real_events(
            points, _required_file(directory, "point_events.csv")
        )
        points["_event_ids"] = points["point_id"].map(events_by_point)

    return PackageInput(
        directory=directory,
        points_csv=points_csv,
        source_audit_csv=source_audit_csv,
        manifest_path=manifest_path,
        manifest=manifest,
        manifest_sha256=_file_hash(manifest_path),
        event_mode=mode,
        points=points,
        input_audit=input_audit,
    )
