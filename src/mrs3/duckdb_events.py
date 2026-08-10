from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import struct
import tempfile
from typing import Iterator, Mapping, Sequence
import zlib

import duckdb
from lxml import etree, html as lxml_html
import pandas as pd

from .source_packs import REAL_INDEPENDENT_EVENTS, SourcePackError, SourcePackage


ACTION_CODEC = "zlib-columnar-json-v1"
EQUITY_CODEC = "zlib-int64-delta-v1"
WALLET_CODEC = "zlib-int64-delta-v1"
SERIES_SCALE = Decimal("100000000")

VERIFICATION_METRICS = {
    "PnL": "TotalPnL",
    "DD": "MaxDrawdown",
    "TotalTrades": "TotalTrades",
    "WinRate": "WinRate",
    "ProfitFactor": "ProfitFactor",
}
VERIFICATION_COLUMNS = [
    "report_id", "source_file", "metric", "source_raw", "source_value",
    "calculated_value", "comparison", "cause",
]
POINT_COLUMNS = [
    "point_id", "Run id", "settings[*].basic.symbol", "settings[*].basic.time_frame",
    "StartDate", "EndDate", "TotalPnL", "TotalPnLPercent", "MaxDrawdown",
    "MaxDrawdownPercent", "TotalTrades", "Win", "Los", "WinRate", "ProfitFactor",
    "settings[*].mrs2.ma_long.len", "settings[*].mrs2.ma_close_long.len",
    "settings[*].mrs2.ma_long.multiplier", "settings[*].mrs2.ma_short.len",
    "settings[*].mrs2.ma_close_short.len", "settings[*].mrs2.ma_short.multiplier",
    "event_mode", "point_event_count", "event_ids_hash", "metric_status",
]


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


def _query_batches(
    con: duckdb.DuckDBPyConnection, query: str, parameters: Sequence[object] = (), *, batch_size: int = 500
) -> Iterator[list[dict[str, object]]]:
    cursor = con.execute(query, parameters)
    columns = [description[0] for description in cursor.description]
    while rows := cursor.fetchmany(batch_size):
        yield [dict(zip(columns, row, strict=True)) for row in rows]


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
    timestamps = grid if isinstance(grid, pd.DatetimeIndex) else pd.to_datetime(grid, utc=True)
    if end <= start:
        raise SourcePackError("window end must be later than start")
    if len(timestamps) != len(equity) or not len(timestamps) or timestamps[0] > start or timestamps[-1] < end:
        raise SourcePackError("grid does not cover requested window")
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise SourcePackError("grid timestamps must be strictly increasing")
    wallet_indexes: list[int] = []
    wallet_values: list[int] = []
    for index, value in wallet_changes:
        if not 0 <= index < len(timestamps):
            raise SourcePackError("wallet change index is outside the grid")
        if wallet_indexes and index <= wallet_indexes[-1]:
            raise SourcePackError("wallet changes must have strictly increasing grid indexes")
        wallet_indexes.append(index)
        wallet_values.append(value)
    start_index = max(int(timestamps.searchsorted(start, side="left")) - 1, 0)
    end_index = int(timestamps.searchsorted(end, side="left")) - 1
    starting_change = bisect_right(wallet_indexes, start_index) - 1
    ending_change = bisect_right(wallet_indexes, end_index) - 1
    if starting_change < 0 or ending_change < 0:
        raise SourcePackError("wallet changes do not cover requested window")
    starting_wallet = wallet_values[starting_change]
    ending_wallet = wallet_values[ending_change]
    window_start_index = int(timestamps.searchsorted(start, side="left"))
    window_end_index = int(timestamps.searchsorted(end, side="left"))
    window_equity = equity[window_start_index:window_end_index]
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


def _unscale(value: int | float) -> int | float:
    result = Decimal(str(value)) / SERIES_SCALE
    return int(result) if result == result.to_integral() else float(result)


def _setting(settings: Mapping[str, object], path: str) -> object:
    value: object = settings
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return pd.NA
        value = value[part]
    return value


