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
    points["metric_status"] = "VERIFIED"
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
    pd.DataFrame([{"status": "ACCEPTED"}]).to_csv(
        directory / "source_audit.csv", index=False, lineterminator="\n"
    )
    (directory / "package_manifest.json").write_text(
        json.dumps(
            {
                "package_version": 1,
                "event_mode": "real_independent_events",
                "window_start": "2026-07-15T00:00:00+00:00",
                "window_end": "2026-08-06T00:00:00+00:00",
                "point_count": len(points),
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


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("package_version", 2, "package_version"),
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


def test_load_package_rejects_unverified_real_point(tmp_path: Path) -> None:
    paths = write_selection_inputs(tmp_path / "inputs")
    package, _ = write_real_package(tmp_path / "package", paths)
    points = pd.read_csv(package / "points.csv")
    points.loc[0, "metric_status"] = "UNVERIFIED"
    points.to_csv(package / "points.csv", index=False)

    with pytest.raises(ValueError, match="VERIFIED"):
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
