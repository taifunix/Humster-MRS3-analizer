# MRS2 trade store, history simulation and sampled tester runs

**Status:** Active — stages 1–2 ✅ implemented and reviewed (`CODE_REVIEW_PASS`); stages 3–4 ✅ implemented, validated on the real tester and reviewed (`CODE_REVIEW_PASS` after three rounds); next: full simulator run of all registry pairs, then READY threshold calibration; screener simulation mode ⏳ to be agreed; decision [ADR-0078](../decisions/0078-mrs2-trade-store.md)
**Date:** 2026-10-17
**Related:** [MRS2 simulator and period generator](2026-10-16-mrs2-period-csv-generator.md) (simulator rules, metrics, tester config), [ADR-0077](../decisions/0077-mrs2-base-limit-fill-plus-tick.md) (`order_limit_fill_plus_tick = true`)

## Goal

One store for MRS2 analysis: per-pair history of trades for the full grid
(simulator for history, real tester for a sampled latest window), hourly
margin-balance series, and a DuckDB catalog. Any window can be analysed, READY
for MRS3 is computed from the store deterministically, and tester jobs are
generated and executed from it automatically.

## Non-goals

- PerformanceDB (MRS3 results) and its imports.
- Migrating Source v6, materialization or old analysis databases.
- SHORT, exchanges other than Bybit (the layout keeps an exchange level).

## Layout (store root on the server)

```
<store>/
  catalog.duckdb
  trades/<exchange>/<SYMBOL>/<source>_<start>_<end>_<engine>.parquet
  hourly/<exchange>/<SYMBOL>/<source>_<start>_<end>_<engine>.parquet
  jobs/<job_id>/...            (tester job packages and results)
```

Minute candles stay in `<bot_root>/tester/data/<exchange>/<SYMBOL>/` (tester
format, automatic top-up).

### Grid registry (catalog)

`point_id` (stable integer) ↔ side, TF, shift bp, open length, close length,
MA type and source. The registry is append-only.

### Trade segment (Parquet, ZSTD)

One row per trade (exact columns: «Stage 1 contract»). A position still open at
the segment end is closed at market (`exit_kind = market_end`), as in the
tester's period report. Segment metadata (also in the catalog): exchange,
symbol, source, start, end, engine version, run settings (lot, fees, slippage,
funding, warm-up, `limit_fill_plus_tick`), price tick, qty step, SHA-256.

### Hourly series and segment metrics (Parquet, ZSTD)

Per point and UTC hour of the segment: margin balance close, low, high
(float32), plus the pair price series; and one exact metrics row per point.
Both are rebuildable from candles + trades; they serve windowed MaxDD, Ulcer and
READY.

### Catalog tables

`segments` (active / superseded), `points`, `runs` (engine and rule versions,
settings), `jobs` (tester queue: block definition, state, executor, result
segment), `ready` (READY outputs with inputs' hashes and rule version).

## Stages

1. ✅ **Store and history simulation.** Segment writer and reader, hourly cache,
   catalog, a CLI that simulates a pair's full history to `today − 20 days` and
   the latest 20-day window (fresh start), pairs in registry priority order,
   parallel with `duckdb_import.workers`. Acceptance: windowed metrics from the
   store equal the simulator's per-window results; a pair's history writes and
   reads back bit-identically; idempotent reruns.
2. ✅ **Window analysis and READY.** Window metrics (PnL, trades, WinRate, MaxDD,
   Ulcer, monthly) from segments + hourly series, vectorised; plateau v4 zone
   scores over the last 2–3 windows; READY JSON (EQUAL only) with provenance.
3. ✅ **Sampled tester runs.** Selection rule (versioned; base: top 10–20 % by the
   simulator plus cube neighbours), rectangular block cover, job queue with
   states, executor (local tester first), HTML trade-table parsing into
   `tester` segments, restore of the tester's own files.
4. ✅ **Panel.** Mode switch (simulation / tester), per-pair periods, progress,
   journal; screener in simulation mode (to be agreed).

## Stage 1 contract: store and history simulation

**Modules.** `src/mrs3/trade_store.py`: segment files, catalog. `src/mrs3/trade_history.py`:
segment plan, simulation of one segment, registry pair order. CLI
`scripts/simulate_history.py`.

**History is cut on the P2 grid.** For a pair with listing day `L`:
- `first = L + warm-up`;
- `end` = the day after the last archived day;
- history = `split_periods(first, end − 20 days)` (P2: 20-day periods, the
  remainder joins the last one), then the latest window `[end − 20, end)`.

Every segment is a fresh simulator run of its span, with warm-up before it. So:
- a P2 window has the tester's period semantics (it is a fresh run of exactly
  that window);
