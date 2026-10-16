# ADR-0072: Keep only core import sections in MRS3 tester reports

**Status:** Accepted
**Date:** 2026-10-14

## Context

The current tester renamed `report.include_strategy_settings` to
`report.include_settings` and added optional analytics, heatmap, position-stat,
and Monte Carlo report controls. Performance v2 requires the HTML settings,
summary metrics, action rows, and balance/equity series. The separate analytics
section and monthly/regime tables are optional to core import. The required
Metric/Value summary can still contain scalar analytics labels, so disabling
the analytics section does not guarantee that every optional scalar metric is
empty. Extra charts, position statistics, Monte Carlo, and reports-history CSVs
are not read by PerformanceDB.

## Decision

- Enable HTML, settings, summary metrics, actions, and balance/equity output.
- Disable analytics, monthly-return heatmap, OHLC/position charts, position
  statistics, Monte Carlo, timing logs, and reports-history output.
- Emit the current `include_settings` key only.
- Retain `report.analytics_monte_carlo_seed=42` from the supplied current-format
  config as inert metadata; `include_analytics=false` and
  `analytics_monte_carlo=0` leave Monte Carlo analysis disabled.
- Keep Bybit's tester margin/fill controls explicit using the supplied config
  values; keep target-drawdown thresholds at zero.
- Keep account credentials and account identity outside the shared templates.
- Verify generated reports through Performance v2 collection, without
  importing them into the database during the tester run.

## Consequences

- Core Performance v2 imports remain possible. The separate analytics and
  monthly/regime tables are absent in the verified reports, while scalar
  analytics values present in the required summary remain importable.
- HTML omits unconsumed visual and position sections; Monte Carlo and history
  CSV work are disabled.
- Turning on optional analytics later requires an explicit report-profile
  change and a new collection run for the affected strategies.
