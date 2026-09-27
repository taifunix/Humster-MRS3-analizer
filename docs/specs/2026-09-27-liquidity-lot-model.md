# Liquidity ceiling for a full MRS3 strategy lot

Status: mathematical model approved by the user on 2026-09-27 after worked
EQUAL/INCOME examples. Runtime integration is not implemented. Model approval
does not imply empirical calibration of the new curve.

## Scope and inputs

Estimate the maximum full strategy notional in USDT from minute liquidity,
opening shifts and existing order weights. Risk, DD, margin and leverage
policies are outside this change. This ceiling does not replace portfolio-wide
liquidity constraints or prescribe using the entire available size.

- `L = V25 * A15`: the existing liquidity feature, using one explicit window.
- `V25`: Q25 of positive minute `close * base_volume`, a quote-turnover proxy.
- `A15`: positive UTC 15-minute bins / all calendar bins in that window.
- Absent minute rows mean no trades; missing daily files mean unavailable data.
- `K`: the base coefficient from the workbook model, not yet an optimizer
  setting; this decision does not recalibrate it.
- `s_i`: opening shift in percentage points, e.g. `0.5`, for ordered levels i.
- `w_i`: positive normalized order shares, summing to one; 1-4 orders.
- `W_i = sum(w_j for j <= i)`: cumulative share.

The agreed shift range is 0.3-5.5%. Behavior beyond that range is not specified;
do not silently extrapolate. The formula uses the configured order sequence;
different MA lengths do not guarantee that shift order equals actual price order.

## Accepted formula

```text
M(s) = 1 + 1.1 * (max(0, s - 0.6) / 4.9)^2
B = min_i(M(s_i) / W_i)
N_max = K * L * B
N_i = N_max * w_i
```

The reduction applies to the shift premium and is already included in `1.1`.
There is no additional multiplier of 0.5 on the multiorder result.
No frequency-of-entry coefficient or per-symbol fitted curve is introduced.

EQUAL uses equal shares. INCOME retains the existing shares proportional to
positive source PnL; the liquidity model does not reallocate them. Source PnL
is only the allocation input, not measured final MRS3 PnL.

## Invariants and worked evidence

- Shifts up to 0.6% receive no premium: `M=1`.
- The curve is convex above 0.6%; the 4-to-5% increment exceeds the 1-to-2% increment.
- At 5.5%, `M=2.1`; a single order at that shift has a 2.1x base ceiling.
- Every level satisfies `N_max * W_i <= K * L * M(s_i)`.
- If all shifts are <=0.6%, `B=1`, regardless of order count or shares.
- Splitting equal-shift exposure into several orders does not increase the ceiling.

Illustrative base `K * L = 1000 USDT`; INCOME shares here are examples, not
claims about a particular stored strategy. Full notionals rounded to cents:

| Shifts (%) | Shares | Full strategy ceiling (USDT) |
|---|---|---:|
| 0.5 / 3 | 50 / 50 | 1263.89 |
| 0.5 / 3 | 70 / 30 | 1263.89 |
| 0.5 / 2.5 / 4 | equal thirds | 1529.61 |
| 0.5 / 2.5 / 4 | 50 / 30 / 20 | 1456.74 |
| 0.5 / 3 / 5.5 | equal thirds | 1895.84 |
| 0.5 / 3 / 5.5 | 50 / 30 / 20 | 1579.86 |
| 0.5 / 3 / 5.5 | 80 / 15 / 5 | 1250.00 |
| 0.5 / 2 / 3.5 / 5.5 | equal quarters | 1847.06 |
| 0.5 / 2 / 3.5 / 5.5 | 40 / 30 / 20 / 10 | 1539.22 |

## Evidence boundaries

The original workbook audit and descriptive probes are local artifacts in
`Output/liquidity-model-audit-2026-09-26/`. Original-curve pass rates must not be
reported as validation of this accepted replacement curve. The user chose this
simple shape and endpoint; neither is a newly fitted execution guarantee.

For any later price-to-level research, the user corrected the MA definition to
SMA OHLC4 using closed candles. Earlier SMA/open calculations are superseded.
Further exploratory price mapping is not a dependency for recording this model.

## Proposed optimizer integration

The [reviewed implementation plan](../superpowers/plans/2026-09-27-liquidity-lot-optimizer-integration.md)
keeps the current shared symbol cap and adds this formula as a separate
per-candidate ceiling. It proposes two future config/Panel controls: base `K`
and the maximum deep-shift bonus (default `1.1`). The defaults preserve this
approved formula; changing the bonus would be an explicit future operator
choice, not a reinterpretation of the worked examples above. The plan is not
runtime authorization or evidence of empirical calibration.
