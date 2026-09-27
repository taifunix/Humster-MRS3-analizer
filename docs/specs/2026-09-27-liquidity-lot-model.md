# Liquidity ceiling for a full MRS3 strategy lot

Status: mathematical model approved by the user on 2026-09-27 after worked
EQUAL/INCOME examples. Runtime integration is not implemented. Model approval
does not imply empirical calibration of the new curve.

## Scope and inputs

Estimate the maximum full strategy notional in USDT from minute liquidity,
opening shifts and existing order weights. Risk, DD, margin and leverage
policies are outside this change. This is the single liquidity ceiling used
for each selected directional strategy; it replaces the old mean-turnover
participation formula and does not prescribe using the entire available size.

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

## Optimizer integration: corrected user contract

The earlier two-cap implementation plan is superseded by the user's explicit
corrections. There is one model, not a minimum of old and new liquidity caps.
For each candidate compute `C=K*V25*A15*B`, floor to the existing USDT step
(default `10`), and apply exchange quantity/minimum constraints. The resulting
upper bound applies to that strategy's full lot.

One portfolio contains at most one strategy per canonical `(symbol, side)`.
LONG and SHORT on the same symbol may both be selected. Their liquidity
ceilings are independent; no aggregate LONG+SHORT liquidity budget is added.
Each portfolio calculation is independent of all other calculations. There
is no cross-run position inventory or liquidity reservation.

Expose base `K` (default `9`) and deep-shift bonus (default `1.1`) in the existing
config and Panel settings. Retire the old mean-turnover participation control.
Retain `liquidity.round_down_usdt`, default `10`. The curve shape and existing
EQUAL/INCOME weights remain as approved above.

### One active direction per symbol

On 2026-09-27 the user explicitly fixed the optimizer's model: no hedge;
at most one active position direction per symbol. Assume the dedicated
closing order closes the current position before an opposite opening order.
Ignore opposite-opening reduction/reversal effects. This is a modeling
assumption, not evidence that the external bot or exchange account has been
configured or verified. Do not add an account-mode confirmation gate.

Both directions remain eligible as portfolio members. The implementation
must prevent overlapping accepted LONG/SHORT position cycles within the same
calculation and consistently use the accepted cycles for modeled PnL, equity,
DD and occupancy. Do not merely suppress a replay diagnostic while leaving
the optimizer's equity input unchanged. Existing risk thresholds and
conservative margin formulas remain unchanged.

Investigation found that the current implementation does not enforce this
rule: `prepare_weighted_input` enforces pair-side uniqueness, but
`replay_limiter` treats opposite-side cycles as independent slots; the existing
`test_replay_same_symbol_opposite_sides_use_distinct_slots` accepts both with
`L=2`, and Stage 1 uses `L=0`. The weighted adapter emits separate directional
payloads. The main optimizer spec describes no simultaneous positions as a
target, not an already verified implementation.

The [integration plan](../superpowers/plans/2026-09-27-liquidity-lot-optimizer-integration.md)
must implement this model rule together with the new liquidity formula.
The revised integration plan v3 received independent PLAN_APPROVED; on
2026-09-27 the user authorized implementation with GPT-6 Luna and Opus review.

## WS1.3 runtime contract

The approved integration plan defines the exact runtime acceptance contract.
Compute eligibility and each strategy's exchange-rounded bound before freezing
the surviving composition. Then admit source cycles per symbol over
`[first_fill, final_flat)` with closes before opens and ties ordered by original
opening time, strategy ID, source ordinal and cycle ID. Reject an overlapping
opposite cycle completely; do not queue it. Same-side source-cycle overlaps
fail `ONE_WAY_SOURCE_CYCLE_OVERLAP`. Include valid source-window boundary cycles
using original opening time; they are not prior optimizer-run positions.

Validate rejected source data too. At the existing segmented attribution seam,
only admitted cycle contributions enter normalized equity; preserve each
cycle's own tested `source_basis`. Admitted cycles feed downstream occupancy
and replay. Mixed-direction missing attribution fails
`ONE_WAY_CYCLE_ATTRIBUTION_UNAVAILABLE`. Freeze the schedule per composition:
later zero or below-exchange-minimum allocations do not reactivate suppressed
cycles. This is an explicit approximation, not conservative PnL evidence;
skipped cycles can contain gains or losses. Do not reconstruct compounding.

Use the latest completed UTC date whose following midnight plus frozen archive
lag is no later than Campaign creation, and its six preceding dates. Missing
or invalid creation time fails `LIQUIDITY_MODEL_ANCHOR_UNAVAILABLE`; no current
clock fallback. Require all seven daily files and positive turnover. Type-7
linear Q25 uses positive close-times-base-volume minutes; A15 counts positive
UTC 15-minute bins divided by 672. Freeze source hashes and feature evidence.

Use Decimal precision 28 with HALF_EVEN intermediate arithmetic. Permit
`shift_bp=0` or `30..550`; `s=shift_bp/100` percentage points. Require 1-4
unique configured order IDs and positive finite lots; cumulative W is the raw
cumulative lot divided by total lot, so the final W is exactly one. First
configured order wins a binding tie. Floor raw C to the USDT step, constrain
maxQty at frozen mark, floor quantity to qtyStep, then validate exchange
minQty/minNotional. U is final quantity times mark. Step 10 is granularity,
not an extra minimum. Invalid geometry/size excludes the candidate; no
survivors returns `LIQUIDITY_MODEL_NO_ELIGIBLE_CANDIDATE` before LP.

WS1.3 uses independent bounds `0<=x_j<=U_j` and objective upper bound
`sum(max(0,coefficient_j)*U_j)`. No symbol equality or aggregate liquidity row.
Source `position_size_usdt=U`, candidate `capacity_usdt=U`; executable position
size is the rounded actual allocation. Omit zero/below-minimum payload members
and guard division. Positive payloads use `facts.C=U` and
`basic.max_balance=U*bank/actual`, never replacing actual allocation with U.
The solver's historical path, DD and bootstrap evidence apply to its pre-exchange-
rounding vector. On 2026-09-27 the user explicitly chose not to rerun expensive
bootstrap or historical optimization after downward exchange rounding. Check
`0<=actual<=x<=U` for every retained member and label the resulting evidence
as an estimate for the executable vector. Uneven rounding can change portfolio
DD even though every individual order becomes no larger; do not claim exact
post-rounding risk verification.

Config schema 3 adds global frozen Campaign parameters K (exact Decimal 1..20,
default 9) and bonus (0..2, default 1.1), retaining rounding default 10. Migrate
v1/v2 in memory, retire participation without translating it into K, and write
only on explicit CAS Save. Reject retired keys in v3. The operator confirmed
there are no old WS1.2 result artifacts to support; all Campaign execution
and reading require WS1.3.

Acceptance evidence: focused failing-then-passing tests for formula, sparse
minute features, frozen window, cycle arbitration/attribution and downstream
consistency, independent caps, payload readback, migration and historical
reads; relevant broader tests and independent Opus CODE_REVIEW_PASS. No live
database mutation, real tester or exchange action is included.
