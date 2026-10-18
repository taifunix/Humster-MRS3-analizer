# Private listing-date input

Place the operator-provided listing-date workbook here as `dates.xlsx` before
starting fresh Source v6 analysis or a Performance v2 retest. The file is
intentionally ignored: it may contain local operational data and is not part
of the repository.

To use another location, set `panel_workflow.listing_dates_path` in
`config.local.json`.

## TradFi liquidity registry

The operator-maintained Bybit TradFi liquidity workbook
`bybit_tradfi_liquidity.xlsx` may also be placed here (ignored by Git). Fresh
Source v6 analysis can read listing dates from it: point
`panel_workflow.listing_dates_path` at it; analysis reads only the symbol and
Bybit listing-date columns from the `Пары` sheet and never rewrites the file.
The pair screener that used its `Скрининг` sheet was removed in Panel rework
stage 1a (`docs/specs/2026-10-18-panel-simulation-workflow.md`); the pair
registry (`docs/specs/2026-10-18-pair-registry.md`) replaces the workbook.
