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

**Minute day files (2026-10-16).** The tester stores one file per symbol and UTC
day, `<SYMBOL><YYYY-MM-DD>_1m.parquet`, and no longer keeps CSV. The export
reads only these parquet files; a `_1m.csv` file is ignored (the day counts as
missing). A backfilled day is written in the tester's own format so the tester
can use it:
- columns `timestamp` INT64, `open`/`high`/`low`/`close`/`volume`/`buy_volume`/`sell_volume` DOUBLE, `trades` INT32;
- all columns REQUIRED;
- ZSTD compression;
- one key-value entry `hb.schema = candles_1m/1` and no Arrow schema blob.

Column types must match the tester schema exactly, otherwise the day is
invalid. Parquet doubles are converted to Decimal through their shortest round-trip
text, so the exact checks (`volume = buy_volume + sell_volume`, OHLC bounds,
positive trades, ordered in-day minutes) apply unchanged. Writing uses
`pyarrow`, a project dependency since this change; DuckDB cannot mark parquet
columns REQUIRED.

## Output and safety

**Layout (2026-10-16).** A — pair, B — listing date (both unchanged), C — the
10-day average `Среднее 10д, USDT`, D:M — ten date columns, oldest to newest.
Columns after M remain in place.

- **Average (C).** Recomputed on every saved run for every pair row: the mean of
  the numeric lots in D:M (blank and `ERROR:` cells are ignored), floored to the
  USDT step, integer format `#,##0`; blank when the pair has no numeric lot.
  A run whose dates are complete but whose averages differ from D:M is not a
  no-op. If C1 holds anything other than the average header, the run fails
  without replacing the workbook.
- **Previous layout (dates in C:L).** Detected by a date in C1. The run moves
  C:L one column right to D:M (values, styles, comments, column widths and
  filter criteria of those columns), then fills C with the average. Column M
  must be empty, otherwise the run fails without replacing the workbook. The
  move is one-off and limited: conditional formatting, data validation and sort
  state are not shifted, and a grouped `<col>` entry touching C:M is rewritten as
  single columns keeping only width and style (outline/hidden flags are lost).
- **Date headers.** A header may be a real date or strict ISO text `YYYY-MM-DD`
  (Excel tables store header cells as text); every saved run writes the ten
  headers back as real dates.

On first run, the recognized legacy layout (C — base lot, `Actual!D1` is
`Дата актуализации`) is replaced by the ten date columns D:M for the latest
ten UTC dates, and C becomes the average. The first run calculates every
eligible pair for all ten dates, not just the current date. Each date is the
header of a size column; its cells contain numeric lot sizes calculated from
the seven complete UTC dates immediately preceding that header date using
`K*V25*A15`. Existing data in E:M blocks initialization rather than being
overwritten. The `--rebuild-history` option explicitly recalculates all ten
current date columns when recovering a partially initialized workbook. Values
use integer format `#,##0`, without a decimal separator. The worksheet
AutoFilter includes all columns A:M (and extends through any populated columns
after M); one is created if the sheet has none. Criteria on A:C and columns
after M are retained (a filter on the average survives daily runs), while
criteria on rewritten date columns are cleared; legacy initialization also
clears criteria on C because the column changes meaning.

On later runs, the ten date columns are first aligned with the run date (see
below). Then every cell of every retained date is checked: a cell that holds a
numeric lot is kept and not recalculated; a blank, `ERROR:` or other non-numeric
cell is calculated when its pair is listed by that date (change of 2026-10-16:
previously only `ERROR:` cells of earlier dates were retried, so a pair added to
the sheet later kept blank earlier dates). A run whose ten current dates are
all filled is a no-op. Unresolved error cells remain red and are retried on
later runs until fixed or their date rolls out. On the next day, the
ten date columns shift left, the oldest date is dropped, and the new date is
calculated for every pair listed by that date. If one or more days were skipped,
the script rebuilds the latest ten consecutive dates by date key: values and
styles for overlapping dates move with their headers and are preserved, while
uncovered dates and retained non-numeric cells are calculated. A pair whose
listing date is later than a header date has a blank cell in that column. A
pair added to the sheet later receives values for every retained date on or
after its listing date. Per-pair/date failures are written as
`ERROR: ...` with a light-red fill. The command saves once through a temporary
sibling file and atomic replacement.

The explicit `--rebuild-history` operation replaces all ten overlapping output
columns only after every eligible symbol/date calculation succeeds. If any
calculation fails, the command reports the failure and leaves the workbook
unchanged. Its anchor must not be older than the newest existing history date.
The exported floor step must be a whole number so the integer display format
cannot round a fractional lot value; preserved fractional legacy cells retain
a decimal format until they are recalculated.

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
  recognized legacy outputs with the ten date columns and calculates all ten
  dates; `--rebuild-history` repairs a partially initialized ten-date sheet.
  Both paths reject unexpected populated output ranges; daily runs rotate,
  retries do not, and date gaps
  rebuild a contiguous ten-day history without mislabeling overlapping values.
- Errors and blank cells in retained history are filled on the next run
  (same date, next date or after a gap); numeric cells are kept; future listing
  dates remain blank until the date is reached.
- Day files are read only from the tester parquet; a backfilled day is written
  as parquet with the tester's schema (types, REQUIRED, ZSTD, `hb.schema`).
- Valid and failed symbol/date cells share one atomic workbook save; an empty
  universe does not replace the workbook.
- Workbook feature guards reject unsupported charts and tables before any
  replacement.

Openpyxl rewrites legacy comment package parts and can drop calculation-chain
or printer-settings packaging metadata; the source workbook is replaced only
after all calculations and validation have completed.

## Pair registry stage 4 note (2026-10-19)

- The export reads the base lots from the pair registry (LOT_MODEL_V2,
  ADR-0079) by default. `--source minute-files` keeps the minute-file
  calculation described above.
- The workbook records the model of its values (defined name
  `MRS3_BASE_LOT_MODEL`). A run of the other model refuses to add to an
  existing history; only `--rebuild-history` replaces all ten dates.
- Contract: [pair registry](2026-10-18-pair-registry.md), «Stage 4 contract».
