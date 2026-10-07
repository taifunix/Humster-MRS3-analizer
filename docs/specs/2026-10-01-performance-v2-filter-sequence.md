# Performance v2 Panel: agreed filter sequence

Status: implementation contract. Date: 2026-10-01.

## Goal and scope

Make screen `4. Pareto and filters` apply the agreed stages in a fixed
prefix, preserve the historical selection ledger, and expose clear reasons in
the Panel and exported workbook. This supersedes the corresponding rule and
order sections of the earlier finalist-selection, lot-variant and A/B specs.

Inputs are the current ACTIVE Performance v2 strategies, their current result,
versioned windows/equity facts and reliably completed action cycles. Outputs
are a disposable preview, a finished XLSX, an immutable selection snapshot,
durable equity rejection evidence, and `REJECTED` tags for published exclusions
at stages 2–4.

The approved source is the [filter decision ledger](../superpowers/plans/2026-10-01-performance-v2-filter-review.md),
[ADR-0052](../decisions/0052-performance-v2-hard-cutoff-rejected.md),
[ADR-0053](../decisions/0053-performance-v2-dd-profit-guard.md), and
[ADR-0058](../decisions/0058-performance-v2-filter-rejected-status.md).
Research XLSX PnL/30 cutoffs, later Pareto redesign, tester runs, deletion,
portfolio simulation and historical snapshot rewriting are outside scope.

## Stage order and request contract

The server and Panel fix this prefix, independent of submitted order:

1. `filter_equity_regime` (existing rule, still default OFF);
2. `filter_lot_variant_redundancy` (new rule, default ON);
3. `filter_hard_cutoffs` (new, default ON);
4. `ab_deterioration` (new rule, default ON);
5. `filter_best_trade_dependency` (now top-five, default ON);
6. `filter_min_shift` (default ON, threshold `0.3%`);
7. `pair_side_pnl_upper_half` (default ON);
8. `structural_stage_1` (default ON);
9. `structural_stage_2` (default ON);
10. `pair_side_stage_3` (default ON).

The minimum-Shift stage is fixed immediately before the PnL DD5/30 + PnL B/30
stage. Later existing stages retain their relative order and current defaults. An
enabled stage only evaluates survivors of its predecessors. Missing facts do
not become zero or cause exclusion. Every request has one stage entry per ID;
the current Panel request includes `filter_min_shift` enabled with the values
above. Legacy API requests without that stage retain their existing handling;
the server does not inject a new stage into those historical requests. When
the stage is present, server order is authoritative, and snapshots preserve
the effective order and config.

The Panel does not persist the stage order or enabled set in local storage or a
server preset; each page load starts from this documented default. A request
that includes `filter_min_shift` must provide its typed `min_shift_pct` and
scope fields; malformed older or hand-built requests fail closed with the
existing invalid-stage error instead of silently disabling the filter.

For each enabled stage, `eliminated` counts incoming rows that do not continue
to the next stage, including rows assigned `RESERVE`; `remaining` counts only
rows passed forward. Thus incoming rows equal `eliminated + remaining`, and
`reserved` is a diagnostic subset of `eliminated`. In particular, equity
`STALLED` rows remain `RESERVE` for review but are excluded from later stages.

The agreed numbers are the defaults in `SelectionConfig` and
`config.performance.json`. Explicit local numeric overrides remain possible
only within their typed bounds: positive DD and DD-profit multiplier,
nonnegative PnL floor, ratio in `(0,1]`, Lot multipliers at least 1,
positive lot tolerance, positive calendar-day and cycle gates, Win Rate and
top-five share in `[0,100]`, A/B divisor at least 1 and A/B decline cap at
least the B floor. Absent fields use documented defaults; invalid/out-of-range
fields fail closed with a typed config error. Overrides cannot remove the DD
profit guard or change strict/inclusive boundary operators. Panel help and
snapshot config show the effective values.