- longer windows are slices across consecutive segments;
- moving `end` forward re-simulates only the last history period and the latest
  window.

A pair whose latest window does not fit (`first > end − 20`) gets no segments
(`NO_FULL_PERIOD`). A segment is simulated only when every candle day file of
`[start − warm-up, end)` exists. Otherwise it is skipped with `MISSING_DAYS`
(the journal lists the days) and retried on the next run: a day lost to a failed
download must never be stored as a quiet market under the segment's permanent
id. A segment whose warm-up has no trades on its first day is skipped
(`NO_WARMUP_DATA`). Benchmark (SNOW, 2026-10-17): the full grid of
3,780 points over a 20-day period takes about 17 s on one process.

**`point_id`.** Computed as `TF minutes × 1,000,000 + shift bp × 100 + open
length × 10 + close length`, for example `1h, 230, 3, 5` → `60023035`. The id
is stable without any state. Point `0` is reserved for the pair's price series
in the hourly file.

**Segment files.** One segment is three immutable Parquet files (ZSTD, no
dictionary), named `<source>_<start>_<end>_<engine>.parquet`:

- `trades/…` — one row per trade, ordered by `point_id` and then entry.
  - Columns: `point_id` int32; `entry_ms`, `exit_ms` int64; `entry_price`,
    `exit_price`, `qty`, `fee_in`, `fee_out`, `funding` float64; `exit_kind`
    string.
  - `exit_kind` is `limit`, or `market_end` for a position closed at market on
    the last candle (`price × (1 − slippage)`, taker fee).
  - These are exactly the trades and amounts of `simulate_period`.
- `hourly/…` — one row per point and UTC hour of `[start, end)`.
  - Columns: `point_id` int32, `hour_ms` int64 (hour start), `close`, `low`,
    `high`, `drawdown` float32 (`drawdown`: see «Stage 2 contract»).
  - `close` is the margin balance at the hour end: the last path value
    stamped before it, the same sample the Ulcer rule uses. `low` and `high` are
    the extremes of the path values in the hour, including the value carried in
    from the previous hour.
  - An hour without path points repeats the previous close.
  - Point `0` rows hold the pair price instead: the last minute close before
    the hour end, and the low and high of the minute closes. Funding and window
    end prices come from these rows.
- `metrics/…` — one row per point: the exact simulator result of the segment
  span.
  - Columns: `point_id`, `final_balance`, `marked_final`, `trades`, `wins`,
    `gross_profit`, `gross_loss`, `fees`, `max_drawdown`,
    `max_drawdown_percent`, `ulcer_index`, `position_avg_percent`,
    `position_max_percent`, `last_ms` (last path stamp).

**Writing.**
- Workers write the files: a flushed (fsync) temporary sibling is hard-linked
  into place.
- An existing file with identical bytes is accepted, which makes a rerun after a
  crash idempotent.
- Different bytes are an error, except when the caller knows that no
  registered segment owns the file. In that case the file is replaced: the id is
  absent from the catalog, or it is a superseded segment whose files were found
  damaged.
- Only the main process writes the catalog. Registering a segment marks every
  active segment of the same pair and source whose span overlaps it as
  superseded (`superseded_by`), so active simulator segments of a pair never
  overlap.
- Reads verify the SHA-256 recorded in the catalog.

**Catalog tables.** `points`, `segments` (paths and hashes of the three files,
settings JSON, tick, qty step, status), `runs` (CLI invocations: engine,
settings, end day, counts), `jobs` and `ready` (stage 3 and stage 2 schemas,
created empty). The journal of per-segment statuses is `journal.jsonl` in the
store root.

**CLI.** Main options:
- `--store`, or `trade_store.root` in the config;
- `--pairs`, `--limit`, `--timeframes`, `--end-day`, `--no-download`;
- `--workers`, defaulting to `duckdb_import.workers`.

Pairs follow the registry priority (screening GO, CHECK, none, STOP, then 7-day
turnover); duplicate entries are dropped.
- A pair task downloads missing days (`minute_data.ensure_days`) and plans its
  segments.
