# Performance v2 robust finalist ranking implementation plan

**Goal:** Add robustness filters, controlled first-Shift preference and a
deterministic final Top-50 ranking to the disposable Performance DB v2
selection pipeline.

**Spec:** `docs/specs/2026-09-01-performance-v2-robust-finalist-ranking.md`

**Status:** Implemented; focused verification passed; follow-up independent review `CODE_REVIEW_PASS`.

## Constraints

- Reuse the current closed stage executor, window cache, counters and workbook.
- No Performance DB schema migration and no Stage 3 persistence.
- New movable stages default to Pair + Side + TF; final ranking is fixed last
  at Pair + Side.
- Missing facts do not cause filter or Pareto elimination. Ranking weights are
  renormalised across each candidate's present components.
- Use TDD and `.venv\Scripts\python.exe -m pytest` exclusively.

## Task 1: Freeze request and configuration contracts

**Files:**
- Modify: `config.performance.json`
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `tests/test_performance_v2_selection.py`

- [ ] Add failing parser tests for the four movable stage IDs and fixed
  `rank_robust_top_n`.
- [ ] Add validation for `pnl_tolerance_pct` in `[0, 100)`, positive integer
  `top_n`, fixed ranking scope and mandatory-last ranking position.
- [ ] Add the `35%`, minimum four profitable trips and minimum `10` bp Shift
  advantage configuration defaults.
- [ ] Cover the parser rejection matrix: ranker not last/duplicate/wrong scope,
  non-positive/non-integer N, and tolerance `0` accepted, `100`/negative
  rejected.
- [ ] Run the focused parser/config tests.

## Task 2: Derive best-trade dependency facts

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `tests/test_performance_v2_selection.py`

- [ ] Add failing action fixtures for a complete trip, partial reductions,
  separate fees, multiple trips and an incomplete tail.
- [ ] Derive completed-trip realised PnL in one grouped DuckDB query for the
  selected Pair + Side; do not load actions once per candidate and do not
  subtract the separately stored fee from PnL a second time.
- [ ] Add best-trade share and PnL-without-best facts to candidate rows.
- [ ] Prove incomplete tails, non-canonical side flips and fewer than four
  profitable completed trips do not eliminate.

## Task 3: Cache four chronological consistency windows

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `src/mrs3/panel_performance_v2.py`
- Modify: `tests/test_performance_v2_selection.py`
- Modify: `tests/test_panel_performance_v2.py`

- [ ] Add failing tests for four equal requested intervals, flat-boundary
  snapping and positive-window counting.
- [ ] Extend the existing window worker from full/A/B to full/A/B plus four
  chronological windows while loading each result source once.
- [ ] Extend Pair + Side readiness and both recalculation actions to require the
  seven cached windows.
- [ ] Keep the existing bounded worker configuration; add no new executor.
- [ ] Prove a legacy full/A/B-only cache is not ready and missing quarters cause
  zero consistency eliminations.
- [ ] Run cache/readiness/recalculation tests and record a live before/after
  timing for one large Pair + Side during acceptance.

## Task 4: Execute the new robustness stages

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `tests/test_performance_v2_selection.py`

- [ ] Add failing tests for `filter_best_trade_dependency` at `35%` and zero
  PnL without the best trade.
- [ ] Add failing tests for `filter_time_consistency` at `3/4` and unavailable
  windows.
- [ ] Add derived robust PnL, worst DD, worst holding, A/B stability and minimum
  per-order plateau points.
- [ ] Add `pareto_robust` through the existing vectorized Pareto helper.
- [ ] Add row-order-independent `pareto_shift_near_tie` with the exact 10%
  PnL boundary, minimum `10` bp Shift advantage, one simultaneous pass and
  positive-PnL requirement.
- [ ] Run stage order, scope, tie and missing-data tests.

## Task 5: Add deterministic fixed Top-N ranking

**Files:**
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `tests/test_performance_v2_selection.py`

- [ ] Add failing tests for the `38/17/15/10/10/10` score components, including
  the Close MA component.
- [ ] Implement average-rank quality percentiles across final Pair + Side
  survivors, with the points component ranked within TF, including one-value,
  zero-DD and missing-component weight-renormalisation rules.
- [ ] Implement deterministic tie-breaks, `final_score`, `final_rank` and the
  enabled Top-N elimination flag.
- [ ] Prove an entirely unrankable row is retained with explicit status,
  disabled ranking remains diagnostic, an N-boundary tie keeps exactly N
  rankable rows, and survivors fewer than N are retained.

## Task 6: Expose defaults, counters and XLSX diagnostics

**Files:**
- Modify: `src/mrs3/panel_web/index.html`
- Modify: `src/mrs3/panel_web/app.js`
- Modify: `src/mrs3/panel_web/app.css`
- Modify: `src/mrs3/panel.py`
- Modify: `src/mrs3/panel_performance_v2.py`
- Modify: `src/mrs3/performance_v2_selection.py`
- Modify: `tests/test_panel_static_ui.py`
- Modify: `tests/test_panel_performance_v2.py`
- Modify: `tests/test_performance_v2_selection.py`

- [ ] Add the recommended movable order, checks and Pair + Side + TF scopes.
- [ ] Keep historical stages visible but disabled by default.
- [ ] Add one compact 10% PnL-tolerance input to the Shift stage.
- [ ] Add a fixed final ranking row with enabled checkbox, Top-N input default
  50, Pair + Side label and automatic counter.
- [ ] Serialize ranking last and reject bypass attempts server-side.
- [ ] Add the specified numeric diagnostics and enabled-stage booleans to both
  workbook sheets.
- [ ] Export A/B stability and each ranking component percentile for score
  auditability.
- [ ] Export present-weight coverage and each effective renormalised component
  weight; label score and Top-N as relative to the selected Pair + Side.
- [ ] Run focused panel, endpoint, workbook and JavaScript syntax checks.

## Task 7: Acceptance and handoff

- [ ] Run:
  `.venv\Scripts\python.exe -m pytest tests/test_performance_v2_selection.py tests/test_panel_performance_v2.py tests/test_panel_static_ui.py -q`
- [ ] Run `node --check src/mrs3/panel_web/app.js`.
- [ ] Run permutation-invariance tests for robust Pareto, Shift near-tie and
  final ranking.
- [ ] Run a baseline regression with all new stages and ranking disabled.
- [ ] Run `git diff --check` and inspect the scoped diff.
- [ ] Send the compact ASCII requirement/diff/test packet to the independent
  reviewer and resolve confirmed findings.
- [ ] Update `progress.md` and PRD evidence only after verification.
- [ ] Do not commit or push until explicitly requested by the user.
