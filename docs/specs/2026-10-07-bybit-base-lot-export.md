# Bybit optimizer base lot export

## Goal

Provide one command that reads symbols from the first column of the `Actual`
sheet in `Input/bybit_tradfi_liquidity.xlsx`, makes the seven frozen official
Bybit daily minute files available, computes the optimizer base lot, and writes
it to columns C and D of the same workbook.

The exported value is the liquidity model base only:

```text
base_lot_usdt = floor_to_step(K * V25 * A15, round_down_usdt)
```

The distance and order geometry factor `B` is deliberately excluded. This
export does not run the optimizer, tester, Panel, or database.

## Inputs and defaults

- Workbook: `D:\SHARE\!MN\hamster\MRS-Analizer\Input\bybit_tradfi_liquidity.xlsx`.
- Sheet: `Actual`; symbols are nonblank values in its first column after the
  header row.
- Bybit minute data root:
  `D:\SHARE\!MN\hamster\hb\tester\data\bybit`.
- Anchor: one timezone-aware UTC run anchor. The command uses the current UTC
  time unless `--anchor-created-at` supplies a fixed ISO timestamp.
- Frozen archive lag: 6 hours.
- Base coefficient `K`: exact Decimal `9`.
- USDT floor step: exact Decimal `10`.
- Backfill workers: 16 maximum.

The command wrapper is `scripts\calculate_bybit_base_lots.cmd`; it invokes the
repository `.venv` interpreter and forwards command-line options.

The existing `calculate_minute_capacity(..., lot_model=True)` helper is the
source of `V25`, `A15`, the seven-day window, and file hash evidence. Missing
files are downloaded only from the official public Bybit daily trade archive
and converted through the existing validated backfill path.

## Output and safety

Column C contains the numeric floored base lot. Column D has the header
`Дата актуализации`; successful rows contain the current UTC date with the
`yyyy-mm-dd` display format and failed rows contain `ERROR: ...` with a
light-red fill and General number format. A failed symbol clears its C value while other symbols
continue. The command saves the workbook once through a temporary sibling file
and atomic replacement after all rows are processed.

The export accepts ordinary `.xlsx` workbooks only. The raw XLSX package is
checked for modern chart, drawing/image, table, pivot, slicer, OLE, and VBA
parts before openpyxl loads it; legacy VML comment drawings are allowed because
they are present in the current workbook. Unsupported workbooks are rejected
before publication because this round-trip path is intentionally limited to the
current ordinary workbook. Per-symbol errors are printed to stdout and
make the command return a nonzero exit code after the workbook is saved.
Validation failures affecting the workbook itself, including a missing symbol
universe or malformed symbol text, fail without replacing it. A valid symbol's
data/download failure is row-scoped and is recorded in column D.

## Acceptance evidence

- Decimal formula floors without binary floating point and excludes `B`.
- A fixed anchor produces the expected seven dates and reproducible values.
- Existing official files are reused; missing files use the official archive
  backfill with no more than 16 workers.
- Valid and failed symbols share one atomic workbook save; an empty universe
  does not replace the workbook.
- The CLI has verified success and error paths and has no separate report
  option or JSON output.
- Workbook feature guards reject unsupported charts and tables before any
  replacement; the ordinary production workbook has no rejected modern parts.

Read-only copy smoke against the configured production workbook (using an
invalid fake archive fetch, so no network data was consumed) produced zero
unrelated cell changes and preserved the existing comment. Openpyxl rewrites
legacy comment package parts and drops calculation-chain/printer-settings
packaging metadata; the source workbook is never changed by this command.
- The command wrapper invokes the local `.venv` interpreter.
