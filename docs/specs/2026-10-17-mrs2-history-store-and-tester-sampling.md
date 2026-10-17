# MRS2 history store and sampled tester runs (draft for discussion)

**Status:** Draft, details to be agreed with the user
**Date:** 2026-10-17
**Builds on:** [MRS2 period CSV generator](2026-10-16-mrs2-period-csv-generator.md), [ADR-0077](../decisions/0077-mrs2-base-limit-fill-plus-tick.md)

## Goal

Replace the fixed 20-day period CSVs (needed only because the tester's CSV mode
returns no trades) with a per-pair history of simulator trades from which any
window can be analysed, and run the real tester only on a deterministic,
analysis-driven sample of points for the latest ~20 days.

## Evidence that supports the scheme (2026-10-17)

Slicing one continuous simulation into 20-day windows versus a fresh simulation
of each window (the tester's semantics: flat start, market close at the end),
all 3780 grid points:

| Pair | Windows | Trades exact | PnL in tolerance | DD | Ulcer 15 % | All | Rank corr. |
| --- | --- | --- | --- | --- | --- | --- | --- |
| KORU | 3 | 95.3 % | 97.0 % | 98.1 % | 99.9 % | 95.7 % | 1.000 |
| SNOW | 3 | 100 % | 100 % | 100 % | 95.7 % | 95.7 % | 1.000 |
| XOM | 2 | 99.4 % | 99.4 % | 100 % | 93.6 % | 93.0 % | 1.000 |

Differences come only from window boundaries (a position or an order state
carried across). Training on several 20-day periods keeps working on sliced
history; official windows can be re-simulated fresh in seconds.

## Proposed scheme

1. **History.** Per pair, the simulator runs the full grid (3780 points) from
   `listing + WarmupDays` to `today − 20 days`, and separately the latest
   20-day window (fresh start). Output per pair and run: `trades.parquet`
   (point id, entry/exit ms, prices, size, fees, funding; ~40 B per trade,
   about 1–2 GB for the whole base) and optionally an hourly margin-balance
   series per point (~2 KB per point and 20 days). Minute candles stay in the
   tester data folder; the minute equity of any point is rebuilt from trades +
   candles exactly as the simulator computes it.
2. **Windows on demand.** Analysis slices the history into windows of any length
   (fast, vectorised), or re-simulates a window fresh when tester semantics are
   required (READY, CSV Direct export). CSVs in the tester format are produced on
   demand, not stored in advance.
3. **Sampled tester runs for the latest window.** A deterministic, versioned
   rule picks per pair the points worth a real run (for example: passes a loose
   economic filter on the simulator's latest window and history, or ranks in the
   top N %), plus their cube neighbours (shift × Open × Close) so zone scores are
   complete. The rule version and a hash of the selection are journalled.
4. **Tester jobs.** The tester's parameter mining is a Cartesian product, so the
   sample is covered by a minimal set of rectangular blocks (TF × multipliers ×
   Open range × Close range), one config per block, deterministic for the same
   input. A job queue runs blocks one by one: install config and strategy, run
   with the HTML trade table only, parse trades into the same `trades.parquet`
   format, delete the HTML, restore the tester's own files. States
   `queued / running / done / failed`; restart resumes; done jobs are never rerun.
5. **Merge.** The latest window gets tester trades for the sampled points and
   simulator trades for the rest (source per point is stored). Each new window
   also re-measures simulator versus tester agreement.

## Open questions

- Selection rule and thresholds; the share of good points lost by the sample is
  to be measured on full tester grids (5 pairs, `order_limit_fill_plus_tick=true`).
- Whether hourly series are stored or always rebuilt.
- CSV Direct: load from the trade store directly or keep CSV export only.
- Data repository layout for the trade store and the job journal.
