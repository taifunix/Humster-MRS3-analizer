# Performance v2 filter sequence implementation

Version: D2 (revises D1 after complete Advisor findings ledger D1-01..D1-13).
Date: 2026-10-01. Status: `PLAN_APPROVED` (Claude Opus 5/high, D2).

Contract: [active specification](../../specs/2026-10-01-performance-v2-filter-sequence.md),
[filter decision ledger](2026-10-01-performance-v2-filter-review.md),
[ADR-0052](../../decisions/0052-performance-v2-hard-cutoff-rejected.md),
[ADR-0053](../../decisions/0053-performance-v2-dd-profit-guard.md).

Implementation is isolated in branch `feat/performance-v2-filter-sequence`.
No live DB writes, tester runs, portfolio work or historical snapshot rewrites.
Use the root `.venv` for tests, run them in a fresh C: TEMP directory, and
delete verified temporary targets afterward. Baseline selection tests:
13 passed, 169 deselected. Source tree worktree is otherwise clean except
copied decision and research documents.

Before implementation, inspect a disposable Performance v2 schema fixture in
C: TEMP. Assert `strategy_tags` primary key is exactly `(strategy_id, tag)`,
`source` and `source_ref` are VARCHAR, and `REJECTED` is admitted. If the
schema differs, stop: a migration is outside this plan. No live DB is opened.
Preflight passed on a disposable initialized DuckDB fixture; C: TEMP was
removed (`SCHEMA_PREFLIGHT_PASS`).

## 1. Contract and config

Owner: filter engine. Files: `config.performance.json`,
`src/mrs3/performance_v2_selection.py`, `tests/test_performance_v2_selection.py`.

1. Add failing request/config/order tests. Define typed default thresholds in
   `SelectionConfig` and load/validate file settings: Lot 1.10 DD5 / 1.10 DD
   and lot tolerance `1e-9`; hard DD 23 with full PnL/30 < 3 * DD,
   full PnL/30 floor 4, ratio 0.75, ratio gates 45 days/25 reliable cycles;
   A/B 14-day B, floor 4, decline divisor 10 and cap 15, Win Rate 55 after
   25 actual B cycles; top-five 80 after 45 days/25 profitable cycles.
   Remove old trade-frequency decline and old single-trade thresholds.
   Explicit overrides use the spec bounds: Lot multipliers >=1, positive
   tolerance/DD/DD-profit multiplier, nonnegative PnL floor, ratio in (0,1],
   positive integer day/cycle gates, win rate and top-five share in [0,100],
   A/B divisor >=1, decline cap >= B floor. Absent fields default; invalid
   fields raise a typed config error. No override removes the DD profit guard
   or changes strict/inclusive operators. Snapshot and Panel help echo values.
2. Add `filter_hard_cutoffs` to the registry. Enforce the fixed five-stage
   prefix in `effective_selection_stages()` regardless of submitted order,
   preserving later relative order. Equity remains OFF by default in the UI;
   other four are ON. Preserve explicit disabled stages and legacy implicit Lot.
3. Retire enabled `filter_time_consistency` requests with a typed error;
   disabled legacy requests are inert. Keep historical snapshot reading
   (including old single-best reasons and enabled time consistency) without
   a public runtime bypass. Unknown/duplicate IDs remain typed errors;
   empty-stage requests keep existing implicit defaults. Do not migrate old
   snapshot rows. Add focused compatibility tests for each case.
4. Run focused selection config/order tests; then relevant selection tests.

## 2. Candidate facts and five filter rules

Owner: filter engine. Files: `src/mrs3/performance_v2_selection.py`,
`tests/test_performance_v2_selection.py`.

Write each failing focused test before its implementation:

1. Extend candidate loading with `initial_balance`, actual B completed
   cycle count and B win rate from the same reliably reconstructed fully
   closed cycles in the effective B window, reliable full completed cycle
   count from action reconstruction, effective history length, and completed
   net/top-five/remainder/profitable count/reliability. Extend the existing
   SQL cycle query rather than creating another reader. `WindowMetrics.trade_count`
   can include a partially open tail after a realised decrease and must not
   be used for the 25-cycle gate. Verify open tails, exact B close boundary,
   zero-PnL cycles (counted in 25, omitted from win-rate denominator), side
   flips and commission omission. Both full and B counts remain unavailable
   on unreliable reconstruction.
2. Replace Lot lexicographic winner selection with a direct unambiguous
   exactly-two-member EQUAL/INCOME comparison: EQUAL per-order lot spread
   <=1e-9, INCOME spread >1e-9; identical settings/interval/positive initial
   balance and positive total lot within tolerance. For each variant x:
   `full_dd5_x = full_pnl30_x * 5 / full_dd_x` and
   `b_dd5_x = b_pnl30_x * 5 / full_dd_x` (B uses FULL DD denominator).
   Require positive DD on BOTH sides, positive EQUAL full/B return and all
   finite facts. INCOME wins only if full_dd5_income >= 1.10 * full_dd5_equal,
   full_dd_income <= 1.10 * full_dd_equal, b_dd5_income >= b_dd5_equal,
   and b_pnl30_income >= b_pnl30_equal. Otherwise EQUAL wins when complete.
   Record representative ID on both rows and specific loser reason. Missing,
   nonpositive baseline, same-shaped pairs and ambiguous/>2-member groups stay.
   Test equality, each failed criterion, zero DD, 3ORD `1e-12` rounding and
   no false collapse. Existing protect_equity is false when Equity is OFF;
   when Equity is ON the new first stage removes blocked rows before Lot.
   Test both, then remove redundant protection.