def _selector_row(
    report_number: int,
    report: Mapping[str, object],
    metrics: Mapping[str, int | float | None],
    event_ids: Sequence[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict[str, object]:
    settings = json.loads(str(report["settings_json"]))
    point = {
        "point_id": report["point_id"],
        "Run id": report_number,
        "settings[*].basic.symbol": report["symbol"],
        "settings[*].basic.time_frame": report["timeframe"],
        "StartDate": start.isoformat(),
        "EndDate": end.isoformat(),
        "TotalPnL": metrics["TotalPnL"],
        "TotalPnLPercent": metrics["TotalPnLPercent"],
        "MaxDrawdown": metrics["MaxDrawdown"],
        "MaxDrawdownPercent": metrics["MaxDrawdownPercent"],
        "TotalTrades": metrics["TotalTrades"],
        "Win": metrics["Win"],
        "Los": metrics["Los"],
        "WinRate": metrics["WinRate"],
        "ProfitFactor": metrics["ProfitFactor"],
        "settings[*].mrs2.ma_long.len": _setting(settings, "mrs2.ma_long.len"),
        "settings[*].mrs2.ma_close_long.len": _setting(settings, "mrs2.ma_close_long.len"),
        "settings[*].mrs2.ma_long.multiplier": _setting(settings, "mrs2.ma_long.multiplier"),
        "settings[*].mrs2.ma_short.len": _setting(settings, "mrs2.ma_short.len"),
        "settings[*].mrs2.ma_close_short.len": _setting(settings, "mrs2.ma_close_short.len"),
        "settings[*].mrs2.ma_short.multiplier": _setting(settings, "mrs2.ma_short.multiplier"),
        "event_mode": REAL_INDEPENDENT_EVENTS,
        "point_event_count": len(event_ids),
        "event_ids_hash": sha256("|".join(event_ids).encode("utf-8")).hexdigest(),
    }
    point["metric_status"] = "UNVERIFIED"
    return point


def _decimal_token(raw: str) -> tuple[Decimal, int]:
    match = re.search(r"[-+]?(?:\d{1,3}(?:[ ,]\d{3})+|\d+)(?:[.,]\d+)?", raw)
    if not match:
        raise SourcePackError(f"metric has no numeric value: {raw!r}")
    token = match.group(0).replace(" ", "")
    if "," in token and "." in token:
        token = token.replace(",", "") if token.rfind(".") > token.rfind(",") else token.replace(".", "").replace(",", ".")
    elif "," in token:
        token = token.replace(",", ".")
    try:
        value = Decimal(token)
    except InvalidOperation as error:
        raise SourcePackError(f"invalid metric value: {raw!r}") from error
    return value, len(token.partition(".")[2])


def _html_summary(path: Path) -> dict[str, tuple[str, Decimal, int]]:
    aliases = {
        "pnl": "PnL", "totalpnl": "PnL", "profitloss": "PnL", "profitandloss": "PnL",
        "dd": "DD", "drawdown": "DD", "maxdrawdown": "DD", "maximumdrawdown": "DD",
        "totaltrades": "TotalTrades", "trades": "TotalTrades",
        "winrate": "WinRate", "winningrate": "WinRate",
        "profitfactor": "ProfitFactor", "profitfactorgrossprofitgrossloss": "ProfitFactor",
    }
    try:
        document = lxml_html.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, etree.ParserError) as error:
        raise SourcePackError(f"cannot parse verification HTML: {path.name}") from error
    found: dict[str, tuple[str, Decimal, int]] = {}
    for row in document.xpath("//tr"):
        cells = [" ".join(" ".join(cell.itertext()).split()) for cell in row.xpath("./th|./td")]
        if len(cells) < 2:
            continue
        label = re.sub(r"[^a-z0-9]", "", cells[0].casefold())
        if label in {"maxdrawdown", "maximumdrawdown"} and "%" in cells[0]:
            continue
        metric = aliases.get(label)
        if metric:
            value, precision = _decimal_token(cells[-1])
            found[metric] = (cells[-1], value, precision)
    missing = [metric for metric in VERIFICATION_METRICS if metric not in found]
    if missing:
        raise SourcePackError(f"verification HTML missing summary metrics: {missing}")
    return found


def _verification(
    reports: Sequence[Mapping[str, object]],
    html_root: Path | None,
    sample_count: int,
) -> tuple[list[dict[str, object]], str, str]:
    if html_root is None:
        return [], "UNVERIFIED", "HTML_ROOT_ABSENT"
    if not 3 <= sample_count <= 5:
        return [], "UNVERIFIED", "SAMPLE_COUNT_OUT_OF_RANGE"
    if len(reports) < sample_count:
        return [], "UNVERIFIED", "INSUFFICIENT_REPORTS"
    rows: list[dict[str, object]] = []
    cause = ""
    for report in reports[:sample_count]:
        source_file = Path(str(report["source_file"])).name
        path = html_root / source_file
        if not path.is_file():
            rows.append({"report_id": report["report_id"], "source_file": source_file, "metric": "ALL",
                         "source_raw": "", "source_value": "", "calculated_value": "",
                         "comparison": "MISMATCH", "cause": "SOURCE_HTML_MISSING"})
            cause = cause or "SOURCE_HTML_MISSING"
            continue
        try:
            parsed = _html_summary(path)
        except SourcePackError as error:
            rows.append({"report_id": report["report_id"], "source_file": source_file, "metric": "ALL",
                         "source_raw": "", "source_value": "", "calculated_value": "",
                         "comparison": "MISMATCH", "cause": str(error)})
            cause = cause or "HTML_PARSE_ERROR"
            continue
        metrics = report["metrics"]
        assert isinstance(metrics, Mapping)
        for metric, calculated_name in VERIFICATION_METRICS.items():
            source_raw, source_value, precision = parsed[metric]
            calculated = metrics[calculated_name]
            equal = calculated is not None and Decimal(str(calculated)).quantize(
                Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP
            ) == source_value
            comparison = "EQUAL" if equal else "MISMATCH"
            row_cause = "" if equal else "VALUE_MISMATCH"
            cause = cause or row_cause
            rows.append({"report_id": report["report_id"], "source_file": source_file, "metric": metric,
                         "source_raw": source_raw, "source_value": source_value,
                         "calculated_value": calculated, "comparison": comparison, "cause": row_cause})
    return rows, ("VERIFIED" if not cause else "UNVERIFIED"), cause


def build_duckdb_package(
    database_path: Path,
    window_start: str,
    window_end: str,
    output_dir: Path,
    *,
    verification_html_root: Path | None = None,
    verification_sample_count: int = 3,
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
        reports = [
            report
            for batch in _query_batches(
                con,
                """select r.report_id,r.source_file,r.settings_json,r.raw_action_count,
                          r.equity_sample_count,r.wallet_change_count,p.series_codec,p.actions_codec,
                          c.point_id,c.symbol,c.side,c.timeframe,g.sample_count,
                          g.start_timestamp_ms,g.end_timestamp_ms
                     from report_runs r join report_payloads p using(report_id)
                     join point_configs c using(point_id) join time_grids g using(grid_id)
                    order by r.report_id""",
            )
            for report in batch
        ]
        duplicates = sorted(
            point_id for point_id, count in Counter(str(report["point_id"]) for report in reports).items() if count > 1
        )
        if duplicates:
            raise SourcePackError(f"duplicate point_id reports are not supported: {duplicates}")
        for report in reports:
            if report["actions_codec"] != ACTION_CODEC:
                raise SourcePackError(f"unsupported actions codec: {report['actions_codec']}")
            if report["series_codec"] != EQUITY_CODEC:
                raise SourcePackError(f"unsupported series codec: {report['series_codec']}")
        report_numbers = {str(report["report_id"]): number for number, report in enumerate(reports, start=1)}
        reports_by_id = {str(report["report_id"]): report for report in reports}
        start_ns, end_ns = start.value, end.value

        audit_rows: list[dict[str, object]] = []
        accepted_reports: list[dict[str, object]] = []
        event_rows: list[dict[str, str]] = []
        exclusion_totals: Counter[str] = Counter()
        for batch_start in range(0, len(reports), 500):
            report_batch = reports[batch_start:batch_start + 500]
            action_placeholders = ",".join("?" for _ in report_batch)
            actions_by_id = {
                str(action["report_id"]): action
                for actions_batch in _query_batches(
                    con,
                    f"""select r.report_id,p.actions_zlib from report_runs r
                         join report_payloads p using(report_id)
                        where r.report_id in ({action_placeholders}) order by r.report_id""",
                    tuple(str(report["report_id"]) for report in report_batch),
                )
                for action in actions_batch
            }
            covering: dict[str, tuple[tuple[dict[str, str], ...], list[str], dict[str, object]]] = {}
            for report in report_batch:
                report_id = str(report["report_id"])
                action_payload = actions_by_id[report_id]
                actions = decode_compact_actions(bytes(action_payload["actions_zlib"]), int(report["raw_action_count"]))
                reconstruction = reconstruct_closed_cycles(
                    report_id, str(report["symbol"]), str(report["timeframe"]), actions, window_start, window_end
                )
                event_ids = sorted({cycle.event_id for cycle in reconstruction.included})
                reconstructed = len(reconstruction.included) + sum(
                    reconstruction.exclusions.get(reason, 0)
                    for reason in ("OPEN_BEFORE_WINDOW", "CLOSE_ON_OR_AFTER_WINDOW")
                )
                audit: dict[str, object] = {
                    "report_id": report["report_id"], "point_id": report["point_id"],
                    "source_file": Path(str(report["source_file"])).name,
                    "coverage_status": "REJECTED", "coverage_reason": "GRID_NOT_COVERED",
                    "raw_action_count": report["raw_action_count"], "reconstructed_cycles": reconstructed,
                    "included_cycles": len(reconstruction.included), **reconstruction.exclusions,
                }
                audit_rows.append(audit)
                for reason, count in reconstruction.exclusions.items():
                    exclusion_totals[reason] += count
                if (
                    int(report["sample_count"]) > 0
                    and int(report["start_timestamp_ms"]) * 1_000_000 <= start_ns
                    and int(report["end_timestamp_ms"]) * 1_000_000 >= end_ns
                ):
                    covering[report_id] = (actions, event_ids, audit)
            if not covering:
                continue
            placeholders = ",".join("?" for _ in covering)
            series_by_id = {
                str(series["report_id"]): series
                for series_batch in _query_batches(
                    con,
                    f"""select r.report_id,p.equity_zlib,p.wallet_zlib,g.timestamps_zlib
                           from report_runs r join report_payloads p using(report_id)
                           join time_grids g using(grid_id)
                          where r.report_id in ({placeholders}) order by r.report_id""",
                    tuple(covering),
                )
                for series in series_batch
            }
            for report_id, (actions, event_ids, audit) in covering.items():
                report = reports_by_id[report_id]
                series = series_by_id[report_id]
                timestamps_ms = decode_compact_deltas(
                    bytes(series["timestamps_zlib"]), int(report["sample_count"]), codec=str(report["series_codec"])
                )
                grid = pd.to_datetime(timestamps_ms, unit="ms", utc=True)
                raw_metrics = calculate_point_metrics(
                    grid,
                    decode_compact_deltas(bytes(series["equity_zlib"]), int(report["equity_sample_count"]), codec=str(report["series_codec"])),
                    decode_wallet_changes(bytes(series["wallet_zlib"]), int(report["wallet_change_count"]), codec=str(report["series_codec"])),
                    actions,
                    window_start,
                    window_end,
                )
                metrics = dict(raw_metrics)
                metrics["TotalPnL"] = _unscale(raw_metrics["TotalPnL"])
                metrics["MaxDrawdown"] = _unscale(raw_metrics["MaxDrawdown"])
                audit.update({"coverage_status": "ACCEPTED", "coverage_reason": "", **metrics})
                report["metrics"] = metrics
                report["event_ids"] = event_ids
                report["point"] = _selector_row(report_numbers[report_id], report, metrics, event_ids, start, end)
                accepted_reports.append(report)
                event_rows.extend({"point_id": str(report["point_id"]), "event_id": event_id} for event_id in event_ids)
    finally:
        con.close()

    verification_rows, verification_status, verification_cause = _verification(
        accepted_reports, verification_html_root, verification_sample_count
    )
    point_rows = [dict(report["point"], metric_status=verification_status) for report in accepted_reports]
    point_rows.sort(key=lambda row: str(row["point_id"]))
    event_rows.sort(key=lambda row: (row["point_id"], row["event_id"]))
    audit_rows.sort(key=lambda row: str(row["report_id"]))
    manifest = {
        "package_version": 1,
        "event_mode": REAL_INDEPENDENT_EVENTS,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "source_database_sha256": sha256(database_path.read_bytes()).hexdigest(),
        "report_count": len(reports),
        "coverage_accepted_reports": len(accepted_reports),
        "coverage_rejected_reports": len(reports) - len(accepted_reports),
        "point_count": len(point_rows),
        "raw_action_count": sum(int(row["raw_action_count"]) for row in audit_rows),
        "reconstructed_cycles": sum(int(row["reconstructed_cycles"]) for row in audit_rows),
        "included_cycles": sum(int(row["included_cycles"]) for row in audit_rows),
        "flat_trades": sum(int(row.get("flat_trades", 0)) for row in audit_rows),
        "exclusions": dict(sorted(exclusion_totals.items())),
        "verification_sample_count": verification_sample_count,
        "verification_status": verification_status,
        "verification_cause": verification_cause,
    }
    target = output_dir.resolve()
    if target.exists() and any(target.iterdir()):
        raise SourcePackError(f"package output directory is not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=target.parent))
    try:
        pd.DataFrame(point_rows, columns=POINT_COLUMNS).to_csv(
            stage / "points.csv", index=False, lineterminator="\n"
        )
        pd.DataFrame(event_rows, columns=["point_id", "event_id"]).to_csv(stage / "point_events.csv", index=False, lineterminator="\n")
        pd.DataFrame(audit_rows).fillna(0).to_csv(stage / "source_audit.csv", index=False, lineterminator="\n")
        pd.DataFrame(verification_rows, columns=VERIFICATION_COLUMNS).to_csv(
            stage / "metric_verification.csv", index=False, lineterminator="\n"
        )
        (stage / "package_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if target.exists():
            target.rmdir()
        stage.replace(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return SourcePackage(
        target,
        target / "points.csv",
        target / "source_audit.csv",
        target / "package_manifest.json",
        manifest,
        target / "point_events.csv",
        target / "metric_verification.csv",
    )
