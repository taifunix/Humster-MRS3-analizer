from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from typing import Mapping, Sequence
import zlib

import duckdb
import pandas as pd

from .source_packs import REAL_INDEPENDENT_EVENTS, SourcePackError, SourcePackage


ACTION_CODEC = "zlib-columnar-json-v1"


@dataclass(frozen=True, slots=True)
class ClosedCycle:
    event_id: str
    symbol: str
    position_side: str
    timeframe: str
    opened_at: pd.Timestamp
    closed_at: pd.Timestamp


@dataclass(frozen=True, slots=True)
class CycleReconstruction:
    included: tuple[ClosedCycle, ...]
    exclusions: dict[str, int]


def _utc(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def decode_compact_actions(blob: bytes, expected_count: int) -> tuple[dict[str, str], ...]:
    try:
        payload = json.loads(zlib.decompress(blob).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, zlib.error) as error:
        raise SourcePackError("invalid compact action payload") from error
    headers = payload.get("headers")
    rows = payload.get("rows")
    if not isinstance(headers, list) or not all(isinstance(header, str) for header in headers):
        raise SourcePackError("invalid compact action headers")
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise SourcePackError("compressed action count does not match report metadata")
    if any(not isinstance(row, list) or len(row) != len(headers) for row in rows):
        raise SourcePackError("invalid compact action rows")
    return tuple({header: str(value) for header, value in zip(headers, row, strict=True)} for row in rows)


def _event_id(symbol: str, position_side: str, timeframe: str, opened_at: pd.Timestamp) -> str:
    payload = "|".join((symbol, position_side, timeframe, opened_at.isoformat()))
    return sha256(payload.encode("utf-8")).hexdigest()


def reconstruct_closed_cycles(
    report_id: str,
    symbol: str,
    timeframe: str,
    actions: Sequence[Mapping[str, str]],
    window_start: str,
    window_end: str,
) -> CycleReconstruction:
    """Reconstruct sequential position cycles and retain only fully in-window ones."""
    del report_id  # report identity partitions the caller's action sequence.
    start, end = _utc(window_start), _utc(window_end)
    if end <= start:
        raise SourcePackError("window end must be later than start")
    indexed: list[tuple[pd.Timestamp, int, Mapping[str, str]]] = []
    for index, action in enumerate(actions):
        required = {"Timestamp", "Symbol", "Action", "Side"}
        if missing := sorted(required.difference(action)):
            raise SourcePackError(f"required action columns missing: {missing}")
        timestamp = _utc(str(action["Timestamp"]))
        action_symbol = str(action["Symbol"])
        kind = str(action["Action"]).casefold()
        if action_symbol != symbol or kind not in {"opened", "closed"}:
            continue
        indexed.append((timestamp, index, action))
    indexed.sort(key=lambda item: (item[0], item[1]))
    open_cycles: dict[str, deque[pd.Timestamp]] = defaultdict(deque)
    included: list[ClosedCycle] = []
    exclusions: Counter[str] = Counter()
    for timestamp, _, action in indexed:
        if str(action["Action"]).casefold() == "opened":
            position_side = str(action.get("Post Side", "")).strip().casefold()
            if not position_side:
                exclusions["INVALID_ORDER"] += 1
            else:
                open_cycles[position_side].append(timestamp)
            continue
        close_side = str(action["Side"]).strip().casefold()
        position_side = {"sell": "long", "buy": "short"}.get(close_side)
        if position_side is None or not open_cycles[position_side]:
            exclusions["INVALID_ORDER"] += 1
            continue
        opened_at = open_cycles[position_side].popleft()
        if opened_at < start:
            exclusions["OPEN_BEFORE_WINDOW"] += 1
        elif timestamp >= end:
            exclusions["CLOSE_ON_OR_AFTER_WINDOW"] += 1
        elif timestamp < opened_at:
            exclusions["INVALID_ORDER"] += 1
        else:
            included.append(
                ClosedCycle(
                    event_id=_event_id(symbol, position_side, timeframe, opened_at),
                    symbol=symbol,
                    position_side=position_side,
                    timeframe=timeframe,
                    opened_at=opened_at,
                    closed_at=timestamp,
                )
            )
    exclusions["NO_CLOSE"] += sum(len(opens) for opens in open_cycles.values())
    return CycleReconstruction(tuple(included), dict(sorted(exclusions.items())))


def build_duckdb_package(
    database_path: Path, window_start: str, window_end: str, output_dir: Path
) -> SourcePackage:
    """Read v4 compact reports and publish one real-independent-events package."""
    start, end = _utc(window_start), _utc(window_end)
    if end <= start:
        raise SourcePackError("window end must be later than start")
    con = duckdb.connect(str(database_path), read_only=True)
    try:
        schema = con.execute("select value from schema_info where key='schema_version'").fetchone()
        if not schema or str(schema[0]) != "4":
            raise SourcePackError("DuckDB schema_version must be 4")
        reports = con.execute(
            """select r.report_id,r.raw_action_count,p.actions_codec,p.actions_zlib,
                      c.point_id,c.symbol,c.side,c.timeframe
                 from report_runs r join report_payloads p using(report_id)
                 join point_configs c using(point_id) order by r.report_id"""
        ).fetchall()
    finally:
        con.close()
    audit_rows: list[dict[str, object]] = []
    points: dict[str, dict[str, object]] = {}
    for report_id, raw_count, codec, blob, point_id, symbol, side, timeframe in reports:
        if codec != ACTION_CODEC:
            raise SourcePackError(f"unsupported actions codec: {codec}")
        reconstruction = reconstruct_closed_cycles(
            str(report_id), str(symbol), str(timeframe),
            decode_compact_actions(bytes(blob), int(raw_count)), window_start, window_end,
        )
        audit = {"report_id": report_id, "point_id": point_id, "raw_action_count": raw_count,
                 "included_cycles": len(reconstruction.included), **reconstruction.exclusions}
        audit_rows.append(audit)
        entry = points.setdefault(str(point_id), {"point_id": point_id, "symbol": symbol, "side": side,
            "timeframe": timeframe, "event_mode": REAL_INDEPENDENT_EVENTS, "event_ids": set()})
        entry["event_ids"].update(cycle.event_id for cycle in reconstruction.included)
    point_rows = []
    for entry in points.values():
        event_ids = sorted(entry.pop("event_ids"))
        entry["point_event_count"] = len(event_ids)
        entry["event_ids_hash"] = sha256("|".join(event_ids).encode()).hexdigest()
        point_rows.append(entry)
    target = output_dir.resolve()
    if target.exists() and any(target.iterdir()):
        raise SourcePackError(f"package output directory is not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    exclusion_totals: Counter[str] = Counter()
    for audit in audit_rows:
        for key, value in audit.items():
            if key not in {"report_id", "point_id", "raw_action_count", "included_cycles"}:
                exclusion_totals[key] += int(value)
    manifest = {"package_version": 1, "event_mode": REAL_INDEPENDENT_EVENTS,
                "window_start": start.isoformat(), "window_end": end.isoformat(),
                "source_database_sha256": sha256(database_path.read_bytes()).hexdigest(),
                "report_count": len(reports), "point_count": len(point_rows),
                "included_cycles": sum(int(row["included_cycles"]) for row in audit_rows),
                "exclusions": dict(sorted(exclusion_totals.items()))}
    try:
        pd.DataFrame(point_rows).to_csv(stage / "points.csv", index=False, lineterminator="\n")
        pd.DataFrame(audit_rows).fillna(0).to_csv(stage / "source_audit.csv", index=False, lineterminator="\n")
        (stage / "package_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if target.exists():
            target.rmdir()
        stage.replace(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return SourcePackage(target, target / "points.csv", target / "source_audit.csv", target / "package_manifest.json", manifest)
