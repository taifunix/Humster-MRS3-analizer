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
   Trades/30, Lot DD5, holding metrics, `Top 5 share, %`, `Fees share, %`,
   `Fees/30, %`, and A/B stability diagnostics. The verbose `History days`, cycle-count, completed-net-PnL,
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

## Fee efficiency columns (amended 2026-10-12)

`Fees share, %` and `Fees/30, %` follow `Top 5 share, %` in block 2 and in the
aggregate workbook headers. Both come from the cached full-report window facts,
the same window that gives `PnL/30` and `Trades/30`; no extra database read is
needed.

- `Fees share, %` = `fees_pct / (return_pct + fees_pct) × 100`, the share of gross
  pre-fee return paid as fees. Both inputs use the window baseline. The cell is
  blank when that gross is not positive.
- `Fees/30, %` = `fees_pct × 30 / calendar days`, blank below one day.

They are display-only and take no part in any filter or ranking. Funding is
not separated: it stays inside the balance-derived return. Panel shows no
candidate metric table, so the columns appear only in the XLSX.

- When a window does not start flat, `fees_pct` counts only fees of included
  round trips, while fees of excluded scoped actions stay inside `return_pct`.
  Gross is then slightly understated and the share slightly overstated.
- Under the FIXED protocol (protocol v2 §4.6), `fees_pct` and `return_pct` must
  be rebased on the same base B, otherwise their sum is no longer gross.
- Aggregate workbooks exported before these columns stay importable: the
  validator accepts the previous header tuple, each checked against its own
  `headers_sha256`. A previous header tuple with the current hash is rejected.

## Reason of REJECTED rows (amended 2026-10-12)

For a row shown with status `REJECTED`, `Причина` (aggregate: `Elimination
Reason`) names the filter or review that set `REJECTED`, not the first stage
that fired in the current run. The source is the earliest of the rejections
currently stored for the strategy: its `strategy_tags` tag `REJECTED` (one row
per strategy; the latest writer replaces its source) and its
`strategy_rejection_sources` rows. Without a stored source, the automatic
filter that rejects it in this run is shown. Known limits: review imports and
finalist retest replace `REJECTED` tags with source `SELECTION_REVIEW`, so a
strategy first rejected by an automatic filter shows `USER_REVIEW;REJECTED`
after such a write; the all-pairs automatic-rejection publication stores
`SELECTION_AUTOMATIC_REJECTION` without the filter name and shows
`AUTOMATIC_FILTER;REJECTED`. Applies to the pair selection workbook and the
all-pairs workbook. Finalist control/retest candidate workbooks are excluded:
their `Причина` column is the importer's protected `Auto Reason`. Labels:
`FILTER_LOT_VARIANT_REDUNDANCY;REJECTED`, `FILTER_HARD_CUTOFFS;REJECTED`,
`AB_DETERIORATION;REJECTED`, `USER_REVIEW;REJECTED`, `AUTOMATIC_FILTER;REJECTED` and
`FILTER_EQUITY_REGIME;<reason code>;REJECTED`. If the current reason already
starts with the same filter, its detailed text stays. Display only: persisted
`selection_results.auto_reason` and stage traces are unchanged.

## MA type column (amended 2026-10-13)

Protocol v2 §4.4 records the MA type and source of the close MA and of every
opening order (schema v12). `MA type` follows `MA` in block 4 and in the
aggregate workbook headers. The column is always present, so the header does
not depend on the data.

- An order or the close MA is shown as `<type> <source>`; a missing half is
  `?`; an order with no facts is `—` (an em dash, so the aggregate workbook
  does not escape it as a formula).
- If every order and the close MA have the same complete pair, the cell is that
  pair once, for example `SMA ohlc4`.
- Otherwise the orders are joined with ` / ` and the close MA follows after
  `; Close `, for example `SMA ohlc4 / EMA close; Close SMA ohlc4`.
- A strategy without any facts (every legacy row) gets a blank cell.

Display only: no filter or ranking reads it, and the portfolio input snapshot
drops it, so snapshot documents and digests do not change. Selection reads the facts only
from a schema-v12 database; an older database gives blank cells. Aggregate
workbooks exported before this column stay importable: the validator also
accepts the header tuple without `MA type` (with and without the fee columns),
each checked against its own `headers_sha256`.

## Lot mode column (amended 2026-10-13)

`Lot mode` follows `MA type` in block 4 and in the aggregate headers: `FIXED`
for a result whose stored sizing is the protocol v2 fixed lot (protocol v2
§4.5), otherwise `DYNAMIC`. For `FIXED` rows `DD`, the derived `Lot DD5`, DD5
and CE use the drawdown as a fraction of the base B (protocol v2 §4.6). The
portfolio input snapshot drops the column. Aggregate workbooks exported
without it stay importable, each accepted header tuple with its own hash.

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
