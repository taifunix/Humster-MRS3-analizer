# Performance v2 robust finalist selection and ranking

**Status:** Implemented and independently reviewed (`CODE_REVIEW_PASS`)
**Date:** 2026-09-01
**Depends on:** [Performance v2 finalist selection and XLSX](2026-08-31-performance-v2-finalist-selection.md)

## Purpose

Extend the disposable Performance DB v2 finalist pipeline with explicit
robustness checks, a controlled preference for a larger first-order Shift, and
a deterministic final Top-N ranking. The operator must still be able to change
stage order, enabled state and ordinary stage scope before refreshing counters
or producing XLSX.

## Boundaries and non-goals

- Read only ACTIVE current results from Performance DB v2.
- Reuse `strategy_actions`, `strategy_equity` and versioned `window_metrics`;
  no database schema migration is required.
- Keep every existing filter and Pareto stage available. The new recommended
  stages change defaults, not the closed historical algorithms.
- Do not persist selection runs, tags, discard, RETEST, ranking history or
  edited-XLSX imports. Those remain Stage 3.
- Do not claim out-of-sample proof, portfolio performance or independence
  between finalists.
- Do not add correlation clustering or strategy-neighbour analysis in this
  iteration.

## New facts

### Completed round trips

Reconstruct completed round trips from ordered `strategy_actions`. A round
trip starts with `opened`, may include `increased`/`decreased`, and completes
when an action leaves exact DECIMAL `post_size = 0`. The stored size is not a
binary float, so no approximate-zero rule is introduced. Its realised PnL is
the sum of `pnl` on its `decreased` and `closed` actions; positive PnL is a
profit and negative PnL is a loss. `fee` remains a separate imported fact and
is not subtracted again, matching the existing Performance v2 PnL and Profit
Factor semantics. An incomplete tail is excluded from every concentration
sum but retained as a diagnostic count.

A side flip without an intervening flat action is not a canonical completed
trip. If it occurs, concentration facts for that result are unavailable and
the filter does not eliminate.

For each strategy derive:

```text
completed_trade_pnl
best_trade_pnl = max(completed_trade_pnl)
gross_positive_trade_pnl = sum(max(completed_trade_pnl, 0))
pnl_without_best_trade = sum(completed_trade_pnl) - best_trade_pnl
best_trade_profit_share_pct =
    best_trade_pnl / gross_positive_trade_pnl * 100
completed_profitable_trade_count = count(completed_trade_pnl > 0)
```

The dependency filter is evaluated only when the action sequence is reliable
and `completed_profitable_trade_count >= 4`. A result with zero to three
profitable completed trades still exports diagnostics but is not eliminated by
this stage. The minimum count defaults to `4` in
`best_trade_min_profitable_trades`.

### Four chronological windows

Split each result's requested `[report_start_utc, report_end_utc]` interval into
four equal chronological intervals. Calculate every interval through the
existing safe flat-boundary window calculator and store it in the existing
versioned `window_metrics` cache. A window is positive when its geometrically
normalised 30-day return is greater than zero.

An interval is available only when the existing calculator returns
`availability_status = AVAILABLE`, including sufficient equity samples and at
least one realised completed trip. `NO_TRADES`, collapsed or otherwise
unavailable intervals are not interpreted as zero return. A return exactly
equal to zero is available but not positive. Flat-boundary adjustment may make
the four effective wall-clock durations unequal.

Derive `positive_quarter_count`. The consistency filter is evaluated only when
all four intervals are available; otherwise it does not eliminate.

The selection cache readiness contract expands from full/A/B to full/A/B plus
the four chronological windows. `Recalculate facts` and `Recalculate all
pairs` warm all seven requested windows while loading each result source once.
The existing cache key already includes exact requested start/end timestamps
and `metrics_version`; a change to boundary/calculation semantics requires a
new metrics version. Legacy caches containing only full/A/B are reported not
ready and require recalculation. Until all seven rows exist, missing quarter
facts never eliminate.

