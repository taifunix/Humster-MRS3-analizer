# MRS3 minimal Performance v2 report profile

**Status:** Implemented / live-verified
**Date:** 2026-10-14
**Decision:** [ADR-0072](../decisions/0072-mrs3-core-import-report-profile.md)
**Related contracts:** [PerformanceDB new tester report format](2026-10-12-performance-db-new-report-format.md), [MRS3 evaluation protocol v2](2026-10-12-mrs3-evaluation-protocol-v2.md)

## Goal

Render MRS3 Single Mode reports in the current tester's configuration schema,
keeping the HTML sections required by the Performance v2 core importer while
disabling optional report sections and auxiliary report generation.

## Non-goals

- Change tester simulation semantics, Performance v2 parsing/schema/import, or
  the MRS3 evaluation protocol.
- Import reports into a PerformanceDB as part of tester execution.
- Put per-machine account credentials or account names in shared templates.

## Contract

- Core import inputs stay enabled: HTML, strategy settings, balance/equity,
  summary metrics, and the actions table.
- The separate analytics section and monthly-return heatmap stay disabled.
  The required Metric/Value summary can still contain scalar analytics labels;
  this profile does not guarantee all optional scalar columns will be NULL.
- OHLC/position charts, position statistics, Monte Carlo, timing logs, and
  reports-history outputs stay disabled.
- Use the current tester key `report.include_settings`; do not emit the legacy
  `include_strategy_settings` alias.
- Single Mode uses an empty `parameter_mining` list. Account configuration
  remains local to the tester installation.
- Both exchange configs explicitly set the new tester's fill options false,
  target drawdowns zero, and reports-history options false. Bybit additionally
  sets its exchange-specific `maintenance_margin_rate` to 0.005; BingX omits
  that Bybit-only key.
- `analytics_monte_carlo` is set to zero, with the separate analytics section
  and timing logs off. Keep `analytics_monte_carlo_seed=42` from the supplied
  current-format example; it is report metadata and does not affect this run
  while analytics is disabled and the simulation count is zero.

## Acceptance evidence

- Config writer tests assert the exact core/optional report profile and ensure
  stale top-level report switches are absent.
- Both exchange MRS3 templates use the current report key and explicit report
  options; the Bybit template records the provided tester controls.
- The related verification set passes 594 tests with one Windows symlink skip.
- A live 20-strategy Single Mode run for 2026-09-13 through 2026-10-13 reaches
  COMMITTED, and the report collection reaches VERIFIED with 20 entries.
- The read-only Performance v2 parser accepts all 20 reports as format
  `2026-10` / tester `2026/10/10`; all contain settings, Metric/Value data,
  action-table structure, wallet series, and equity series. Across the
  collection are 268 action rows and 25,999 wallet/equity samples per series.
  No monthly or regime rows are present. An HTML marker scan finds no OHLC
  chart, position chart, position-statistics, or timing sections in any report;
  no Monte Carlo text is present. The required summary still supplies 792
  scalar analytics values across the 20 reports.
- No PerformanceDB import was launched.
