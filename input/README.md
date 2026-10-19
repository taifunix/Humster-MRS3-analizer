# Private listing-date input

Place the operator-provided listing-date workbook here as `dates.xlsx` before
importing tick-test results into PerformanceDB or running a Performance v2
retest. The file is
intentionally ignored: it may contain local operational data and is not part
of the repository.

To use another location, set `panel_workflow.listing_dates_path` in
`config.local.json`.

## TradFi liquidity registry

The operator-maintained Bybit TradFi liquidity workbook
`bybit_tradfi_liquidity.xlsx` may also be placed here (ignored by Git). The
Performance v2 import can read listing dates from it: point
`panel_workflow.listing_dates_path` at it; only the symbol and Bybit
listing-date columns of the `Пары` sheet are read and the file is never
rewritten.
The pair screener that used its `Скрининг` sheet was removed in Panel rework
stage 1a (`docs/specs/2026-10-18-panel-simulation-workflow.md`); the pair
registry (`docs/specs/2026-10-18-pair-registry.md`) replaces the workbook.
