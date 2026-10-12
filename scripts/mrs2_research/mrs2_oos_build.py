"""MRS2-only out-of-sample: point facts in an earlier analysis vs the same point's B tail (last 14 days)
in a later analysis whose B starts after the earlier window ends. Read-only on analysis DBs.
Scans <repo root>/data/Analysis/** (deduplicated by analysis_id). Usage: mrs2_oos_build.py <repo root> <work dir>
"""
import glob
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

root, out = Path(sys.argv[1]), Path(sys.argv[2])
seen, frames, meta = set(), [], []
for f in sorted(glob.glob(str(root / "data/Analysis/**/*.analysis-v6.duckdb"), recursive=True)):
    c = duckdb.connect(f, read_only=True)
    aid = c.execute("select value from manifest where key='analysis_id'").fetchone()[0]
    cfg = c.execute("select value from manifest where key='algorithm_config_sha256'").fetchone()
    if aid in seen:
        c.close(); continue
    seen.add(aid)
    rows = []
    for (payload,) in c.execute("select payload_json from points").fetchall():
        d = json.loads(payload)
        ab = d.get("pretest_ab") or {}
        rows.append({
            "aid": aid, "point_id": d["point_id"], "symbol": d["symbol"], "side": d["side"], "tf": d["timeframe"],
            "shift_bp": d["shift_bp"], "open_ma": d["open_ma"], "close_ma": d["close_ma"],
            "pnl": float(d["pnl_pct"]), "dd": float(d["dd_pct"]), "trades": d["trades"],
            "wr": float(d["win_rate_pct"]) if d.get("win_rate_pct") is not None else np.nan,
            "pf": float(d["profit_factor"]) if d.get("profit_factor") is not None else np.nan,
            "eff": float(d["efficiency"]) if d.get("efficiency") is not None else np.nan,
            "days": float(d["effective_days"]), "events": d.get("point_event_count"),
            "economic_pass": d.get("economic_pass"), "event_eligible": d.get("event_eligible"),
            "standalone_eligible": d.get("standalone_eligible"), "depth_eligible": d.get("depth_eligible"),
            "sample_pass": d.get("standalone_sample_pass"), "required_min_trades": d.get("required_min_trades"),
            "plateau_id": d.get("plateau_id"), "plateau_points": d.get("plateau_point_count"),
            "plateau_role": d.get("plateau_role"), "selection_role": d.get("selection_role"),
            "report_start": d["report_start"], "report_end": d["report_end"],
            "ab_status": ab.get("status"), "b_pnl": float(ab["b_pnl"]) if ab.get("b_pnl") is not None else np.nan,
            "b_days": ab.get("b_days"), "b_trades": ab.get("b_round_trips"),
            "b_start_ms": ab.get("b_start_ms"), "a_pnl": float(ab["a_pnl"]) if ab.get("a_pnl") is not None else np.nan,
            "a_trades": ab.get("a_round_trips"),
        })
    df = pd.DataFrame(rows)
    frames.append(df)
    meta.append({"aid": aid, "file": Path(f).name, "cfg": cfg[0] if cfg else None, "start": df["report_start"].min(),
                 "end": df["report_end"].max(), "symbols": sorted(df["symbol"].unique()), "points": len(df),
                 "b_start": pd.to_datetime(df["b_start_ms"].dropna().min(), unit="ms", utc=True) if df["b_start_ms"].notna().any() else None})
    c.close()
pts = pd.concat(frames, ignore_index=True)
meta = pd.DataFrame(meta)
pts.to_pickle(out / "mrs2_points.pkl")
meta.to_pickle(out / "mrs2_meta.pkl")
print(meta[["file", "cfg", "start", "end", "b_start", "points"]].to_string())
print("points", len(pts), "config variants", meta["cfg"].nunique())