Acceptance includes a live before/after timing measurement for one large Pair
+ Side. No duration is promised before that measurement; the implementation
must retain the existing bounded worker pool and one source load per result.

### Conservative robustness facts

When both inputs are present:

```text
robust_pnl_30d_pct = min(ab_return_a_30d_pct, ab_return_b_30d_pct)
worst_drawdown_pct = max(max_drawdown_pct, ab_drawdown_b_pct)
worst_holding_p95_minutes =
    max(holding_p95_minutes, ab_holding_p95_minutes)
ab_stability_ratio = min(A, B) / max(A, B), only when A > 0 and B > 0
minimum_plateau_point_count = min(points for every existing order)
```

Missing inputs remain missing and never fabricate zero.
`robust_pnl_30d_pct` requires both A and B; worst DD requires both full and B
DD; worst holding requires both full and B holding; A/B stability requires two
positive inputs; minimum plateau points requires a count for every configured
order.

## New ordered stages

All new movable stages default to `pair_side_timeframe`. Missing required facts
do not eliminate or dominate.

### `filter_best_trade_dependency`

Enabled by default. Eliminate when either condition holds:

```text
pnl_without_best_trade <= 0
best_trade_profit_share_pct > 35
```

The `35%` and minimum-four-profitable-trades defaults are loaded from
`unified_performance_v2.finalist_selection.best_trade_max_profit_share_pct`
and `best_trade_min_profitable_trades`.

### `filter_time_consistency`

Enabled by default. Eliminate an evaluable strategy when fewer than three of
its four chronological windows have positive 30-day-normalised return.

Defaults are fixed for this iteration at four windows and a minimum of three
positive windows.

### `pareto_robust`

Enabled by default. Ordinary Pareto dominance:

- maximize `robust_pnl_30d_pct`;
- maximize `first_shift_bp`;
- minimize `worst_drawdown_pct`;
- minimize `worst_holding_p95_minutes`.

Including first Shift here prevents a structurally wider entry from being
discarded before the explicit near-tie preference stage.
Only rows with every objective present participate: an incomplete row can
neither dominate nor be dominated.

### `pareto_shift_near_tie`

Enabled by default and ordered after `pareto_robust`. Strategy A eliminates B
when all conditions hold:

```text
first_shift_bp_A >= first_shift_bp_B + min_shift_advantage_bp
robust_pnl_30d_pct_A >= robust_pnl_30d_pct_B * (1 - pnl_tolerance_pct / 100)
worst_drawdown_pct_A <= worst_drawdown_pct_B
worst_holding_p95_minutes_A <= worst_holding_p95_minutes_B
```

Both robust PnL values must be positive. `pnl_tolerance_pct` is a finite value
in `[0, 100)`, defaults to `10`, and is exposed as one compact input in the
stage row. This relation is calculated from the complete survivor group and
is independent of row order. `min_shift_advantage_bp` defaults to exact integer
`10` bp (`0.1%`) in configuration; no floating-point epsilon is required.
Rows missing any compared fact can neither eliminate nor be eliminated.

The stage is one simultaneous pass against the complete pre-stage survivor
set. A row marked for elimination may still eliminate another row in that same
pass; results never depend on iteration or input row order.

### `pareto_close_ma_near_tie`

Enabled by default as movable stage 10 with `pair_side_timeframe` scope.
It uses the same positive robust-PnL tolerance, DD and holding conditions as
`pareto_shift_near_tie`, but Strategy A eliminates B only when
`close_ma_len_A < close_ma_len_B`. Equal Close MA values do not eliminate
either strategy. Missing facts remain non-evaluable and do not eliminate.

## Fixed final ranking

`rank_robust_top_n` is a separate final stage, enabled by default with
`top_n = 50`. It is displayed below the movable stage list, has fixed
`pair_side` scope, and cannot be reordered. The request parser rejects it when
it is not last, has another scope, is duplicated, or has a non-positive
integer `top_n`.

