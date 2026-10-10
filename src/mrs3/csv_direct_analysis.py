"""Direct MRS3 tester CSV -> fresh Analysis DuckDB read by the unchanged downstream.

The analysis is the Source v6 fresh analysis with a different input: each CSV
row becomes the same per-point analysis row that ``source_v6_analysis_fresh``
builds from a materialized surface, and goes through the same
``_analyze_points`` engine in ``real_independent_events`` mode, so every
plateau, base and multi-order admission gate applies unchanged.

Two inputs differ from Source v6 by construction, because CSV has no trades:

* ``_event_ids`` are point-local count slots (see ``csv_legacy_compat``);
* ``events_last_30d`` is the exact Decimal monthly rate from
  ``csv_direct_input``; the engine floors the plateau sum once.

No Source v6 database or materialized surface is created. Without PRETEST A/B
evidence the artifact uses the legacy v1 fresh fingerprints.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from .config import AlgorithmConfig
from .csv_direct_input import ACTIVITY_CONTRACT, INPUT_CONTRACT, DirectCsvInput, load_csv_direct
from .csv_legacy_compat import (
    EVENT_ID_SEMANTICS,
    LEGACY_ANALYSIS_FINGERPRINT,
    LEGACY_EVENT_MODE,
    LEGACY_SURFACE_FINGERPRINT,
    TRADE_COUNT_CONTRACT,
    countslot_ids,
    event_ids_hash,
)
from .locking import OutputDirectoryLock
from .pipeline import ALGORITHM_VERSION, _analyze_points, _canonical
from .source_v6 import _canonical_json
from .source_v6_analysis_fresh import _frames, _publish


@dataclass(frozen=True)
class DirectAnalysisPayload:
    identity: dict[str, object]
    analysis_id: str
    config_sha256: str
    results: list[dict[str, object]]
    input_digest: str
    point_count: int


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def analysis_row(point: Mapping[str, object]) -> dict[str, object]:
    """One CSV point as the Source v6 fresh analysis row (`_analysis_frame_row`).

    Same keys and types, except that ``pretest_ab`` is absent and
    ``events_last_30d`` is an exact Decimal rate instead of a timestamp count.
    """
    point_id = str(point["point_id"])
    trades = int(point["trades"])
    event_ids = tuple(countslot_ids(point_id, trades))
    shift_bp = int(point["shift_bp"])
    profit_factor = point["profit_factor"]
    activity = point["activity_30d"]
    if not isinstance(activity, Decimal) or not activity.is_finite() or not 0 <= activity <= trades:
        raise ValueError(f"invalid 30-day activity for {point_id}")
    return {
        "point_id": point_id, "symbol": str(point["symbol"]), "side": str(point["side"]),
        "timeframe": str(point["timeframe"]),
        "shift_bp": shift_bp, "shift_pct": shift_bp / 100, "open_ma": int(point["open_ma"]),
        "close_ma": int(point["close_ma"]), "pnl_pct": float(point["pnl_pct"]),
        "dd_pct": float(point["dd_pct"]),
        "trades": trades, "wins": int(point["wins"]), "losses": int(point["losses"]),
        "win_rate_pct": float(point["win_rate_pct"]),
        "profit_factor": None if profit_factor is None or pd.isna(profit_factor) else float(profit_factor),
        "point_event_count": len(event_ids), "_event_ids": event_ids,
        "events_last_30d": activity,
        "event_ids_hash": event_ids_hash(event_ids), "event_mode": LEGACY_EVENT_MODE,
        "report_start": pd.Timestamp(point["report_start"]),
        "report_end": pd.Timestamp(point["report_end"]),
        "listing_date": pd.Timestamp(point["listing_date"]),
    }


def _scope_digest(scope_key: str, group: pd.DataFrame) -> str:
    """Content of one scope's normalized CSV points, independent of other scopes."""
    rows = []
    for record in group.drop(columns=["run_tag"], errors="ignore").to_dict("records"):
        rows.append({
            key: (value.isoformat() if isinstance(value, pd.Timestamp) else format(value, "f") if isinstance(value, Decimal) else value)
            for key, value in sorted(record.items())
        })
    return _digest({"scope": scope_key, "points": sorted(rows, key=lambda row: str(row["point_id"]))})


