from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
import math
from pathlib import Path
import re

import pandas as pd

from .config import AlgorithmConfig
from .loader import InputError, load_points, normalize_shift
from .models import InputAudit, Side
from .source_packs import (
    EVENT_MODES,
    LEGACY_TRADES_PROXY,
    REAL_INDEPENDENT_EVENTS,
    require_single_event_mode,
)


SUMMARY_METRICS = frozenset({"PnL", "DD", "TotalTrades", "WinRate", "ProfitFactor"})
ACTION_SUMMARY_METRICS = frozenset({"TotalTrades", "WinRate", "ProfitFactor"})
NOT_COMPARABLE_WINDOW_SCOPE = "NOT_COMPARABLE_WINDOW_SCOPE"
VERIFICATION_COLUMNS = [
    "report_id", "source_file", "source_sha256", "metric", "source_raw",
    "source_value", "calculated_value", "comparison", "cause",
]
SOURCE_AUDIT_COLUMNS = {
    "report_id", "source_file", "source_sha256", "raw_action_count",
    "reconstructed_cycles", "included_cycles", "source_total_trades",
    "source_win_rate", "source_profit_factor",
}


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


def _manifest(path: Path) -> tuple[dict[str, object], int, str, pd.Timestamp, pd.Timestamp]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageInputError(f"invalid package_manifest.json: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageInputError("package_manifest.json must contain an object")
    version = value.get("package_version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
        raise PackageInputError("package_version must be 1 or 2")
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
    return value, version, str(mode), start.tz_convert("UTC"), end.tz_convert("UTC")


def _required_file(directory: Path, name: str) -> Path:
    path = directory / name
    if not path.is_file():
        raise PackageInputError(f"package requires regular file {name}")
    return path


def _validate_real_events(
    points: pd.DataFrame, events_path: Path, package_point_ids: set[str]
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

    mapped_ids = set(events["point_id"])
    if not mapped_ids.issubset(package_point_ids):
        raise PackageInputError("point_events.csv contains an unknown point mapping")
    point_ids = set(points["point_id"].astype(str))
    events = events.loc[events["point_id"].isin(point_ids)]
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


def _real_points_for_side(
    raw_points: pd.DataFrame, side: Side, config: AlgorithmConfig
) -> pd.DataFrame:
    if "point_id" not in raw_points:
        raise PackageInputError("real package points.csv requires point_id")
    raw_ids = raw_points["point_id"]
    if (
        raw_ids.isna().any()
        or raw_ids.astype(str).str.strip().eq("").any()
        or raw_ids.duplicated().any()
    ):
        raise PackageInputError("real package point_id values must be non-empty and unique")
    parts = raw_ids.astype(str).str.split("|", expand=True)
    if (
        parts.shape[1] != 6
        or not parts[1].isin({"LONG", "SHORT"}).all()
        or parts.eq("").any().any()
    ):
        raise PackageInputError("real package point_id must be a six-part identity with LONG or SHORT side")
    for row, identity in zip(raw_points.to_dict("records"), parts.itertuples(index=False, name=None), strict=True):
        symbol, declared_side, timeframe, shift_text, open_text, close_text = (str(value) for value in identity)
        declared = Side(declared_side)
        columns = {**config.base_columns, **config.side_columns[declared]}
        try:
            expected = "|".join(
                (
                    str(row[columns["symbol"]]).strip(),
                    declared.value,
                    str(row[columns["timeframe"]]).strip(),
                    str(normalize_shift(declared, row[columns["multiplier"]], config.grid_tolerance_bp)),
                    str(int(row[columns["open_ma"]])),
                    str(int(row[columns["close_ma"]])),
                )
            )
        except (KeyError, TypeError, ValueError, InputError) as exc:
            raise PackageInputError("real package point_id cannot be validated against source fields") from exc
        if "|".join((symbol, declared_side, timeframe, shift_text, open_text, close_text)) != expected:
            raise PackageInputError("real package point_id is not canonical for its source fields")
    selected = raw_points.loc[parts[1].eq(side.value)].copy()
    if selected.empty:
        raise PackageInputError(f"real package has no points for requested side {side.value}")
    return selected


def _same_number(left: object, right: object) -> bool:
    try:
        left_number = float(left)
        right_number = float(right)
        return math.isfinite(left_number) and math.isfinite(right_number) and left_number == right_number
    except (TypeError, ValueError):
        return False


def _decimal_token(raw: object) -> tuple[Decimal, int]:
    """Parse metric evidence with the same locale-tolerant rules as the builder."""
    text = str(raw)
    match = re.search(r"[-+]?(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:[.,]\d+)?", text)
    if not match:
        raise PackageInputError("real v2 metric_verification.csv numeric evidence is invalid")
    token = match.group(0).replace(" ", "")
    if "," in token and "." in token:
        token = token.replace(",", "") if token.rfind(".") > token.rfind(",") else token.replace(".", "").replace(",", ".")
    elif "," in token:
        token = token.replace(",", ".")
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise PackageInputError("real v2 metric_verification.csv numeric evidence is invalid") from exc
    if not value.is_finite():
        raise PackageInputError("real v2 metric_verification.csv numeric evidence is invalid")
    return value, len(token.partition(".")[2])


def _validate_real_v2_numeric_evidence(verification: pd.DataFrame) -> None:
    for row in verification.itertuples(index=False):
        if row.metric not in SUMMARY_METRICS:
            continue
        source_raw, precision = _decimal_token(row.source_raw)
        source_value, _ = _decimal_token(row.source_value)
        calculated, _ = _decimal_token(row.calculated_value)
        if source_value != source_raw:
            raise PackageInputError("real v2 metric_verification.csv numeric evidence does not reconcile")
        if row.metric in ACTION_SUMMARY_METRICS:
            rounded = calculated.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
            if rounded != source_raw:
                raise PackageInputError("real v2 metric_verification.csv numeric evidence does not reconcile")


def _validate_real_v2_evidence(
    directory: Path,
    manifest: dict[str, object],
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
) -> None:
    database_hash = manifest.get("source_database_sha256")
    if not isinstance(database_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", database_hash):
        raise PackageInputError("real v2 source_database_sha256 must be a lowercase SHA-256 digest")
    source_summary_cause = manifest.get("source_summary_cause")
    if not isinstance(source_summary_cause, str) or source_summary_cause.strip():
        raise PackageInputError("real v2 source_summary_cause must be present and empty for VERIFIED")
    samples = manifest.get("source_summary_samples")
    if not isinstance(samples, list) or not 3 <= len(samples) <= 5:
        raise PackageInputError("real v2 source_summary_samples must contain 3 to 5 records")
    if manifest.get("source_summary_sample_count") != len(samples):
        raise PackageInputError("real v2 source_summary_sample_count must match source_summary_samples")

    sample_by_report: dict[str, dict[str, str]] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            raise PackageInputError("real v2 source_summary_samples entries must be objects")
        try:
            report_id = str(sample["report_id"])
            source_file = str(sample["source_file"])
            source_sha256 = str(sample["source_sha256"])
            range_start = pd.Timestamp(sample["source_range_start"])
            range_end = pd.Timestamp(sample["source_range_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PackageInputError("real v2 source_summary_samples evidence is invalid") from exc
        if (
            not report_id
            or not source_file
            or Path(source_file).name != source_file
            or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
            or range_start.tzinfo is None
            or range_end.tzinfo is None
            or range_start.tz_convert("UTC") > window_start
            or range_end.tz_convert("UTC") < window_end
            or report_id in sample_by_report
        ):
            raise PackageInputError("real v2 source_summary_samples identity or range evidence is invalid")
        sample_by_report[report_id] = {
            "source_file": source_file,
            "source_sha256": source_sha256,
        }

    verification = pd.read_csv(
        _required_file(directory, "metric_verification.csv"), dtype=str, keep_default_na=False
    )
    if list(verification.columns) != VERIFICATION_COLUMNS:
        raise PackageInputError("real v2 metric_verification.csv schema is invalid")
    if len(verification) != len(sample_by_report) * len(SUMMARY_METRICS):
        raise PackageInputError("real v2 metric_verification.csv must contain five rows per sample")
    if verification.isna().any().any():
        raise PackageInputError("real v2 metric_verification.csv must not contain empty evidence")
    evidence_values = [
        "report_id", "source_file", "source_sha256", "metric", "source_raw",
        "source_value", "calculated_value", "comparison",
    ]
    if any(verification[column].str.strip().eq("").any() for column in evidence_values):
        raise PackageInputError("real v2 metric_verification.csv must not contain empty evidence")
    action_rows = verification["metric"].isin(ACTION_SUMMARY_METRICS)
    non_comparable_rows = verification["metric"].isin({"PnL", "DD"})
    if (
        not verification.loc[action_rows, "cause"].str.strip().eq("").all()
        or not verification.loc[action_rows, "comparison"].eq("EQUAL").all()
        or not verification.loc[non_comparable_rows, "comparison"].eq(NOT_COMPARABLE_WINDOW_SCOPE).all()
        or not verification.loc[non_comparable_rows, "cause"].eq(NOT_COMPARABLE_WINDOW_SCOPE).all()
    ):
        raise PackageInputError(
            "real v2 metric_verification.csv must mark PnL/DD as NOT_COMPARABLE_WINDOW_SCOPE "
            "and contain only EQUAL action-metric rows without causes"
        )
    _validate_real_v2_numeric_evidence(verification)

    for report_id, identity in sample_by_report.items():
        rows = verification.loc[verification["report_id"].eq(report_id)]
        if (
            len(rows) != len(SUMMARY_METRICS)
            or set(rows["metric"]) != SUMMARY_METRICS
            or not rows["source_file"].eq(identity["source_file"]).all()
            or not rows["source_sha256"].eq(identity["source_sha256"]).all()
        ):
            raise PackageInputError("real v2 metric_verification.csv does not match source_summary_samples")
    if not set(verification["report_id"]).issubset(sample_by_report):
        raise PackageInputError("real v2 metric_verification.csv contains an unknown sample")

    audit = pd.read_csv(_required_file(directory, "source_audit.csv"), dtype=str)
    missing_audit_columns = sorted(SOURCE_AUDIT_COLUMNS.difference(audit.columns))
    if missing_audit_columns:
        raise PackageInputError(f"real v2 source_audit.csv is missing columns: {missing_audit_columns}")
    for report_id, identity in sample_by_report.items():
        rows = audit.loc[audit["report_id"].eq(report_id)]
        if len(rows) != 1 or rows.iloc[0]["source_file"] != identity["source_file"] or rows.iloc[0]["source_sha256"] != identity["source_sha256"]:
            raise PackageInputError("real v2 source_audit.csv does not match source_summary_samples")
        row = rows.iloc[0]
        try:
            raw_actions = int(row["raw_action_count"])
            reconstructed = int(row["reconstructed_cycles"])
            included = int(row["included_cycles"])
            trades = float(row["source_total_trades"])
        except (TypeError, ValueError) as exc:
            raise PackageInputError("real v2 source_audit.csv action reconciliation is invalid") from exc
        if (
            not math.isfinite(trades)
            or not trades.is_integer()
            or raw_actions < 0
            or reconstructed < 0
            or included < 0
            or trades < 0
            or included > reconstructed
            or reconstructed > raw_actions // 2
            or trades < reconstructed
            or trades > raw_actions
        ):
            raise PackageInputError("real v2 source_audit.csv action reconciliation is invalid")
        calculated = verification.loc[verification["report_id"].eq(report_id)].set_index("metric")["calculated_value"]
        if not (
            _same_number(row["source_total_trades"], calculated["TotalTrades"])
            and _same_number(row["source_win_rate"], calculated["WinRate"])
            and _same_number(row["source_profit_factor"], calculated["ProfitFactor"])
        ):
            raise PackageInputError("real v2 source_audit.csv action metrics do not reconcile")


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
    manifest, package_version, mode, window_start, window_end = _manifest(manifest_path)

    raw_points = pd.read_csv(points_csv)
    if raw_points.empty:
        raise PackageInputError("points.csv must contain at least one point")
    if require_single_event_mode(raw_points) != mode:
        raise PackageInputError("manifest event_mode does not match points.csv")
    count_key = "point_count" if mode == REAL_INDEPENDENT_EVENTS else "accepted_rows"
    if manifest.get(count_key) != len(raw_points):
        raise PackageInputError(f"manifest {count_key} does not match points.csv")

    selected_raw_points = raw_points
    if mode == REAL_INDEPENDENT_EVENTS:
        selected_raw_points = _real_points_for_side(raw_points, side, config)
    points, input_audit = load_points(selected_raw_points, dates_path, side, config)
    starts = pd.to_datetime(points["report_start"], utc=True)
    ends = pd.to_datetime(points["report_end"], utc=True)
    if not starts.eq(window_start).all() or not ends.eq(window_end).all():
        raise PackageInputError("points.csv rows must match the declared package window")

    events_path = directory / "point_events.csv"
    points = points.copy()
    if mode == LEGACY_TRADES_PROXY:
        if package_version != 1:
            raise PackageInputError("legacy package_version must be 1")
        if events_path.exists():
            raise PackageInputError("legacy package must not contain point_events.csv")
        points["_event_ids"] = [()] * len(points)
    else:
        if package_version != 2:
            raise PackageInputError("real package_version v1 is audit-only; v2 is required")
        if manifest.get("source_summary_status") != "VERIFIED":
            raise PackageInputError("real v2 source_summary_status must be VERIFIED")
        if manifest.get("window_metrics_status") != "DERIVED_FROM_VERIFIED_SOURCE":
            raise PackageInputError(
                "real v2 window_metrics_status must be DERIVED_FROM_VERIFIED_SOURCE"
            )
        if "window_metrics_status" not in raw_points or not raw_points[
            "window_metrics_status"
        ].eq("DERIVED_FROM_VERIFIED_SOURCE").all():
            raise PackageInputError(
                "every real package point must have window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE"
            )
        _validate_real_v2_evidence(directory, manifest, window_start, window_end)
        raw_ids = selected_raw_points["point_id"]
        if set(raw_ids.astype(str)) != set(points["point_id"]):
            raise PackageInputError("points.csv point_id values do not match normalized points")
        events_by_point = _validate_real_events(
            points,
            _required_file(directory, "point_events.csv"),
            set(raw_points["point_id"].astype(str)),
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
