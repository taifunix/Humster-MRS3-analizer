from __future__ import annotations

from hashlib import sha256
import importlib
import json
from pathlib import Path

import pandas as pd
import pytest

from mrs3.config import AlgorithmConfig
from mrs3.models import Side
from mrs3.source_packs import build_csv_package
from tests.factories import write_selection_inputs


WINDOW_START = "2026-07-15T00:00:00Z"
WINDOW_END = "2026-08-06T00:00:00Z"


def write_real_package(
    directory: Path, paths: dict[str, Path]
) -> tuple[Path, dict[str, tuple[str, ...]]]:
    points = pd.read_csv(paths["csv"])
    events_by_point: dict[str, tuple[str, ...]] = {}
    hashes: list[str] = []
    point_ids: list[str] = []
    for row in points.to_dict("records"):
        shift_bp = round((1 - float(row["settings[*].mrs2.ma_long.multiplier"])) * 10000)
        point_id = "|".join(
            (
                str(row["settings[*].basic.symbol"]),
                "LONG",
                str(row["settings[*].basic.time_frame"]),
                str(shift_bp),
                str(row["settings[*].mrs2.ma_long.len"]),
                str(row["settings[*].mrs2.ma_close_long.len"]),
            )
        )
        event_ids = tuple(
            sorted(
                (
                    f"shift-{shift_bp}",
                    f"open-{row['settings[*].mrs2.ma_long.len']}",
                    f"close-{row['settings[*].mrs2.ma_close_long.len']}",
                )
            )
        )
        point_ids.append(point_id)
        events_by_point[point_id] = event_ids
        hashes.append(sha256("|".join(event_ids).encode("utf-8")).hexdigest())

    points["point_id"] = point_ids
    points["event_mode"] = "real_independent_events"
    points["point_event_count"] = 3
    points["event_ids_hash"] = hashes
    points["window_metrics_status"] = "DERIVED_FROM_VERIFIED_SOURCE"
    directory.mkdir(parents=True)
    points.to_csv(directory / "points.csv", index=False, lineterminator="\n")
    event_rows = [
        {"point_id": point_id, "event_id": event_id}
        for point_id, event_ids in events_by_point.items()
        for event_id in event_ids
    ]
    pd.DataFrame(event_rows).sort_values(["point_id", "event_id"], kind="mergesort").to_csv(
        directory / "point_events.csv", index=False, lineterminator="\n"
    )
    source_summary_samples = []
    verification_rows = []
    audit_rows = []
    metrics = {
        "PnL": "1", "DD": "1", "TotalTrades": "3", "WinRate": "100", "ProfitFactor": "3",
    }
    for index in range(1, 4):
        report_id = f"R{index}"
        source_file = f"report-{index}.html"
        source_sha256 = sha256(report_id.encode("utf-8")).hexdigest()
        source_summary_samples.append(
            {
                "report_id": report_id,
                "source_file": source_file,
                "source_sha256": source_sha256,
                "source_range_start": "2026-07-15T00:00:00+00:00",
                "source_range_end": "2026-08-06T00:00:00+00:00",
            }
        )
        verification_rows.extend(
            {
                "report_id": report_id,
                "source_file": source_file,
                "source_sha256": source_sha256,
                "metric": metric,
                "source_raw": value,
                "source_value": value,
                "calculated_value": value,
                "comparison": "NOT_COMPARABLE_WINDOW_SCOPE" if metric in {"PnL", "DD"} else "EQUAL",
                "cause": "NOT_COMPARABLE_WINDOW_SCOPE" if metric in {"PnL", "DD"} else "",
            }
            for metric, value in metrics.items()
        )
        audit_rows.append(
            {
                "report_id": report_id,
                "source_file": source_file,
                "source_sha256": source_sha256,
                "raw_action_count": 6,
                "reconstructed_cycles": 3,
                "included_cycles": 3,
                "source_total_trades": 3,
                "source_win_rate": 100,
                "source_profit_factor": 3,
            }
        )
    pd.DataFrame(audit_rows).to_csv(directory / "source_audit.csv", index=False, lineterminator="\n")
    pd.DataFrame(verification_rows).to_csv(
        directory / "metric_verification.csv", index=False, lineterminator="\n"
    )
    (directory / "package_manifest.json").write_text(
        json.dumps(
            {
                "package_version": 2,
                "event_mode": "real_independent_events",
                "window_start": "2026-07-15T00:00:00+00:00",
                "window_end": "2026-08-06T00:00:00+00:00",
                "source_database_sha256": sha256(b"source database").hexdigest(),
                "point_count": len(points),
                "source_summary_status": "VERIFIED",
                "source_summary_cause": "",
                "source_summary_sample_count": 3,
                "source_summary_samples": source_summary_samples,
                "window_metrics_status": "DERIVED_FROM_VERIFIED_SOURCE",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return directory, events_by_point


def _load_package(*args):
    return importlib.import_module("mrs3.package_loader").load_package(*args)


def test_load_package_accepts_valid_legacy_package(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package = build_csv_package(
        [paths["csv"]], WINDOW_START, WINDOW_END, tmp_path / "package"
    )

    loaded = _load_package(
        package.directory,
        paths["dates"],
        Side.LONG,
        AlgorithmConfig.from_json(paths["config"]),
    )

    assert loaded.event_mode == "legacy_trades_proxy"
    assert loaded.manifest == package.manifest
    assert loaded.manifest_sha256 == sha256(
        package.manifest_path.read_bytes()
    ).hexdigest()
    assert loaded.points["_event_ids"].tolist() == [()] * len(loaded.points)


def test_load_package_accepts_verified_real_mapping(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, events_by_point = write_real_package(tmp_path / "package", paths)

    loaded = _load_package(
        package,
        paths["dates"],
        Side.LONG,
        AlgorithmConfig.from_json(paths["config"]),
    )

    assert loaded.event_mode == "real_independent_events"
    assert dict(
        zip(loaded.points["point_id"], loaded.points["_event_ids"], strict=True)
    ) == events_by_point


def test_load_package_accepts_different_pnl_dd_values_marked_not_comparable(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    verification_path = package / "metric_verification.csv"
    verification = pd.read_csv(verification_path, dtype=str, keep_default_na=False)
    verification.loc[verification["metric"].eq("PnL"), "calculated_value"] = "999"
    verification.loc[verification["metric"].eq("DD"), "calculated_value"] = "888"
    verification.to_csv(verification_path, index=False)

    loaded = _load_package(
        package,
        paths["dates"],
        Side.LONG,
        AlgorithmConfig.from_json(paths["config"]),
    )

    assert loaded.event_mode == "real_independent_events"


def test_load_package_rejects_fabricated_v2_without_source_summary_evidence(
    tmp_path: Path,
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    manifest_path = package / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("source_summary_samples")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source_summary_samples"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("package_version", 1, "package_version"),
        ("package_version", True, "package_version"),
        ("event_mode", "unknown", "event_mode"),
        ("window_end", "2026-07-15T00:00:00+00:00", "window"),
    ],
)
def test_load_package_rejects_invalid_manifest(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    manifest_path = package / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("sample_count", "source_summary_samples"),
        ("summary_cause_missing", "source_summary_cause"),
        ("summary_cause_present", "source_summary_cause"),
        ("identity", "identity or range"),
        ("range", "identity or range"),
        ("verification_schema", "metric_verification.csv schema"),
        ("verification_result", "only EQUAL"),
        ("pnl_false_equal", "NOT_COMPARABLE_WINDOW_SCOPE"),
        ("verification_rows", "five rows per sample"),
        ("impossible_cycles", "action reconciliation"),
        ("trade_undercount", "numeric evidence|action reconciliation"),
        ("audit_reconciliation", "action metrics do not reconcile"),
    ],
)
def test_load_package_rejects_invalid_real_v2_evidence_chain(
    tmp_path: Path, fault: str, message: str
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    manifest_path = package / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verification_path = package / "metric_verification.csv"
    verification = pd.read_csv(verification_path, keep_default_na=False)
    audit_path = package / "source_audit.csv"
    audit = pd.read_csv(audit_path, keep_default_na=False)
    if fault == "sample_count":
        manifest["source_summary_samples"] = manifest["source_summary_samples"][:2]
    elif fault == "summary_cause_missing":
        manifest.pop("source_summary_cause", None)
    elif fault == "summary_cause_present":
        manifest["source_summary_cause"] = "VALUE_MISMATCH"
    elif fault == "identity":
        manifest["source_summary_samples"][0]["source_sha256"] = "not-a-hash"
    elif fault == "range":
        manifest["source_summary_samples"][0]["source_range_start"] = "2026-07-15T00:00:01+00:00"
    elif fault == "verification_schema":
        verification = verification.drop(columns=["cause"])
    elif fault == "verification_result":
        verification.loc[(verification["report_id"] == "R1") & (verification["metric"] == "TotalTrades"), "comparison"] = "MISMATCH"
    elif fault == "pnl_false_equal":
        rows = (verification["report_id"] == "R1") & (verification["metric"] == "PnL")
        verification.loc[rows, ["comparison", "cause"]] = ["EQUAL", ""]
    elif fault == "verification_rows":
        verification = verification.iloc[1:]
    elif fault == "impossible_cycles":
        audit.loc[0, ["raw_action_count", "reconstructed_cycles", "included_cycles"]] = [3, 3, 3]
    elif fault == "trade_undercount":
        audit.loc[0, "source_total_trades"] = 2
        verification.loc[
            (verification["report_id"] == "R1") & (verification["metric"] == "TotalTrades"),
            "calculated_value",
        ] = 2
    else:
        audit.loc[0, "source_total_trades"] = 4
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    verification.to_csv(verification_path, index=False)
    audit.to_csv(audit_path, index=False)

    with pytest.raises(ValueError, match=message):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


@pytest.mark.parametrize(
    ("metric", "field", "value", "message"),
    [
        ("TotalTrades", "source_value", 2, "numeric evidence"),
        ("WinRate", "calculated_value", 2, "numeric evidence"),
        ("ProfitFactor", "comparison", 2, "numeric evidence"),
    ],
)
def test_load_package_rejects_tampered_real_v2_metric_evidence(
    tmp_path: Path, metric: str, field: str, value: int, message: str
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    verification_path = package / "metric_verification.csv"
    verification = pd.read_csv(verification_path, keep_default_na=False)
    rows = (verification["report_id"] == "R1") & (verification["metric"] == metric)
    if field == "comparison":
        verification.loc[rows, "source_raw"] = value
    else:
        verification.loc[rows, field] = value
    verification.to_csv(verification_path, index=False)

    with pytest.raises(ValueError, match=message):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


@pytest.mark.parametrize("field", ["source_raw", "source_value", "calculated_value"])
def test_load_package_rejects_non_numeric_pnl_dd_diagnostics(tmp_path: Path, field: str) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    verification_path = package / "metric_verification.csv"
    verification = pd.read_csv(verification_path, keep_default_na=False)
    rows = (verification["report_id"] == "R1") & verification["metric"].isin(["PnL", "DD"])
    verification[field] = verification[field].astype(str)
    verification.loc[rows, field] = "not-a-number"
    verification.to_csv(verification_path, index=False)

    with pytest.raises(ValueError, match="numeric evidence"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


def test_load_package_rejects_real_v2_database_hash_with_invalid_shape(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    manifest_path = package / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_database_sha256"] = "not-a-sha256"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source_database_sha256"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


def test_load_package_rejects_mixed_point_modes(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    points = pd.read_csv(package / "points.csv")
    points.loc[1, "event_mode"] = "legacy_trades_proxy"
    points.to_csv(package / "points.csv", index=False)

    with pytest.raises(ValueError, match="event_mode|mixed"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


def test_load_package_rejects_real_v1_as_audit_only(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    manifest_path = package / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_version"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="v2"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_summary_status", "UNVERIFIED"),
        ("window_metrics_status", "UNVERIFIED"),
    ],
)
def test_load_package_rejects_real_v2_without_verified_manifest_conjunction(
    tmp_path: Path, field: str, value: str
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    manifest_path = package / "package_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source_summary_status|window_metrics_status"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


def test_load_package_rejects_real_v2_with_non_derived_point(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    points = pd.read_csv(package / "points.csv")
    points.loc[0, "window_metrics_status"] = "UNVERIFIED"
    points.to_csv(package / "points.csv", index=False)

    with pytest.raises(ValueError, match="window_metrics_status"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


def test_load_package_rejects_missing_real_event_mapping(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    (package / "point_events.csv").unlink()

    with pytest.raises(ValueError, match="point_events.csv"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


@pytest.mark.parametrize("fault", ["unsorted", "duplicate", "count", "hash", "unknown_point"])
def test_load_package_rejects_incorrect_real_event_mapping(
    tmp_path: Path, fault: str
) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    events_path = package / "point_events.csv"
    events = pd.read_csv(events_path, dtype=str)
    if fault == "unsorted":
        events = events.iloc[::-1]
    elif fault == "duplicate":
        events = pd.concat([events, events.iloc[[0]]], ignore_index=True)
    elif fault == "count":
        events = events.iloc[1:]
    elif fault == "hash":
        points = pd.read_csv(package / "points.csv")
        points.loc[0, "event_ids_hash"] = "0" * 64
        points.to_csv(package / "points.csv", index=False)
    else:
        events.loc[0, "point_id"] = "UNKNOWN"
        events = events.sort_values(["point_id", "event_id"], kind="mergesort")
    events.to_csv(events_path, index=False, lineterminator="\n")

    with pytest.raises(ValueError, match="event|mapping|hash|count|sorted|unique"):
        _load_package(
            package,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )


def test_load_package_rejects_event_mapping_on_legacy_package(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package = build_csv_package(
        [paths["csv"]], WINDOW_START, WINDOW_END, tmp_path / "package"
    )
    (package.directory / "point_events.csv").write_text(
        "point_id,event_id\na,b\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="legacy.*point_events"):
        _load_package(
            package.directory,
            paths["dates"],
            Side.LONG,
            AlgorithmConfig.from_json(paths["config"]),
        )