def prepare_csv_direct_analysis(
    data: DirectCsvInput,
    config: AlgorithmConfig,
    *,
    listing_dates: Mapping[str, object],
    algorithm_version: str = ALGORITHM_VERSION,
) -> DirectAnalysisPayload:
    """Compute all seven frames per scope without opening/writing any DuckDB."""
    if not data.scope_keys or data.frame.empty:
        raise ValueError("CSV analysis input contains no scopes")
    if not isinstance(algorithm_version, str) or not algorithm_version:
        raise ValueError("CSV_DIRECT_INVALID_ALGORITHM_VERSION: algorithm_version must be text")
    try:
        json.loads(algorithm_version)
    except ValueError:
        pass
    else:
        # The manifest reader JSON-decodes stored values; "1", "true" or a quoted
        # string would come back changed and break the analysis_id hash.
        raise ValueError("CSV_DIRECT_INVALID_ALGORITHM_VERSION: algorithm_version must not be JSON")
    actual_symbols = {str(v) for v in data.frame["symbol"]}
    if actual_symbols - set(str(symbol).upper() for symbol in listing_dates):
        raise ValueError("listing_dates does not cover all CSV symbols")
    results: list[dict[str, object]] = []
    scope_digests: dict[str, str] = {}
    for scope_key, group in data.scopes().items():
        rows = [analysis_row(point) for point in group.to_dict("records")]
        frames = _frames(_analyze_points(pd.DataFrame(rows), config))
        digest = _scope_digest(scope_key, group)
        scope_digests[scope_key] = digest
        results.append({"scope_key": scope_key, "scope_digest": digest, "frames": frames})
    config_hash = _digest(_canonical(config))
    listing_hash = _digest(dict(sorted((str(k), str(v)) for k, v in listing_dates.items())))
    identity: dict[str, object] = {
        "fingerprint": LEGACY_ANALYSIS_FINGERPRINT,
        "surface_fingerprint": LEGACY_SURFACE_FINGERPRINT,
        # Virtual, content-addressed: no surface database exists for CSV Direct.
        "surface_id": "csv-direct-" + data.input_digest[:32],
        "source_content_digest": data.input_digest,
        "scope_digests": scope_digests,
        "algorithm_version": algorithm_version,
        "algorithm_config_sha256": config_hash,
        "listing_dates_sha256": listing_hash,
        # The fresh reader's fixed point-schema tag; see csv_direct below.
        "event_mode": LEGACY_EVENT_MODE,
        "csv_direct": {
            "input_contract": INPUT_CONTRACT,
            "trade_count_contract": TRADE_COUNT_CONTRACT,
            "activity_contract": ACTIVITY_CONTRACT,
            "event_id_semantics": EVENT_ID_SEMANTICS,
            "pretest_ab_available": False,
            "csv_sha256": sorted({digest for _label, digest in data.csv_sha256}),
            "report_start": data.window_start.isoformat(),
            "report_end": data.window_end.isoformat(),
        },
    }
    analysis_id = _digest(identity)
    return DirectAnalysisPayload(identity, analysis_id, config_hash, results, data.input_digest, len(data.frame))


def publish_csv_direct_analysis(
    payload: DirectAnalysisPayload,
    output_dir: str | Path,
    *,
    filename: str,
) -> Path:
    """Publish ONE immutable Analysis DuckDB with the Source v6 fresh writer."""
    if Path(filename).name != filename or not filename.endswith(".analysis-v6.duckdb"):
        raise ValueError("filename must be a plain *.analysis-v6.duckdb basename")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with OutputDirectoryLock(directory):
        target = directory / filename
        if target.exists():
            raise FileExistsError(f"Analysis DB already exists: {target}")
        return _publish(
            directory, target, payload.identity, payload.analysis_id,
            payload.config_sha256, payload.results,
        )


def run_csv_direct_analysis(
    paths: list[str | Path],
    output_dir: str | Path,
    *,
    listing_dates: Mapping[str, object],
    config: AlgorithmConfig,
    filename: str,
    allow_shared_snapshot_template: bool = False,
) -> Path:
    parsed = load_csv_direct(
        paths,
        listing_dates=listing_dates,
        allow_shared_snapshot_template=allow_shared_snapshot_template,
    )
    payload = prepare_csv_direct_analysis(parsed, config, listing_dates=listing_dates)
    return publish_csv_direct_analysis(payload, output_dir, filename=filename)