- Pair tasks are submitted lazily: at most 2 at a time, and only while fewer
  than `workers` tasks are pending. So the segments of the first pairs run
  before later pairs are planned.
- Download threads per pair task are `workers // 2`, so the run stays within the
  shared limit.

For every planned segment id:
- **active** — skipped;
- **superseded** — reactivated after its three file hashes are verified (no
  re-simulation; overlapping active segments are superseded);
- **superseded with missing or damaged files** — `REACTIVATE_FAILED` in the
  journal, then simulated again and its catalog row replaced;
- **absent** — simulated as a process task.

A failure of one pair or segment, registration included, is journaled and
counted. It never stops the run; the `runs` row is always finished.

**Stage 1 acceptance.**
1. `point_id` round-trips for the whole grid and is unique.
2. The trade ledger of a segment equals `simulate_period`:
   - the trade list matches;
   - the sums agree: `final = lot + Σ gross − Σ fees − Σ funding`.
3. The hourly series equal a direct aggregation of the simulator path (close at
   every hour end, low and high).
4. The metrics rows equal `simulate_period` for every point.
5. Writing the same segment twice gives byte-identical files and hashes.
6. Overlapping registrations supersede older ones.
7. A CLI rerun with the same end day writes nothing new. Going back to an
   earlier end day reactivates the superseded plan; damaged superseded files
   are re-simulated; a missing candle day stores nothing.
8. A real-data pilot on several pairs reports runtime and store size.

**Stage 1 evidence (2026-10-17).**
- Tests: `tests/test_trade_store.py` 10 passed (including missing days,
  reactivation with duplicate pairs, and damaged superseded files).
- Independent review: the first pass gave `CODE_REVIEW_FAIL` (1 blocker: failed
  download days were stored; 1 major: re-registering a superseded id stopped
  the run; 3 minor). All five were fixed. The re-review confirmed the fixes and
  found one more major (a reactivation of damaged files stopped the run), which
  is now fixed. The second re-review gave `CODE_REVIEW_PASS`.
- Pilot: SNOW, XOM, KORU, PURR and FLY, `end = 2026-10-17`, 15 processes, with
  the download enabled (55 days created).
  - 19 segments written in 194 s.
  - Store size: trades 17 MB, hourly 69 MB, metrics 4.7 MB, catalog 2.6 MB.
  - Rerun: 0 written, 19 skipped, 8 s.
  - Re-simulating the XOM latest window into an empty root reproduced all
    three file hashes.

## Stage 2 contract: window analysis and READY

### Hourly drawdown column

The hourly file gets a `drawdown` column (float32). It holds the largest
drawdown inside the hour, in percent, measured against the running maximum of
the hour's values, including the carried value.

With it, MaxDD% of a window is
`max over hours of max((P − low) / P, drawdown)`, where `P` is the peak before
the hour. For one segment this equals the simulator's minute-level value.

### Window metrics (`src/mrs3/trade_windows.py`)

A window is a run of consecutive active segments of one pair and source (whole
segments only). Per point:
- **Balance and counts:** final balance `lot + Σ (final_k − lot)`; trades,
  wins, gross profit and loss, and fees are summed.
- **Curve:** the hourly series are concatenated, each segment shifted by the
  PnL of the segments before it.
- **MaxDD%:** the formula above. In a later segment the intra-hour term
  `(m − v) / m` was measured on the segment's own scale. It is rescaled to the
  shifted curve by `ref / (ref + offset)`, where `ref` is the hour's own high
  when the offset is positive and its own low when it is negative. This is a
  tight upper bound, and it is exact when the intra-hour peak is the hour's
  high.
- **Ulcer:** hourly closes from the first hour end through the hour of the
  last curve point, which is the latest `last_ms` among segments with trades.
  It is 0 when no segment has trades.
- **Monthly:** month-end closes; the last month ends at the last segment's
  `marked_final`.

Acceptance:
- a one-segment window equals the segment metrics (final and trades exactly;
  MaxDD% and Ulcer within float32 tolerance, |Δ| ≤ 1e-3 pp);
- a multi-segment window's Ulcer and monthly figures equal the same rules
  applied to the concatenated simulator paths;
- MaxDD% is never below the path value and exceeds it by at most a few
  hundredths of a point.

Windows that cut a segment need a fresh run (not part of stage 2).

