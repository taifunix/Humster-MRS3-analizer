# Unified liquidity and one-way history implementation plan

Status: implemented and independently reviewed. PLAN_REVISION v3 received independent PLAN_APPROVED; implementation received sequential Opus 5/high CODE_REVIEW_PASS on 2026-09-27 after two finding rounds. The previous two-cap design is superseded by explicit user corrections.

Goal: one approved liquidity ceiling per strategy and one active position direction per symbol. Both LONG and SHORT remain selectable. Spec: [liquidity model](../../specs/2026-09-27-liquidity-lot-model.md).

## Agreed contract

- One strategy per canonical (symbol, side), already enforced in input/adapter.
- Replace the old mean-turnover participation cap; no minimum of two formulas.
- LONG and SHORT have independent caps, no aggregate liquidity constraint.
- No hedge. Assume dedicated close before opposite entry; ignore opposite-fill reduction/reversal. No account-mode confirmation gate.
- Independent portfolio calculations, no prior-run positions or reservations.
- Retain existing rounding setting, default 10 USDT, always floor.
- K default9 and deep-shift bonus default1.1 are the only new model controls.
- Risk thresholds and conservative additive margin formulas stay unchanged.

## Tasks

### 1. Consistent one-way history

- [x] Update runtime spec and add ADR before code changes.
- [x] Reuse input.py prepare_weighted_input and existing segmented equity attribution in a two-pass flow: collect/validate cycles, then determine per-symbol admission before contributing to normalized_delta.
- [x] Compute liquidity/exchange bounds and apply invalid-source/geometry/capacity exclusions BEFORE freezing the composition and its admission mask. Rebuild preparation if that source membership changes. Post-LP zero/minimum-size omission is the explicit frozen-mask approximation below, not a source exclusion.
- [x] Use [first_fill, final_flat) intervals; closes precede opens at the same timestamp. Simultaneous opens use original opening time, strategy ID, source ordinal, cycle ID. A symbol remains occupied until its admitted cycle is flat. Reject a whole overlapping opposite cycle; do not queue or admit its tail.
- [x] Preserve source-run boundary cycles; their clipped history is not cross-run position state.
- [x] Same-side source cycles must not overlap; fail ONE_WAY_SOURCE_CYCLE_OVERLAP if they do. For boundary cycles from either selected direction, use original opening time and deterministic ties to select the occupying cycle.
- [x] Validate source deltas and source_basis even for rejected cycles. Exclude the whole rejected cycle's contributions, including closing/cost deltas, from effective PnL/equity/DD. Downstream occupancy and replay use admitted cycles. Retain rejection/conflict witnesses and source diagnostics.
- [x] Preserve each admitted cycle's own source_basis normalization denominator; it is not a sum over active positions and must not be recomputed from the admission count.
- [x] Fail mixed-symbol history without complete attribution with ONE_WAY_CYCLE_ATTRIBUTION_UNAVAILABLE. Do not fall back to independent curves. Include policy/mask/participants in cache and preparation identity.
- [x] Verify that coefficients, DD, bootstrap and replay consume the same effective aligned path, without a second simulator.

The admission mask is frozen per composition before LP. A member later sized to zero does not reactivate suppressed cycles. This is a deterministic approximation, NOT a conservative PnL guarantee: suppressing a cycle may remove profit or loss. Source-recorded normalization bases remain; no counterfactual compounding or tick reconstruction. Document and test this boundary.

### 2. Features and liquidity formula

- [x] minute_capacity.py: resolve seven UTC dates from frozen Campaign creation and publication lag (latest completed date whose next midnight plus lag <= creation, plus six preceding days). Backfill only those dates. Require all seven valid files and positive turnover; fail before search on missing/invalid data.
- [x] Missing/invalid Campaign creation anchor fails LIQUIDITY_MODEL_ANCHOR_UNAVAILABLE; previews do not silently use the current clock.
- [x] Compute Type-7 Q25 of positive minute close*volume as V25; A15=positive UTC 15-minute bins/672. Absent minute rows in valid files mean zero trades. Record lag/window/file hashes/features.
- [x] position_sizing.py: compute L=V25*A15, M_i=1+bonus*(max(0,s_i-0.6)/4.9)^2, W_i=cumulative lot_x/total lot_x, B=min(M_i/W_i), C_raw=K*L*B. Use configured order IDs, never shift sorting. Require 1-4 unique valid IDs, finite positive lots, shift_bp exactly0 or30..550. First configured binding tie wins. EQUAL/INCOME retain existing lots.
- [x] Decimal precision28 HALF_EVEN arithmetic; FLOOR USDT/quantity steps. Floor C_raw to configured USDT step, cap with exchange maxQty/mark, floor qtyStep, validate minima. Result U is the individual upper bound. Malformed geometry/minimum failures exclude only that candidate; no survivors -> LIQUIDITY_MODEL_NO_ELIGIBLE_CANDIDATE. No old-formula fallback.

Goldens at K*L=1000: shifts .5/3 and lots50/50 -> raw1263.890045814244064972927947, floor1260; shifts .5/3/5.5 and lots80/15/5 ->1250.00, first order binds. Final cumulative W exactly1.

### 3. Search and payloads

