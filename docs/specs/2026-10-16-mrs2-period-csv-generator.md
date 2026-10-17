# MRS2 period CSV generator (simulator, tester `reports_history` format)

**Status:** Active, implementation in progress (branch `wip/period-csv-generator`)
**Date:** 2026-10-16
**Plan:** [plateau v4 validation](../superpowers/plans/2026-10-15-plateau-v4-validation-plan.md) — P2 (periods), A12a (separate periods), «Итоговые правила READY v4»
**Handoff:** [remote handoff, part C](../superpowers/plans/2026-10-16-remote-handoff-bingx-parquet-source-v6-csv.md)
**Related:** [tester day file format](2026-10-10-bingx-data-downloader.md) («Формат дня (parquet тестера)»), [MRS2 tester configs](2026-10-16-mrs2-tester-config-current-format.md)

## Goal

Produce, without re-running history in the tester, period CSVs identical in
format to the tester's `reports_history.csv` + `run_snapshot_*.json` for MRS2
LONG on Bybit pairs. History periods come from a MRS2 simulator on the tester's
own minute candles; the latest period of every pair is left to a real tester run
(the generator writes its tester config). The CSVs are loaded through CSV Direct
into the analysis database and feed READY v4.

## Decision (user, 2026-10-16)

- Source v6 is not used: its equity is sparse and its trades miss fills (it was
  run with `LimitOrderVolumeCheck=true`). The simulator replaces it.
- Missing minute data is downloaded automatically on request into the tester's
  data folder (`<bot_root>/tester/data/bybit/<SYMBOL>/`), in the tester format.
- The latest period is run by the real tester.
- Pairs are processed most liquid / most promising first.

## Non-goals

- Import into the analysis database or the data repository (next stage).
- Merging CSV Direct into `main`; its loader is only used to check the output.
- SHORT, exchanges other than Bybit, 5m (behind a flag, off by default).

## Inputs

- Pairs and listing dates: liquidity registry (`panel_workflow.listing_dates_path`,
  sheet with listing dates); order: screening verdict GO, CHECK, others; inside a
  group by 30-day turnover, descending.
- Minute candles: `<bot_root>/tester/data/bybit/<SYMBOL>/<SYMBOL><day>_1m.parquet`.
  A missing day is built from the official Bybit trade archive
  (`minute_capacity.fetch_bybit_trade_archive` + backfill, written with
  `os.link`, never overwritten). Verified 2026-10-16: an archive-built day equals
  the tester-written day in every row and field (SNOW, XOM 2026-09-10).
- Instrument parameters: price tick and quantity step from Bybit
  `instruments-info` (cached per run).
- Tester run settings (reference `grid750` configs): `InitialBalance` and fixed
  lot 10 000 (`use_fix=true`, `use_upnl=false`), maker 0.00001, taker 0.0002,
  slippage 0.001, funding 0.00025 / 8 h, `LimitOrderVolumeCheck=false`,
  `WarmupDays=4`; strategy `templates/strategies/source-v6-mrs2/long.json` with
  `my_fix_balance=10000`, `more_data=false`.

## Grid

Shifts 70, 90, 110, 140, 170, 200, 230, 270, 310, 350, 390, 430, 470, 510, 550 bp
(multiplier `1 − bp/10000`); TF 15m, 30m, 45m, 1h, 2h, 3h, 4h; Open/Close MA 2–7,
SMA ohlc4: 3780 points per pair and period.

## Periods (P2, CSV Direct rules)

- Span: from `max(listing + WarmupDays, requested start)` to the start of the
  current UTC day (the last full day must have its archive published; otherwise
  the span ends a day earlier).
- Cut into the maximum number of full 20-day periods; the remainder joins the
  last one (20–39 days). One CSV = one pair × one period; `StartDate`/`EndDate`
  are the period bounds.
- Every period except the last: simulator → CSV. The last period: tester config
  + strategy for a real run (CSV only, current tester format, `reports_history`
  with `UlcerIndex`).
- A period whose warm-up (`StartDate − WarmupDays`) has no candles is not
  generated (journal status `NO_WARMUP_DATA`).

