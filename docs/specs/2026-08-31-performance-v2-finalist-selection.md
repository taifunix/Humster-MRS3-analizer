# Performance v2 finalist selection and XLSX

**Status:** Accepted design; implementation pending
**Date:** 2026-08-31
**Depends on:** [Unified Performance Analytics v2](2026-08-28-unified-performance-analytics-v2.md)

## Purpose

From committed Performance DB v2 facts, calculate a disposable, ordered selection for one chosen Pair + Side and download one XLSX. This is calculation-only, never a tester result.

## Stage 2 boundary

The button runs from the current v2 database and does **not** create `selection_runs`, `selection_results`, tags, lifecycle changes, RETEST jobs or an XLSX import path. Those are Stage 3.

The request is `symbol`, `side`, and ordered `{id, enabled, scope}` stages. IDs must be known built-ins; scope is `pair_side` or `pair_side_timeframe`. Unknown or duplicate IDs, unknown scope, inactive Pair + Side, and path traversal fail with typed 400. UI edits remain local until the explicit XLSX click.

The universe is every ACTIVE strategy with a current result for requested `(symbol, side)`. A stage operates only on survivors of preceding enabled stages. Disabled stages never eliminate; finalists survive all enabled stages.

## Derived facts

Before filtering, derive all facts from current v2 tables and cache default A/B metrics in `window_metrics`. No stage reads HTML, v1 tables, CSV, or Analysis DB lineage.

For `D = max_drawdown_pct`, `r = daily_log_return`, and lot multipliers `L`:

```text
risk_scale = 5 / D
dd5_proxy = r * risk_scale
scaled_lot_sum = sum(L) * risk_scale
capital_proxy = scaled_lot_sum + 0.05
capital_efficiency = dd5_proxy / capital_proxy
```

`D` must be positive. These are labelled `DD5_PROXY` and `CALCULATION_ONLY`; no value claims a tick-tested PnL. `first_shift_bp` is the first order shift. Holding p95 is reconstructed from committed action cycles in minutes; it is never invented. Per-order plateau counts come from `analysis_plateaus`; missing is never zero.

## A/B deterioration

`config.performance.json` defaults are A = available report interval excluding final 14 days and B = final 14 days. Both are normalized to 30 days. If required A/B facts are unavailable, invalid or too short, the stage does not eliminate and traces `AB_NOT_EVALUATED_INSUFFICIENT_DATA`.

When evaluable, it eliminates when any condition holds:

```text
B return_pct_30d <= 5
A return_pct_30d > 0 and B return_pct_30d <= A return_pct_30d / 10
B win_rate_pct < 58
A trade_rate_30d > 0 and B trade_rate_30d <= A trade_rate_30d / 7
```

The XLSX exports only `ab_pnl_change_30d_pct = (B return_pct_30d / A return_pct_30d - 1) * 100` when A is positive; otherwise blank. No other A/B columns are exported.

## Stage registry

Each stage uses its selected scope: `pair_side` compares all requested candidates; `pair_side_timeframe` splits by timeframe. Stages 1–4 default to `pair_side`, all other visible stages to `pair_side_timeframe`. Missing values neither fabricate failure nor dominate.

| ID | Rule |
| --- | --- |
| `filter_holding_outlier` | Eliminate `holding_p95_minutes > Q3 + 1.5 * IQR`. |
| `filter_low_trades` | Eliminate `total_trades < Q1 - 1.5 * IQR`. |
| `ab_deterioration` | Apply A/B rule above. |
| `pareto_dd5_balanced` | Maximize `dd5_proxy`, `first_shift_bp`; minimize `capital_proxy`, holding p95, Close MA. |
| `pareto_plateau_points_per_order` | Equal order count only. A eliminates B when `dd5_proxy_A >= dd5_proxy_B * plateau_points_pareto_pnl_multiplier` and every A plateau count is at least B's. Proxy condition is strict under multiplier; equal counts are allowed. |
| `pareto_plateau_points_total` | Equal order count only. Same strict proxy condition and A total plateau count is at least B's. |
| `pareto_efficiency_shift` | Maximize capital efficiency and first shift. |
| `pareto_dd5_holding` | Maximize DD5 proxy; minimize holding p95. |
| `pareto_dd5_close_ma` | Maximize DD5 proxy; minimize Close MA. |
| `pareto_dd5_first_shift` | Maximize DD5 proxy and first shift. |
| `pareto_conditional_close_ma` | Only if scope has >3 survivors: maximize capital efficiency, minimize Close MA. |
| `pareto_primary` | Maximize DD5 proxy; minimize capital proxy. |
| `pareto_dd5_capital` | Maximize DD5 proxy; minimize capital proxy. |

Ordinary Pareto eliminates iff a candidate is no worse in every objective and strictly better in at least one. Plateau stages follow their explicit rule. `near_tie_rank` stays hidden, unsubmitted and unexported.

## XLSX

Download one deterministic workbook with `All candidates` and `Finalists`, both from the same in-memory result. `All candidates` retains every input strategy. It includes identity/settings, current result metrics, DD5 proxy diagnostics, holding, ordered plateau counts and total, the sole A/B column above, then `eliminated_by_<stage_id>` for every submitted stage, `finalist`, and `elimination_reason`.

Display fractions round to two decimals; IDs and counts remain integral; holding is minutes. A stage boolean is true only if that stage actually eliminated the row.

## Acceptance evidence

- Tests cover every stage, both scopes, stage order, deterministic ties, and missing-data non-elimination.
- Endpoint tests prove only the requested active Pair + Side is exported and no selection/tag table is mutated.
- XLSX tests prove all candidates, ordered plateau counts, stage booleans and exactly one A/B column.
- API and workbook visibly label DD5 as calculation-only.

## Stage 3 (deferred)

Persisted selection runs/results, tags, discard, RETEST, history and import of user-edited XLSX tags begin only after acceptance of real Stage 2 XLSX output.