3. Add independent hard-cutoff evaluator. Test DD 23/24, full PnL equal to
   `3*DD`, below it and missing; PnL floor 4; dual ratio equal/below 0.75;
   44/45 days and 24/25 reliable cycles; overlap. Store every triggered
   condition and measured values in a deterministic `elimination_reason`
   beginning with stage ID. Missing full PnL never triggers DD, floor or
   ratio; missing ratio facts do not suppress an available DD/floor failure.
   Keep Auto Status `FILTERED`.
4. Revise A/B evaluator to independent OR branches; test 4/15/55,
   24/25 fully closed B cycles, partially missing facts and zero-PnL
   denominator from the same B cycle set. Record
   triggered conditions and incomplete diagnostics distinctly.
5. Replace one-best-trade exclusion with top-five share of net completed PnL;
   test 44/45 days, 24/25 profitable cycles, 80 equality/>80, nonpositive
   net, unreliable actions and fewer than five profitable cycles. Export
   top-five share, completed net and remainder in the selection XLSX with
   top-five labels/reasons (retain legacy stage ID). No one-best claim remains.
6. Remove time-consistency elimination while retaining calculations and
   `Positive windows` XLSX. Correct `_consistency_summary` for `NO_TRADES`:
   two positive plus two no-trade = 2/2; all no-trade and missing = N/A.
7. Test missing-is-not-zero in Lot, hard, A/B and top-five cases. Run full
   `tests/test_performance_v2_selection.py` including workbook column tests.

## 3. Snapshot publication and effective status

Owner: publication/UI integration, after filter engine. This owner alone edits
`panel.py`. Files: `src/mrs3/performance_v2_selection_review.py`,
`src/mrs3/panel.py`, `tests/test_performance_v2_selection_review.py`,
`tests/test_panel_performance_v2.py`, optionally
`tests/test_panel_performance_v2_export.py`.

1. First add failing tests that preview creates zero runs, results and tags;
   that non-stage-3 losers and stage-3 survivors receive no tag;
   one tag for an actual stage-3 exclusion, immutable run/result evidence,
   current User Status in the published workbook, and existing User Rank.
2. Inside `persist_selection_snapshots()` existing transaction, after stale
   current-result/equity source checks and result inserts, upsert only actual
   hard-cutoff losers into `strategy_tags` as `REJECTED`, source
   `SELECTION_HARD_CUTOFF`, source_ref exactly selection_run_id. Upsert conflict
   target is `(strategy_id, tag)`. The run stores effective
   request/config; result `auto_reason` stores rule/value evidence. Repeat
   publication updates provenance on one tag row without duplicates. No delete
   on disable. Recheck current result IDs inside the transaction.
3. Test injected tag-write failure and stale source/result rollback: no
   partial run, result or tag. Retain the existing combined-workbook atomic
   multi-snapshot path.
4. Resolve a present current REJECTED tag before older manual review status;
   a later explicit review import can delete it. Preserve User Rank. Test both
   temporal directions.
5. Overlay existing and pending REJECTED statuses before building XLSX bytes;
   if publication fails, return no workbook. Test normal and combined export.
6. Assert combined publications without a true hard-cutoff flag do not tag.
   Run review, Panel and export focused tests.

## 4. Panel order and explanations

Owner: same publication/UI integration owner, after its publication slice.
Files: `src/mrs3/panel_web/index.html`,
`src/mrs3/panel_web/app.js`, `src/mrs3/panel.py`, `tests/test_panel_static_ui.py`,
`tests/test_panel_performance_v2.py`.

1. Add failing static/client tests for exact first five order, fixed prefix
   movement, current enabled defaults, absent time-consistency control,
   active A/B label, nonempty existing help field for every visible stage,
   unchanged later relative order and payload inclusion of disabled Equity;
   stage-5 visible label/help/XLSX reason describe top five and its gates.
2. Reorder controls, insert hard cutoff, remove time-consistency stage, and
   update Panel default order/enabled sets and fixed prefix. Existing later
   stages remain. Keep accessible label/help relationship.
3. Provide current typed selection config through the existing Panel
   metadata/catalog response and update help when loaded. Static fallback
   help matches default config. Explicitly describe Lot's four AND checks,
   hard cutoff's guarded DD and three OR branches plus published REJECTED,
   A/B's independent OR checks and B cycle gate, top-five gates/share, and
   the other existing stage criteria. Editable thresholds describe the input.
   Remove the obsolete Lot/Equity warning after testing ON/OFF behavior.
4. Run static UI/Panel/export tests and `node --check`.

## 5. Integration and final gate

Owner: root. Files: `PRD.md`, `progress.md`, active spec and plan status,
ADR-0052/0053, older finalist/lot/robust specs and decision ledger.

1. Integrate each slice; reconcile shared `panel.py` changes; run the
   relevant full selection/review/Panel/static/export suites through `.venv`
   from C: TEMP and clean them. Check `node --check`, `git diff --check` and
   inspect complete/staged diffs. Run broader tests only for a concrete risk.
2. Verify supersession notes and both user confirmations in the ledger. Send
   a compact self-contained ASCII packet with requirements, diff and
   verification to independent `review_code`. Fix findings, rerun affected
   tests and re-review until `CODE_REVIEW_PASS`.
3. Update PRD/progress with verified state and limitations. Create one scoped
   conventional commit on the feature branch after review. Do not include
   generated XLSX or local database files. Report branch, commit, tests,
   review and remaining unresolved product choices.