### READY v4 from the store (`src/mrs3/ready_store.py`, CLI `scripts/ready_from_store.py`)

Rule version `ready-v4-store-1`. The thresholds come from the draft «Итоговые
правила READY v4» and P2.7 of the [plateau v4 plan](../superpowers/plans/2026-10-15-plateau-v4-validation-plan.md).
They were fitted on daily-scale Source v6 curves, so they are **provisional**
until they are recalibrated on store data (hourly Ulcer scale).

1. **Periods.** The last 3 active simulator segments of the pair, newest first.
   At least 2 are required, otherwise `NOT_ENOUGH_PERIODS`. The periods are
   restricted to the production-grid points present in all of them, because
   older segments may hold another TF set or extra shifts. An error in one pair
   gives that pair the status `ERROR` in the manifest (no catalog row) and
   never stops the run.
2. **Per period and point.**
   - `pnl30 = PnL% × 30 / days`;
   - `upi = pnl30 / max(Ulcer, 0.25)`;
   - `maxdd` = MaxDD%;
   - `trades`.
3. **Zone score per period.** The median `upi` of the 26 cube neighbours (shift
   index over the 15 grid shifts, Open MA 2–7, Close MA 2–7) inside the TF.
   Neighbours outside the grid or without data are ignored.
4. **Combined score.** A weighted mean over the periods, newest first: weights
   0.5/0.3/0.2 for 3 periods and 0.6/0.4 for 2.
5. **Active points.** Σ trades ≥ 6, at least 1 trade in every period, and
   shift ≥ 70 bp.
6. **Structure per TF.**
   - For every Close MA: per shift, the active Open MA with the best combined
     score; then the top 6 shifts by score.
   - Every pair of orders with `shift₂ ≥ 1.5 × shift₁` is a candidate.
   - Its score is the mean of the two orders' scores.
   - Its DD is the maximum over periods of the mean MaxDD% of the two orders.
   - The best structure per TF wins on: score, then lower DD, then lower
     shifts, then lower Close MA.
7. **Admission.** Score > 20 and DD ≤ 6.
8. **Risk category.** Low (≤ 2), moderate (≤ 3.5) or high (≤ 6). The `best`
   flag marks the highest score inside a category for the run.
9. **Pair rank.** Category (low, moderate, high), then score descending.
10. **Output.** A folder containing:
    - `strategies/<name>.json`: `strategy_json.generate_strategy` with the
      Bybit MRS3 template and EQUAL lots (ADR-0076);
    - `ready_manifest.json`: rule version and parameters; for each pair, the
      status and the period segment ids with their metrics SHA-256; for each
      admitted structure, its orders, point ids, score, DD, category, best flag
      and rank.

    One `ready` catalog row is written per pair:
    - `inputs_sha256` is the hash of the rule version and the period metrics
      hashes;
    - the payload is the pair's manifest entry without the cross-pair
      `best_in_category` flag, so it depends only on the pair's inputs.

    The same inputs give the same files.

**Stage 2 evidence (2026-10-17).**
- Independent review: `CODE_REVIEW_PASS`. Its 3 findings are fixed:
  - **major:** the intra-hour drawdown is now rescaled for later segments
    (regression test: +3000 offset, 300 intra-hour swing);
  - **minor:** cross-pair flag left out of the catalog payload;
  - **minor:** common grid points and a per-pair `ERROR` status.

  An independent pandas re-implementation of READY v4 on the pilot store
  agreed per TF on score, DD, shifts and MAs (tolerance 1e-9).
- The re-review gave `CODE_REVIEW_PASS`. It checked 400 random 3-segment
  windows: window MaxDD minus the chained-path MaxDD stayed within ±1e-5. It
  also asked that an `ERROR` pair writes no catalog row, which is now done.
- Tests:
  - `tests/test_trade_windows.py` 4 passed (hourly drawdown, one-segment
    window equals the metrics, multi-segment chaining, consecutive segments
    only);
  - `tests/test_ready_store.py` 4 passed (zone median, structure choice and
    activity, period weights, deterministic READY folder and catalog);
  - related suites together: 95 passed.
- Pilot store regenerated with the `drawdown` column: 5 pairs, 19 segments,
  196 s on 15 processes; hourly 97 MB.
  - Every one-segment window equals its metrics: final balance and trades
    exactly; MaxDD% and Ulcer within 1e-3.
  - A window over all 4 segments of a pair (3,780 points) takes 0.8 s.
