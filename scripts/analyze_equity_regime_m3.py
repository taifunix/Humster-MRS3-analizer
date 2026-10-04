#!/usr/bin/env python
"""Read-only, descriptive Equity M3 research on current Performance v2 results.

The threshold grid is a proposal. This script never writes to DuckDB or cache.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mrs3.performance_v2_equity_quality import EquitySample, calculate_equity_quality_facts
from mrs3.performance_v2_equity_cache import decode_equity_facts, equity_source_revision
from mrs3.performance_v2_store import load_performance_v2_config, performance_v2_database_path

D = Decimal
ZERO = D(0)
PRE_MIN = timedelta(days=14)
DIRECTION_GRID = ("0.00000001", "0.25", "0.5", "1", "2")
ABS_GRID = ("0", "0.25", "0.5", "1", "2", "5")
REL_GRID = ("0", "0.1", "0.2", "0.3", "0.5")


def _days(delta: timedelta) -> Decimal:
    return D(delta.days * 86400 + delta.seconds) / D(86400) + D(delta.microseconds) / D(86400000000)


def _utc(stamp: object) -> datetime:
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError("invalid_utc_timestamp")
    return stamp.astimezone(timezone.utc)


def _jsonable(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value


def _canonical(value) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def direction(metric: dict | None, epsilon: Decimal) -> str | None:
    if metric is None:
        return None
    t, e = D(str(metric["trend30"])), D(str(metric["endpoint30"]))
    if t > epsilon and e > epsilon:
        return "UP"
    if t < -epsilon and e < -epsilon:
        return "DOWN"
    if abs(t) <= epsilon and abs(e) <= epsilon:
        return "FLAT"
    return "MIXED"


def _window_dict(facts) -> dict:
    return {str(w.days): {"trend30": str(w.trend30), "endpoint30": str(w.endpoint30),
                          "return_pct": str(w.return_pct), "er": str(w.er),
                          "grid_points": w.grid_points} for w in facts.windows}


def _variable_metric(times: list[datetime], values: list[Decimal], left: datetime, right: datetime) -> dict | None:
    if not times or left >= right or times[0] > left:
        return None
    nodes = [right]
    while nodes[-1] - timedelta(hours=6) > left:
        nodes.append(nodes[-1] - timedelta(hours=6))
    nodes.append(left)
    nodes.reverse()
    sampled = [values[bisect_right(times, t) - 1] for t in nodes]
    with localcontext() as ctx:
        ctx.prec = 38
        xs = [_days(t - left) for t in nodes]
        logs = [(v / sampled[0]).ln() for v in sampled]
        n = D(len(xs))
        xm = sum(xs, ZERO) / n
        ym = sum(logs, ZERO) / n
        slope = sum(((x - xm) * (y - ym) for x, y in zip(xs, logs)), ZERO) / sum(((x - xm) ** 2 for x in xs), ZERO)
        path = sum((abs(b - a) for a, b in zip(logs, logs[1:])), ZERO)
        er = ZERO if path == ZERO else max(D(-1), min(D(1), logs[-1] / path))
        return {"trend30": str(D(3000) * slope),
                "endpoint30": str(D(3000) * logs[-1] / _days(right - left)),
                "return_pct": str(D(100) * (sampled[-1] / sampled[0] - 1)),
                "er": str(er), "grid_points": len(nodes)}


def _at_facts(points: list[tuple[int, datetime, Decimal]], start: datetime, end: datetime,
              cached_windows: dict | None = None) -> dict:
    with localcontext() as context:
        context.prec = 38
        return _at_facts_precise(points, start, end, cached_windows)


def _at_facts_precise(points: list[tuple[int, datetime, Decimal]], start: datetime, end: datetime,
                      cached_windows: dict | None = None) -> dict:
    if not points:
        return {"status": "NOT_EVALUATED", "reason": "empty_source", "raw_count": 0}
    if points[0][1] > end - timedelta(days=28):
        return {"status": "NOT_EVALUATED", "reason": "w28_unavailable", "raw_count": len(points)}
    raw = [EquitySample(1, i, t, v) for i, t, v in points]
    if cached_windows is None:
        old = calculate_equity_quality_facts(1, start, end, raw)
        if old.erf_disposition == "NOT_EVALUATED":
            return {"status": "NOT_EVALUATED", "reason": old.reason, "raw_count": len(points)}
        windows = _window_dict(old)
    else:
        windows = cached_windows
    times = [t for i, t, v in points]
    values = [v for i, t, v in points]
    first = times[0]
    left28, left14, left7 = (end - timedelta(days=d) for d in (28, 14, 7))
    pre_days = _days(left28 - first)
    pre = _variable_metric(times, values, first, left28) if left28 - first >= PRE_MIN else None
    boundaries = (left28, left14, left7, end)
    hwm, stages, records = {}, [0, 0, 0], []
    stage_ath = [{"count": 0, "first": None, "last": None} for _ in range(3)]
    peak, peak_time = values[0], first
    raw_dd = []
    growth_seen = False
    severe_dd_after_growth = False
    for position, (i, stamp, value) in enumerate(points):
        if value > peak:
            peak, peak_time = value, stamp
            if position:
                records.append((stamp, value, i))
                growth_seen = True
                for k, (a, b) in enumerate(zip(boundaries, boundaries[1:])):
                    if a < stamp <= b:
                        stages[k] += 1
                        event = {"time": stamp.isoformat().replace("+00:00", "Z"),
                                 "value": str(value), "sample_index": i}
                        stage_ath[k]["count"] += 1
                        if stage_ath[k]["first"] is None:
                            stage_ath[k]["first"] = event
                        stage_ath[k]["last"] = event
        current_dd = D(100) * (1 - value / peak)
        raw_dd.append((stamp, current_dd))
        if stamp >= left14 and current_dd >= D(23) and growth_seen:
            severe_dd_after_growth = True
    at_left14 = [(t, risk) for t, risk in raw_dd if t <= left14]
    if (at_left14 and at_left14[-1][1] >= D(23)
            and any(t <= left14 for t, value, index in records)):
        severe_dd_after_growth = True
    for key, at in (("28", left28), ("14", left14), ("7", left7), ("0", end)):
        prior = [r for r in records if r[0] <= at]
        initial = [(i, t, v) for i, t, v in points if t <= at]
        if initial:
            largest = max(initial, key=lambda p: p[2])[2]
            earliest = next(t for i, t, v in initial if v == largest)
            hwm[key] = {"value": str(largest), "time": earliest.isoformat().replace("+00:00", "Z")}
        else:
            hwm[key] = None
    dd = {}
    for key, left in (("14", left14), ("7", left7)):
        in_window = [value for t, value in raw_dd if left <= t <= end]
        prior = [value for t, value in raw_dd if t <= left]
        if prior:
            in_window.append(prior[-1])
        dd[key] = str(max(in_window)) if in_window else None
    previous = hwm["7"]
    breakout = bool(previous and stages[2] and values[-1] > D(previous["value"]))
    full_grid_steps = int((end - first) // timedelta(hours=6))
    grid_max = max([values[0]] + [values[bisect_right(times, end - timedelta(hours=6) * j) - 1]
                                  for j in range(full_grid_steps + 1)])
    raw_max = max(values)
    return {"status": "READY", "reason": None, "raw_count": len(points),
            "pre28_days": pre_days, "pre28": pre, "windows": windows,
            "hwm": hwm, "stages": stages, "stage_ath": stage_ath,
            "dd": dd, "max_dd": str(max(v for t, v in raw_dd)),
            "held_weekly_breakout": breakout, "close": str(values[-1]),
            "strict_ath_count": len(records), "severe_dd_after_growth": severe_dd_after_growth,
            "raw_peak_missed_by_grid": raw_max > grid_max}


def curve_facts(points: list[tuple[int, datetime, Decimal]], start: datetime, end: datetime,
                cached_windows: dict | None = None, prefixes: bool = False) -> dict:
    """Validate sample order first; calculate all full-history facts only after validation."""
    try:
        start, end = _utc(start), _utc(end)
        if end < start:
            raise ValueError("invalid_report_interval")
        parsed = []
        previous_index, previous_time = -1, None
        nonpositive = 0
        for index, raw_time, raw_value in points:
            stamp = _utc(raw_time)
            if type(index) is not int or index <= previous_index:
                raise ValueError("source_invalid_sample_index")
            if previous_time is not None and stamp < previous_time:
                raise ValueError("source_invalid_chronology")
            if not start <= stamp <= end:
                raise ValueError("source_invalid_interval")
            if isinstance(raw_value, bool) or raw_value is None:
                raise ValueError("source_invalid_equity")
            value = raw_value if isinstance(raw_value, Decimal) else D(str(raw_value))
            if not value.is_finite():
                raise ValueError("source_invalid_equity")
            nonpositive += value <= 0
            parsed.append((index, stamp, value))
            previous_index, previous_time = index, stamp
    except (ValueError, TypeError, ArithmeticError) as exc:
        return {"status": "NOT_EVALUATED", "reason": str(exc), "raw_count": len(points)}
    if nonpositive:
        return {"status": "NOT_EVALUATED", "reason": "nonpositive_equity", "raw_count": len(points)}
    result = _at_facts(parsed, start, end, cached_windows)
    if prefixes and result["status"] == "READY":
        result["prefixes"] = {}
        for offset in (14, 7):
            cutoff = end - timedelta(days=offset)
            left = parsed[:bisect_right([p[1] for p in parsed], cutoff)]
            result["prefixes"][str(offset)] = _at_facts(left, start, cutoff)
    return result


def classify(facts: dict, epsilon: Decimal, family: str, tolerance: Decimal,
             prior: tuple[str, ...] = ()) -> dict:
    if facts.get("status") != "READY":
        return {"status": "NOT_EVALUATED", "decision": "NOT_EVALUATED", "reason": facts.get("reason")}
    dirs = {key: direction(facts.get("windows", {}).get(key), epsilon) for key in ("28", "14", "7")}
    pre = direction(facts.get("pre28"), epsilon)
    dd14, dd7 = (D(str(facts["dd"][key])) for key in ("14", "7"))
    if max(dd14, dd7) >= D(23) and facts.get("severe_dd_after_growth", False):
        return {"status": "DECLINING", "decision": "DROP", "reject60": True, "reason": "recent_dd_ge_23"}
    if max(dd14, dd7) >= D(23):
        return {"status": "UNRESOLVED", "decision": "DROP", "reason": "recent_dd_ge_23_prior_growth_unproven"}
    if dirs["28"] != "UP":
        return {"status": "UNRESOLVED", "decision": "DROP", "reason": "W28_NOT_UP", "directions": dirs, "pre": pre}
    if pre == "DOWN":
        return {"status": "UNRESOLVED", "decision": "DROP", "reason": "PRE28_DOWN", "directions": dirs, "pre": pre}
    all_up = all(dirs[key] == "UP" for key in ("28", "14", "7"))
    stages = facts.get("stages", [0, 0, 0])
    weekly = dirs["7"] == "UP" and facts.get("held_weekly_breakout", False)
    growth = all_up and pre in (None, "UP") and all(stages) and weekly
    stalled_before = "STALLED" in prior
    resumed_before = "RESUMED" in prior
    if weekly and stalled_before and (not resumed_before or not growth):
        return {"status": "RESUMED", "decision": "PASS", "reason": "proved_stall_and_held_ath"}
    if growth and (not stalled_before or resumed_before):
        v28 = D(str(facts["windows"]["28"]["trend30"]))
        v14 = D(str(facts["windows"]["14"]["trend30"]))
        v7 = D(str(facts["windows"]["7"]["trend30"]))
        if family == "abs":
            slow = v28 - v14 > tolerance and v28 - v7 > tolerance
        elif family == "rel":
            slow = v14 < v28 * (1 - tolerance) and v7 < v28 * (1 - tolerance)
        else:
            raise ValueError("unknown slowdown family")
        return {"status": "WEAKENING" if slow else "GROWING", "decision": "PASS", "reason": "all_up_staged_ath"}
    if weekly and (not stalled_before or resumed_before):
        return {"status": "UNRESOLVED", "decision": "DROP", "reason": "resume_candidate_unproven"}
    return {"status": "STALLED", "decision": "PASS", "rank": "RESERVED", "reason": "growth_paused_or_below_ath"}


def group_stream(chunks, expected_ids: set[int]):
    """Group an already result_id/sample_index ordered cursor, including empty IDs."""
    current, group, seen = None, [], set()
    ordered = sorted(expected_ids)
    position = 0
    for chunk in chunks:
        for row in chunk:
            rid = int(row[0])
            if rid not in expected_ids:
                raise RuntimeError("unexpected_result_id")
            if current is not None and rid != current:
                if rid <= current:
                    raise RuntimeError("result_group_order_invalid")
                seen.add(current)
                yield current, group
                group = []
                position += 1
            while position < len(ordered) and ordered[position] < rid:
                missing = ordered[position]
                if missing not in seen:
                    seen.add(missing)
                    yield missing, []
                position += 1
            if rid in seen:
                raise RuntimeError("result_reopened")
            current = rid
            group.append(row[1:])
    if current is not None:
        seen.add(current)
        yield current, group
    for rid in ordered:
        if rid not in seen:
            yield rid, []


def _analyze_task(item):
    rid, rows, meta = item
    points = [(int(index), stamp, value) for index, stamp, value in rows]
    facts = curve_facts(points, meta["start"], meta["end"], meta["windows"], prefixes=True)
    if len(points) != facts["raw_count"]:
        raise RuntimeError("worker_row_conservation_failed")
    return rid, facts


def _file_digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _source_info(con, path: Path) -> dict:
    stat = path.stat()
    catalog = con.execute("select table_name,column_name,data_type from information_schema.columns "
                          "where table_schema='main' order by table_name,ordinal_position").fetchall()
    counts = {table: con.execute(f"select count(*) from {table}").fetchone()[0]
              for table in ("strategies", "strategy_results", "strategy_equity", "equity_quality_metrics")}
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "schema": dict(con.execute("select key,value from schema_info").fetchall()),
            "catalog_sha256": hashlib.sha256(_canonical(catalog).encode()).hexdigest(),
            "counts": counts, "file_sha256": _file_digest(path)}


def _read_inventory(con):
    sql = """select s.strategy_id,s.symbol,s.side,s.lifecycle_status,
       r.result_id,r.report_start_utc,r.report_end_utc,r.imported_at_utc,
       r.effective_start_utc,r.effective_end_utc,r.optimizer_source_metadata_json,
       c.source_revision,c.algo_version,c.facts_json,c.facts_sha256
       from strategies s join strategy_results r on r.result_id=s.current_result_id
       and r.strategy_id=s.strategy_id
       left join equity_quality_metrics c on c.result_id=r.result_id
       where s.lifecycle_status='ACTIVE' order by r.result_id"""
    by_id = {}
    old_states = Counter()
    cache_problems = Counter()
    cache_fingerprint = hashlib.sha256()
    for (sid, symbol, side, lifecycle, rid, start, end, imported, effective_start,
         effective_end, source_meta, revision, algo, payload, digest) in con.execute(sql).fetchall():
        meta = {"result_id": int(rid), "strategy_id": int(sid), "symbol": symbol, "side": side,
                "start": _utc(start), "end": _utc(end), "imported_at_utc": _utc(imported),
                "effective_start_utc": _utc(effective_start) if effective_start is not None else None,
                "effective_end_utc": _utc(effective_end) if effective_end is not None else None,
                "optimizer_source_metadata_json": source_meta}
        try:
            revision_meta = {"result_id": meta["result_id"],
                             "report_start_utc": meta["start"], "report_end_utc": meta["end"],
                             "imported_at_utc": meta["imported_at_utc"],
                             "effective_start_utc": meta["effective_start_utc"],
                             "effective_end_utc": meta["effective_end_utc"],
                             "optimizer_source_metadata_json": source_meta}
            if revision != equity_source_revision(revision_meta):
                raise ValueError("stale_source_revision")
            cache = decode_equity_facts(payload, digest)
            if cache.result_id != rid or algo != cache.algo_version or cache.algo_version != "equity-quality-r7.3-v1":
                raise ValueError("cache_version_or_result_mismatch")
            if cache.report_start_utc != meta["start"] or cache.report_end_utc != meta["end"]:
                raise ValueError("cache_report_bounds_mismatch")
            meta["windows"] = _window_dict(cache)
            meta["old_state"] = cache.state
            meta["expected_rows"] = cache.raw_sample_count
            old_states[cache.state] += 1
            cache_fingerprint.update(f"{rid}|{revision}|{digest}\n".encode())
        except (ValueError, TypeError) as exc:
            cache_problems[str(exc)] += 1
            continue
        by_id[int(rid)] = meta
    if cache_problems:
        raise RuntimeError(f"source cache invalid: {dict(cache_problems)}")
    if len(by_id) != con.execute("select count(*) from strategies where lifecycle_status='ACTIVE'").fetchone()[0]:
        raise RuntimeError("active_inventory_cache_coverage_failed")
    return by_id, dict(old_states), cache_fingerprint.hexdigest()


def _preregister(outdir: Path, by_id: dict, source: dict, cache_fingerprint: str) -> dict:
    seed = "equity-m3-2026-10-04-v1"
    ids_by_old_bucket = defaultdict(list)
    for rid, meta in by_id.items():
        report_days = _days(meta["end"] - meta["start"])
        bucket = (meta["old_state"], meta["side"], "42plus" if report_days >= 42 else "short")
        ids_by_old_bucket[bucket].append(rid)
    reference_ids = []
    for bucket, ids in sorted(ids_by_old_bucket.items()):
        ids.sort(key=lambda rid: hashlib.sha256(f"{seed}|{rid}".encode()).digest())
        reference_ids.extend(ids[:3])
    manifest = {
        "research": "Equity M3 descriptive read-only calibration",
        "spec": "docs/specs/2026-10-03-equity-regime-status-map.md M3",
        "source_schema": source["schema"], "source_digest_before": source["file_sha256"],
        "source_counts": source["counts"], "source_catalog_sha256": source["catalog_sha256"],
        "validated_cache_revision_digest_sha256": cache_fingerprint,
        "python_version": sys.version.split()[0], "duckdb_version": duckdb.__version__,
        "harness_sha256_at_extraction": _file_digest(Path(__file__)),
        "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                    capture_output=True, text=True, check=True).stdout.strip(),
        "git_dirty_at_extraction": bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                                       capture_output=True, text=True, check=True).stdout.strip()),
        "output_fields": ["result_id", "strategy_id", "symbol", "side", "old_state", "facts"],
        "facts_fields": ["status", "reason", "raw_count", "pre28_days", "pre28", "windows", "hwm",
                         "stages", "stage_ath", "dd", "max_dd", "held_weekly_breakout", "close", "strict_ath_count",
                         "severe_dd_after_growth", "raw_peak_missed_by_grid", "prefixes"],
        "seed": seed, "reference_ids": sorted(reference_ids),
        "symbol_split": "SHA256(seed|symbol) first byte < 204 => development, else holdout",
        "direction_epsilon_log_pp_30d": DIRECTION_GRID,
        "slowdown_absolute_log_pp_30d": ABS_GRID,
        "slowdown_relative_fraction": REL_GRID,
        "families_separate": True,
        "grid": "6h right anchored to applicable cutoff; PRE starts at first actual sample",
        "ath": "strict raw full-history updates; first point initializes; stage intervals (T-28,T-14],(T-14,T-7],(T-7,T]",
        "dd": "raw history HWM carried at window left; D14,D7 <23 for STALLED/RESUMED",
        "pre28": "available at actual first sample <= cutoff-42d, equality included",
        "source_order": "ORDER BY result_id,sample_index; nondecreasing UTC timestamp checked before dedup",
        "current_prefixes_days_before_T": [14, 7, 0],
        "unaccepted": ["direction epsilon", "slowdown family/tolerance", "numeric FLAT predicate", "numeric COLLAPSING predicate"],
    }
    path = outdir / "manifest.prereg.json"
    encoded = _canonical(manifest) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise RuntimeError("preregistration_manifest_conflict")
    else:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
    return manifest


def _split(symbol: str, seed: str) -> str:
    digest = hashlib.sha256(f"{seed}|{symbol}".encode()).digest()
    return "development" if digest[0] < 204 else "holdout"


def _classify_history(facts: dict, eps: Decimal, family: str, tolerance: Decimal):
    history = []
    for offset in (14, 7):
        history.append(classify(facts.get("prefixes", {}).get(str(offset),
                                      {"status": "NOT_EVALUATED", "reason": "missing_prefix"}),
                                eps, family, tolerance, tuple(x["status"] for x in history)))
    current = classify(facts, eps, family, tolerance, tuple(x["status"] for x in history))
    return history, current


def summarize(outdir: Path, manifest: dict, panel_ids: set[int] | None = None) -> dict:
    path = outdir / "facts.jsonl"
    counts = Counter()
    split_counts = defaultdict(Counter)
    old_to_new = Counter()
    transitions = Counter()
    panel_counts = Counter()
    action_counts = Counter()
    reason_counts = Counter()
    pre_dirs = Counter()
    dd_bins = Counter()
    examples = defaultdict(list)
    rows = 0
    initial_facts_sha = _file_digest(path)
    facts_hash = hashlib.sha256()
    classified_once = hashlib.sha256()
    classified_twice = hashlib.sha256()
    raw_peak_missed = 0
    selected_grid = (D("0.5"), "abs", D("1"))  # reporting anchor, not an accepted policy
    with path.open("rb") as handle:
        for line in handle:
            facts_hash.update(line)
            item = json.loads(line)
            rows += 1
            facts = item["facts"]
            old = item["old_state"]
            split = _split(item["symbol"], manifest["seed"])
            raw_peak_missed += bool(facts.get("raw_peak_missed_by_grid"))
            pre_dirs[direction(facts.get("pre28"), selected_grid[0]) or "UNAVAILABLE"] += 1
            if facts.get("dd", {}).get("14") is not None:
                d = D(facts["dd"]["14"])
                dd_bins["ge23" if d >= 23 else "10to23" if d >= 10 else "lt10"] += 1
            for epsilon in DIRECTION_GRID:
                for family, family_grid in (("abs", ABS_GRID), ("rel", REL_GRID)):
                    for tolerance in family_grid:
                        key = f"eps={epsilon}|{family}={tolerance}"
                        history, current = _classify_history(facts, D(epsilon), family, D(tolerance))
                        status = current["status"]
                        counts[(key, status)] += 1
                        split_counts[(key, split)][status] += 1
                        if (D(epsilon), family, D(tolerance)) == selected_grid:
                            old_to_new[(old, status)] += 1
                            transitions[(history[0]["status"], history[1]["status"], status)] += 1
                            action_counts[(current["decision"], bool(current.get("reject60")),
                                           current.get("rank") or "")] += 1
                            reason_counts[current.get("reason") or "NONE"] += 1
                            if panel_ids and item["result_id"] in panel_ids:
                                panel_counts[status] += 1
                            if len(examples[status]) < 5:
                                examples[status].append({"result_id": item["result_id"],
                                                         "symbol": item["symbol"], "side": item["side"],
                                                         "W28": direction(facts.get("windows", {}).get("28"), D(epsilon)),
                                                         "W14": direction(facts.get("windows", {}).get("14"), D(epsilon)),
                                                         "W7": direction(facts.get("windows", {}).get("7"), D(epsilon)),
                                                         "pre": direction(facts.get("pre28"), D(epsilon)),
                                                         "D14": facts.get("dd", {}).get("14"),
                                                         "stages": facts.get("stages")})
            canonical = _canonical({"id": item["result_id"], "anchor": _classify_history(facts, *selected_grid)})
            classified_once.update((canonical + "\n").encode())
            canonical_again = _canonical({"id": item["result_id"], "anchor": _classify_history(facts, *selected_grid)})
            classified_twice.update((canonical_again + "\n").encode())
    if facts_hash.hexdigest() != initial_facts_sha or _file_digest(path) != initial_facts_sha:
        raise RuntimeError("facts_artifact_changed_during_replay")
    if classified_once.digest() != classified_twice.digest():
        raise RuntimeError("reclassification_nondeterministic")
    if sum(action_counts.values()) != rows or sum(reason_counts.values()) != rows:
        raise RuntimeError("anchor_action_reason_conservation_failed")
    summary = {"results": rows, "facts_sha256": facts_hash.hexdigest(),
               "anchor": "eps=0.5, absolute slowdown=1 log-pp/30d; research only",
               "classification_sha256": classified_once.hexdigest(),
               "harness_sha256_at_replay": _file_digest(Path(__file__)),
               "harness_sha256_at_extraction": manifest.get("harness_sha256_at_extraction"),
               "deterministic_replay": True,
               "anchor_actions": {"|".join((decision, "REJECT60" if reject else "NO_REJECT60", rank or "NO_RANK")): value
                                  for (decision, reject, rank), value in sorted(action_counts.items())},
               "anchor_reasons": dict(sorted(reason_counts.items())),
               "counts": {k: dict(sorted((status, value) for (setting, status), value in counts.items() if setting == k))
                          for k in sorted({setting for setting, status in counts})},
               "split_counts": {f"{setting}|{split}": dict(value) for (setting, split), value in split_counts.items()},
               "old_to_anchor": {f"{old}->{new}": value for (old, new), value in sorted(old_to_new.items())},
               "anchor_prefix_transitions": {"->".join(states): value for states, value in sorted(transitions.items())},
               "latest_panel_anchor": dict(panel_counts), "pre28_directions_anchor": dict(pre_dirs),
               "D14_bins": dict(dd_bins), "raw_peak_missed_by_grid": raw_peak_missed,
               "examples": dict(examples)}
    (outdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _temp_usage(path: Path) -> int:
    return sum((Path(root) / name).stat().st_size for root, dirs, files in os.walk(path) for name in files)


def _memory_usage() -> int:
    try:
        import psutil
        process = psutil.Process()
        return process.memory_info().rss + sum(child.memory_info().rss for child in process.children(recursive=True))
    except (ImportError, OSError):
        return -1


def _temp_guard(path: Path, peak: dict):
    temp_bytes = _temp_usage(path)
    free = shutil.disk_usage(path).free
    memory = _memory_usage()
    peak["temp_bytes"] = max(peak["temp_bytes"], temp_bytes)
    peak["process_tree_rss_bytes"] = max(peak["process_tree_rss_bytes"], memory)
    if temp_bytes > 12 * (1024 ** 3) or free < 8 * (1024 ** 3) or memory > 12 * (1024 ** 3):
        raise RuntimeError("resource_guard_exceeded")


def run(limit: int | None, outdir: Path, temp_dir: Path) -> dict:
    start_wall = time.monotonic()
    database = performance_v2_database_path(load_performance_v2_config(ROOT / "config.performance.json"))
    if not database.is_file():
        raise FileNotFoundError("configured_performance_database_missing")
    temp_parent = Path(os.environ.get("TEMP", "")).resolve()
    safe_temp = temp_dir.resolve()
    if temp_parent.drive.upper() != "C:" or safe_temp.parent != temp_parent or not safe_temp.name.startswith("mrs-equity-m3-"):
        raise RuntimeError("unsafe_temp_directory")
    if shutil.disk_usage(temp_parent).free < 20 * (1024 ** 3):
        raise RuntimeError("insufficient_C_drive_space_for_12GiB_spill_and_8GiB_reserve")
    if safe_temp.exists() and any(safe_temp.iterdir()):
        raise RuntimeError("temp_directory_not_empty")
    safe_temp.mkdir(exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    if (outdir / "facts.jsonl").exists():
        raise RuntimeError("facts_output_already_exists")
    peak = {"temp_bytes": 0, "process_tree_rss_bytes": 0}
    try:
        con = duckdb.connect(str(database), read_only=True)
        try:
            con.execute("SET TimeZone='UTC'")
            con.execute("SET threads=16")
            con.execute("SET memory_limit='4GB'")
            con.execute("SET temp_directory = ?", [str(safe_temp)])
            con.execute("SET max_temp_directory_size='12GB'")
            con.execute("BEGIN TRANSACTION")
            before = _source_info(con, database)
            inventory, old_states, cache_fingerprint = _read_inventory(con)
            if limit is not None:
                chosen = sorted(inventory, key=lambda rid: hashlib.sha256(f"m3-sample|{rid}".encode()).digest())[:limit]
                inventory = {rid: inventory[rid] for rid in sorted(chosen)}
            expected_ids = set(inventory)
            manifest = _preregister(outdir, inventory, before, cache_fingerprint)
            reference_ids = set(manifest["reference_ids"])
            latest = con.execute("select selection_run_id,symbol,side,candidate_count "
                                 "from selection_runs order by created_at_utc desc limit 1").fetchone()
            panel_ids = set()
            if latest:
                panel_ids = {int(rid) for (rid,) in con.execute(
                    "select result_id_at_selection from selection_results where selection_run_id=?", [latest[0]]).fetchall()}
            (outdir / "panel_ids.json").write_text(
                _canonical({"selection_run_id": latest[0] if latest else None,
                            "result_ids": sorted(panel_ids)}) + "\n", encoding="utf-8")
            source_total = before["counts"]["strategy_equity"] if limit is None else sum(meta["expected_rows"] for meta in inventory.values())
            if limit is None:
                query, params = "select result_id,sample_index,timestamp_utc,equity from strategy_equity order by result_id,sample_index", []
            else:
                placeholders = ",".join("?" for _ in inventory)
                query = f"select result_id,sample_index,timestamp_utc,equity from strategy_equity where result_id in ({placeholders}) order by result_id,sample_index"
                params = list(inventory)
            cursor = con.execute(query, params)

            def chunks():
                while True:
                    batch = cursor.fetchmany(8192)
                    if not batch:
                        return
                    yield batch

            scanned, submitted, emitted, empty = 0, 0, 0, 0
            group_rows, worker_rows = 0, 0
            from collections import deque
            pending = deque()

            def collect(facts_file):
                nonlocal emitted, worker_rows
                rid, count, future = pending.popleft()
                returned_id, facts = future.result()
                if returned_id != rid or facts["raw_count"] != count:
                    raise RuntimeError("worker_result_conservation_failed")
                worker_rows += facts["raw_count"]
                emitted += 1
                meta = inventory[rid]
                facts_file.write(_canonical({"result_id": rid, "strategy_id": meta["strategy_id"],
                                             "symbol": meta["symbol"], "side": meta["side"],
                                             "old_state": meta["old_state"], "facts": facts}) + "\n")

            with (outdir / "facts.jsonl").open("x", encoding="utf-8") as facts_file, \
                 (outdir / "reference_raw.jsonl").open("x", encoding="utf-8") as reference_file, \
                 ProcessPoolExecutor(max_workers=16) as pool:
                for rid, rows in group_stream(chunks(), expected_ids):
                    count = len(rows)
                    if count != inventory[rid]["expected_rows"]:
                        raise RuntimeError(f"cache_raw_row_count_mismatch:{rid}")
                    if count > 100000:
                        raise RuntimeError(f"oversized_result_group:{rid}")
                    scanned += count
                    group_rows += count
                    submitted += 1
                    if count == 0:
                        empty += 1
                    if rid in reference_ids:
                        reference_file.write(_canonical({"result_id": rid, "start": inventory[rid]["start"],
                                                         "end": inventory[rid]["end"], "rows": rows}) + "\n")
                    pending.append((rid, count, pool.submit(_analyze_task, (rid, rows, inventory[rid]))))
                    if len(pending) >= 32:
                        collect(facts_file)
                    if submitted % 100 == 0:
                        _temp_guard(safe_temp, peak)
                    if submitted % 500 == 0:
                        print(f"groups={submitted}/{len(inventory)} scanned_rows={scanned} elapsed_s={time.monotonic()-start_wall:.1f}", flush=True)
                while pending:
                    collect(facts_file)
            _temp_guard(safe_temp, peak)
            if not (scanned == group_rows == worker_rows == source_total) or submitted != emitted or emitted != len(inventory):
                raise RuntimeError("total_row_conservation_failed")
            after = _source_info(con, database)
            if before != after:
                raise RuntimeError("source_changed_during_read_only_snapshot")
            con.execute("ROLLBACK")
            summary = summarize(outdir, manifest, panel_ids)
            run_info = {"results": emitted, "raw_rows": scanned, "empty_results": empty,
                        "source_before": before, "source_after": after, "source_unchanged": True,
                        "old_state_inventory": old_states, "latest_panel": {"symbol": latest[1], "side": latest[2],
                        "candidates": latest[3], "matched_current_results": len(panel_ids & expected_ids)} if latest else None,
                        "peak": peak, "elapsed_seconds": time.monotonic()-start_wall,
                        "duckdb_version": duckdb.__version__, "python_version": sys.version.split()[0],
                        "thread_count": 16, "worker_count": 16, "duckdb_memory_limit": "4GB", "temp_limit": "12GB"}
            (outdir / "run.json").write_text(json.dumps(run_info, indent=2) + "\n", encoding="utf-8")
            return {"run": run_info, "summary": summary}
        finally:
            con.close()
    finally:
        # Only the literal task-owned directory under the verified C: temp root.
        if safe_temp.parent == temp_parent and safe_temp.name.startswith("mrs-equity-m3-"):
            shutil.rmtree(safe_temp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="analyze all ACTIVE current results")
    parser.add_argument("--limit", type=int, default=20, help="deterministic pilot size")
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    name = "full" if args.full else f"sample-{args.limit}"
    outdir = ROOT / "Output" / "EquityM3" / "2026-10-04" / name
    if args.summarize_only:
        manifest = json.loads((outdir / "manifest.prereg.json").read_text(encoding="utf-8"))
        panel_path = outdir / "panel_ids.json"
        if not panel_path.exists():
            raise RuntimeError("frozen_panel_ids_missing")
        panel_ids = set(json.loads(panel_path.read_text(encoding="utf-8"))["result_ids"])
        result = summarize(outdir, manifest, panel_ids)
        print(_canonical({"results": result["results"], "classification_sha256": result["classification_sha256"]}))
    else:
        temp_parent = Path(os.environ["TEMP"]).resolve()
        result = run(None if args.full else args.limit, outdir, temp_parent / f"mrs-equity-m3-{name}")
        print(_canonical({"results": result["run"]["results"], "raw_rows": result["run"]["raw_rows"],
                          "elapsed_seconds": result["run"]["elapsed_seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
