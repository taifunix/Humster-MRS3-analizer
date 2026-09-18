# Private listing-date input

Place the operator-provided listing-date workbook here as `dates.xlsx` before
starting fresh Source v6 analysis or a Performance v2 retest. The file is
intentionally ignored: it may contain local operational data and is not part
of the repository.

To use another location, set `panel_workflow.listing_dates_path` in
`config.local.json` to a path relative to the repository root.

## TradFi liquidity registry

Place the operator-maintained Bybit TradFi liquidity workbook here as
`bybit_tradfi_liquidity.xlsx` before running the pair screener. It supplies
the candidate pair universe (`Пары` sheet: symbol, listing date, liquidity
metrics) and is where the screener records its own findings in a separate
`Скрининг` sheet it owns (verdict, BIG_SHIFT, researched window, final
decision) — the other sheets (`Пары`, `По дням`, `Оборот 30д`, `Методика`)
are refreshed by a separate liquidity process and are read-only to the
screener. The file is intentionally ignored; see
`docs/specs/2026-09-18-pair-screener.md` for the exact sheet contract.

To use another location, set `screener.liquidity_registry_path` in
`config.local.json` to a path relative to the repository root.
