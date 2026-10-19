"""Computed pipeline state of every registry pair (registry stage 3).

Contract: docs/specs/2026-10-18-pair-registry.md, «Stage 3 contract». Derived only, never typed: trade store
segments and READY catalog, imported tests, finalists, decisions, history sufficiency and the track record.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import duckdb

from .history import sufficiency
from .records import (
    current_decision, current_finalists, effective_ready_rule, import_finalists, import_tester_samples,
    import_tick_tests,
)
from .refresh import write_journal
from .track import track_inputs_sha256, track_record

STAGES = ("EXCLUDED", "FINALIST", "TICK", "TESTER", "READY", "HISTORY", "NEW")


def stage_of(*, decision: str | None, finalists: int, tick_tests: int, tester_on_newest: bool, ready_now: bool,
             periods: int) -> str:
    """First match wins: EXCLUDED, FINALIST, TICK, TESTER, READY, HISTORY (>= 2 periods), NEW."""
    if decision == "EXCLUDE":
        return "EXCLUDED"
    if finalists:
        return "FINALIST"
    if tick_tests:
        return "TICK"
    if tester_on_newest:
        return "TESTER"
    if ready_now:
        return "READY"
    return "HISTORY" if periods >= 2 else "NEW"


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _latest_ready(store, exchange: str) -> dict[str, dict]:
    with store.connect(read_only=True) as source:
        rows = source.execute("select symbol, rule_version, created_utc, payload_json from ready where exchange = ? "
                              "order by symbol, created_utc, ready_id", [exchange]).fetchall()
    latest: dict[str, dict] = {}
    for symbol, rule_version, created, payload in rows:
        latest[symbol] = {"rule_version": rule_version, "created": created, "payload": json.loads(payload)}
    return latest


def compute_pipeline(con: duckdb.DuckDBPyConnection, store, exchange: str, as_of_day: date, machine: str,
                     *, performance_db: Path | None = None, now_ms: int | None = None,
                     track: bool = True) -> dict[str, object]:
    """Import tests and finalists, then write one pipeline row per pair (side LONG) for `as_of_day`."""
    started = now_ms or _now_ms()
    try:
        result = _compute(con, store, exchange, as_of_day, machine, performance_db, started, track)
    except Exception as error:
        try:
            write_journal(con, "pipeline", exchange, "FAILED", None, started, f"{type(error).__name__}: {error}")
        except Exception:
            pass
        raise
    write_journal(con, "pipeline", exchange, "OK", result, started, None)
    return result


def _compute(con, store, exchange, as_of_day, machine, performance_db, now, track) -> dict[str, object]:
    imported = {"tester_samples": import_tester_samples(con, store, exchange, now_ms=now)}
    if performance_db is not None and Path(performance_db).is_file():
        imported["tick_tests"] = import_tick_tests(con, performance_db, exchange, now_ms=now)
        imported["finalist_changes"] = import_finalists(con, performance_db, exchange, now_ms=now)
    ready = _latest_ready(store, exchange)
    finalists = {}
    for row in current_finalists(con, exchange):
        if row["status"] == "FINALIST" and row["side"] == "LONG":
            finalists[row["symbol"]] = finalists.get(row["symbol"], 0) + 1
    ticks = dict(con.execute("select symbol, count(*) from pair_tests where exchange = ? and kind = 'mrs3_tick' "
                             "and side = 'LONG' group by symbol", [exchange]).fetchall())
    pairs = con.execute("select symbol, listing_ms from pairs where exchange = ? and status != 'DELISTED' "
                        "order by symbol", [exchange]).fetchall()
    stages = dict.fromkeys(STAGES, 0)
    tracked = 0
    for symbol, listing_ms in pairs:
        segments = store.segments(symbol, exchange, source="simulator")
        testers = store.segments(symbol, exchange, source="tester")
        newest = segments[-1] if segments else None
        on_newest = [s for s in testers if newest and (s["start_ms"], s["end_ms"]) == (newest["start_ms"], newest["end_ms"])]
        latest_tester = testers[-1] if testers else None
        entry = ready.get(symbol)
        payload = entry["payload"] if entry else {}
        structures = payload.get("structures") or []
        best = max(structures, key=lambda item: item.get("score", float("-inf")), default=None)
        decision = current_decision(con, exchange, symbol, "LONG")
        rule, _overridden = effective_ready_rule(con, exchange, symbol)
        inputs = track_inputs_sha256(segments, rule)
        previous = con.execute("select track_cuts, track_admitted_cuts, track_loss_rate, track_inputs_sha256 "
                               "from pipeline where exchange = ? and symbol = ? and side = 'LONG' "
                               "order by as_of_day desc limit 1", [exchange, symbol]).fetchone()
        if previous is not None and previous[3] == inputs:
            cuts, admitted_cuts, loss_rate = previous[0], previous[1], previous[2]
        elif track and len(segments) >= 3:
            record = track_record(store, symbol, exchange, rule)
            cuts, admitted_cuts = record["cuts"], record["admitted_cuts"]
            loss_rate = None if record["forward_loss_rate"] is None else f"{record['forward_loss_rate']:.4f}"
            tracked += 1
        else:
            cuts = admitted_cuts = loss_rate = None
            inputs = None if not track else inputs
        stage = stage_of(decision=decision, finalists=finalists.get(symbol, 0), tick_tests=ticks.get(symbol, 0),
                         tester_on_newest=bool(on_newest), ready_now=payload.get("status") == "READY",
                         periods=len(segments))
        stages[stage] += 1
        history = sufficiency(listing_ms, as_of_day)["status"] if listing_ms else None
        con.execute("delete from pipeline where exchange = ? and symbol = ? and side = 'LONG' and as_of_day = ?",
                    [exchange, symbol, as_of_day])
        con.execute("insert into pipeline values (?, ?, 'LONG', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [exchange, symbol, as_of_day, machine, now, stage, len(segments),
                     _day(segments[0]["start_ms"]) if segments else None, _day(newest["end_ms"]) if newest else None,
                     payload.get("status"), entry["rule_version"] if entry else None,
                     len(structures) if payload.get("status") == "READY" else 0,
                     None if best is None else format(best["score"], ".4f"),
                     None if best is None else format(best["dd"], ".4f"), None if best is None else best.get("category"),
                     f"{_day(latest_tester['start_ms'])}..{_day(latest_tester['end_ms'])}" if latest_tester else None,
                     ticks.get(symbol, 0), finalists.get(symbol, 0), decision, history,
                     cuts, admitted_cuts, loss_rate, inputs])
    return {"pairs": len(pairs), "stages": {key: value for key, value in stages.items() if value},
            "tracked": tracked, **imported}
