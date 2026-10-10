# Bybit optimizer base lot history export

## Goal

Provide one command that reads symbols from the first column of the `Actual`
sheet in `Input/bybit_tradfi_liquidity.xlsx`, makes official Bybit daily minute
files available, computes dated optimizer base lots, and writes a rolling
10-day history to the same workbook.

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
- Anchor: timezone-aware UTC run anchor; current UTC date by default, or a fixed
  ISO timestamp supplied with `--anchor-created-at`.
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

Columns A and B (pair and listing date) remain unchanged. On first run, the
recognized legacy layout (`Actual!D1` is `Дата актуализации`) is replaced by
ten size columns C:L for the latest ten UTC dates, oldest to newest. Each date
is the header of a size column; its cells contain numeric lot sizes calculated
from the seven complete UTC dates immediately preceding that header date
using `K*V25*A15`. Existing data in E:L blocks initialization rather than
being overwritten. Other columns after L remain in place. The worksheet
AutoFilter includes all columns A:L (and extends through any populated columns
after L); one is created if the sheet has none. Criteria on A/B and columns
after L are retained, while criteria on rewritten output columns are cleared.

On later runs, a complete current date is a no-op unless an earlier retained
date still contains an `ERROR:` cell. Incomplete cells for pairs already listed
by the date are retried in place; unresolved error cells remain red and are
retried on later runs until fixed or their date rolls out. On the next day, the
ten date columns shift left, the oldest date is dropped, and the new date is
calculated for every pair listed by that date. If one or more days were skipped,
the script rebuilds the latest ten consecutive dates by date key: values and
styles for overlapping dates move with their headers and are preserved, while
only uncovered dates and retained error cells are recalculated. A pair whose
listing date is later than a header date has a blank cell in that column. Pairs
added after earlier dates keep those earlier cells blank and receive values
from the date they first appear onward. Per-pair/date failures are written as
`ERROR: ...` with a light-red fill. The command saves once through a temporary
sibling file and atomic replacement.

The export accepts ordinary `.xlsx` workbooks only. The raw XLSX package is
checked for modern chart, drawing/image, table, pivot, slicer, OLE, and VBA
parts before openpyxl loads it; legacy VML comment drawings are allowed because
they are present in the current workbook. Unsupported workbooks are rejected
before publication. Per-symbol/date errors are printed and make the command
return a nonzero exit code after the workbook is saved. Workbook-level
validation errors fail without replacing it.

## Acceptance evidence

- Decimal formula floors without binary floating point and excludes `B`.
- A fixed anchor produces ten output dates, each calculated from the seven
  complete dates immediately before its header date.
- Existing official files are reused; missing files use official archive
  backfill with no more than 16 workers.
- First run preserves pair/listing columns and filter criteria on A/B, replaces
  recognized legacy outputs with the ten date columns, and rejects unexpected
  populated output ranges; daily runs rotate, retries do not, and date gaps
  rebuild a contiguous ten-day history without mislabeling overlapping values.
- Errors in retained history are retried; future listing dates remain blank
  until the date is reached.
- Valid and failed symbol/date cells share one atomic workbook save; an empty
  universe does not replace the workbook.
- Workbook feature guards reject unsupported charts and tables before any
  replacement.

Openpyxl rewrites legacy comment package parts and can drop calculation-chain
or printer-settings packaging metadata; the source workbook is replaced only
after all calculations and validation have completed.