The ranking population is exactly the pre-stage survivor set entering the
fixed final stage for the selected Pair + Side, after every movable stage. It
therefore compares timeframes only through dimensionless or already
time-normalised facts, except Close MA which is an explicit relative preference.
It does not use raw trade count or timeframe duration. Scores are group-relative
and are not comparable between different Pair + Side selections.

For each metric, convert present values to deterministic quality percentiles
from `0` (worst) to `1` (best), using average rank for ties. A single present
value receives `1`. PnL, DD, A/B stability, Shift and Close MA percentiles use the full
Pair + Side survivor set. Minimum plateau-point percentiles are calculated
inside each timeframe before pooling, avoiding a raw grid-density preference
between timeframes. Holding remains wall-clock minutes and is used only by the
timeframe-scoped Pareto stages, not by the cross-timeframe score. A and B PnL
are the same geometric 30-day-normalised percentage for every timeframe.

For a missing component, omit its weight and renormalise over the candidate's
present components:

```text
weighted_sum = (
    0.38 * percentile(robust_pnl_30d_pct, higher is better)
  + 0.17 * percentile(worst_drawdown_pct, lower is better)
  + 0.15 * percentile(ab_stability_ratio, higher is better)
  + 0.10 * percentile(first_shift_bp, higher is better)
  + 0.10 * percentile(minimum_plateau_point_count, higher is better)
  + 0.10 * percentile(close_ma_len, lower is better)
)
score = 100 * weighted_sum / sum(weights of present components)
```

The DD component is the ascending-quality percentile: a smaller non-negative
DD receives higher quality; it is not `1 / DD`. Zero DD is valid and best,
negative DD is invalid/missing. If no component is present, score and rank are
unavailable, the row receives `RANK_NOT_EVALUATED_INSUFFICIENT_DATA`, and the
Top-N ranker does not eliminate it. This safety exception can make the final
sheet exceed N only by explicitly unranked rows.

Final order is deterministic:

1. score descending;
2. `robust_pnl_30d_pct` descending, missing last;
3. `worst_drawdown_pct` ascending, missing last;
4. `first_shift_bp` descending, missing last;
5. `strategy_id` ascending.

The ranker assigns `final_rank` to every rankable survivor. When enabled, ranks
greater than `top_n` receive `eliminated_by_rank_robust_top_n = true` and are
not finalists. The ranker, not a separate workbook truncation, defines the
`Finalists` sheet. Ties crossing N are cut deterministically by the listed
tie-breaks, so exactly N rankable rows are retained. When disabled, the ranker
is fully inert: it emits no rank or score and removes no survivor. If at most `top_n`
rankable candidates survive, all remain finalists. Default-on ranking is an
intentional change to the default finalist set; disabling every new stage and
the ranker restores the prior pipeline behavior.

## Recommended default order

1. `filter_holding_outlier`
2. `filter_low_trades` (disabled)
3. `filter_min_shift` (`0.3%`, disabled)
4. `ab_deterioration`
5. `filter_best_trade_dependency`
6. `filter_time_consistency`
7. `pareto_dd5_balanced`
8. `pareto_robust`
9. `pareto_shift_near_tie` (`10%` PnL tolerance)
10. `pareto_close_ma_near_tie` (`10%` PnL tolerance)

The fixed `rank_robust_top_n` stage (`50`) runs after all enabled movable
stages. Other disabled alternatives retain their movable positions after this
default sequence.

The five new movable stages default to `pair_side_timeframe`; Holding, A/B and
Balanced Pareto retain `pair_side`. Existing alternative Pareto stages remain
visible but disabled by default.

## Counter and XLSX contract

- Automatic counters include all enabled movable stages and the fixed final
  ranker.
- `All candidates` retains every requested strategy.
- `Finalists` contains final ranks up to the enabled Top-N cap plus any explicit
  `RANK_NOT_EVALUATED_INSUFFICIENT_DATA` safety rows.
- Enabled new stage booleans follow the submitted stage order at the end of the
  workbook, as in the existing contract.
