# Source v6 → period CSV generator (tester `reports_history` format)

**Status:** Draft, work in progress (branch `wip/period-csv-generator`): calibration of the MRS2 simulator
**Date:** 2026-10-16
**Plan:** [plateau v4 validation](../superpowers/plans/2026-10-15-plateau-v4-validation-plan.md) — P2 (periods), A12a (separate periods), «Итоговые правила READY v4»
**Handoff:** [remote handoff, part C](../superpowers/plans/2026-10-16-remote-handoff-bingx-parquet-source-v6-csv.md)

## Goal

Build period CSVs in the exact tester `reports_history.csv` + `run_snapshot_*.json`
format from the full Source v6 MRS2 databases, so history does not have to be
re-run in the tester. The CSVs are loaded through CSV Direct into the analysis
database, later extended by real tester runs, and feed READY v4.

## Non-goals

- Import into the analysis database or the data repository (next stage).
- Merging CSV Direct (`feat/csv-direct-analysis`) into `main`; the branch is only
  used to check the generated files.
- SHORT, exchanges other than Bybit, shifts outside 70–550 bp.

## Inputs

- Source v6 databases (`*.duckdb`), read only through `source_v6_storage`.
- Tester minute candles `<bot_root>/tester/data/bybit/<SYMBOL>/<SYMBOL><day>_1m.parquet`
  (format: `src/mrs3/tester_candles.py`).
- Listing dates: project registry (`panel_workflow.listing_dates_path`).
- Tester settings of the target runs (reference `grid750` configs): lot 10 000
  fixed (`use_fix=true`, `use_upnl=false`), maker 0.00001, taker 0.0002,
  slippage 0.001, funding 0.00025 every 8 h, `LimitOrderVolumeCheck=false`,
  `WarmupDays=4`.

## Grid

LONG; shifts 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470,
510, 550 bp (multiplier `1 − bp/10000`); TF 15m, 30m, 45m, 1h, 2h, 3h, 4h (5m
behind a flag, off by default); Open/Close MA 2–7, SMA ohlc4. A TF missing for
a pair is skipped and logged.

## Periods (P2)

From listing + 4 days (or a given start date) to the end of the last full UTC
day covered by Source v6 (`EndDate` = start of the next day). The span is cut
into the maximum number of full 20-day periods; the remainder is added to the
last one (20–39 days). One CSV = one pair × one period.

## Semantics (reproduce the tester)

Calibrated against the current tester, which reproduces the reference
`grid750` rows exactly (48 of 48 probe points, all metrics).

1. **Trades.** A Source v6 cycle is entry (first fill of the open order) →
   exit (first reducing fill of the close order). Entry/exit prices come from
   the fill fee (maker) and the closing PnL. At lot 10 000 without volume
   check, the order fills completely at the first fill; size =
   `floor(10000 / entry price / qty step) · qty step`.
2. **Period start without a position.** Only cycles opened at or after the
   period start are used; a cycle open at the start is dropped.
3. **Period end.** A position open at `EndDate` is closed at market on the last
   minute candle before `EndDate`: price · (1 − slippage 0.001), taker fee
   0.0002; it counts as a trade (verified on a tester trade table).
4. **Equity.** Balance + size · (minute close − entry price) while in a
   position; funding `size · price · 0.00025` at 00:00/08:00/16:00 UTC while in
   a position.
5. **Metrics.** `TotalPnL = FinalBalance − 10000`, `TotalPnLPercent` =
   `TotalPnL / 10000 · 100`; `MaxDrawdown(Percent)` — peak-to-trough of the
   minute equity path (exact on the Source v6 reports); `Win`/`Los` and
   `ProfitFactor` on gross trade PnL (`n/a` without losses); `UlcerIndex` —
   hourly equity, see «Open questions»; `PositionAvgPercent`,
   `PositionMaxPercent`, `MonthlyReturn_*` — see «Open questions».

## Output