## Simulator (tester semantics, calibrated 2026-10-16)

Calibrated against the current tester, which reproduces the reference `grid750`
rows exactly. Rules:
- TF bars on the full minute grid (a minute without trades is a flat candle at
  the previous close); order price = SMA(ohlc4) of the closed bars ×
  multiplier, rounded to the price tick; limit orders fill at their price.
- Orders are placed at every TF bar start and right after each fill; a candle at
  the bar's first minute still meets the previous order; trading starts after
  the first bar start of the period.
- Close (sell) order priced at or below the market → placed one tick above the
  last price; post-only open (buy) order priced at or above the market → rests
  one tick below it; `distance_filter = 0.3` %.
- Fixed lot: size = `floor(10000 / entry price / qty step) · qty step`; fees
  maker on limit fills; funding `size · price · 0.00025` at 00/08/16 UTC while
  in a position; a position open at `EndDate` closes at market on the last
  candle: price · (1 − 0.001), taker fee, counted as a trade.
- Metrics: `TotalPnL = FinalBalance − 10000`; `MaxDrawdown(Percent)` —
  peak-to-trough of the minute margin-balance path; `Win`/`Los`,
  `ProfitFactor` on gross trade PnL (empty without losses); `Fills` =
  `TotalTrades`; `UlcerIndex` — hourly margin balance (last value of the hour,
  carried through empty hours, running peak, `√(Σdd²/N)`), hours only up to the
  last curve point, empty without trades.

Reference gate (10 pairs, 27 000 points, tolerances PnL 0.5 pp or 10 %, DD
0.5 pp, trades ±1, Ulcer 15 %): 77.5 % of points within all tolerances; trades
exact 80 %; rank correlation of PnL, DD, Ulcer ≈ 0.99; UPI top-10 % overlap per
pair 74–95 %. Further rule work improves the tail; it does not change the
output format.

## Output

`<out>/<SYMBOL>/<YYYY-MM-DD>_<YYYY-MM-DD>/`:
- `reports_history.csv` — exactly the tester's header and value formats
  (`ReportUtc`, `NameComment`, …, `UlcerIndex`, `Run id`, `RunTag`,
  `MonthlyReturn_YYYY_MM` of the period months, `settings[*]…`), `PairsCount=1`,
  one row per point, UTF-8 without BOM, LF;
- `run_snapshot_<name>.json` — `settings` (the strategy with the pair symbol,
  SMA ohlc4, fixed lot 10 000), `tester_config` (the run settings and grid),
  `program_config` empty (machine-local in the tester).

Last period: `<out>/<SYMBOL>/<start>_<end>_tester/config_tester.json` and the
strategy file.

Journal `<out>/journal.jsonl`, one line per period: pair, side, start, end,
source (`simulator` / `tester_pending`), CSV SHA-256, rows, listing date,
status, data days downloaded, generator version.

## Acceptance evidence

- Unit tests on synthetic candles: periods, bars and order prices, fills,
  period end, funding, Ulcer rule, CSV/snapshot format.
- Reference gate re-run with the production code on the 10 reference pairs.
- Generated CSVs load with `load_csv_direct` (branch `feat/csv-direct-analysis`).
- Header and value formats byte-compared with a tester `reports_history.csv`.

## Known limits

- `PositionAvgPercent`: the tester's averaging rule is not reproduced; the
  generator writes the mean of cost / margin balance at entries (tester values
  are 1–6 pp lower; not used by CSV Direct or READY). `PositionMaxPercent` uses
  cost / margin balance (wallet + unrealized PnL) at entries and exits.
- The simulator matches the tester exactly in about 80 % of points; the rest
  differ by one or more trades (rules of order placement at bar starts not fully
  reproduced). Ranking is preserved (rank correlation ≈ 0.99).

## Next stages (user request 2026-10-16, details to be agreed)

- Panel: control of the generator and of CSV Direct tester runs with a mode
  switch (simulation or real tester), automatic period calculation per pair,
  progress and journal view.
- SCREENER 01 in simulation mode (rework of the current screener; details to be
  discussed).
