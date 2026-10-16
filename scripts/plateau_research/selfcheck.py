"""Step 3 (§5.1): compare the production materializer with our full-window rows.

`materialize_source_v6_from_database` is called for a few scopes (it only
measures and returns rows; nothing is written) and its `scope.analysis_input`
rows are compared field by field with the raw `analysis_input_row` dicts that
measure.py stored for the full READY window.

Run: .venv\\Scripts\\python.exe scripts\\plateau_research\\selfcheck.py DB::SCOPE [DB::SCOPE ...]
Output: WORK/selfcheck.parquet (one row per compared field mismatch + summary)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import SOURCE_ROOT, WORK, read_parquet, write_parquet  # noqa: E402
from measure import scope_dir  # noqa: E402

FIELDS = ("pnl_pct", "dd_pct", "trades", "wins", "losses", "win_rate_pct", "profit_factor",
          "event_ids_hash", "events_last_30d", "pretest_ab")


def main() -> None:
    from mrs3.source_v6_materializer import materialize_source_v6_from_database

    summary, mismatches = [], []
    for item in sys.argv[1:]:
        database, scope = item.split("::")
        path = str(SOURCE_ROOT / database)
        ours = read_parquet(scope_dir(path, scope) / "window_points.parquet")
        ours = ours.loc[ours["wtype"].eq("full")].set_index("point_id")
        result = materialize_source_v6_from_database(path, [scope], workers=15)
        theirs = {row["point_id"]: row for row in result.scopes[0].analysis_input}
        compared = 0
        for point_id, row in theirs.items():
            if point_id not in ours.index:
                mismatches.append({"database": database, "scope": scope, "point_id": point_id, "field": "<missing>"})
                continue
            mine = ours.loc[point_id]
            for field in FIELDS:
                a = row[field]
                b = json.loads(mine[field]) if field == "pretest_ab" else mine[field]
                if field in ("trades", "wins", "losses", "events_last_30d"):
                    b = int(b)
                if a != b and not (a is None and pd.isna(b)):
                    mismatches.append({"database": database, "scope": scope, "point_id": point_id, "field": field,
                                       "materializer": json.dumps(a), "ours": json.dumps(b, default=str)})
                compared += 1
        summary.append({"database": database, "scope": scope, "points_materializer": len(theirs),
                        "points_ours": len(ours), "fields_compared": compared,
                        "mismatches": sum(1 for m in mismatches if m["scope"] == scope and m["database"] == database)})
        print(summary[-1], flush=True)
    write_parquet(pd.DataFrame(summary), WORK / "selfcheck_summary.parquet")
    write_parquet(pd.DataFrame(mismatches or [{"database": None}]), WORK / "selfcheck_mismatches.parquet")


if __name__ == "__main__":
    main()