- `reports_history.csv` with the reference header (incl. `UlcerIndex`, `Run id`,
  `RunTag`, `MonthlyReturn_YYYY_MM` of the period months, `settings[*]…`),
  `PairsCount = 1`, one row per point.
- `run_snapshot_<name>.json` in the reference shape: `basic.symbol` = pair,
  `use_fix=true`, `use_upnl=false`, `my_fix_balance=10000`, SMA ohlc4 MAs.
- Period journal (JSONL): pair, side, period, source `source_v6_generated`,
  database name and SHA-256, CSV SHA-256, rows, status, listing date, flags.

## Gate before any import

Generate the reference windows (`grid750_G1_train_hist`, `grid750_G2_train_hist`)
and compare point by point with the reference on the common grid (shifts
70–550, TF 15m–2h): differences, share in tolerance, rank correlation per pair.
Working tolerances: PnL 0.5 pp or 10% relative, DD 0.5 pp, trades ±1, Ulcer 15%
relative, ≥ 95% of a pair's points. Pairs outside are not imported (fix the
cause or mark «rerun in the tester»).

## Calibration findings (2026-10-16)

Truth source: the current tester reproduces the reference `grid750` rows exactly
(48 of 48 probe points, every metric); single points are re-run with HTML trade
tables for trade-level checks.

- **Ulcer (hourly)** — developer answers plus fit: curve = margin balance
  (wallet + unrealized PnL); hourly value = last curve value of the hour, an hour
  without points carries the previous value; running peak; `UI = √(Σdd²/N)`;
  the index covers hours only up to the curve's last point (the tester's last
  action), hours after it are not counted. With equal trades: 98–100 % of points
  within 15 %, median error ≈ 0.
- **Period end:** an open position closes at market on the last minute candle
  before `EndDate`: price × (1 − 0.001), taker 0.0002, counted as a trade.
- **Funding:** `size · price · 0.00025` at 00/08/16 UTC while in a position.
- **Formats:** `ProfitFactor` empty without losing trades; `UlcerIndex` empty
  without trades; CSV without BOM, LF.

### Engine 1: replay of Source v6 cycles

Exact when Source v6 has the same trades (98–100 % of such points), but Source v6
was run with `LimitOrderVolumeCheck=true`: partial exits left residual positions
that blocked new entries, so trades are missing. Reference gate, all metrics in
tolerance: CXMT 98 %, LLY 94 %, RDW 92 %, SNOW 91 %, XOM 87 %, FLY 77 %, IONQ 76 %,
PURR 73 %, REGN 57 %, CLSK 56 %.

### Engine 2: MRS2 simulator on tester minute candles (no Source v6)

2–5 ms per point (a pair's grid in 5–13 s). Rules found so far:
- TF bars on the full minute grid (minutes without trades are flat candles at the
  previous close); order price = SMA(ohlc4) of closed bars × multiplier, rounded
  to the tick; limit fills at the order price;
- orders are re-placed at every TF bar start; a candle at the bar's first minute
  still meets the previous order; trading starts after the first bar start of the
  period;
- MRS2 keys (strategy): open order `post_only=true` (rejected placement is
  retried until it rests), close order `post_only=false` (a close price below the
  market executes at market with slippage), `distance_filter=0.3` (no orders
  while the close-MA price is < 0.3 % above the open-MA price),
  `replace_open_order` concerns partial fills only.

Reference gate (all metrics), current best: XOM 93 %, REGN 87 %, SNOW 78 %;
trades ±1: 89–99 %. Next step: trade-level diffs against tester HTML runs on
several pairs/TF/shifts to finish the bar-start placement rules.

## Known limits

- Source v6 was run with `LimitOrderVolumeCheck=true` at lot 100/1000: limit
  orders that did not fill there are absent; the tester at lot 10 000 without
  the check fills them. Such points miss trades and PnL.
- A cycle open at the period start is dropped; the fresh tester run may trade
  in that gap.

## Open questions (calibration)

- Hourly Ulcer: the tester's rule is not reproduced for low-activity points.
- `PositionAvgPercent` and `MonthlyReturn_*` formulas.
