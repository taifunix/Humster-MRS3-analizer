# MRS2 trade store, history simulation and sampled tester runs

**Status:** Active (stage 1 next); decision [ADR-0078](../decisions/0078-mrs2-trade-store.md)
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

One row per trade: `point_id` (int32), `entry_ms`, `exit_ms` (int64; exit
NULL = open at the segment end), `entry_price`, `exit_price`, `qty` (float64),
`fee_in`, `fee_out`, `funding` (float64), `exit_kind` (limit / market_end).
Segment metadata (also in the catalog): exchange, symbol, source, start, end,
engine version, run settings (lot, fees, slippage, funding, warm-up,
`limit_fill_plus_tick`), price tick, qty step, data days used, SHA-256.

### Hourly series (Parquet, ZSTD)

Per point and UTC hour of the segment: margin balance close, min, max (float32).
Rebuildable from candles + trades; used for windowed MaxDD and Ulcer.

### Catalog tables

`segments` (active / superseded), `points`, `runs` (engine and rule versions,
settings), `jobs` (tester queue: block definition, state, executor, result
segment), `ready` (READY outputs with inputs' hashes and rule version).

## Stages

1. **Store and history simulation.** Segment writer and reader, hourly cache,
   catalog, a CLI that simulates a pair's full history to `today − 20 days` and
   the latest 20-day window (fresh start), pairs in registry priority order,
   parallel with `duckdb_import.workers`. Acceptance: windowed metrics from the
   store equal the simulator's per-window results; a pair's history writes and
   reads back bit-identically; idempotent reruns.
2. **Window analysis and READY.** Window metrics (PnL, trades, WinRate, MaxDD,
   Ulcer, monthly) from segments + hourly series, vectorised; plateau v4 zone
   scores over the last 2–3 windows; READY JSON (EQUAL only) with provenance.
3. **Sampled tester runs.** Selection rule (versioned; base: top 10–20 % by the
   simulator plus cube neighbours), rectangular block cover, job queue with
   states, executor (local tester first), HTML trade-table parsing into
   `tester` segments, restore of the tester's own files.
4. **Panel.** Mode switch (simulation / tester), per-pair periods, progress,
   journal; screener in simulation mode (to be agreed).

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