- [x] weighted_search.py: remove same-symbol capacity equality/aggregate LP rows and checks. Enforce heterogeneous member bounds0<=x_j<=U_j in LP, proposals, residual checks, rescue, conversion and final validation. Upper target=sum(max(0,coefficient_j)*U_j).
- [x] size_composition_vector: per-strategy capacities, per-symbol instruments/marks; check each rounded actual<=U without aggregate L/S cap or redistribution.
- [x] adapter.py: source position_size_usdt=U, candidate capacity_usdt=U, x_usdt=solver allocation; executable position_size_usdt=rounded actual. facts.C=U, max_balance=U*bank/actual. Do not arm U when solver selected less. Preserve both directional payloads; no account-mode field or gate.
- [x] Per explicit user correction, do not rerun bootstrap or historical search after exchange rounding. Verify each actual size is nonnegative and no greater than its LP allocation and U. Label historical/DD/bootstrap metrics as pre-rounding estimates; unequal rounding can alter portfolio DD despite smaller individual orders.
- [x] Omit zero or exchange-below-minimum rounded allocations from payloads with a recorded reason; guard the actual>0 division explicitly. Do not reactivate suppressed cycles after this omission. No positive payload members is an explicit terminal no-candidate outcome.
- [x] Include geometry/features/settings/binding/raw/floored cap/reference and one-way evidence in appropriate identities. Active WS1.3 and portfolio_optimizer_sizing_v3. The operator confirmed no old WS1.2 result artifacts exist; add no compatibility path for them.

### 4. Config and Panel

- [x] Schema3: liquidity.parameters.lot_model_base_coefficient exactDecimal1..20 default9; lot_model_max_shift_bonus exactDecimal0..2 default1.1. Keep round_down_usdt default10.
- [x] K/bonus are global settings frozen per Campaign. Geometry belongs to individual candidate inputs, not the config document: reject invalid geometry at enrichment, not by rewriting historic payloads or invalidating unrelated config.
- [x] Migrate v1/v2 in memory, remove close_volume_participation_pct without translating it into K. Explicit CAS Save persists v3; frozen Campaign bytes/digests unchanged. Reject retired keys in strict v3.
- [x] Replace participation with K/bonus in existing panel_web/index.html and app.js Settings card, retain rounding. Existing API/CAS only, no new screen or shape controls.

### 5. Checks and delivery

- [x] Failing focused tests before implementation: minute quantile/activity/sparse files/frozen window; formula goldens/bounds/ties/lots/exchange minima; whole-cycle exclusion including fees/close; sequential sides/close-before-open/ties/boundaries/independent symbols/rejected validation; consistent effective metrics and frozen-mask zero-size approximation.
- [x] Independent heterogeneous L/S caps with known optimum, duplicate pair-side rejection, overflow, payload actual/U readback, identity sensitivity, config migration/CAS and strict WS1.3 reads.
- [x] Test exclusions before admission, mask idempotence, same-side source overlap, boundary conflicts, preservation of different source bases, missing anchor, x=0 and positive x rounding to zero, all-candidate exclusions, endpoint M(0.6)=1 and M(5.5)=1+bonus, independent caps with additive margin. The USDT rounding step is not an invented minimum-notional constraint.
- [x] Run .venv\Scripts\python.exe -m pytest focused then relevant broader suites, JS syntax, compilation, git diff --check, independent CODE_REVIEW_PASS. Update this task's docs/progress only, preserve concurrent shortlist edits. Scoped conventional commit after review.

## Findings ledger

F1 remove mixed-member/account gate; F2 preserve source boundary cycles, no cross-run state; F3 retain rounding; F4 schema3 migration; F5 accept zero shift; F6 independent caps; F7 reuse attribution; F8 frozen mask is approximation, not conservative; F9 coherent effective metrics; F10 additive margin; F11 no external execution.

User correction after PLAN_APPROVED: exchange flooring must not trigger a second
bootstrap or historical search. Preserve the cheap `actual<=x<=U` check and
distinguish pre-rounding risk evidence from executable sizes.

Advisor first round PLAN_REVISE. F12 accepted exclusion-before-mask with explicit distinction for later zero-size omission. F13 accepted positive denominator/zero-payload guard. F14 accepted per-symbol occupancy and same-side/source-boundary checks. F15 rejected aggregate-denominator recomputation: existing attribution uses each cycle's own source_basis. F16 accepted units/pairing/endpoint tests. F17 partially accepted: stable below-minimum/all-excluded outcomes, but retain approved USDT-then-quantity floor order; step10 is not a minimum notional. F18 accepted named missing-anchor failure. F19 partially accepted: global settings; geometry is candidate data validated at enrichment, not config-load fields. F20 accepted corresponding focused tests. Independent re-review accepted all dispositions and returned PLAN_APPROVED; no material findings remain.

## Implementation acceptance evidence (2026-09-27)

The GPT-6 Luna slices implemented feature extraction, settings/UI,
input/search and adapter integration. The operator confirmed there are no old
WS1.2 result artifacts; the temporary historical-read exception was removed.
Ten-USDT downward exchange rounding is checked against `actual<=LP x<=U`
without a second bootstrap, per the operator's instruction.

The final eight-suite run passed 1224 tests with one Windows symlink skip.
After the last narrow review fixes, the affected input/search/adapter suite
passed 713 tests. `node --check`, Python compilation and `git diff --check`
passed. Independent Opus 5/high review returned `CODE_REVIEW_FINDINGS` in two
sequential rounds, then `CODE_REVIEW_PASS` after the missing proofs and narrow
guards were added. No tester, live account or exchange action was performed.
