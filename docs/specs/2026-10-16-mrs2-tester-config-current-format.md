# MRS2 tester configs in the current tester format (RUNNER 01 and SCREENER 01)

**Status:** Implemented
**Date:** 2026-10-16
**Decision basis:** [ADR-0072](../decisions/0072-mrs3-core-import-report-profile.md) (core import report profile)
**Related contracts:** [BingX data downloader](2026-10-10-bingx-data-downloader.md), [Pair screener](2026-09-18-pair-screener.md), [MRS3 core import report profile](2026-10-14-mrs3-core-import-report-profile.md)

## Goal

On 2026-10-16 the Panel `bot_root` moved to the current tester installation.
The four MRS2 tester templates used by RUNNER 01 and SCREENER 01
(`templates/tester/mrs2/config_tester_{long,short}{,_screen}.json`) were still in
the previous tester's schema. Rendered as is, the current tester writes HTML
reports that the project parser rejects (`metrics contain duplicate or empty
keys`), so Source v6 import of RUNNER 01 reports would fail. The templates are
moved to the current schema with the core import report profile.

## Non-goals

- Change the MRS2 grid (`parameter_mining`), dates, balances, fees, warm-up,
  `LimitOrderVolumeCheck`, or the strategy templates.
- Change the HTML parser, Source v6 import or screener evaluation.
- Change the MRS3 exchange templates (ADR-0072 already covers them).

## Contract

- Top-level keys are those of the current tester config, in its order. The
  options the previous schema did not have use the current tester's neutral
  values: `single_mode=false`, `order_trigger_fill_worst=false`,
  `order_limit_fill_plus_tick=false`, `order_market_delay=false`,
  `target_drawdown_open=0`, `target_drawdown=0`, and
  `maintenance_margin_rate=0.005` (Bybit-only key; BingX mode removes it, as
  the BingX MRS3 template omits it).
- `report` — core import profile of ADR-0072: HTML, `include_settings`
  (never the legacy `include_strategy_settings`), summary table, trades table
  and balance chart on; OHLC/position charts, analytics, monthly-return
  heatmap, position statistics, Monte Carlo and timing logs off;
  `analytics_monte_carlo_seed=42` kept as inert metadata.
- `reports_history` — enabled with `ulcer_index=true` (other optional columns
  off), as in the reference `grid750` runs: SCREENER 01 evaluates pairs from
  `reports_history*.csv`, and CSV Direct reads the same format with
  `UlcerIndex`.
- Rendering (`render_tester_config`, screener renderer) is unchanged except the
  BingX mode removal of `maintenance_margin_rate`.

## Acceptance evidence

- Tests assert the exact report and reports-history profile and the current
  key set of all four templates, and that the BingX render omits
  `maintenance_margin_rate`.
- Live check on the current tester (2026-10-16), one-point grids rendered by
  RUNNER 01 for BingX TSLL (23 trades) and Bybit CLSK (34 trades): reports
  parse with the project parser and normalise to Source v6 fragments; the
  screener reads `reports_history*.csv` (written as `reports_history_p<pid>.csv`)
  with `UlcerIndex`. The same run from the unadapted template produced HTML
  the parser rejects.
