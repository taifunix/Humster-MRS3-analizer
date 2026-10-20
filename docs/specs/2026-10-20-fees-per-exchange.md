# One fee schedule per exchange, set on «Настройки», for every calculation and test

**Status:** requirements approved by the user 2026-10-20; **not implemented**. It
is the next implementation plan after the sync / history / tag work
([progress.md](../../progress.md)). Implement spec-first, TDD, with an independent
review; this file is the starting point and must be completed (inputs, outputs,
invariants, acceptance evidence) before the code.
**Depends on:** [Panel for the simulation workflow](2026-10-18-panel-simulation-workflow.md)
(«Настройки»), [MRS2 trade store](2026-10-17-mrs2-history-store-and-tester-sampling.md),
[P7 exchange templates](2026-10-13-panel-performance-db-selection.md),
[ADR-0077](../decisions/0077-mrs2-base-limit-fill-plus-tick.md).

## Requirements (user, 2026-10-20)

1. **One fee schedule per exchange**, set by hand on «Настройки»: maker and
   taker fee, with slippage and funding as the schedule needs. Every
   calculation and test of that exchange takes it, so a result never depends on
   which tool produced it.
2. **The Stage 2 rule `MakerFee = 0` for Bybit is removed**, together with the
   «lifelong zero»: Stage 2 tester runs take the exchange's schedule like
   everything else (decision of the user, 2026-10-20, answering the open
   conflict).
3. **No exchange name in the code decides a fee.** A new exchange gets its
   schedule on «Настройки» (ADR-0084 for tags and `KNOWN_EXCHANGES` follows
   the same idea).

## Where the fees live today (read 2026-10-20)

| Place | What it holds | Used by |
| --- | --- | --- |
| `RunSettings()` defaults, `mrs2_simulator.py` | maker 0.00001, taker 0.0002, slippage 0.001, funding 0.00025 / 8 h | the Bybit MRS2 simulator history (`exchange_run_settings`, `scripts/generate_period_csv.py`) |
| `templates/exchanges/BingX/mrs3-tester.json` | maker 0.00012, taker 0.0003, funding | the BingX simulator (`exchange_run_settings`) and the BingX MRS3 tick test |
| `templates/exchanges/Bybit/mrs3-tester.json` | maker 1e-05, taker 0.0002 | the Bybit MRS3 tick test, retest |
| `templates/exchanges/Bybit/portfolio-tester.json` | maker 1e-05, taker 0.0002 | the optimizer's Stage 2 tester template |
| `templates/tester/mrs2/config_tester_long.json`, `..._short.json` | maker 0.00001, taker 0.0002 | MRS2 tester sampling (`tester_jobs`) |
| `panel_testing._BINGX_TESTER_SETTINGS` | maker 0.00012, taker 0.0003 | a second, hard-coded BingX copy |
| `panel_portfolio.py` (Stage 2 start) | `MakerFee = 0.0` for Bybit-only batches | the operator rule that is removed |
| `exchange_profiles.CHECKED_TESTER_FIELDS` | the launch check compares strategy and tester config with the templates' `MakerFee`, `TakerFee`, `FundingRate`, `FundingIntervalHours` | every protocol-v2 launch |
| `portfolio/margin.py` | reads `MakerFee` / `TakerFee` from the manifests it is given | the optimizer's margin / fee model |
| registry `pairs.maker_fee`, `taker_fee` | the fee the exchange reports per pair (reference data, not a setting) | informational |

Observed differences that this spec removes: the Bybit simulator, MRS2 tester
templates and MRS3 templates agree today, but they are four copies; BingX has
three (template, hard-coded dict, simulator); Stage 2 uses 0.

## Questions to settle in the spec

- **Where the schedule is stored:** `config.local.json` (local, per machine,
  like the other panel settings) or the pair registry (synced between
  machines, append-only)? Fees affect results, so a synced, journaled value
  may be better; decide with the user.
- **How the templates change:** the exchange template fee fields become a
  fallback only, or are removed and rendered from the schedule at launch. The
  launch check (`CHECKED_TESTER_FIELDS`) must then compare with the schedule,
  not with the template (P7 contract: a change needs its own review).
- **What a change does to stored results:** a segment records the
  `settings_json` it was simulated with. A changed schedule should mark the
  affected segments stale, as a changed tick size does
  ([history spec](2026-10-20-panel-history-incremental.md), requirement 5);
  PerformanceDB results keep the fee of their report.
- **Slippage, funding, funding interval:** part of the schedule or separate
  settings of the exchange.
- **The 40 % BingX discount** (user, 2026-10-16: maker 0.00012, taker 0.0003)
  is a value of the schedule, not code.

## Non-goals

- No change of the fee formulas of the simulator or of the optimizer's margin
  model.
- No change of stored PerformanceDB results.

## Acceptance evidence (to fill when implemented)

- one test per consumer (simulator history, MRS2 sampling, MRS3 tick test,
  retest, Stage 2) shows the value of the schedule reaching it;
- Stage 2 no longer forces `MakerFee = 0`;
- the launch check compares with the schedule;
- changing the schedule marks the dependent segments for recomputation.
