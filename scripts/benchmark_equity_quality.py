#!/usr/bin/env python
"""Read-only M0 diagnostic for the canonical R7.3 equity-only contract."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
from pathlib import Path
import statistics
import time

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "performance-v2" / "strategy_performance.duckdb"
EPS = Decimal("1e-8")
ZERO = Decimal(0)
GRID = timedelta(hours=6)


@dataclass(frozen=True)
class Point:
    index: int
    time: datetime
    equity: Decimal


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}: expected timezone-aware UTC datetime (TIMESTAMPTZ)")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field}: expected UTC offset +00:00")
    return value.astimezone(timezone.utc)


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}: expected numeric equity")
    try:
        # Preserve DuckDB Decimal directly; other numeric values parse from
        # their text form to avoid a float round-trip.
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field}: malformed numeric equity") from exc
    if not result.is_finite():
        raise ValueError(f"{field}: non-finite equity")
    return result


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _days(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) / Decimal(86400) + Decimal(delta.microseconds) / Decimal(86_400_000_000)


def _hours(delta: timedelta) -> Decimal:
    return _days(delta) * Decimal(24)


def _pct(values: list[Decimal], q: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int((Decimal(len(ordered) - 1) * q).to_integral_value(rounding=ROUND_HALF_EVEN))))]


def _distribution(values: list[Decimal]) -> dict[str, object]:
    return {"count": len(values), "min": min(values) if values else None, "p50": _pct(values, Decimal("0.5")), "p90": _pct(values, Decimal("0.9")), "max": max(values) if values else None}


def _effective(points: list[Point]) -> tuple[list[datetime], list[Point]]:
    points = sorted(points, key=lambda p: (p.time, p.index))
    times: list[datetime] = []
    effective: list[Point] = []
    for point in points:
        if times and point.time == times[-1]:
            effective[-1] = point
        else:
            times.append(point.time)
            effective.append(point)
    return times, effective


def _equity_at(times: list[datetime], effective: list[Point], when: datetime) -> Decimal | None:
    index = bisect_right(times, when) - 1
    return None if index < 0 else effective[index].equity


def _grid(start: datetime, end: datetime, step_hours: int = 6) -> list[datetime]:
    step = timedelta(hours=step_hours)
    nodes = []
    node = start
    while node < end:
        nodes.append(node)
        node += step
    if not nodes or nodes[-1] != end:
        nodes.append(end)
    return nodes


def _metric(points: list[Point], start: datetime, end: datetime, width: int, step_hours: int = 6) -> dict[str, Decimal | int] | None:
    times, effective = _effective(points)
    base = _equity_at(times, effective, start)
    terminal = _equity_at(times, effective, end)
    if base is None or terminal is None or base <= ZERO or terminal <= ZERO:
        return None
    nodes = _grid(start, end, step_hours)
    values = [_equity_at(times, effective, node) for node in nodes]
    if any(value is None or value <= ZERO for value in values):
        return None
    values = [value for value in values if value is not None]
    with localcontext() as ctx:
        ctx.prec = 38
        logs = [(value / base).ln() for value in values]
        xs = [_days(node - start) for node in nodes]
        n = Decimal(len(xs))
        xbar = sum(xs, ZERO) / n
        ybar = sum(logs, ZERO) / n
        denom = sum(((x - xbar) ** 2 for x in xs), ZERO)
        slope = sum(((x - xbar) * (y - ybar) for x, y in zip(xs, logs)), ZERO) / denom
        path = sum((abs(b - a) for a, b in zip(logs, logs[1:])), ZERO)
        endpoint_log = logs[-1]
        er = ZERO if path == ZERO else endpoint_log / path
        # Risk path uses every raw observation in [start,end], retaining duplicates in sample_index order.
        raw = sorted((point for point in points if start <= point.time <= end), key=lambda p: (p.time, p.index))
        raw_path = list(raw)
        if not any(point.time == start for point in raw):
            raw_path.insert(0, Point(-1, start, base))
        if not any(point.time == end for point in raw):
            raw_path.append(Point(2**63 - 1, end, terminal))
        peak = ZERO
        drawdown = ZERO
        for point in raw_path:
            peak = max(peak, point.equity)
            if peak > ZERO:
                drawdown = max(drawdown, Decimal(1) - point.equity / peak)
        max_equity = max(point.equity for point in raw_path)
        peak_gap = Decimal(1) - terminal / max_equity if max_equity > ZERO else ZERO
        trend = Decimal(3000) * slope
        endpoint = Decimal(3000) * endpoint_log / Decimal(width)
        returns = Decimal(100) * (terminal / base - Decimal(1))
        dedup_path: list[Point] = []
        if not any(point.time == start for point in raw):
            dedup_path.append(Point(-1, start, base))
        dedup_path.extend(point for point in _effective(raw)[1])
        if not any(point.time == end for point in raw):
            dedup_path.append(Point(2**63 - 1, end, terminal))
        dedup_peak = ZERO
        dedup_dd = ZERO
        for point in dedup_path:
            dedup_peak = max(dedup_peak, point.equity)
            if dedup_peak > ZERO:
                dedup_dd = max(dedup_dd, Decimal(1) - point.equity / dedup_peak)
        dedup_peak_gap = Decimal(1) - terminal / max(point.equity for point in dedup_path)
        raw_logs = [(point.equity / base).ln() for point in raw_path]
        raw_path_len = sum((abs(b - a) for a, b in zip(raw_logs, raw_logs[1:])), ZERO)
        raw_er = ZERO if raw_path_len == ZERO else raw_logs[-1] / raw_path_len
    return {
        "trend30": trend, "endpoint30": endpoint, "return_pct": returns, "er": er,
        "drawdown": drawdown, "peak_gap": peak_gap, "grid_points": len(nodes),
        "raw_er": raw_er, "duplicate_dd_delta": drawdown - dedup_dd,
        "duplicate_peak_gap_delta": peak_gap - dedup_peak_gap,
    }


def _grid_er(points: list[Point], start: datetime, end: datetime, step_hours: int) -> Decimal | None:
    times, effective = _effective(points)
    base = _equity_at(times, effective, start)
    nodes = _grid(start, end, step_hours)
    values = [_equity_at(times, effective, node) for node in nodes]
    if base is None or base <= ZERO or any(value is None or value <= ZERO for value in values):
        return None
    with localcontext() as ctx:
        ctx.prec = 38
        logs = [(value / base).ln() for value in values if value is not None]
        path = sum((abs(b - a) for a, b in zip(logs, logs[1:])), ZERO)
        return ZERO if path == ZERO else logs[-1] / path


def analyze(meta: dict[str, object], source_points: list[Point], malformed: list[str] | None = None, grid_sensitivity: bool = False) -> dict[str, object]:
    start, end = meta["start"], meta["end"]
    assert isinstance(start, datetime) and isinstance(end, datetime)
    malformed = malformed or []
    points = sorted(source_points, key=lambda p: (p.time, p.index))
    out = [point for point in points if point.time < start or point.time > end]
    inside = [point for point in points if start <= point.time <= end]
    invalid = list(malformed)
    if end < start:
        invalid.append("INVALID_REPORT_RANGE")
    if out:
        invalid.append("EQUITY_OUTSIDE_REPORT_INTERVAL")
    indices = [point.index for point in points]
    if len(indices) != len(set(indices)):
        invalid.append("DUPLICATE_SAMPLE_INDEX")
    nonfinite = [point for point in points if not point.equity.is_finite()]
    if nonfinite:
        invalid.append("NONFINITE_EQUITY_SOURCE")
    nonpositive = [point for point in inside if point.equity.is_finite() and point.equity <= ZERO]
    result: dict[str, object] = {
        "strategy_id": meta["strategy_id"], "result_id": meta["result_id"], "symbol": meta["symbol"],
        "side": meta["side"], "timeframe": meta.get("timeframe"), "report_start": _iso(start), "report_end_T": _iso(end),
        "age_days": _days(end - start), "raw_equity_rows": len(points) + len(malformed), "in_report_rows": len(inside),
        "out_of_interval_rows": len(out), "nonpositive_in_report_rows": len(nonpositive), "invalid_reasons": sorted(set(invalid)),
        "first_raw_utc": _iso(points[0].time) if points else None, "last_raw_utc": _iso(points[-1].time) if points else None,
        "malformed_source_rows": len(malformed),
        "min_raw_minus_report_start_hours": min((_hours(p.time - start) for p in points), default=None),
        "max_raw_minus_T_hours": max((_hours(p.time - end) for p in points), default=None),
        "quiet_tail_hours": _hours(end - inside[-1].time) if inside else None,
        "max_sample_gap_hours": max((_hours(b.time - a.time) for a, b in zip(inside, inside[1:])), default=None),
        "leading_sample_delay_hours": _hours(inside[0].time - start) if inside else None,
        "max_carry_gap_hours": max([
            *([_hours(inside[0].time - start)] if inside else []),
            *[_hours(b.time - a.time) for a, b in zip(inside, inside[1:])],
            *([_hours(end - inside[-1].time)] if inside else []),
        ], default=None),
        "duplicate_timestamp_count": len(inside) - len({point.time for point in inside}),
        "windows": {}, "horizon": None, "state": None, "equity_class": None, "erf_disposition": None,
        "score12": None, "reason": None, "available_baselines": [], "grid_er_sensitivity": {},
    }
    if invalid:
        result.update(state="UNKNOWN_INVALID_SOURCE", reason="INVALID_OR_OUT_OF_INTERVAL_SOURCE", erf_disposition="NOT_EVALUATED", available_baselines=[])
        return result
    if nonpositive:
        result.update(state="NONPOSITIVE_EQUITY", reason="NONPOSITIVE_IN_REPORT_EQUITY", erf_disposition="BLOCK", available_baselines=[])
        return result
    age = end - start
    times, effective = _effective(inside)
    available = []
    for width in (28, 14, 7):
        boundary = end - timedelta(days=width)
        if start <= boundary and _equity_at(times, effective, boundary) is not None:
            available.append(width)
    result["available_baselines"] = available
    if not available:
        state = "INSUFFICIENT_HISTORY" if age < timedelta(days=7) else "MISSING_BASELINE"
        result.update(state=state, reason=state, erf_disposition="NOT_EVALUATED")
        return result
    horizon = max(available)
    widths = [width for width in (7, 14, 28) if width <= horizon]
    windows: dict[int, dict[str, Decimal | int]] = {}
    for width in widths:
        metrics = _metric(inside, end - timedelta(days=width), end, width)
        if metrics is None:
            invalid.append("INVALID_GRID_EQUITY")
        else:
            windows[width] = metrics
    if invalid:
        result.update(state="UNKNOWN_INVALID_SOURCE", reason="INVALID_OR_NONFINITE_GRID_EQUITY", invalid_reasons=sorted(set(invalid)), erf_disposition="NOT_EVALUATED")
        return result
    main = windows[horizon]
    growth = min(main["trend30"], main["endpoint30"])
    hup = main["trend30"] > EPS and main["endpoint30"] > EPS
    nondeclining = main["trend30"] >= -EPS and main["endpoint30"] >= -EPS
    flat = abs(main["trend30"]) <= EPS and abs(main["endpoint30"]) <= EPS
    if hup:
        short_decline = any(windows[w]["trend30"] < -EPS or windows[w]["endpoint30"] < -EPS for w in widths if w < horizon)
        state, equity_class = ("WEAKENING", 1) if short_decline else ("GROWING", 0)
        disposition = "PASS"
        reason = "SHORT_WINDOW_DECLINE" if short_decline else "H_UP_SHORTS_NONDECLINING"
    elif flat:
        state, equity_class, disposition, reason = "FLAT", 2, "BLOCK_IF_ERF_ENABLED", "H_FLAT"
    else:
        state, equity_class, disposition = "DECLINING_OR_MIXED", (2 if nondeclining else 3), "BLOCK"
        reason = "H_NONDECLINING_NOT_UP" if nondeclining else "H_DECLINING_OR_MIXED"
    q = max(ZERO, main["er"])
    with localcontext() as ctx:
        ctx.prec = 38
        if growth > EPS:
            score = growth * q * (Decimal(1) - main["drawdown"]) * (Decimal(1) - main["peak_gap"])
        elif abs(growth) <= EPS:
            score = ZERO
        else:
            score = growth * (Decimal(1) + main["drawdown"] + main["peak_gap"])
        score = score.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_EVEN)
    result.update(state=state, reason=reason, horizon=horizon, equity_class=equity_class, erf_disposition=disposition,
                  score12=score, windows=windows)
    result["selected_H_metrics"] = main
    if grid_sensitivity:
        result["grid_er_sensitivity"] = {
            "1h": _grid_er(inside, end - timedelta(days=horizon), end, 1),
            "3h": _grid_er(inside, end - timedelta(days=horizon), end, 3),
            "6h": main["er"],
        }
    return result


def _metadata(connection: duckdb.DuckDBPyConnection) -> tuple[list[dict[str, object]], dict[str, object], str]:
    rows = connection.execute(
        """select s.strategy_id,s.strategy_name,s.symbol,s.side,s.timeframe,r.result_id,
                  r.report_start_utc,r.report_end_utc,r.imported_at_utc
             from strategies s join strategy_results r
               on r.result_id=s.current_result_id and r.strategy_id=s.strategy_id
            where s.lifecycle_status='ACTIVE' order by s.symbol,s.side,s.strategy_id"""
    ).fetchall()
    candidates = []
    digest = hashlib.sha256()
    for strategy_id, name, symbol, side, timeframe, result_id, raw_start, raw_end, raw_imported in rows:
        start, end = _utc(raw_start, "report_start_utc"), _utc(raw_end, "report_end_utc")
        imported = _utc(raw_imported, "imported_at_utc")
        item = {"strategy_id": int(strategy_id), "strategy_name": str(name), "symbol": str(symbol), "side": str(side),
                "timeframe": str(timeframe), "result_id": int(result_id), "start": start, "end": end, "imported_at": imported}
        candidates.append(item)
        digest.update("|".join((str(strategy_id), str(result_id), str(symbol), str(side), str(timeframe), _iso(start), _iso(end), _iso(imported))).encode())
        digest.update(b"\n")
    ages = [_days(item["end"] - item["start"]) for item in candidates]
    ends = Counter(_iso(item["end"]) for item in candidates)
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for item in candidates:
        groups[(item["symbol"], item["side"])].append(item)
    group_rows = []
    for (symbol, side), members in sorted(groups.items()):
        end_counts = Counter(_iso(row["end"]) for row in members)
        group_rows.append({"symbol": symbol, "side": side, "active_candidates": len(members), "distinct_T": len(end_counts), "report_end_counts": dict(sorted(end_counts.items()))})
    age_bins = {"under_7d": 0, "7_to_14d": 0, "14_to_28d": 0, "at_least_28d": 0}
    for age in ages:
        age_bins["under_7d" if age < 7 else "7_to_14d" if age < 14 else "14_to_28d" if age < 28 else "at_least_28d"] += 1
    summary = {
        "active_candidates": len(candidates), "pair_side_groups": len(groups),
        "report_age_days": {"min": min(ages) if ages else None, "p50": _pct(ages, Decimal(".5")), "p90": _pct(ages, Decimal(".9")), "max": max(ages) if ages else None, **age_bins},
        "report_end_counts_utc": dict(sorted(ends.items())), "timeframes": dict(Counter(item["timeframe"] for item in candidates)),
        "pair_side_T_metadata": group_rows,
        "common_T_gate": "NOT APPLICABLE; each candidate uses its own stored report_end_utc T",
    }
    return candidates, summary, digest.hexdigest()


def _even(items: list[dict[str, object]], count: int) -> list[dict[str, object]]:
    if len(items) <= count:
        return list(items)
    return [items[min(len(items) - 1, int((Decimal(i) + Decimal("0.5")) * len(items) / count))] for i in range(count)]


def _select(candidates: list[dict[str, object]], full: bool, per_pair: int, limit: int | None) -> tuple[list[dict[str, object]], dict[str, object]]:
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for item in candidates:
        groups[(item["symbol"], item["side"])].append(item)
    selected = list(candidates) if full else [row for members in groups.values() for row in _even(members, per_pair)]
    selected.sort(key=lambda row: row["result_id"])
    before_limit = len(selected)
    if limit is not None:
        selected = _even(selected, limit)
    ids = [item["result_id"] for item in selected]
    if len(ids) != len(set(ids)) or not set(ids).issubset({item["result_id"] for item in candidates}):
        raise RuntimeError("selection duplicate/drop audit failed")
    selected_groups = Counter((item["symbol"], item["side"]) for item in selected)
    group_detail = []
    for key, members in sorted(groups.items()):
        expected = len(members) if full else min(per_pair, len(members))
        actual = selected_groups[key]
        reason = "all ACTIVE rows (--full)" if full else "evenly spaced Pair+Side sample" if len(members) > per_pair else "all members; group smaller than sample cap"
        if actual < expected:
            reason += "; reduced by global --limit"
        group_detail.append({"symbol": key[0], "side": key[1], "active": len(members), "expected_before_global_limit": expected, "selected": actual, "reason": reason})
    expected_after = min(before_limit, limit) if limit is not None else before_limit
    if len(selected) != expected_after:
        raise RuntimeError("selection count mismatch")
    return selected, {"expected_before_global_limit": before_limit, "expected_after_global_limit": expected_after,
                     "actual": len(selected), "unique_result_ids": len(set(ids)), "silent_drop_or_double_count": False,
                     "groups": group_detail}


def _timestamp_contract(connection: duckdb.DuckDBPyConnection) -> dict[str, object]:
    expected = {"strategy_results.report_start_utc", "strategy_results.report_end_utc", "strategy_results.imported_at_utc", "strategy_equity.timestamp_utc", "strategy_actions.timestamp_utc"}
    rows = connection.execute("""select table_name,column_name,data_type from information_schema.columns where table_schema='main' and
        ((table_name='strategy_results' and column_name in ('report_start_utc','report_end_utc','imported_at_utc')) or
         (table_name in ('strategy_equity','strategy_actions') and column_name='timestamp_utc'))""").fetchall()
    actual = {f"{table}.{column}": kind for table, column, kind in rows}
    if set(actual) != expected or any(kind != "TIMESTAMP WITH TIME ZONE" for kind in actual.values()):
        raise SystemExit(f"R7.3 timestamp schema invariant mismatch: {actual}")
    return {"types": dict(sorted(actual.items())), "epoch_unit": "not applicable; native DuckDB TIMESTAMPTZ values, not numeric epoch values",
            "timezone": "decoded values checked as aware offset +00:00 and normalized to UTC", "equity_timestamp_reads": "checked at decode for every selected raw row"}


def _database_info(connection: duckdb.DuckDBPyConnection, path: Path) -> dict[str, object]:
    schema = dict(connection.execute("select key,value from schema_info").fetchall())
    catalog = connection.execute("select table_name,column_name,data_type from information_schema.columns where table_schema='main' order by table_name,ordinal_position").fetchall()
    counts = {table: int(connection.execute(f"select count(*) from {table}").fetchone()[0]) for table in ("strategy_equity", "strategy_actions", "window_metrics")}
    return {"size_bytes": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns, "schema_info": schema,
            "catalog_sha256": hashlib.sha256(json.dumps(catalog, separators=(",", ":")).encode()).hexdigest(), "row_counts": counts}


def _assert_result_accounting(expected_ids: list[int], emitted_ids: list[int], per_result_rows: dict[int, int], scanned_rows: int) -> None:
    if (len(expected_ids) != len(set(expected_ids)) or len(emitted_ids) != len(set(emitted_ids)) or
            set(expected_ids) != set(emitted_ids) or set(expected_ids) != set(per_result_rows) or
            sum(per_result_rows.values()) != scanned_rows):
        raise RuntimeError("streamed per-result row accounting mismatch")


def _load_and_analyze(connection: duckdb.DuckDBPyConnection, candidates: list[dict[str, object]], grid_sensitivity: bool) -> tuple[list[dict[str, object]], dict[str, object]]:
    if candidates:
        placeholders = ",".join("?" for _ in candidates)
        rows = connection.execute(f"select result_id,sample_index,timestamp_utc,equity from strategy_equity where result_id in ({placeholders}) order by result_id,sample_index", [row["result_id"] for row in candidates])
    else:
        rows = iter(())
    results = []
    current_id = None
    group: list[Point] = []
    malformed: list[str] = []
    scanned_rows = 0
    group_rows = 0
    per_result_rows: dict[int, int] = {}
    finalized_ids: set[int] = set()
    by_id = {int(item["result_id"]): item for item in candidates}

    def finish(result_id: int, points: list[Point], bad: list[str], raw_rows: int) -> dict[str, object]:
        if result_id not in by_id or result_id in finalized_ids or raw_rows != len(points) + len(bad):
            raise RuntimeError("invalid or duplicate streamed result group")
        finalized_ids.add(result_id)
        per_result_rows[result_id] = raw_rows
        meta = by_id[result_id]
        result = analyze(meta, points, bad, grid_sensitivity)
        valid_times = [point.time for point in points]
        result["timestamp_offsets_hours"] = {
            "min_equity_minus_T": min((_hours(value - meta["end"]) for value in valid_times), default=None),
            "max_equity_minus_T": max((_hours(value - meta["end"]) for value in valid_times), default=None),
            "min_equity_minus_report_start": min((_hours(value - meta["start"]) for value in valid_times), default=None),
            "max_equity_minus_report_start": max((_hours(value - meta["start"]) for value in valid_times), default=None),
        }
        return result

    # ORDER BY keeps a result contiguous even across fetchmany boundaries.
    # Only one result's points are retained; group_rows counts every source row,
    # including malformed rows, until its transition or the final flush.
    while True:
        chunk = rows.fetchmany(8192) if hasattr(rows, "fetchmany") else []
        if not chunk:
            break
        for result_id_raw, sample_index, stamp_raw, equity_raw in chunk:
            result_id = int(result_id_raw)
            if result_id not in by_id:
                raise RuntimeError("equity query returned an unselected result")
            if current_id is not None and result_id != current_id:
                results.append(finish(current_id, group, malformed, group_rows))
                group, malformed = [], []
                group_rows = 0
            if result_id in finalized_ids:
                raise RuntimeError("ORDER BY result group was not contiguous")
            current_id = result_id
            scanned_rows += 1
            group_rows += 1
            try:
                stamp = _utc(stamp_raw, f"strategy_equity[{result_id}].timestamp_utc")
            except ValueError as exc:
                malformed.append(str(exc))
                continue
            try:
                equity = _decimal(equity_raw, f"strategy_equity[{result_id}].equity")
            except ValueError as exc:
                malformed.append(str(exc))
                continue
            group.append(Point(int(sample_index), stamp, equity))
    if current_id is not None:
        results.append(finish(current_id, group, malformed, group_rows))
    seen = set(per_result_rows)
    for meta in candidates:
        if meta["result_id"] not in seen:
            results.append(finish(int(meta["result_id"]), [], [], 0))
    results.sort(key=lambda item: item["result_id"])
    emitted_ids = [int(item["result_id"]) for item in results]
    expected_ids = [int(item["result_id"]) for item in candidates]
    _assert_result_accounting(expected_ids, emitted_ids, per_result_rows, scanned_rows)
    return results, {"selected_results": len(candidates), "equity_queries": int(bool(candidates)), "equity_rows_scanned": scanned_rows,
                     "action_queries": 0, "action_rows_scanned": 0, "equity_fact_query_count": int(bool(candidates)),
                     "bounded_memory": "one result group plus fetchmany(8192); metadata/result summary and per-result row counts retained",
                     "per_result_row_accounting": {"result_ids_flushed": len(per_result_rows), "equity_rows_accounted": sum(per_result_rows.values()),
                                                   "final_group_flushed": current_id is not None,
                                                   "ordering": "ORDER BY result_id,sample_index; groups remain contiguous across fetchmany chunks",
                                                   "silent_drop_or_duplicate": False}}


def _rank(results: list[dict[str, object]], top_n: int, full_group_sizes: dict[tuple[str, str], int]) -> dict[str, object]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    input_group_sizes = Counter((row["symbol"], row["side"]) for row in results)
    for item in results:
        if item["score12"] is not None:
            grouped[(item["symbol"], item["side"])].append(item)
    moved = top_changed = candidate_count = 0
    summaries = []
    deltas = []
    complete_top_changed = 0
    complete_groups = 0
    sampled_groups = 0
    for key, members in sorted(grouped.items()):
        complete_input = input_group_sizes[key] == full_group_sizes.get(key, input_group_sizes[key])
        complete_groups += int(complete_input)
        sampled_groups += int(not complete_input)
        class_order = sorted(members, key=lambda row: (row["equity_class"], -row["score12"], row["selected_H_metrics"]["drawdown"], row["selected_H_metrics"]["peak_gap"], -row["horizon"], row["strategy_id"]))
        score_order = sorted(members, key=lambda row: (-row["score12"], row["selected_H_metrics"]["drawdown"], row["selected_H_metrics"]["peak_gap"], -row["horizon"], row["strategy_id"]))
        class_rank = {row["strategy_id"]: i + 1 for i, row in enumerate(class_order)}
        score_rank = {row["strategy_id"]: i + 1 for i, row in enumerate(score_order)}
        for row in members:
            row["equity_rank_diagnostic"] = class_rank[row["strategy_id"]]
            row["score_only_rank_diagnostic"] = score_rank[row["strategy_id"]]
            row["rank_delta_vs_score_only"] = class_rank[row["strategy_id"]] - score_rank[row["strategy_id"]]
            deltas.append(row)
        changed = len(set(row["strategy_id"] for row in class_order[:top_n]) ^ set(row["strategy_id"] for row in score_order[:top_n]))
        moved += sum(row["rank_delta_vs_score_only"] != 0 for row in members)
        top_changed += changed // 2
        if complete_input:
            complete_top_changed += changed // 2
        candidate_count += len(members)
        summaries.append({"symbol": key[0], "side": key[1], "rankable_selected": len(members), "top_n": min(top_n, len(members)),
                          "complete_input_group": complete_input, "top_n_members_changed_sample": changed // 2})
    return {"selected_rankable_candidates": candidate_count, "candidates_moved_vs_score_only": moved,
            "top_n_members_changed_vs_score_only": top_changed, "groups": summaries,
            "complete_input_groups": complete_groups, "partially_sampled_groups": sampled_groups,
            "top_n_members_changed_in_complete_input_groups": complete_top_changed,
            "largest_absolute_rank_movements": [
                {key: row.get(key) for key in ("strategy_id", "result_id", "symbol", "side", "state", "equity_class", "score12", "horizon", "equity_rank_diagnostic", "score_only_rank_diagnostic", "rank_delta_vs_score_only")}
                for row in sorted(deltas, key=lambda row: (abs(row["rank_delta_vs_score_only"]), row["strategy_id"]), reverse=True)[:12]
                if row["rank_delta_vs_score_only"] != 0
            ],
            "unscoreable_reserve_candidates": sum(row["score12"] is None for row in results),
            "scope_warning": "sample-based rank diagnostics unless --full; no legacy production ranking baseline implied"}


def _summarize(candidates: list[dict[str, object]], results: list[dict[str, object]], top_n: int) -> dict[str, object]:
    ages = {"under_7d": [], "7_to_14d": [], "14_to_28d": [], "at_least_28d": []}
    for row in results:
        age = row["age_days"]
        key = "under_7d" if age < 7 else "7_to_14d" if age < 14 else "14_to_28d" if age < 28 else "at_least_28d"
        ages[key].append(row)
    def summary(rows: list[dict[str, object]]) -> dict[str, object]:
        states = Counter(row["state"] for row in rows)
        available = Counter(str(max(row["available_baselines"])) if row["available_baselines"] else "none" for row in rows)
        baseline_window_counts = {str(width): sum(width in row["available_baselines"] for row in rows) for width in (28, 14, 7)}
        age_ge7 = [row for row in rows if row["age_days"] >= 7]
        return {"denominator": len(rows), "states": dict(states), "state_pct": {key: Decimal(count) * 100 / len(rows) for key, count in states.items()} if rows else {},
                "available_H_distribution": dict(available), "available_baseline_counts_by_width": baseline_window_counts,
                "invalid_or_out_of_interval": sum(row["state"] == "UNKNOWN_INVALID_SOURCE" for row in rows),
                "nonpositive_block": sum(row["state"] == "NONPOSITIVE_EQUITY" for row in rows),
                "under_7_insufficient_history": sum(row["state"] == "INSUFFICIENT_HISTORY" for row in rows),
                "missing_baseline": sum(row["state"] == "MISSING_BASELINE" for row in rows),
                "coverage_denominator_age_ge_7": len(age_ge7),
                "max_gap_hours": _distribution([row["max_sample_gap_hours"] for row in rows if row["max_sample_gap_hours"] is not None]),
                "max_carry_gap_hours": _distribution([row["max_carry_gap_hours"] for row in rows if row["max_carry_gap_hours"] is not None]),
                "leading_sample_delay_hours": _distribution([row["leading_sample_delay_hours"] for row in rows if row["leading_sample_delay_hours"] is not None]),
                "time_since_last_sample_to_T_hours": _distribution([row["quiet_tail_hours"] for row in rows if row["quiet_tail_hours"] is not None]),
                "raw_rows": sum(row["raw_equity_rows"] for row in rows), "in_report_raw_rows": sum(row["in_report_rows"] for row in rows),
                "out_of_interval_raw_rows": sum(row["out_of_interval_rows"] for row in rows), "malformed_source_rows": sum(row["malformed_source_rows"] for row in rows),
                "duplicate_timestamp_count": sum(row["duplicate_timestamp_count"] for row in rows),
                "quiet_tail_gt_6h": sum((row["quiet_tail_hours"] or ZERO) > 6 for row in rows),
                "quiet_tail_gt_24h": sum((row["quiet_tail_hours"] or ZERO) > 24 for row in rows),
                "class_counts": dict(Counter(str(row["equity_class"]) for row in rows if row["equity_class"] is not None))}
    duplicate_dd_deltas = [abs(row["selected_H_metrics"]["duplicate_dd_delta"]) for row in results if row.get("selected_H_metrics")]
    duplicate_p_deltas = [abs(row["selected_H_metrics"]["duplicate_peak_gap_delta"]) for row in results if row.get("selected_H_metrics")]
    raw_grid_er_delta = [abs(row["selected_H_metrics"]["raw_er"] - row["selected_H_metrics"]["er"]) for row in results if row.get("selected_H_metrics")]
    sensitivity_rows = [row for row in results if row.get("grid_er_sensitivity")]
    sensitivity = {}
    for step in ("1h", "3h", "6h"):
        values = [row["grid_er_sensitivity"][step] for row in sensitivity_rows]
        deltas = [abs(row["grid_er_sensitivity"][step] - row["grid_er_sensitivity"]["6h"]) for row in sensitivity_rows]
        sensitivity[step] = {"ER": _distribution(values), "absolute_delta_vs_6h": _distribution(deltas)}
    by_timeframe = {}
    for timeframe in sorted({str(row.get("timeframe")) for row in results}):
        by_timeframe[timeframe] = summary([row for row in results if str(row.get("timeframe")) == timeframe])
    by_tail = {}
    for bucket, predicate in (("0_to_6h", lambda x: x is not None and x <= 6), ("6_to_24h", lambda x: x is not None and 6 < x <= 24), ("over_24h", lambda x: x is not None and x > 24), ("no_in_report_samples", lambda x: x is None)):
        by_tail[bucket] = summary([row for row in results if predicate(row["quiet_tail_hours"])])
    grid_rows_by_window = {str(width): sum(int(row["windows"].get(width, {}).get("grid_points", 0)) for row in results) for width in (7, 14, 28)}
    return {"overall": summary(results), "by_report_age": {key: summary(value) for key, value in ages.items()},
            "by_timeframe": by_timeframe, "by_quiet_tail_duration": by_tail,
            "grid_node_counts_by_window": grid_rows_by_window,
            "grid_node_count_selected_H": sum(int(row.get("selected_H_metrics", {}).get("grid_points", 0)) for row in results),
            "trip_rate_strata": "not collected: no action queries or action-derived facts in this R7.3 diagnostic",
            "duplicate_raw_D_delta_abs_distribution": _distribution(duplicate_dd_deltas),
            "duplicate_raw_P_delta_abs_distribution": _distribution(duplicate_p_deltas),
            "raw_vs_6h_grid_ER_abs_delta": _distribution(raw_grid_er_delta),
            "grid_ER_sensitivity": sensitivity if sensitivity_rows else "not requested",
            "short_decline_only_blocks": 0, "H_UP_denominator": sum(row["state"] in ("GROWING", "WEAKENING") for row in results),
            "ranking": _rank(results, top_n, Counter((item["symbol"], item["side"]) for item in candidates)),
            "gap_interpretation": "gaps and quiet terminal tails are carried right-continuously and never invalidate equity facts; tail duration is not proof that underlying equity was economically flat"}


def _self_check() -> dict[str, object]:
    end = datetime(2026, 1, 29, tzinfo=timezone.utc)
    start = end - timedelta(days=28)
    meta = {"strategy_id": 1, "result_id": 1, "symbol": "X", "side": "LONG", "timeframe": "5m", "start": start, "end": end}
    pts = [Point(i, start + timedelta(hours=6 * i), Decimal(1000 + 10 * (i // 4))) for i in range(113)]
    def a(items, m=meta): return analyze(m, items)
    growing = a(pts)
    four_hour = dict(meta, timeframe="4h")
    four_hour_result = a(pts, four_hour)
    assert all(growing[key] == four_hour_result[key] for key in ("state", "equity_class", "erf_disposition", "score12"))
    # Quiet tail is just carry-forward: long H remains UP and flat short windows still PASS.
    quiet = [p for p in pts if p.time <= end - timedelta(days=14)]
    tail = a(quiet)
    assert tail["state"] == "GROWING" and tail["erf_disposition"] == "PASS" and tail["quiet_tail_hours"] == Decimal(336)
    short_loss = [Point(p.index, p.time, Decimal(1180) if p.time >= end - timedelta(days=7) else p.equity) for p in pts]
    short_loss[-1] = Point(short_loss[-1].index, end, Decimal(1170))
    weakening = a(short_loss)
    assert weakening["state"] == "WEAKENING" and weakening["erf_disposition"] == "PASS"
    flat = [Point(i, p.time, Decimal(1000)) for i, p in enumerate(pts)]
    assert a(flat)["state"] == "FLAT" and a(flat)["erf_disposition"] == "BLOCK_IF_ERF_ENABLED"
    assert a([Point(0, start, Decimal(1000))])["state"] == "FLAT"  # one baseline fills all W
    sparse = [Point(0, start, Decimal(1000)), Point(1, start + timedelta(days=9), Decimal(1300)), Point(2, start + timedelta(days=10), Decimal(1350))]
    assert a(sparse)["state"] == "GROWING"  # arbitrary internal/final gaps carry
    assert a([Point(0, end - timedelta(days=28), Decimal(1000)), Point(1, end, Decimal(1100))])["available_baselines"] == [28, 14, 7]
    boundary = end - timedelta(days=28)
    duplicate_low_high = [Point(0, boundary, Decimal(500)), Point(1, boundary, Decimal(1200)), Point(2, end, Decimal(1000))]
    duplicate_high_low = [Point(0, boundary, Decimal(1200)), Point(1, boundary, Decimal(500)), Point(2, end, Decimal(1000))]
    m1, m2 = _metric(duplicate_low_high, boundary, end, 28), _metric(duplicate_high_low, boundary, end, 28)
    assert m1 and m2 and m1["drawdown"] < m2["drawdown"] and m2["duplicate_dd_delta"] > m1["duplicate_dd_delta"]
    duplicate_T_low_high = [Point(0, start, Decimal(1000)), Point(1, end, Decimal(500)), Point(2, end, Decimal(1200))]
    duplicate_T_high_low = [Point(0, start, Decimal(1000)), Point(1, end, Decimal(1200)), Point(2, end, Decimal(500))]
    t_low_high = _metric(duplicate_T_low_high, start, end, 28)
    t_high_low = _metric(duplicate_T_high_low, start, end, 28)
    assert t_low_high and t_high_low and t_low_high["endpoint30"] > t_high_low["endpoint30"]
    spike = [Point(0, boundary, Decimal(1000)), Point(1, boundary + timedelta(days=10), Decimal(1500)), Point(2, end, Decimal(1000))]
    assert _metric(spike, boundary, end, 28)["drawdown"] > ZERO and _metric(spike, boundary, end, 28)["peak_gap"] > ZERO
    scaled = [Point(p.index, p.time, p.equity * 7) for p in pts]
    original_metrics, scaled_metrics = _metric(pts, start, end, 28), _metric(scaled, start, end, 28)
    assert all(original_metrics[key] == scaled_metrics[key] for key in ("trend30", "endpoint30", "er", "drawdown", "peak_gap", "return_pct"))
    # Rows outside [report_start,T] invalidate; neither may establish a baseline.
    assert a([Point(0, start - timedelta(seconds=1), Decimal(1000)), Point(1, end, Decimal(1100))])["state"] == "UNKNOWN_INVALID_SOURCE"
    future_meta = dict(meta, start=end - timedelta(days=28))
    assert analyze(future_meta, [Point(1, end, Decimal(1100))])["state"] == "MISSING_BASELINE"
    under7 = dict(meta, start=end - timedelta(days=6))
    neg = [Point(0, end - timedelta(days=3), Decimal(-1))]
    assert analyze(under7, neg)["state"] == "NONPOSITIVE_EQUITY"
    assert a([Point(0, start, Decimal(1000)), Point(1, end - timedelta(days=1), ZERO), Point(2, end, Decimal(1100))])["state"] == "NONPOSITIVE_EQUITY"
    no_baseline_with_negative = [Point(0, end, Decimal(-1))]
    assert analyze(future_meta, no_baseline_with_negative)["state"] == "NONPOSITIVE_EQUITY"
    older = dict(meta, start=end - timedelta(days=60))
    assert analyze(older, [Point(0, older["start"], Decimal(-1)), Point(1, end - timedelta(days=28), Decimal(1000)), Point(2, end, Decimal(1100))])["state"] == "NONPOSITIVE_EQUITY"
    assert a([Point(0, start, Decimal("NaN"))])["state"] == "UNKNOWN_INVALID_SOURCE"
    # Structural invalidity wins over a colliding in-report nonpositive value.
    assert a([Point(0, start - timedelta(seconds=1), Decimal(1000)), Point(1, start, Decimal(-1)), Point(2, end, Decimal(1100))])["state"] == "UNKNOWN_INVALID_SOURCE"
    assert a([Point(0, start, Decimal("NaN")), Point(1, end, Decimal(-1))])["state"] == "UNKNOWN_INVALID_SOURCE"
    assert a([Point(0, start, Decimal(1000)), Point(0, end, Decimal(-1))])["state"] == "UNKNOWN_INVALID_SOURCE"
    assert analyze(meta, [Point(0, start, Decimal(-1))], ["malformed numeric equity"])["state"] == "UNKNOWN_INVALID_SOURCE"
    tiny = Decimal("1e-400")
    assert _decimal(tiny, "self-check") is tiny and tiny > ZERO
    tiny_result = a([Point(0, start, tiny)])
    assert tiny_result["state"] == "FLAT" and tiny_result["nonpositive_in_report_rows"] == 0
    negative_zero = a([Point(0, start, Decimal("-0"))])
    assert negative_zero["state"] == "NONPOSITIVE_EQUITY" and negative_zero["nonpositive_in_report_rows"] == 1
    # A nonpositive observation before H, but inside report, still blocks before any baseline/log.
    assert a([Point(0, start, Decimal(-1)), Point(1, end - timedelta(days=28), Decimal(1000)), Point(2, end, Decimal(1100))])["state"] == "NONPOSITIVE_EQUITY"
    ages = {}
    for days in ("6.99", "7", "13.99", "14", "27.99", "28"):
        aged = dict(meta, start=end - timedelta(seconds=int(Decimal(days) * 86400)))
        ages[days] = analyze(aged, [Point(0, aged["start"], Decimal(1000)), Point(1, end, Decimal(1010))])["horizon"]
    assert ages == {"6.99": None, "7": 7, "13.99": 7, "14": 14, "27.99": 14, "28": 28}
    mixed = [dict(meta, end=end), dict(meta, strategy_id=2, result_id=2, end=end - timedelta(days=1))]
    assert len({row["end"] for row in mixed}) == 2  # distinct T is valid; no common-T preflight.
    # The accounting assertion includes empty results and the final flushed group.
    _assert_result_accounting([1, 2, 3], [1, 2, 3], {1: 2, 2: 0, 3: 1}, 3)
    checks = {"five_minute_and_four_hour_labels_identical": all(growing[key] == four_hour_result[key] for key in ("state", "equity_class", "erf_disposition", "score12")), "quiet_tail_H_UP_short_flat_PASS": tail["state"],
              "H_UP_short_decline": weakening["state"], "H_flat": "FLAT/BLOCK_IF_ERF_ENABLED",
              "single_baseline_fills_H": "FLAT", "arbitrary_gaps_carried": a(sparse)["state"],
              "exact_left_boundary_and_duplicate_order": "PASS", "outside_interval_invalid": "PASS",
              "future_only_no_baseline": "MISSING_BASELINE", "nonpositive_precedence": "PASS",
              "invalid_collision_precedence": "PASS", "nonfinite_invalid": "PASS",
              "decimal_no_float_roundtrip": "PASS", "tiny_positive_not_nonpositive": tiny_result["state"],
              "negative_zero_is_nonpositive": negative_zero["state"], "stream_accounting_final_flush": "PASS",
              "H_availability_age_boundaries": ages,
              "duplicate_DD_delta_high_to_low": m2["duplicate_dd_delta"], "duplicate_T_order": "PASS", "grid_logging_invariant": "PASS",
              "mixed_T_no_common_gate": "PASS", "short_decline_only_blocks": 0}
    return {"assertions": "PASS", "contract": "R7.3", "checks": checks}


def _json(value: object) -> object:
    if isinstance(value, Decimal): return format(value, "f")
    if isinstance(value, datetime): return _iso(value)
    if isinstance(value, dict): return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_json(v) for v in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only M0 evidence scan for Performance v2 equity quality R7.3.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--full", action="store_true", help="Scan equity rows for every ACTIVE current result.")
    parser.add_argument("--per-pair", type=int, default=8, help="Deterministic evenly spaced result sample per Pair+Side.")
    parser.add_argument("--limit", type=int, help="Further evenly sampled cap on selected result IDs.")
    parser.add_argument("--grid-sensitivity", action="store_true", help="Also report 1h/3h/6h ER sensitivity.")
    parser.add_argument("--benchmark-repeats", action="store_true", help="One warmup plus three read-only R7.3 diagnostic scans.")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--include-results", action="store_true", help="Include each sampled candidate row in JSON output.")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.per_pair < 1 or args.top_n < 1 or (args.limit is not None and args.limit < 1): parser.error("--per-pair, --top-n and --limit must be positive")
    if args.full and args.limit is not None: parser.error("--full and --limit cannot be combined")
    if args.self_check:
        print(json.dumps(_json(_self_check()), indent=2, sort_keys=True))
        return 0
    db = args.database.resolve(strict=True)
    before_stat = db.stat()
    started = time.perf_counter()
    with duckdb.connect(str(db), read_only=True) as con:
        con.execute("set threads=1")
        con.execute("set timezone='UTC'")
        info_before = _database_info(con, db)
        schema = info_before["schema_info"]
        if schema.get("database_kind") != "unified_performance_v2" or schema.get("schema_version") != "5":
            raise SystemExit("requires unified_performance_v2 schema 5 (read-only)")
        timestamp_contract = _timestamp_contract(con)
        candidates, corpus, identity = _metadata(con)
        selected, selection_audit = _select(candidates, args.full, args.per_pair, args.limit)
        runs = []
        try:
            import psutil
            process = psutil.Process()
        except ImportError:
            process = None
        count = 4 if args.benchmark_repeats else 1
        for _ in range(count):
            before = time.perf_counter()
            results, scan = _load_and_analyze(con, selected, args.grid_sensitivity)
            runs.append({"wall_seconds": time.perf_counter() - before, "rss_bytes_after_scan": process.memory_info().rss if process else None,
                         "scan": scan, "results": results})
        benchmark = None
        if args.benchmark_repeats:
            benchmark = {"warmup_runs": 1, "measured_runs": 3, "wall_seconds_measured": [item["wall_seconds"] for item in runs[1:]],
                         "median_wall_seconds": statistics.median(item["wall_seconds"] for item in runs[1:]),
                         "rss_bytes_after_each_run": [item["rss_bytes_after_scan"] for item in runs],
                         "equity_query_counts_per_run": [item["scan"]["equity_fact_query_count"] for item in runs],
                         "equity_rows_per_run": [item["scan"]["equity_rows_scanned"] for item in runs],
                         "action_queries_and_rows_per_run": 0, "writes": 0, "cache_mode": "not applicable; raw equity diagnostic only"}
        final_results, final_scan = runs[-1]["results"], runs[-1]["scan"]
        output = {"contract": "R7.3", "database_before": info_before, "timestamp_contract": timestamp_contract,
                  "corpus": corpus, "identity_digest_sha256_including_T": identity,
                  "selection": selection_audit, "selection_mode": "full" if args.full else "bounded_deterministic",
                  "scan": final_scan, "equity_quality": _summarize(candidates, final_results, args.top_n),
                  "result_samples": final_results if args.include_results else None,
                  "result_details_omitted": not args.include_results, "benchmark": benchmark,
                  "diagnostic_runtime_seconds_including_metadata": time.perf_counter() - started,
                  "source_queries": {"metadata_queries": 1, "equity_fact_queries_last_scan": final_scan["equity_fact_query_count"],
                                     "action_fact_queries": 0, "action_fact_rows": 0,
                                     "invariant_query_accounting": "database_before/after separately read schema/catalog and aggregate COUNT(*) for 3 tables (including action table); no action fact rows were read/materialized by equity calculation"}}
    after_stat = db.stat()
    with duckdb.connect(str(db), read_only=True) as con:
        con.execute("set threads=1")
        con.execute("set timezone='UTC'")
        info_after = _database_info(con, db)
    output["database_after"] = info_after
    output["database_unchanged"] = (before_stat.st_size == after_stat.st_size and before_stat.st_mtime_ns == after_stat.st_mtime_ns and
                                    info_before["catalog_sha256"] == info_after["catalog_sha256"] and info_before["row_counts"] == info_after["row_counts"])
    print(json.dumps(_json(output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
