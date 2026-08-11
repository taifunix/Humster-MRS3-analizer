from __future__ import annotations

from dataclasses import asdict, FrozenInstanceError
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys

import pytest

from mrs3 import duckdb_events


ROOT = Path(__file__).parents[1]
IMPORTER_DIR = ROOT / "programs" / "Обработчик HTML-DuckDB"
FIXTURES = ROOT / "tests" / "fixtures" / "duckdb_import"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, IMPORTER_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


V3 = _load("test_compact_importer_v3", "mrs3_html_compact_importer_v3.py")
V4 = _load("test_compact_importer_v4", "mrs3_html_parallel_compact_importer_v4.py")


@pytest.mark.parametrize(
    ("filename", "timestamps", "equity", "wallet", "action_count"),
    [
        (
            "report_a.html",
            (1704067200000, 1704070800000, 1704074400000),
            (10000000000, 9825000000, 10250000000),
            ((0, 10000000000), (2, 10250000000)),
            2,
        ),
        (
            "report_b.html",
            (1706745600000, 1706746500000, 1706747400000, 1706748300000),
            (20000000000, 19750000000, 20112500000, 20350000000),
            ((0, 20000000000), (1, 19875000000), (3, 20350000000)),
            3,
        ),
    ],
)
def test_v3_v4_and_mrs3_adapter_share_compact_record_parity(
    filename: str,
    timestamps: tuple[int, ...],
    equity: tuple[int, ...],
    wallet: tuple[tuple[int, int], ...],
    action_count: int,
) -> None:
    path = FIXTURES / filename

    direct = V3.build_compact_record(path)
    worker = V4._parse_worker(str(path))

    assert worker.error_classification is None
    assert worker.record is not None
    assert asdict(worker.record) == asdict(direct)
    assert direct.source_sha256 == sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert (direct.series_codec, direct.actions_codec) == (
        "zlib-int64-delta-v1",
        "zlib-columnar-json-v1",
    )
    assert (direct.sample_count, direct.equity_sample_count, direct.wallet_change_count, direct.raw_action_count) == (
        len(timestamps),
        len(equity),
        len(wallet),
        action_count,
    )

    decoded = duckdb_events.decode_compact_record(direct)
    assert decoded["timestamps_ms"] == timestamps
    assert decoded["equity_scaled"] == equity
    assert decoded["wallet_changes"] == wallet
    assert len(decoded["actions"]) == action_count
    assert decoded["payload_bytes"] == {
        "timestamps_zlib": direct.timestamps_zlib,
        "actions_zlib": direct.actions_zlib,
        "equity_zlib": direct.equity_zlib,
        "wallet_zlib": direct.wallet_zlib,
    }


def test_compact_record_is_immutable() -> None:
    record = V3.build_compact_record(FIXTURES / "report_a.html")

    with pytest.raises(FrozenInstanceError):
        record.raw_action_count = 0


def test_v3_and_v4_classify_invalid_reports_identically(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.html"
    malformed.write_text(
        (FIXTURES / "report_a.html").read_text(encoding="utf-8").replace("equitySeries", "missingSeries"),
        encoding="utf-8",
    )

    direct = V3.read_compact_record(malformed)
    worker = V4._parse_worker(str(malformed))

    assert direct.record is None
    assert worker.record is None
    assert direct.source_sha256 == worker.source_sha256 == sha256(malformed.read_bytes()).hexdigest()
    assert direct.error_classification == worker.error_classification == "INVALID_REPORT"
    assert direct.error_message == worker.error_message == "missing embedded equitySeries"