The retired `filter_time_consistency` stage is absent from the Panel. An
enabled legacy request for it fails with a typed invalid-stage response; a
disabled entry is accepted for compatibility and never eliminates. Historical
snapshots remain readable.

## 2. Lot variant redundancy

Compare only an unambiguous pair of one EQUAL and one INCOME with the same
symbol, side, timeframe, executable settings, positive total lot (within
`1e-9`), initial balance and exact effective comparison interval. EQUAL has
per-order lot spread at most `1e-9`; INCOME has larger spread. Other group
shapes, unavailable/invalid metrics, nonpositive EQUAL full/B return or DD,
or unknown comparison identity leave all variants for later stages.

For complete comparable pairs, INCOME replaces EQUAL **only if all four hold**:

```text
full_pnl30_INCOME * 5 / full_dd_INCOME
    >= 1.10 * full_pnl30_EQUAL * 5 / full_dd_EQUAL
full_dd_INCOME <= 1.10 * full_dd_EQUAL
b_pnl30_INCOME * 5 / full_dd_INCOME
    >= b_pnl30_EQUAL * 5 / full_dd_EQUAL
b_pnl30_INCOME >= b_pnl30_EQUAL
```

Otherwise EQUAL replaces INCOME. Equalities pass. This final fallback was
confirmed by the user on 2026-10-01 and supersedes the earlier proposal to
retain both when only the ordinary B comparison fails. Record the surviving
representative and a reason for the eliminated variant. Do not use PF to
override these conditions. DD5 here is a source proxy, not a tick-test result.

## 3. Independent hard cutoffs and durable status

Exclude on any **known** violation, with equality handled exactly:

```text
full_dd_pct > 23 AND full_pnl30_pct < 3 * full_dd_pct
OR full_pnl30_pct <= 4
OR (history_days >= 45 AND reliable_completed_cycles >= 25
    AND full_dd_pct > 0 AND full_pnl30_pct / full_dd_pct < 0.75
    AND b_pnl30_pct / full_dd_pct < 0.75)
```

The DD branch requires a known finite full PnL/30d; equality to `3 * DD`
passes that branch. The ratio branch requires both returns known and finite. Check branches
independently; incomplete ratio evidence cannot suppress an available DD or
full-PnL failure. Persist every triggered rule with its measured values in
the selection result reason; config and effective request are in the run.

Preview is read-only. The common durable-tag publication rule for stages 2–4
is specified after the A/B criteria below.

## 4. A/B deterioration

A is the report history before its final 14 days; B is those final 14 days.
Returns are geometric percentages normalized to 30 calendar days. Exclude if
any available condition is true:

```text
B_pnl30 <= 4
OR (A_pnl30 > 0 AND B_pnl30 <= A_pnl30 / 10 AND B_pnl30 <= 15)
OR (B_completed_cycles >= 25 AND B_win_rate < 55)
```

The B gate and B win rate use the same reliably reconstructed set of truly
completed cycles closed in the effective B window. `WindowMetrics.trade_count`
may include a partial open tail after a realised decrease, so it is not
assumed to equal this count without parity evidence. The win-rate denominator
remains profitable plus losing completed cycles;
zero-PnL cycles do not contribute to that denominator, though they count
towards the 25 completed-cycle applicability gate. Actual B cycle count is
used, never B Trades/30. Each branch evaluates independently with its own
required facts. Record triggered conditions and distinguish incomplete
evaluation from a complete pass. Remove the old trade-frequency decline and
unconditional 58% win-rate checks.

## Published REJECTED status for stages 2–4

On explicit selection snapshot publication, atomically insert the immutable
run/results and upsert the existing `strategy_tags` row for each strategy
actually excluded by an enabled stage 2, 3, or 4. The source is
`SELECTION_LOT_VARIANT`, `SELECTION_HARD_CUTOFF`, or
`SELECTION_AB_DETERIORATION`, respectively; `source_ref` is the selection run
ID. Stage trace flags are the exclusion evidence. A passing row, a skipped
stage, a row excluded elsewhere, or an equity `RESERVE` row receives no tag
from these stages. Ambiguous evidence that attributes one row to multiple of
these stages fails publication.

