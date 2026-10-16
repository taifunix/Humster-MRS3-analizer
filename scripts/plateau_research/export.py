"""Step 6 (§6): build the portable export package outside the repository.

Every table is parquet (zstd). A table larger than 500 MB is split into parts
`<name>.partNN.parquet` of at most ~500 MB (user request). The package holds no
local paths: database file names only. manifest.json lists every file with its
size and SHA-256.

Run: set PYTHONPATH=src & .venv\\Scripts\\python.exe scripts\\plateau_research\\export.py
Output: WORK/plateau-oos-export-2026-10-14/
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    FORWARD_DAYS, FORWARD_DAYS_SENS, REPO, ROLLING_TRAIN_DAYS, STEP_DAYS, TRAIN_MIN_DAYS, WORK,
    config_dict, listing_dates, load_config, read_parquet, symbol_half,
)
from measure import RECONCILE  # noqa: E402

PACKAGE = WORK / "plateau-oos-export-2026-10-14"
PART_BYTES = 450_000_000  # keep every file well under 500 MB
PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]|/Users/|\\Users\\")


def glob(name: str) -> str:
    return (WORK / "scopes" / "*" / "*" / f"{name}.parquet").as_posix()


def copy_query(con, sql: str, name: str) -> list[Path]:
    """Write `sql` to <name>.parquet; split into parts when larger than 500 MB."""
    target = PACKAGE / f"{name}.parquet"
    con.execute(f"copy ({sql}) to '{target.as_posix()}' (format parquet, compression zstd)")
    size = target.stat().st_size
    if size <= PART_BYTES:
        return [target]
    rows = con.execute(f"select count(*) from read_parquet('{target.as_posix()}')").fetchone()[0]
    parts = -(-size // PART_BYTES) + 1
    per = -(-rows // parts)
    files = []
    for i in range(parts):
        part = PACKAGE / f"{name}.part{i + 1:02d}.parquet"
        con.execute(f"copy (select * from read_parquet('{target.as_posix()}') order by all limit {per} offset {i * per}) "
                    f"to '{part.as_posix()}' (format parquet, compression zstd)")
        files.append(part)
    pattern = (PACKAGE / f"{name}.part*.parquet").as_posix()
    got, unique = con.execute(f"select count(*), (select count(*) from (select distinct * from read_parquet('{pattern}'))) "
                              f"from read_parquet('{pattern}')").fetchone()
    source_unique = con.execute(f"select count(*) from (select distinct * from read_parquet('{target.as_posix()}'))").fetchone()[0]
    if got != rows or unique != source_unique:
        raise SystemExit(f"{name}: parts hold {got} rows / {unique} unique, source {rows} / {source_unique}")
    target.unlink()
    return files


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    PACKAGE.mkdir(parents=True, exist_ok=True)
    for old in PACKAGE.glob("*"):
        old.unlink()
    con = duckdb.connect()
    con.execute("set preserve_insertion_order=false")
    files: list[Path] = []
    plan = read_parquet(WORK / "inventory" / "plan.parquet")
    inventory = read_parquet(WORK / "inventory" / "scopes.parquet")
    errors = con.execute(f"select * from read_parquet('{glob('errors')}', union_by_name=true)").df()
    excl = errors.groupby(["database", "scope", "wtype", "reason"]).size().rename("points").reset_index()
    scopes = inventory.merge(plan[["database", "scope", "kept", "mode"]], on=["database", "scope"], how="left")
    scopes["kept"] = scopes["kept"].fillna(False)
    scopes["mode"] = scopes["mode"].fillna("not_used")
    scopes["half"] = scopes["symbol"].map(symbol_half)
    scopes["exclusions_json"] = scopes.apply(
        lambda r: json.dumps(excl.loc[excl["database"].eq(r["database"]) & excl["scope"].eq(r["scope"]),
                                      ["wtype", "reason", "points"]].to_dict("records")), axis=1)
    con.register("scopes_df", scopes)
    files += copy_query(con, "select * from scopes_df", "scopes")
    con.register("errors_df", errors)
    files += copy_query(con, "select * from errors_df", "excluded_windows")

    wp = f"read_parquet('{glob('window_points')}', union_by_name=true)"
    files += copy_query(con, f"select * replace (case when wtype like 'train%' then 'train' else wtype end as wtype), wtype as wtype_detail "
                             f"from {wp} where wtype <> 'reconcile'", "window_points")
    files += copy_query(con, f"select * from {wp} where wtype = 'reconcile'", "reconcile_points")
    files += copy_query(con, f"select * from read_parquet('{glob('forward_points')}', union_by_name=true)", "forward_points")
    files += copy_query(con, f"select * from read_parquet('{glob('point_checks')}', union_by_name=true)", "point_checks")
    for name in ("points", "plateaus", "close_profiles", "base_one_order", "structures"):
        files += copy_query(con, f"select * from read_parquet('{glob('stages_' + name)}', union_by_name=true)", f"stages_{name}")
    files += copy_query(con, f"select * from read_parquet('{glob('analysis_log')}', union_by_name=true)", "analysis_log")
    analysis = WORK / "analysis"
    for name in ("picks", "outcomes"):
        files += copy_query(con, f"select * from read_parquet('{(analysis / (name + '.parquet')).as_posix()}')", name)
    if (analysis / "stage2_structures.parquet").exists():
        files += copy_query(con, f"select * from read_parquet('{(analysis / 'stage2_structures.parquet').as_posix()}')", "stage2_structures")
    files += copy_query(con, f"select * from read_parquet('{glob('daily_equity')}', union_by_name=true)", "daily_equity")

    # no local paths inside text columns
    leaks = []
    for path in files:
        cols = con.execute(f"describe select * from read_parquet('{path.as_posix()}')").df()
        for col in cols.loc[cols["column_type"].eq("VARCHAR"), "column_name"]:
            hit = con.execute(f"select count(*) from read_parquet('{path.as_posix()}') where regexp_matches(\"{col}\", '[A-Za-z]:[\\\\/]|Users[\\\\/]')").fetchone()[0]
            if hit:
                leaks.append(f"{path.name}:{col}:{hit}")
    if leaks:
        raise SystemExit(f"local paths found: {leaks}")

    config = load_config()
    listing, listing_path = listing_dates()
    raw_config = (REPO / "config.local.json").read_bytes()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    databases = []
    db_info = read_parquet(WORK / "inventory" / "databases.parquet")
    for r in db_info.itertuples(index=False):
        databases.append({"file": r.database, "database_id": r.database_id, "source_content_digest": r.source_content_digest,
                          "fragments": int(r.fragments), "quarantine": int(r.quarantine_count), "ready_scopes": int(r.ready_scopes)})
    manifest = {
        "package": PACKAGE.name, "repository_commit": commit,
        "algorithm_config": config_dict(config),
        "algorithm_config_source": "config.local.json (panel_workflow.algorithm_config_path not set)",
        "algorithm_config_file_sha256": hashlib.sha256(raw_config).hexdigest(),
        "listing_dates": {"file": listing_path.name, "sha256": hashlib.sha256(listing_path.read_bytes()).hexdigest(),
                          "symbols": len(listing)},
        "databases": databases,
        "symbol_halves": {s: symbol_half(s) for s in sorted(set(plan["symbol"]))},
        "cuts": {"train_min_days": TRAIN_MIN_DAYS, "step_days": STEP_DAYS, "forward_days": [FORWARD_DAYS, FORWARD_DAYS_SENS],
                 "train_start": "READY start (exp) or max(READY start, T-56d) (roll)", "rolling_train_days": ROLLING_TRAIN_DAYS},
        "reconcile_windows": [{"row": r, "start": s, "end_exclusive": e, "symbols": sy.split()} for r, s, e, sy in RECONCILE],
        "forward_method": "full-history series; balance from the action.balance chain (wallet samples are its sparse copy); "
                          "equity path = equity samples + flat-out actions; anchors strictly before T; *_samples columns = literal sample anchors",
        "versions": {"python": platform.python_version(), "duckdb": duckdb.__version__, "pandas": pd.__version__},
        "files": [{"name": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(files)],
    }
    (PACKAGE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    total = sum(p.stat().st_size for p in files)
    print(f"files {len(files)}, total {total / 1e6:.1f} MB")
    for p in sorted(files):
        print(f"{p.name}\t{p.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
