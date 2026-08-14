# Post-test Sequential Selection Design

## Goal

Produce one deterministic final-selection pipeline per `symbol + side + timeframe`
from real tester results, before later portfolio analysis.

## Contract

1. Reject only adverse distribution outliers. Within each scope, reject a row
   when `holding_p95_minutes > Q3 + 1.5 * IQR` or
   `trades < Q1 - 1.5 * IQR`. Short holding and high trade counts are never
   rejected by this filter.
2. On eligible rows, retain the Pareto front maximizing `pnl30_dd5` and
   minimizing `capital_requirement_proxy`.
3. On stage-1 survivors, retain the Pareto front maximizing both
   `capital_efficiency_30` and `first_shift_bp`.
4. Only when stage 2 leaves more than three rows in that same
   `symbol + side + timeframe` scope, retain the Pareto front
   maximizing `capital_efficiency_30` and minimizing `common_close_ma`.
5. If stage 2 leaves three or fewer rows in a scope, those rows are final
   without stage 3. Candidate counts from different timeframes are never added
   together for this decision.

## Output

`18_Final_Comparison` exposes the scope thresholds, eligibility, stage-1,
stage-2, stage-3 and final flags. Rows with missing filter or Pareto inputs are
not final and carry an explicit reason. Existing diagnostic Pareto columns stay
available but do not define the final selection.

The workbook opens with two decision sheets:

- `00_Selection_Summary`: one row per `symbol + side + timeframe`, with input,
  filter, stage and final counts plus the two IQR thresholds;
- `01_Finalists`: only rows where `selection_final=TRUE`, sorted by scope and
  descending `pnl30_dd5`.

The full raw, normalized, comparison and holding-audit sheets follow unchanged.

## Non-goals

- No cross-pair or cross-timeframe comparison.
- No forced top-three truncation after the final Pareto front.
- No portfolio simulation or DD5 retest.