- Pilot READY (`ready-v4-store-1`, end 2026-10-17):
  - only XOM is admitted, on all 7 TFs (score 43–53, DD 2.1–3.2,
    `moderate`);
  - SNOW, PURR, FLY and KORU are refused on the DD ceiling (best-TF DD 6.6–14 %;
    PURR scores 36–55).
  - Minute-level MaxDD of 20-day periods runs above the daily-scale Source v6
    MaxDD that set the 6 % ceiling and the category bands. These thresholds must
    be recalibrated on store data before READY from the store is used in
    production.

## Stage 3 contract: sampled tester runs

**Modules.**
- `src/mrs3/tester_sampling.py` — selection, block cover, block config.
- `src/mrs3/tester_jobs.py` — job planning, queue, local executor, HTML
  conversion, import into `tester` segments.
- CLI `scripts/tester_jobs.py` with the commands `plan`, `run` and `import`.

**Window.** A pair's tester window is the span of its latest active simulator
segment (the latest 20-day window of stage 1).

**Selection `select-v1`** (deterministic, from the window's simulator metrics):
- `upi = pnl30 / max(Ulcer, 0.25)`; a point is eligible when it has at least
  6 trades and PnL > 0;
- the top `ceil(10 % × grid points)` eligible points by `upi` (ties: lower
  `point_id`);
- plus every grid neighbour of a selected point in its TF cube (shift index,
  Open MA, Close MA; the 26 neighbours).

`selection_sha256` is the hash of the rule version, the window metrics hash and
the sorted point ids. Evidence: the selection-recall study in «Evidence base»
(10 % with neighbours is about 28 % of the grid and keeps 99 % of the tester's
top 10 %).

**Block cover `cover-v1`** — an exact cover without overlaps:
1. per (TF, Close MA, Open MA) the set of selected shifts;
2. equal shift sets of consecutive Open MAs merge into an Open range;
3. equal (TF, shifts, Open range) of consecutive Close MAs merge into a Close
   range;
4. blocks equal in everything but TF merge into one TF list.

Every block is one tester config: the multiplier `values` are the block shifts,
the Open and Close lengths are `start..end` with step 1, and `time_frame`
values are the block TFs. The sum of block runs equals the number of selected
points.

**Job.**
- One job per block: `job_id = sha256(pair, window, block, rule versions,
  selection hash)[:16]`.
- Package `jobs/<job_id>/`: `job.json`, `config_tester.json` and the MRS2
  strategy (`strategy_document`). The config uses `build_tester_config` with:
  - the block grid;
  - `order_limit_fill_plus_tick = true` and `LimitOrderVolumeCheck = false`;
  - `UpdateData = false`, since the executor makes sure the candles exist;
  - `name_comment = job_<job_id>`;
  - HTML report on with settings, summary, trades table and balance chart;
  - `max_parallel_runs` = `duckdb_import.workers`.
- Catalog `jobs` row states: `queued` → `running` → `done` | `failed`. A
  failed job goes back to `queued` only by an explicit retry. Planning is
  idempotent: an existing `job_id` is kept.
- The catalog table `plans` keeps the latest selection of each (pair, window).
  - Only queued jobs of the current plan run: the latest planned window of the
    pair and its latest selection.
  - Import takes that selection only.
  - Each selection is its own tester segment (engine
    `hb-tester-<selection[:8]>`), so a new selection supersedes the older
    segment. Imported jobs get `result_segment`.

**Local executor** (one at a time: the store lock file `jobs/.executor.lock`, and the
bot's shared `TesterTargetLock` held from the backup to the verified restore, so
no Panel tester caller touches the bot meanwhile):
1. Back up the bot's `config_tester.json` and every file of
   `tester/settings_strategy/` into `jobs/<job_id>/backup/`.
   - A backup that fails midway moves the files it already took back before
     reporting.
   - A copy whose locked original could not be deleted is dropped when the bot
     still has the identical file.
   - A backup that cannot be undone raises `RestoreError` and stops the
     executor (CLI exit code 2).
2. Put the job's config and strategy in place (the strategy folder holds only
   the job's strategy).
3. Run `hb_c.exe --run-tester` in the bot root with a timeout.
4. **Restore** the backup in a `finally` block and verify the hashes.
   Restoring wins over every other outcome: an unverified restore stops the
   executor.
5. Convert every `tester/report/job_<job_id>/*_run_*.html` into result tables
   (`jobs/<job_id>/result_{trades,hourly,metrics}.parquet`). Then delete that
   report folder unless `--keep-html` is given.
6. The job is `done` when every block point has exactly one report. Every exit
   path marks the job: a failure or `KeyboardInterrupt` gives `failed` after
   the restore.
7. **Stop and recovery.**
   - The store lock holds `<pid> <process start time> <job id>`, so a reused
     pid is not taken for a live executor.
   - `run` processes the queue one job at a time and stops between jobs when
     `jobs/.stop` appears (the Panel's Stop). Killing it would skip the
     restore.
   - The Panel clears a stale `.stop` before it starts a run; the run clears it
     only at its end, so a Stop pressed while the run starts up still counts.
   - Before running, `run` performs `recover`. While the lock's process is
     alive it refuses; otherwise it:
     - restores the tester's files from every job backup (or puts back the
       files of an unfinished backup);
     - marks interrupted `running` jobs `failed` (`INTERRUPTED`);
     - removes the stale lock.

     It is also available as `tester_jobs.py recover`.
8. **Zero-trade reports** are converted, not rejected (parsed with
   `allow_zero_activity`). Checked on the real tester: a 2026-10 zero-trade
   report has one equity point and Ulcer `n/a`.

**HTML conversion.** One report (parsed with
`parse_performance_report(report_format_2026_10=True)`) gives one point:
- **Point:** TF, `round((1 − multiplier) × 10000)` bp, Open and Close lengths;
  the symbol and report range must match the job.
- **Trades:** opened → closed pairs.
  - `qty` is the size; `fee_in` and `fee_out` are the fees.
  - `funding = balance after open + PnL − fee_out − balance after close`.
  - `exit_kind` is `market_end` when the closing fee rate is the taker rate,
    otherwise `limit`.
- **Hourly:** the equity (margin balance) series through `_hourly_aggregate`.
- **Metrics:** the last wallet value is the final balance; trades, wins, gross
  profit and loss, and fees come from the trades; MaxDD% and Ulcer come from the
  report («Max Drawdown, %», «Ulcer Index (hourly), %»); `last_ms` is the last
  equity stamp; `marked_final` is the equity before the final market close (or
  the final balance).

**Import.**
- Once every job of a (pair, window, selection) is `done`, the result tables are
  concatenated in `point_id` order and written as a segment with source
  `tester` and engine `hb-tester`, through `write_segment_files` and
  `register`.
- Superseding works inside the `tester` source only. Points outside the
  selection are absent from the tester segment.

**Catalog access.** `TradeStore.connect` waits up to 60 s for a catalog file
lock held by another process (DuckDB allows one writer or several readers).
While a store task runs, the Panel does not open the catalog: it shows the last
summary marked `stale`.

**Stage 3–4 review (2026-10-17).**
- First pass: `CODE_REVIEW_FAIL`, 8 findings. The blocker: the Panel's Stop
  killed a tester run without restoring the tester's files.
- Second pass: 7 of the 8 confirmed fixed. One new major (a locked strategy
  file blocked the executor) and 2 minors (a lost early Stop, a reused pid).
- Third pass: `CODE_REVIEW_PASS`.

**Stage 3 evidence (2026-10-17).**
- Tests: `tests/test_tester_jobs.py` 7 passed:
  - neighbours, selection, exact block cover on random subsets;
  - block config;
  - HTML conversion of a 2026-10 MRS2 report (funding, `market_end`, metrics,
    symbol and range checks);
  - planning idempotence;
  - executor with a fake tester (only the job's strategy in place, restore
    byte-identical, HTML deleted), import and window metrics on the tester
    segment;
  - failure paths: a crash, missing reports, a damaged backup giving
    `RestoreError`.
- Real tester, XOM latest window 2026-09-27…10-17 (pilot store):
  - `select-v1` took 827 of 3,780 points, covered by 27 blocks and 827 runs;
  - all 27 jobs `done`, about 540 s of tester time;
  - the bot's `config_tester.json` and strategy files were byte-identical
    before and after (SHA-256 snapshots);
  - imported as `bybit/XOMUSDT/tester_2026-09-27_2026-10-17_hb-tester`.
- Tester against simulator on those 827 points: trades exact on 99.8 %, all
  tolerances (PnL ±0.5 pp or 10 %, DD ±0.5, trades ±1, Ulcer ±15 %) on 99.8 %,
  rank correlation PnL 0.999, DD 0.999, Ulcer 0.998.
- READY with the newest period from the tester segment: 6 XOM strategies
  (7 from the simulator).
- The hamster-bot HTTP API (README of `ksandric/hamster-bot`) offers only
  `POST /update/config/tester` and strategy listings for the tester: nothing to
  start a test or fetch results. The executor therefore stays file based
  (`hb_c.exe --run-tester`) and needs no running bot.

## Stage 4 contract: Panel

**Module.** `src/mrs3/panel_store.py` (`StoreTaskService`, `store_summary`).
Panel routes:
- `GET /api/v2/store/status` — the task state plus a catalog summary;
- `POST /api/v2/store/start` — `{action, mode, pairs, end_day, download,
  limit}`;
- `POST /api/v2/store/stop`.

The card **MRS2 STORE** sits on the Testing screen after the runner cards.

- **One task at a time.** The work runs in the store CLIs as a child process:
  - `history` → `simulate_history.py`;
  - `plan`, `run`, `import` → `tester_jobs.py`;
  - `ready` → `ready_from_store.py`.
- **Stop.** For `run`, Stop writes `jobs/.stop` and the run ends after the
  current job, with the restore done. The other actions are terminated: their
  files are written atomically and rerun safely.
- **Safety.**
  - Parsed JSON results are path-redacted like the log.
  - A child process that cannot start leaves the task `failed`.
  - The service is created once per controller, under a lock.

  Worker pools never start inside the Panel server. The service keeps the last
  300 log lines (local paths redacted) and every JSON result line.
- **Mode.**
  - `simulation`: READY takes the newest period from the simulator.
  - `tester`: the tester actions are enabled, and READY takes the newest period
    from the imported tester segment of the same span. Without one the pair is
    `TESTER_PENDING` (`ready_store.pair_entry(latest_source=…)`, CLI
    `--latest-source`).
- **Periods.** Automatic. History uses the P2 grid up to the latest archived day
  (or the given end day). Missing candle days are downloaded unless the
  download box is cleared.
- **Inputs.**
  - Pairs: tickers `[A-Z0-9]+USDT` separated by commas or spaces; empty means
    the registry order (history) or every store pair (other actions).
  - End day: `YYYY-MM-DD`.
  - Limit: the number of tester jobs per run.
  - No shell is used; the argv is built from validated values only.
- **READY folders.** `<store>/ready/<date>_<mode>_<time>/`.
- **Status table.** Per pair:
  - active simulator periods, and the first and last day;
  - whether the newest window has a tester segment;
  - tester jobs by state.
- **Screener in simulation mode** — not part of this stage; to be agreed with
  the user.

**Stage 4 evidence (2026-10-17).**
- `tests/test_panel_store.py` 6 passed:
  - command validation per action and mode, and rejected inputs;
  - one task at a time, log redaction and parsed results;
  - failure and stop;
  - catalog summary;
  - real Panel HTTP routes (`_PanelServer`);
  - static card markup and endpoints.
- `tests/test_panel_static_ui.py` 173 passed; `node --check app.js` clean.
- `tests/test_ready_store.py` covers tester mode (`TESTER_PENDING`, then READY
  from the tester segment).

## Evidence base (2026-10-17)

- Slicing a continuous simulation into 20-day windows vs fresh window runs:
  KORU, SNOW, XOM 93–96 % of points within all tolerances, rank correlation
  1.000 (PnL, DD, Ulcer).
- Selection: top 10 % by the simulator with cube neighbours (about 28 % of the
  grid) contains 99 % of the tester's top-10 % points (5 pairs, full tester grids
  with `order_limit_fill_plus_tick = true`); PURR is the weakest (94.8 %).
- Simulator vs tester on those grids: XOM 99.1, FLY 97.3, SNOW 95.6, PURR 83.9,
  KORU 73.5 % of points within all tolerances; rank correlation 0.97–1.00.

## Open questions

- Selection thresholds per stage 3 (to be calibrated on more full tester grids).
- Hourly cache granularity is fixed at one hour (Ulcer rule); daily summaries can
  be added later if analysis needs them.
- Store location and backup on the server; remote executor sync (Syncthing or
  SSH) only if the tester load requires it.
