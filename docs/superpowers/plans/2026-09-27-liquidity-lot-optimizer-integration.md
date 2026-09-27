# Liquidity lot ceiling in Portfolio Optimizer

Status: implementation plan; `PLAN_APPROVED` by independent Advisor on 2026-09-27. No optimizer behavior or Panel setting is implemented by this document. The user-approved default mathematics is in [the model specification](../../specs/2026-09-27-liquidity-lot-model.md). `K=9` and deep-shift bonus `1.1` are proposed defaults, not validation of the replacement curve.

## Decision

Keep two separate liquidity limits for each weighted candidate:

- `C_symbol`: today's exchange-rounded shared symbol cap, derived from calendar mean minute turnover and `liquidity.parameters.close_volume_participation_pct` (currently 200%). All members on a symbol still share `sum(x_i) <= C_symbol`, including LONG and SHORT.
- `C_model`: the candidate's full-strategy lot ceiling, `K * V25 * A15 * min_i(M(s_i) / W_i)`. `M(s) = 1 + bonus * (max(0, s - 0.6) / 4.9)^2`, with default `bonus=1.1`; `W_i` is the cumulative share of the existing `lot_x` through order `i`.
- `C_effective = min(C_symbol, C_model)`: the individual LP and executable ceiling, `x_i <= C_effective`.

Keep the current `position_size_usdt` on the enriched source member and `capacity_usdt` on the weighted candidate equal to `C_symbol`. Add explicit model and effective-cap fields; do not silently change the meaning of existing fields. The JSON strategy payload's `facts.C` and `basic.max_balance` will use `C_effective` under the new weighted algorithm version. Risk, DD, margin, leverage, and allocation policy remain unchanged. EQUAL and INCOME need no branch: the already frozen `lot_x` determines order shares.

The shared cap can bind before the shift bonus, so a candidate is never promised the entire formula result. In the historical workbook's 76 descriptive feature groups, `9*V25*A15` was below the current raw `2*calendar_mean` cap in 71 groups; even `2.1*9*V25*A15` was below it in 59. These different-window observations show that preserving the shared cap does not automatically erase the bonus, but they do not calibrate the new curve.

## Model input and evidence contract

For each Campaign, derive one frozen seven-day UTC window from its immutable `created_at_utc` and its frozen `config_document.liquidity.archive_publication_lag_hours` (existing default 6, allowed 0..48). Its end date is the latest UTC date before the creation date whose following midnight plus publication lag is no later than Campaign creation; the start date is six days earlier. Both caps use that window. An immediate run keeps the current archive-lag choice; a delayed run cannot move it. Backfill, when enabled, may fetch only these seven dates. An absent minute row in a valid daily CSV means no trades. A missing or invalid daily file, or no positive turnover from which to compute the model, fails the run before search with `LIQUIDITY_MODEL_WINDOW_UNAVAILABLE` and symbol/date evidence; it must not silently fall back to `C_symbol` or yield an empty successful run.

Compute `V25` as the Type-7 linear Q25 of positive minute `close * volume` values. Compute `A15` as the number of UTC 15-minute bins with positive turnover divided by `672`. Use the same validated minute rows for the old shared mean. Record the resolved lag, dates, file hashes, V25, A15, K, both raw caps, rounding steps, and model version in deterministic evidence and digests. Changing current Panel settings after Campaign creation must not change its window or identity.

Evaluate the model inside one `Decimal` local context with precision 28 and `ROUND_HALF_EVEN`. Parse numeric inputs exactly; compute `L=V25*A15`, `base=K*L`, then for each existing canonical `order_id` sequence compute `s_i=Decimal(shift_bp)/100` percentage points, `M_i`, `W_i=(sum of lot_x through i)/(sum of all lot_x)`, `B=min_i(M_i/W_i)`, and `raw=base*B`. Do not quantize intermediate values, sort by shift, or convert to binary float. In an exact tie, the first configured order binds. Accept 1–4 unique valid order IDs with finite positive `lot_x`; allow `shift_bp=0` as an explicit no-premium base level, and other shifts only in `30..550` bp. Reject 10 or 600 bp as unsupported rather than extrapolating. The model cap and exchange quantity are rounded down with `ROUND_FLOOR`, first to the configured USDT step, then to the instrument quantity step. Zero or below-minimum results exclude the candidate before the LP. Include both K and bonus as canonical fixed-point Decimal strings in evidence and identity; changing bonus affects the deep premium but leaves shifts through 0.6% at `M=1`.

Golden examples with `K*V25*A15=1000 USDT` and a 10-USDT floor:

| Shifts (%) | `lot_x` | Raw cap (USDT) | Binding order | Floored cap (USDT) |
| --- | --- | ---: | ---: | ---: |
| 0.5 / 3 | 50 / 50 | 1263.890045814244064972927947 | 2 | 1260 |
| 0.5 / 3 / 5.5 | 80 / 15 / 5 | 1250.00 | 1 | 1250 |

If an individual candidate has malformed geometry, unsupported shift, or a cap below the exchange minimum, exclude only it with a stable counted reason. Solve surviving candidates normally. If none remain, return terminal `LIQUIDITY_MODEL_NO_ELIGIBLE_CANDIDATE` with reason counts; never report `PASS` with no candidates.

