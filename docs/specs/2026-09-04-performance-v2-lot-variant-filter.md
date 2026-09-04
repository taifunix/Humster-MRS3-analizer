# Performance v2 lot-variant redundancy filter

Status: implemented, verification in progress.

## Scope

The Performance v2 selection pipeline removes redundant strategy variants that
have the same executable settings and comparison interval but different order
lot vectors. This is a selection-only operation; imported Performance DB rows
are not changed.

## Contract

- Stage id: `filter_lot_variant_redundancy`.
- The stage is enabled by default and runs first. A request or
  `unified_performance_v2.finalist_selection.lot_variant_redundancy_enabled:
  false` disables it.
- Scope is fixed to `pair_side_timeframe`.
- The canonical settings key is
  `symbol, side, timeframe, close_ma_len, order_count,
  sorted[(open_ma_len, shift_bp)]`. Lot values, names, IDs, order IDs and
  provenance are excluded from that key.
- Candidates are grouped only when their known effective comparison intervals
  are identical. Missing or malformed intervals never collapse.
- A valid group must have finite values for `dd5_proxy`, `capital_proxy`,
  `robust_pnl_30d_pct`, `worst_drawdown_pct`, and `profit_factor` for every
  member. Otherwise the whole group remains active.
- The representative is selected by
  `dd5_proxy DESC`, `capital_proxy ASC`, `robust_pnl_30d_pct DESC`,
  `worst_drawdown_pct ASC`, `profit_factor DESC`, `strategy_id ASC`.
- Non-representatives remain in `All candidates` as
  `auto_status=FILTERED`, `elimination_reason=LOT_VARIANT_REDUNDANT`, with
  representative ID and canonical group key. Only the representative reaches
  later filters, Pareto stages and Top-N.

## Non-goals

This stage does not deduplicate imports, delete database data, compare
different periods, or alter the existing unrelated analog grouping.

## Acceptance evidence

Focused selection tests cover default-on and opt-out behavior, ordering,
interval isolation, fail-closed metrics, canonical order permutation, winner
ranking, audit metadata, and downstream exclusion. Panel static tests cover the
default first-position control and fixed scope.
