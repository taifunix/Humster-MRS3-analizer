from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Mapping, Sequence
import zlib

import duckdb
import pandas as pd

from .source_packs import REAL_INDEPENDENT_EVENTS, SourcePackError, SourcePackage


ACTION_CODEC = "zlib-columnar-json-v1"
EQUITY_CODEC = "zlib-int64-delta-v1"
WALLET_CODEC = "zlib-int64-delta-v1"


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


def decode_compact_deltas(
    blob: bytes, expected_count: int, *, codec: str = EQUITY_CODEC
) -> tuple[int, ...]:
    """Decompress signed int64 deltas into the original value series."""
    if codec != EQUITY_CODEC:
        raise SourcePackError(f"unsupported equity codec: {codec}")
    try:
        payload = zlib.decompress(blob)
    except zlib.error as error:
        raise SourcePackError("invalid compact delta payload") from error
    if len(payload) != expected_count * 8:
        raise SourcePackError("compressed delta count does not match report metadata")
    deltas = struct.unpack(f"<{expected_count}q", payload)
    value = 0
    series = []
    for delta in deltas:
        value += delta
        series.append(value)
    return tuple(series)


def decode_wallet_changes(
    blob: bytes, expected_count: int, *, codec: str = WALLET_CODEC
) -> tuple[tuple[int, int], ...]:
    """Decompress indexed wallet snapshots stored as little-endian records."""
    if codec != WALLET_CODEC:
        raise SourcePackError(f"unsupported wallet codec: {codec}")
    try:
        payload = zlib.decompress(blob)
    except zlib.error as error:
        raise SourcePackError("invalid compact wallet payload") from error
    if len(payload) != expected_count * 12:
        raise SourcePackError("compressed wallet count does not match report metadata")
    changes = tuple(struct.iter_unpack("<Iq", payload))
    if any(current[0] <= previous[0] for previous, current in zip(changes, changes[1:])):
        raise SourcePackError("wallet changes must have strictly increasing grid indexes")
    return changes


def _position_side(action: Mapping[str, str]) -> str | None:
    post_side = str(action.get("Post Side", "")).strip().casefold()
    if post_side in {"long", "short"}:
        return post_side
    return {"sell": "long", "buy": "short"}.get(str(action.get("Side", "")).strip().casefold())


def _number(value: object) -> int | float:
    number = float(str(value))
    return int(number) if number.is_integer() else number


def _realised_pnls(
    actions: Sequence[Mapping[str, str]], start: pd.Timestamp, end: pd.Timestamp
) -> tuple[int | float, ...]:
    indexed: list[tuple[pd.Timestamp, int, Mapping[str, str]]] = []
    required = {"Timestamp", "Symbol", "Action", "Side"}
    for index, action in enumerate(actions):
        if missing := sorted(required.difference(action)):
            raise SourcePackError(f"required action columns missing: {missing}")
        if not str(action["Symbol"]).strip():
            raise SourcePackError("action symbol is required")
        indexed.append((_utc(str(action["Timestamp"])), index, action))
    indexed.sort(key=lambda item: (item[0], item[1]))
    open_cycles: dict[tuple[str, str], deque[list[tuple[pd.Timestamp, Mapping[str, str]]]]] = defaultdict(deque)
    realised: list[int | float] = []
    for timestamp, _, action in indexed:
        kind = str(action.get("Action", "")).casefold()
        side = _position_side(action)
        if kind == "opened":
            if side is not None:
                open_cycles[(str(action["Symbol"]), side)].append([(timestamp, action)])
            continue
        cycle_key = (str(action["Symbol"]), side) if side is not None else None
        if kind not in {"decreased", "closed"} or cycle_key is None or not open_cycles[cycle_key]:
            continue
        cycle = open_cycles[cycle_key][0]
        cycle.append((timestamp, action))
        if kind != "closed":
            continue
        open_cycles[cycle_key].popleft()
        opened_at = cycle[0][0]
        if opened_at < start or timestamp >= end or timestamp < opened_at:
            continue
        realised.extend(
            _number(cycle_action.get("PnL", 0))
            for action_time, cycle_action in cycle[1:]
            if str(cycle_action.get("Action", "")).casefold() in {"decreased", "closed"}
            and start <= action_time < end
        )
    return tuple(realised)


def calculate_point_metrics(
    grid: Sequence[str | pd.Timestamp],
    equity: Sequence[int],
    wallet_changes: Sequence[tuple[int, int]],
    actions: Sequence[Mapping[str, str]],
    window_start: str,
    window_end: str,
) -> dict[str, int | float | None]:
    """Calculate source metrics for a fully covered UTC half-open interval."""
    start, end = _utc(window_start), _utc(window_end)
    timestamps = tuple(_utc(str(value)) for value in grid)
    if end <= start:
        raise SourcePackError("window end must be later than start")
    if len(timestamps) != len(equity) or not timestamps or timestamps[0] > start or timestamps[-1] < end:
        raise SourcePackError("grid does not cover requested window")
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise SourcePackError("grid timestamps must be strictly increasing")
    wallet: list[int | None] = [None] * len(timestamps)
    for index, value in wallet_changes:
        if index >= len(wallet):
            raise SourcePackError("wallet change index is outside the grid")
        wallet[index:] = [value] * (len(wallet) - index)
    before_start = [index for index, timestamp in enumerate(timestamps) if timestamp < start]
    start_index = before_start[-1] if before_start else 0
    before_end = [index for index, timestamp in enumerate(timestamps) if timestamp < end]
    if wallet[start_index] is None or not before_end or wallet[before_end[-1]] is None:
        raise SourcePackError("wallet changes do not cover requested window")
    starting_wallet = wallet[start_index]
    ending_wallet = wallet[before_end[-1]]
    assert starting_wallet is not None and ending_wallet is not None
    window_equity = [value for timestamp, value in zip(timestamps, equity) if start <= timestamp < end]
    peak = window_equity[0]
    max_drawdown = 0
    max_drawdown_percent = 0.0
    for value in window_equity:
        peak = max(peak, value)
        drawdown = peak - value
        max_drawdown = max(max_drawdown, drawdown)
        if peak:
            max_drawdown_percent = max(max_drawdown_percent, drawdown / peak * 100)
    realised = _realised_pnls(actions, start, end)
    wins = sum(value > 0 for value in realised)
    losses = sum(value < 0 for value in realised)
    gross_profit = sum(value for value in realised if value > 0)
    gross_loss = -sum(value for value in realised if value < 0)
    pnl = ending_wallet - starting_wallet
    return {
        "TotalPnL": pnl,
        "TotalPnLPercent": pnl / starting_wallet * 100 if starting_wallet else None,
        "MaxDrawdown": max_drawdown,
        "MaxDrawdownPercent": max_drawdown_percent,
        "TotalTrades": len(realised),
        "Win": wins,
        "Los": losses,
        "WinRate": wins / len(realised) * 100 if realised else 0.0,
        "ProfitFactor": gross_profit / gross_loss if gross_loss else None,
        "flat_trades": sum(value == 0 for value in realised),
    }


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