## Integration points

1. Extend `src/mrs3/portfolio/minute_capacity.py` to calculate V25/A15 while reading the existing seven daily files and expose frozen window/model evidence. Anchor both caps to the Campaign snapshot in `src/mrs3/portfolio/adapter.py` instead of the wall clock; retain the current shared-cap formula.
2. Calculate each candidate's model cap from its frozen `strategy_orders` in `src/mrs3/portfolio/position_sizing.py` or the adjacent enrichment seam. Carry `C_symbol`, `C_model`, `C_effective`, the binding order, and the reason if excluded. Existing `lot_x` supports both EQUAL and INCOME.
3. Keep `weighted_search.py`'s `capacities` as identical same-symbol shared caps. Pass heterogeneous individual caps separately. The LP must enforce both `0 <= x_i <= C_effective_i` and `sum(x_i for symbol=s) <= C_symbol_s`; repeat these checks in solver output, proposed vectors, rescue, candidate conversion, bootstrap/replay, and final validation. The existing `upper_target = sum_s C_symbol_s * max(0, max coefficient on s)` is still a safe upper bound because individual caps only shrink the feasible set; final rounded bounds remain the acceptance gate.
4. In `size_composition_vector`, reject any rounded member above its effective cap or rounded symbol total above the shared cap; do not clamp or redistribute. Keep margin coefficients validated over the existing `C_symbol` domain, of which the effective bounds are a subset.
5. Change `build_weighted_strategy_payload` to receive `C_effective` as its executable capacity. For the new version, emit `facts.C = C_effective` plus `C_symbol`, `C_model`, and `C_effective`; read back `C == C_effective <= C_symbol`, `C_effective <= C_model`, actual size `<= C_effective`, and the symbol aggregate `<= C_symbol`. Preserve the existing equality between weighted candidate `capacity_usdt` and enriched source `position_size_usdt`, both of which remain `C_symbol`.
6. Bump the sizing algorithm ID to `portfolio_optimizer_sizing_v3` and Campaign weighted algorithm to `WS1.3`, and include ordered geometry, K, frozen lag/window/source hashes, features, binding order, and all caps in the sizing/executable identities. Reject old WS1.2 Campaigns for new execution; retain existing stored results as read-only artifacts without recomputation.

Before changing behavior, expand the model spec into the exact runtime contract and add ADR-0046 for the separate shared/candidate ceilings. Amend the weighted-search and Panel specs only where the new version and setting alter their contracts. No new tester, live DB, portfolio-risk, or curve-fitting work is required.

## Configuration and Panel

Expose exactly two settings in the existing Portfolio Optimizer Settings card: `liquidity.parameters.lot_model_base_coefficient`, exact Decimal `1..20` with proposed default `9`, and `liquidity.parameters.lot_model_max_shift_bonus`, exact Decimal `0..2` with default `1.1`. K changes the whole ceiling; bonus changes only the deep-shift premium. At 5.5% the multiplier is `1+bonus`, so the default gives `2.1`; through 0.6% the multiplier remains `1`. Relabel the existing participation input so it clearly says **shared symbol cap**; keep its config key and default unchanged. The curve onset `0.6`, endpoint `5.5`, exponent `2`, V25 quantile, A15 bin width, and seven-day window stay in the versioned algorithm.

Keep config `schema_version=2`: `liquidity.parameters` already admits additive leaves. Existing v2 files lacking the two values receive `9` and `1.1` in memory; only an explicit full-document compare-and-swap save writes them to disk. A save cannot modify an already frozen Campaign. Reuse the current Settings API; add no new endpoint, screen, or workbook sheet. Existing member output may show a compact shared/model/effective/binding evidence block. A front-weighted strategy may bind at its shallow first order, in which case changing the deep bonus correctly has no effect on its ceiling.

## Implementation checks

Write focused failing tests before implementation. Required cases are the two exact Decimal goldens plus one non-default bonus case; shift 0 accepted and 10/600 rejected; missing daily file as run failure; sparse minutes; frozen lag/window despite later settings changes; partial and all-candidate exclusions; heterogeneous same-symbol caps with a known feasible optimum; safe loose upper target; floor/quantity minimum; unchanged risk/DD/margin when the model does not bind; old stored WS1.2 result remaining readable; and one binding `C_model < C_symbol` path through proposal, LP, rounding, sizing, payload, readback, and replay without repair. Test K and bonus validation (including bonus 0 and 2), their independent effects, identity stability for identical frozen inputs, and sensitivity to geometry, both settings, window, and file bytes.

Run focused and relevant broader tests only with `.venv\\Scripts\\python.exe -m pytest`, then JS syntax, Python compilation, and `git diff --check`. Obtain independent `CODE_REVIEW_PASS`, address confirmed findings, update `progress.md` and PRD if scope/status changed, and make a scoped conventional implementation commit containing the runtime spec amendment, ADR, code, tests, and evidence. Do not present either proposed default or the new curve as empirically calibrated.
