"""Re-run already built tick-test strategies on their own training windows (real MRS3 equity on train).

Usage: local_ticktest_trainwin_build.py <export dir> <output dir> <set dir> [<set dir> ...]
For every strategy of the given sets (T_*/strategies/*.json + structures.csv) the train window is the
Source v6 cut of its scope: StartDate = S + 4 days (tester warm-up 4 days counts back to S), EndDate = T.
Strategies are grouped into W_<start>_<end> folders, one tester config each; JSON files are copied as is.
"""
import json
import shutil
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
WARMUP_DAYS = 4


def main():
    b, out, sets = Path(sys.argv[1]), Path(sys.argv[2]), [Path(p) for p in sys.argv[3:]]
    cuts = duckdb.connect().execute(f"""select scope, any_value(s_ms) s_ms, t_ms from read_parquet('{b.as_posix()}/outcomes.parquet')
        where variant='exp' group by scope, t_ms""").df()
    cuts["t_date"] = pd.to_datetime(cuts.t_ms, unit="ms").dt.date.astype(str)
    idx = []
    for s in sets:
        i = pd.read_csv(s / "structures.csv")
        i["set"] = s.name
        idx.append(i)
    idx = pd.concat(idx, ignore_index=True)
    idx["scope"] = idx.symbol + "|LONG|" + idx.timeframe
    idx = idx.merge(cuts[["scope", "t_date", "s_ms"]], on=["scope", "t_date"], how="left")
    missing = idx.s_ms.isna().sum()
    idx = idx.dropna(subset=["s_ms"])
    idx["train_start"] = [str(np.datetime64(int(v // 86_400_000), "D") + np.timedelta64(WARMUP_DAYS, "D")) for v in idx.s_ms]
    idx["train_end"] = idx.t_date
    set_dirs = {x.name: x for x in sets}
    tester = json.loads((ROOT / "templates/exchanges/Bybit/mrs3-tester.json").read_text(encoding="utf-8"))
    for (start, end), g in idx.groupby(["train_start", "train_end"]):
        folder = out / f"W_{start}_{end}"
        (folder / "strategies").mkdir(parents=True, exist_ok=True)
        for r in g.itertuples(index=False):
            src = set_dirs[r.set] / f"T_{r.t_date}" / "strategies" / f"{r.strategy}.json"
            shutil.copyfile(src, folder / "strategies" / src.name)
        cfg = dict(tester)
        cfg["StartDate"], cfg["EndDate"], cfg["WarmupDays"] = start, end, WARMUP_DAYS
        (folder / "config_tester.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    idx.drop(columns=["s_ms"]).to_csv(out / "structures.csv", index=False, encoding="utf-8")
    days = (pd.to_datetime(idx.train_end) - pd.to_datetime(idx.train_start)).dt.days
    print(f"strategies {len(idx)} (no train window: {missing}), folders {idx.groupby(['train_start', 'train_end']).ngroups}, "
          f"train days median {days.median():.0f}, min {days.min()}, max {days.max()}, strategy-days {days.sum()}")
    print(idx.groupby("set").size().to_string())


if __name__ == "__main__":
    main()
