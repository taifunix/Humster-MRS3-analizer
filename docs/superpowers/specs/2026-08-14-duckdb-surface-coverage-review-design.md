# DuckDB Surface Coverage Review Design

## Goal

Add a read-only coverage review step to the `Import -> surface -> analysis -> JSON`
panel flow so the user can see which `Pair + Side + TF` combinations exist in
source DuckDB, which continuous interval is currently usable, and which gaps
must be backfilled before materialization.

## Contract

- The panel adds a source-DuckDB coverage scan before surface materialization.
- Coverage is aggregated at `Pair + Side + TF`.
- The right-side panel area that currently hosts `Current operation` is reused:
  before materialization it shows the coverage review; during materialization it
  keeps the existing progress bar and log output.
- Coverage rows are grouped by a `Pair + Side` subheader.
- Each subgroup renders one aligned four-column table:
  `Select | TF | Available interval | Gap`.
- `Select` is a checkbox and is the only interactive cell in the row.
- `TF`, `Available interval`, and `Gap` remain vertically aligned across every
  row in the subgroup.
- Coverage uses only factual source-DuckDB windows derived from
  `report_start/report_end`. There is no external expected range.
- For each `Pair + Side + TF`, windows are sorted and merged only when they form
  a continuous chain without an internal gap.
- If exactly one merged chain exists, the row is selectable and
  `Available interval` shows that chain.
- If multiple merged chains exist, the row is diagnostic-only and not
  selectable.
- For diagnostic rows, `Available interval` shows the longest merged chain.
- `Gap` lists every missing interval in chronological order as
  `missing: YYYY-MM-DD .. YYYY-MM-DD`.
- Rows with any listed gap have a disabled checkbox and are excluded from
  auto-materialization.
- Rows without gaps show `Gap = none` (or equivalent) and may be selected.
- Auto-materialization consumes only the checked selectable rows.
- The existing materialization progress bar and log window remain in the right
  panel for the running build phase and are not removed by this feature.

## Data Flow

1. The user opens the DuckDB surface workflow.
2. The panel requests a coverage scan from source DuckDB.
3. The backend returns grouped `Pair + Side + TF` coverage rows with:
   checkbox eligibility, longest continuous interval, and explicit missing
   intervals.
4. The user reviews the table and checks only selectable rows.
5. Materialization starts only for the selected rows.
6. Once materialization starts, the right-side panel switches from coverage
   review to the existing progress/log presentation for that run.

## Non-goals

- Do not infer missing periods from any expected symbol schedule.
- Do not auto-fill or auto-repair gaps.
- Do not mix this inventory step with required-shift readiness checks.
- Do not remove the existing progress bar or journal area.

## Verification

- Controller tests cover deterministic coverage aggregation from factual
  `report_start/report_end` windows.
- Gap tests prove that rows with multiple merged chains expose every missing
  interval, show the longest usable chain, and disable selection.
- UI tests prove the right-side review layout contains grouped `Pair + Side`
  headers and aligned `Select | TF | Available interval | Gap` columns.
- Materialization request tests prove only checked selectable rows are sent to
  the build path.
