# DD5 Calculation and Finalist Selection

**Status:** Active implementation contract
**Date:** 2026-08-14
**Depends on:** [Performance report import](2026-08-14-performance-report-import-duckdb.md) and [Strategy performance DuckDB](2026-08-14-strategy-performance-duckdb.md)

## 1. Purpose

Produce a reproducible, calculation-only comparison of committed MRS3 tester
results at target DD=5%, then apply the approved sequential filters and Pareto
selection. It writes a human-auditable workbook and persists the calculated
DD5 run. It does not create a new strategy result or replace a tick-test.

## 2. Preconditions and Inputs

DD5 accepts only a committed import with `quarantined_count=0`. It reads from
Performance DuckDB only:

- canonical PnL, DD, win rate, trades and days from committed backtest facts;
- immutable strategy settings for symbol, side, timeframe and active MRS3
  order metadata;
- persisted action timestamps for holding-cycle statistics;
- the selected `AlgorithmConfig`, including `target_dd_pct`.

It never reads report HTML or runner CSV during calculation or regeneration.
The historical runner CSV may be used as a parity oracle in a test/audit, not as
an input source.

## 3. Raw Strategy Metadata

`symbol`, `side` and `timeframe` come from stored strategy evidence. A nonempty
stored side is trimmed, case-normalized and must resolve to LONG or SHORT; an
empty stored side falls back to `basic.use_long`, then `basic.side`. Any other
value fails closed. The resolved side selects the active order list:

```text
mrs3.ma_long  when basic.use_long is true
mrs3.ma_short when basic.use_long is false
```

Only this active list contributes `lots`, `order_count`, `shift_bp_vector`,
`first_shift_bp` and `common_close_ma`. Close-side orders and inactive-side
orders must never be recursively collected as lots.

## 4. DD5 Normalization

For each row, where `D = max_drawdown_pct`, `P = total_pnl_pct`, `T = days`,
and `L` is the active lot vector:

```text
dd5_scale = target_dd_pct / D
scaled_lots = L * dd5_scale
projected_pnl_dd5 = P * dd5_scale
projected_dd_pct = D * dd5_scale
pnl30_dd5 = projected_pnl_dd5 * 30 / T
capital_requirement_proxy = sum(scaled_lots) + projected_dd_pct / 100
capital_efficiency_30 = pnl30_dd5 / capital_requirement_proxy
```

`D` and `T` must be positive. Persisted decimal fields use the declared schema
precision; readback must compare values at storage precision before an export
is accepted. A result label is always `CALCULATION_ONLY`; scaled lots require a
separate tester retest before being called a test result.

## 5. Holding Evidence

Full-position cycles are reconstructed from persisted action evidence. For each
strategy, the comparison records `holding_mean_minutes`,
`holding_median_minutes`, `holding_p95_minutes`, `time_in_market_pct` and
exclusion diagnostics. Missing holding metrics do not receive an invented
value; such rows cannot pass the sequential selection prerequisites.

## 6. Pareto Flags

The primary `pareto` flag is global across the DD5 comparison, not scoped. A
row is kept when no other row has both:

```text
pnl30_dd5 >= candidate.pnl30_dd5
capital_requirement_proxy <= candidate.capital_requirement_proxy
```

with at least one strict inequality.

The following informational scoped flags are calculated independently within
the same `(symbol, side, timeframe)` group:

| Flag | Maximize | Minimize |
| --- | --- | --- |
| `pareto_dd5_capital` | `pnl30_dd5` | `capital_requirement_proxy` |
| `pareto_dd5_holding` | `pnl30_dd5` | `holding_p95_minutes` |
| `pareto_dd5_close_ma` | `pnl30_dd5` | `common_close_ma` |
| `pareto_dd5_first_shift` | `pnl30_dd5`, `first_shift_bp` | none |
| `pareto_dd5_balanced` | `pnl30_dd5`, `first_shift_bp` | capital, holding p95, Close MA |

A blank required objective excludes a row from that individual scoped flag; it
does not fabricate a value. These flags are diagnostics and do not replace the
sequential finalist decision below.

## 7. Sequential Finalist Selection

All thresholds and stages operate independently within each
`(symbol, side, timeframe)` group. The exact order is mandatory.

### Stage 0: Required Inputs

Require holding p95, trades, `pnl30_dd5`, capital proxy, capital efficiency,
first shift and Close MA. Default result is `FILTER_MISSING_METRICS` until all
required inputs are available.

### Stage 1: Statistical Outlier Filters

For the group:

```text
selection_holding_limit = Q3(holding_p95_minutes)
                          + 1.5 * (Q3 - Q1)
selection_trades_floor = Q1(trades)
                         - 1.5 * (Q3 - Q1)
```

Rows with holding p95 above the holding limit receive
`FILTER_HOLDING_OUTLIER`. Remaining rows with trades below the trade floor
receive `FILTER_LOW_TRADES`. Only other complete rows get
`selection_filter_pass=true` and continue.

### Stage 2: Capital Pareto

Among Stage-1 survivors, `selection_stage1=true` when a row is nondominated by:

```text
maximize pnl30_dd5
minimize capital_requirement_proxy
```

Other survivors are marked `OUT_STAGE2`.

### Stage 3: Efficiency and Entry Shift Pareto

Among Stage-2 survivors, `selection_stage2=true` when nondominated by:

```text
maximize capital_efficiency_30 and first_shift_bp
```

Rows that do not survive receive `OUT_STAGE3`.

### Stage 4: Conditional Close MA Pareto

Apply this stage only when Stage 3 has more than three rows in its scope. Keep
rows nondominated by:

```text
maximize capital_efficiency_30
minimize common_close_ma
```

Set `selection_stage3_applied=true` for the scope. If the stage is not applied,
all Stage-3 survivors continue unchanged. The final survivors set
`selection_stage3=true`, `selection_final=true` and `selection_reason=SELECTED`.

## 8. Ranking and Exports

Near-tie rank is a deterministic presentation ranking after normalization; it
does not override Pareto or sequential filters. Output includes:

- `00_Selection_Summary`: scoped candidate/filter/finalist counts and limits;
- `01_Finalists`: only `selection_final=true` rows;
- `16_Raw_MRS3_Results`, `17_DD5_Normalized`, `18_Final_Comparison`;
- `19_Position_Holding_Cycles`, `20_Position_Holding_Exclusions`.

`18_Final_Comparison` keeps every candidate, including filtered-out rows, with
`selection_reason` for auditability. Both `01_Finalists` and
`18_Final_Comparison` must visibly include `symbol`, `timeframe`, active
`lots`, `scaled_lots`, `common_close_ma` displayed as `Close MA`, capital,
Pareto flags and sequential-selection fields. `pareto_dd5_close_ma` is a flag
and must not replace the numeric Close MA column.

The manifest records import ID, DD5 run ID, target, input count, primary Pareto
count, unavailable Profit Factor count and `CALCULATION_ONLY` mode. The workbook
can be regenerated from `dd5_run_id` without HTML or CSV.

## 9. Invariants and Acceptance Evidence

- DD5 reads only committed zero-quarantine DuckDB evidence.
- Primary Pareto and every sequential stage are deterministic for identical
  inputs/configuration.
- Active lot-vector length equals `order_count` for every row.
- No filtered row is silently removed from `18_Final_Comparison`.
- `selection_holding_limit` and `selection_trades_floor` are populated for each
  complete scope.
- Persisted DD5 readback, independent formula recomputation and independent
  primary Pareto recomputation must have zero mismatches.
- Tests cover DD5 formulas, active-side lots, scope isolation, each filter
  reason, conditional Close MA stage and visible Close MA output columns.
