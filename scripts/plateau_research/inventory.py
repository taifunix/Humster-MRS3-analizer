"""Step 1: inventory Source v6 databases, quarantine and READY scope windows.

Run: .venv\\Scripts\\python.exe scripts\\plateau_research\\inventory.py
Writes WORK/inventory/{databases,scopes}.parquet.
"""

from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import FINGERPRINT, SOURCE_ROOT, WORK, write_parquet  # noqa: E402


def inspect(path: str) -> tuple[dict, list[dict]]:
    from mrs3.source_v6_coverage import canonical_ready_intervals
    from mrs3.source_v6_materializer import _point_groups, _witness_window
    from mrs3.source_v6_storage import database_info, fragment_metadata, quarantine_details, source_content_digest

    name = Path(path).name
    record = {"database": name, "status": "OK", "reason": None}
    try:
        info = database_info(path)
    except Exception as error:  # not a fresh compact Source v6 database
        record.update(status="SKIP_NOT_FRESH", reason=str(error)[:300])
        return record, []
    record["fingerprint"] = info.get("fingerprint")
    record["database_id"] = info.get("database_id")
    if info.get("fingerprint") != FINGERPRINT:
        record.update(status="SKIP_FINGERPRINT")
        return record, []
    quarantine = quarantine_details(path)
    record["quarantine_count"] = len(quarantine)
    if quarantine:
        record.update(status="SKIP_QUARANTINE", reason=str(quarantine[0]["reason"])[:300])
        return record, []
    views = fragment_metadata(path)
    record["fragments"] = len(views)
    record["source_content_digest"] = source_content_digest(str(item.fragment_id) for item in views)
    witnesses = {item.scope_key: item for item in canonical_ready_intervals(views)}
    all_scopes = sorted({f"{v.point.symbol}|{v.point.side}|{v.point.timeframe}" for v in views})
    groups = _point_groups(views, all_scopes)
    scopes = []
    for scope in all_scopes:
        witness = witnesses.get(scope)
        symbol, side, timeframe = scope.split("|")
        row = {
            "database": name, "scope": scope, "symbol": symbol, "side": side, "timeframe": timeframe,
            "points": len(groups[scope]), "fragments": sum(len(ids) for ids in groups[scope].values()),
            "ready": witness is not None,
        }
        if witness is not None:
            start_ms, end_ms = _witness_window(witness)
            row.update(ready_start=str(witness.start), ready_end_incl=str(witness.end),
                       ready_start_ms=start_ms, ready_end_ms=end_ms,
                       ready_days=(end_ms - start_ms) / 86_400_000)
        scopes.append(row)
    record["scopes"] = len(all_scopes)
    record["ready_scopes"] = sum(1 for s in scopes if s["ready"])
    return record, scopes


def main() -> None:
    paths = sorted(str(p) for p in SOURCE_ROOT.glob("*.duckdb"))
    records, scopes = [], []
    with ProcessPoolExecutor(max_workers=8) as pool:
        for record, rows in pool.map(inspect, paths):
            print(record["database"], record["status"], record.get("ready_scopes"), flush=True)
            records.append(record)
            scopes.extend(rows)
    out = WORK / "inventory"
    write_parquet(pd.DataFrame(records), out / "databases.parquet")
    write_parquet(pd.DataFrame(scopes), out / "scopes.parquet")


if __name__ == "__main__":
    main()
