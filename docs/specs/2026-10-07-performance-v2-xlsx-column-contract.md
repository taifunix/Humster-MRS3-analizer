# Performance v2 XLSX column contract

## Purpose

All Pareto, PerformanceDB, current finalist control, and bulk finalist
control workbooks use the same `write_selection_workbook` renderer.  Their
column order is a presentation contract.  A previously calculated cache must
not change that contract by itself.

This specification separates the retired R7.3 equity-quality display from the
current equity-regime filter.  It does not change either filter's calculation,
selection, ranking, or database publication rules.

## Root cause addressed

The renderer used the presence of fresh `_equity_cache` data as a reason to
publish the four R7.3 columns.  Consequently, a legacy export could acquire
`Equity state`, `Equity basis`, `Equity DD, %`, and `Equity smoothness` merely
because the pair had previously been processed by the old equity method.  A
new regime snapshot could also add a second equity block.  The same pair could
therefore produce different headers after different historical actions.

## Block rules

The renderer chooses blocks from the current request and valid published
regime snapshots only:

| Condition | R7.3 quality block | Equity-regime block |
| --- | --- | --- |
| No equity stage and no valid published regime snapshot | absent | absent |
| Stale or fresh R7.3 cache only | absent | absent |
| `rank_robust_top_n` with `method=equity_quality_v1` | present | present only when a regime snapshot is also published |
| Enabled `filter_equity_regime` | absent | present |
| Valid current published regime snapshot, regardless of request stages | absent unless the explicit old ranking method is enabled | present |
| Both explicit old ranking and regime filter | present | present |

The old four columns are never added because a cache row exists.  The new
regime filter never adds the old four columns.  A missing value inside an
enabled block is rendered as a blank cell; it does not remove the block.

## Canonical visible order

Every generated candidate sheet follows this order.  Optional review identity
and review columns retain their existing conditions, but never move other
blocks.

1. **Identity and period:** `ID`, optional `Result ID`, `РЎС‚СЂР°С‚РµРіРёСЏ`,
   `РџР°СЂР°`, `Side`, `РўР¤`, `Start`, `End`.
2. **Core strategy/result facts:** `ORD`, `Close`, `PnL/30`, `PnL DD5/30`,
   A/B PnL and day columns, positive windows, CE, PF, DD, W/R, trades,
   Trades/30, Lot DD5, holding metrics, `Top 5 share, %`, and A/B stability
   diagnostics. The verbose `History days`, cycle-count, completed-net-PnL,
   `Top 5 PnL`, and `PnL after top 5` display columns are intentionally
   omitted; the underlying facts remain available in the database/cache.
3. **Selection diagnostics:** rank quality/weight diagnostics and
   `Final score (Pair+Side)`.
4. **Strategy parameter summaries:** `1 Shift`, `2 Shift`, `3 Shift`, `Lots`,
   `Points`, `MA`.
5. **Optional R7.3 quality block:**
   `Equity state`, `Equity basis`, `Equity DD, %`, `Equity smoothness`.
6. **Optional equity-regime block:**
   `Regime rank`, `Regime reasons`, then PRE28, W28, W14, and W7 direction,
   value, and p-value groups in that order, followed by regime DD14/DD7, ATH
   stage counts, `New ATH W7`, and `Held ATH W7`. Each period group's p-value
   column has a double right border. The classifier's `state` and `decision`
   remain internal selection facts and are intentionally not exported as
   columns.
7. **Decision and review:** `Auto Status`, `Auto Rank`, optional `User Status`,
   `User Rank`, `RETEST`, `Comment`, and `РџСЂРёС‡РёРЅР°` last.

`All candidates` and `Finalists` have identical headers.  The finalist-retest
control workbook renames `All candidates` to `Candidates` and preserves that
exact header sequence.  The `Groups`, `Retest Failures`, and very-hidden
metadata sheets keep their existing schemas.

## Compatibility and acceptance

- Review imports continue to use their existing protected headers and ignore
  display-only equity diagnostics.
- Existing old workbooks remain importable; this contract governs newly
  generated workbooks.
- No database write, cache warm, regime recalculation, or ranking is started by
  export.
- Focused tests cover stale old-cache suppression, regime-only output, the
  explicit old-ranking block, exact block order, and control-sheet header
  preservation.
