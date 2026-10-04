#!/usr/bin/env python
"""Replay frozen Equity M3 summaries through the production regime assessor."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import ctypes
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mrs3.performance_v2_equity_regime import (  # noqa: E402
    ALGORITHM_VERSION,
    EPSILON,
    EquityRegimeAssessment,
    EquityRegimeFacts,
    EquityRegimeWindowFacts,
    assess_equity_regime,
)


DEFAULT_INPUT = ROOT / "Output/EquityM3/2026-10-04/full/facts.jsonl"
DEFAULT_OUTPUT = ROOT / "Output/EquityM3/2026-10-04/production-replay.jsonl"
EXPECTED_ROWS = 30_940


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None


def _direction(v: Decimal, p: Decimal) -> str:
    if v > EPSILON and p > EPSILON:
        return "UP"
    if v < -EPSILON and p < -EPSILON:
        return "DOWN"
    if abs(v) <= EPSILON and abs(p) <= EPSILON:
        return "FLAT"
    return "MIXED"


def _window(metric: object, days: int) -> EquityRegimeWindowFacts | None:
    if metric is None:
        return None
    if not isinstance(metric, dict):
        raise ValueError(f"frozen_window_{days}_malformed")
    v = _decimal(metric.get("trend30"))
    p = _decimal(metric.get("endpoint30"))
    grid_points = metric.get("grid_points")
    if v is None or p is None or type(grid_points) is not int:
        raise ValueError(f"frozen_window_{days}_incomplete")
    # The assessor only reads v, p, and direction. The frozen summary has no
    # exact window boundaries or endpoint equities, so those fields stay None.
    return EquityRegimeWindowFacts(
        days, None, None, None, None, None, v, p, _direction(v, p), grid_points
    )


def assess_frozen_row(row: dict[str, Any]) -> EquityRegimeAssessment:
    """Adapt only the frozen decision inputs and call the production assessor."""
    if type(row.get("result_id")) is not int or not isinstance(row.get("facts"), dict):
        raise ValueError("frozen_row_identity_or_facts_malformed")
    source = row["facts"]
    result_id = row["result_id"]
    source_status = source.get("status")
    source_reason = source.get("reason")
    invalid = () if source_status == "READY" and source_reason is None else (
        str(source_reason or "FROZEN_FACTS_NOT_READY"),
    )
    windows = source.get("windows")
    windows = windows if isinstance(windows, dict) else {}
    w28, w14, w7 = (_window(windows.get(key), int(key)) for key in ("28", "14", "7"))
    pre = _window(source.get("pre28"), 0)

    dd = source.get("dd")
    dd = dd if isinstance(dd, dict) else {}
    dd14, dd7 = _decimal(dd.get("14")), _decimal(dd.get("7"))

    hwm = source.get("hwm")
    hwm = hwm if isinstance(hwm, dict) else {}
    boundary_values: list[Decimal | None] = []
    boundary_times: list[datetime | None] = []
    for key in ("28", "14", "7", "0"):
        item = hwm.get(key)
        item = item if isinstance(item, dict) else {}
        boundary_values.append(_decimal(item.get("value")))
        boundary_times.append(_utc(item.get("time")))
    hwm28, hwm14, hwm7, hwm0 = boundary_values
    hwm28_time, hwm14_time, hwm7_time, hwm0_time = boundary_times

    stages = source.get("stages")
    if not isinstance(stages, list) or len(stages) != 3 or any(type(n) is not int for n in stages):
        stages = [0, 0, 0]
        invalid += ("FROZEN_ATH_STAGE_COUNTS_UNAVAILABLE",)
    if dd14 is None or dd7 is None:
        invalid += ("FROZEN_DD_UNAVAILABLE",)
    if w28 is None or w14 is None or w7 is None:
        invalid += ("FROZEN_WINDOW_UNAVAILABLE",)
    if any(value is None for value in boundary_values):
        invalid += ("FROZEN_HWM_BOUNDARY_UNAVAILABLE",)

    final_equity = _decimal(source.get("close"))
    previous_ath = hwm7
    new_ath = stages[2] > 0
    if final_equity is None:
        invalid += ("FROZEN_FINAL_EQUITY_UNAVAILABLE",)
    held = bool(new_ath and previous_ath is not None and final_equity is not None and final_equity > previous_ath)
    source_held = source.get("held_weekly_breakout")
    if type(source_held) is bool and source_held != held:
        raise ValueError(f"frozen_held_breakout_mismatch_result_{result_id}")

    raw_count = source.get("raw_count")
    if type(raw_count) is not int:
        raw_count = 0
        invalid += ("FROZEN_RAW_COUNT_UNAVAILABLE",)

    facts = EquityRegimeFacts(
        algo_version=ALGORITHM_VERSION,
        result_id=result_id,
        report_start_utc=None,
        report_end_utc=None,
        raw_sample_count=raw_count,
        invalid_reasons=tuple(dict.fromkeys(invalid)),
        windows_28=w28,
        windows_14=w14,
        windows_7=w7,
        pre28=pre,
        dd14=dd14,
        dd7=dd7,
        hwm_t28=hwm28,
        hwm_t14=hwm14,
        hwm_t7=hwm7,
        hwm_t=hwm0,
        hwm_t28_time_utc=hwm28_time,
        hwm_t14_time_utc=hwm14_time,
        hwm_t7_time_utc=hwm7_time,
        hwm_t_time_utc=hwm0_time,
        previous_ath_w7=previous_ath,
        ath_stage_counts=tuple(stages),
        # Full event arrays are absent from the frozen summary and unused by
        # assess_equity_regime; only their exact stage counts/derived flags are
        # supplied. The assessment facts are not serialized into replay rows.
        ath_stage_event_times_utc=((), (), ()),
        ath_stage_event_values=((), (), ()),
        ath_stage_strict_increase=bool(
            hwm28 is not None and hwm14 is not None and hwm7 is not None and hwm0 is not None
            and hwm14 > hwm28 and hwm7 > hwm14 and hwm0 > hwm7
        ),
        new_ath_w7=new_ath,
        held_w7_breakout=held,
        final_equity=final_equity,
    )
    return assess_equity_regime(facts)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _peak_rss_bytes() -> int | None:
    if os.name != "nt":
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(peak if sys.platform == "darwin" else peak * 1024)
        except (ImportError, OSError, ValueError):
            return None
    try:
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        current = kernel.GetCurrentProcess
        current.restype = ctypes.c_void_p
        query = psapi.GetProcessMemoryInfo
        query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters), ctypes.c_ulong]
        query.restype = ctypes.c_int
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if query(current(), ctypes.byref(counters), counters.cb):
            return int(counters.PeakWorkingSetSize)
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    return None


def run_replay(
    input_path: str | Path,
    output_path: str | Path,
    *,
    expected_rows: int | None = None,
) -> dict[str, Any]:
    source_path = Path(input_path)
    target_path = Path(output_path)
    if source_path.resolve() == target_path.resolve():
        raise ValueError("input_and_output_must_differ")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    state_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    rank_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    reason_set_counts: Counter[str] = Counter()
    source_status_counts: Counter[str] = Counter()
    unclassified_valid = 0
    not_evaluated = 0
    rows = 0
    input_hash = hashlib.sha256()
    output_hash = hashlib.sha256()
    started = time.perf_counter()
    fd, temp_name = tempfile.mkstemp(prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent)
    try:
        with source_path.open("rb") as source, os.fdopen(fd, "wb") as output:
            for raw_line in source:
                input_hash.update(raw_line)
                if not raw_line.strip():
                    continue
                try:
                    row = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid_jsonl_row_{rows + 1}") from exc
                if not isinstance(row, dict):
                    raise ValueError(f"invalid_jsonl_object_{rows + 1}")
                assessment = assess_frozen_row(row)
                source_facts = row["facts"]
                source_status = str(source_facts.get("status", "UNKNOWN"))
                row_result = {
                    "result_id": row["result_id"],
                    "strategy_id": row.get("strategy_id"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "source_status": source_status,
                    "state": assessment.state,
                    "decision": assessment.decision,
                    "rank": assessment.rank,
                    "reasons": list(assessment.reasons),
                }
                encoded = _canonical(row_result) + b"\n"
                output.write(encoded)
                output_hash.update(encoded)
                rows += 1
                state_counts[assessment.state] += 1
                decision_counts[assessment.decision] += 1
                rank_counts[assessment.rank or "NONE"] += 1
                reason_counts.update(assessment.reasons)
                reason_set_counts[json.dumps(assessment.reasons, separators=(",", ":"))] += 1
                if assessment.state == "NOT_EVALUATED":
                    not_evaluated += 1
                is_valid = (
                    not assessment.facts.invalid_reasons
                    and assessment.facts.windows_28 is not None
                    and assessment.facts.windows_14 is not None
                    and assessment.facts.windows_7 is not None
                    and assessment.facts.dd14 is not None
                    and assessment.facts.dd7 is not None
                )
                if is_valid and "UNCLASSIFIED_GEOMETRY" in assessment.reasons:
                    unclassified_valid += 1
                source_status_counts[source_status] += 1
            output.flush()
            os.fsync(output.fileno())
        if expected_rows is not None and rows != expected_rows:
            raise ValueError(f"expected_{expected_rows}_rows_got_{rows}")
        os.replace(temp_name, target_path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise

    elapsed = time.perf_counter() - started
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "input": source_path.as_posix(),
        "output": target_path.as_posix(),
        "rows": rows,
        "input_sha256": input_hash.hexdigest().upper(),
        "output_sha256": output_hash.hexdigest().upper(),
        "source_statuses": dict(sorted(source_status_counts.items())),
        "states": dict(sorted(state_counts.items())),
        "decisions": dict(sorted(decision_counts.items())),
        "ranks": dict(sorted(rank_counts.items())),
        "reasons": dict(sorted(reason_counts.items())),
        "ordered_reason_sets": dict(sorted(reason_set_counts.items())),
        "not_evaluated": not_evaluated,
        "valid_unclassified_geometry": unclassified_valid,
        "wall_seconds": elapsed,
        "peak_rss_bytes": _peak_rss_bytes(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-rows", type=int, default=EXPECTED_ROWS)
    args = parser.parse_args()
    summary = run_replay(args.input, args.output, expected_rows=args.expected_rows)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