- Add diagnostic columns: best-trade share, PnL without best trade, positive
  quarter count, robust PnL, worst DD, worst holding p95, minimum plateau
  points, A/B stability, every ranking-component percentile, final score and
  final rank. Also export present-weight coverage and each effective
  renormalised component weight so partially scored rows are auditable.
- Panel and XLSX label the final score as relative to the selected Pair + Side.
- Numeric diagnostics remain numeric Excel cells; existing rounding and hidden
  strategy identity behavior remain unchanged.
- `PnL without best, %` is the PnL after removing the best completed trade,
  divided by the result's initial balance. The raw amount remains an internal
  selection fact and is not exported.
- `1 Shift` is the sole exported first-shift column; the duplicate summary
  `Shift 1` is not exported. `Positive trades`, rank coverage, and effective
  rank weights remain present as hidden audit columns.
- Per-order opening MA values are exported as one `MA` text column, ordered
  from first to fourth as `MA1 / MA2 / MA3 / MA4`.
- Per-order plateau point counts are likewise exported as one `Points` text
  column, ordered first-to-fourth, rounded to integers and separated by `/`.
- Per-order lot values are likewise exported as one `Lots` text column in
  first-to-fourth order, as rounded integer percentages.
- The consolidated `MA` text uses rounded integer values. `PnL`, `PnL/30`,
  `PnL DD5/30`, `PF`, both A/B PnL columns, and `PnL without best, %` are
  numeric XLSX cells rounded and displayed as integers.
- `Positive quarters` follows `PnL B/30д, %`; `PF` follows `CE`; `PointsMin` follows `PointsALL`; `ORD` precedes `Close`. `Final rank` is a
  visible integer column immediately before `Final`. `Worst DD` and `A/B
  stability` are hidden audit columns. Widths use data values.
- `Best trade, %` and `PnL without best, %` are hidden audit columns. Final
  rank values are bold. Muted double vertical borders delimit the PnL, hold,
  shifts and points blocks, plus the standalone `MA`, `Final rank` and `Close`
  columns. Every cell from column E onward is horizontally centred; `Lots` and
  `MA` widths are calculated from their values rather than headers.
- In the exported `Причина` column, `PARETO_PLATEAU_POINTS_PER_ORDER` is
  shortened to `PARETO_PL_PTS_PER_ORDER` for readability.
- Exported elimination reasons are prefixed with the one-based position of the
  enabled stage that actually applied. Rows are filled with a pastel stage
  colour from blue through red; finalists are light green.
- Enabled filter columns use `BLOCK` for an excluded strategy and `PASS` for a
  strategy that passed that filter.
- `Robust PnL/30`, `Worst Hold p95`, and the legacy five `Rank q` diagnostics
  remain in the workbook as hidden audit columns. `Rank q Close MA` follows
  them directly before `Final rank`; both are hidden, as is `Final score
  (Pair+Side)`.

## Acceptance evidence

- Round-trip tests cover increased/decreased positions, separate fee semantics,
  incomplete tails, side flips and the zero-to-three-profitable-trades
  non-evaluation boundary.
- Window tests cover exact four-way boundaries, safe flat snapping, unavailable
  windows, cache readiness and one-source-load recalculation.
- Stage tests prove every new rule, both ordinary scopes, missing-data safety,
  row-order independence and the 10% Shift tolerance boundary.
- Ranking tests prove exact weights, percentile ties, missing components,
  deterministic tie-breaks, Top-N capping, disabled ranking and mandatory-last
  validation.
- Permutation tests prove shuffled candidate input produces identical stage
  decisions, scores and ranks for robust Pareto, Shift near-tie and ranking.
- A baseline regression proves all new stages and ranking disabled preserve the
  previous selection output.
- Readiness tests prove legacy full/A/B-only caches cause zero new-stage
  eliminations until recalculated.
- Panel tests prove default order/checks/scopes, compact inputs, automatic
  counters and XLSX readiness.
- Workbook tests prove diagnostics, rank, enabled-stage columns and numeric cell
  types.
- Relevant focused tests, JavaScript syntax check, `git diff --check` and
  independent review pass before commit.