Recheck current result IDs and equity source revision before publication. A
stale result or failed transaction publishes neither run nor tag. Repeated
publication upserts the existing tag; disabling a filter or passing a later
selection does not clear an older tag. Newly tagged rows display
`User Status = REJECTED` in the current XLSX even though its bytes are built
before transaction commit; `Auto Status` remains `FILTERED`. A present
`REJECTED` tag wins over an older manual status when resolving the effective
status. An explicit later review import may clear the tag through the existing
review workflow. Numeric User Rank is unchanged. These tags do not delete a
strategy or change its lifecycle to `DISCARDED`. Hard equity rejection
provenance continues to use its separate existing rejection-source contract.

## 5. Top-five profitable trades

Use reliably reconstructed completed trade cycles over the current effective
history, excluding open tails and without subtracting commissions. For
history at least 45 calendar days, at least 25 profitable completed cycles
and positive net completed-cycle PnL, calculate:

```text
top5_share_pct = sum(five largest positive cycle PnLs)
                 / sum(all completed cycle PnLs) * 100
```

Exclude only when the share is strictly greater than 80%. Exactly 80% passes.
Shorter histories, fewer profitable cycles, unreliable reconstruction and
nonpositive/unknown net PnL are diagnostic only, not exclusions. Export share,
completed net PnL and remainder after top five for review. The former
one-best-trade exclusion is retired; historical data is not rewritten.

## 6. Minimum Shift

`filter_min_shift` is a pair-side filter enabled by default with a `0.3%`
threshold. It excludes a strategy when any existing order has
`shift_bp < 30`. Missing Shift facts do not exclude the strategy. The stage is
evaluated before `pair_side_pnl_upper_half`, so the PnL DD5/30 + PnL B/30 stage
only receives survivors of the minimum-Shift check. The threshold remains
explicitly configurable in the typed request as any positive decimal; zero,
negative and nonnumeric values are rejected. Shift is compared as a signed
`shift_bp` value, so a negative existing Shift is below every positive
threshold and is excluded.

## Time-window diagnostic and Panel help

Continue calculating positive sequential windows and exporting `Positive
windows`; do not use them to exclude. A confirmed `NO_TRADES` window is omitted
from assessed and profitable counts. Two positive plus two `NO_TRADES` windows
display `2/2`. All `NO_TRADES` or damaged/unavailable metrics remain N/A.

Use the existing help field beneath **every** Panel stage name. State its
current criterion, units, scope, important applicability gates and whether
multiple conditions use AND or OR. Stage 3 help explicitly says that a
published exclusion marks User Status `REJECTED`. Descriptions must reflect
the effective configured thresholds; the A/B stage loses its `PLANNED` badge.

## Acceptance evidence

- TDD checks for fixed order on server and Panel, enabled defaults and all
  fixed-stage reasons/booleans, including the Lot 10/10, B boundaries and
  minimum-Shift placement before the PnL stage.
- DD guard boundaries 23 and 3x, including missing full PnL/30d; A/B
  boundaries 4/15/55, 24/25 cycles, missing A/B facts and independent
  branches; top-five 45 days/25 profits/80%, nonpositive net and incomplete
  cycles; hard-cutoff 23/4/0.75 and applicability guards.
- `NO_TRADES` 2/2, all `NO_TRADES`, unavailable windows, calculation and XLSX
  after the filter is removed.
- API preview leaves tags untouched; XLSX publication stores result/reason/tag
  atomically and fills User Status; stale/failing publication rolls back;
  manual clearing and older-manual precedence are verified.
- Focused tests and relevant Panel/export regressions run from `.venv` with
  temporary test files on C: cleaned afterward; independent code review passes.
