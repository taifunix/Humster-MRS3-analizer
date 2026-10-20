# Panel «Пересчитать историю»: incremental by default, fees and contract data in Настройки

**Status:** requirements recorded from the user, 2026-10-20.
- **Requirement 1** is met by the existing P2 plan: the user accepted it as it
  is («пока оставляем и ничего не меняем»).
- **Requirement 5** («принял годится, делай пересчет при изменении») and the
  «пересчёт не требуется» message are implemented («Contract: instrument and
  up-to-date plan» below).
- **Requirement 6** is approved (ADR-0084) and pending.
- **Requirements 2 and 4** are pending.
**Depends on:** [MRS2 trade store](2026-10-17-mrs2-history-store-and-tester-sampling.md)
(segment plan, supersede rule), [ADR-0078](../decisions/0078-mrs2-trade-store.md),
[Panel for the simulation workflow](2026-10-18-panel-simulation-workflow.md),
[sync by exchange](2026-10-20-panel-sync-by-exchange.md) (candle download).

## Requirements (user, 2026-10-20)

1. **Incremental by default.**
   - **A new pair** (no segments yet, or its listing has just become old
     enough) gets the full P2 history: 20-day segments, the remainder joined
     to the last one.
   - **A pair with history** is not recomputed from the start. Only the last
     period is recomputed with the new days and replaces the stored one.
   - When the new days make the last period long enough to hold two periods,
     two segments are simulated and stored instead of one: a 20-day one and
     one of 20 days plus the remainder.
2. **Recompute interval N.** The last period is recomputed only when more
   than N days have passed since the last computation. N is set on
   «Настройки».
3. **No download in this action.** The candles are downloaded by
   «Синхронизировать». «Пересчитать историю» only checks that the needed days
   are present and current, and downloads only a missing day.
4. **Fees per exchange on «Настройки»**, set by hand (maker, taker; with
   slippage and funding as today). **One value per exchange for every
   calculation and test** (user, 2026-10-20): the simulator, the MRS2 tester
   sampling, the MRS3 tick test, the retest and the optimizer all take it;
   the tester templates no longer hold their own fees. Today:
   - Bybit takes the simulator defaults (`RunSettings`: maker 0.00001, taker
     0.0002);
   - BingX takes `templates/exchanges/BingX/mrs3-tester.json` (maker 0.00012,
     taker 0.0003).
5. **Contract data from one source.** Bybit takes tick size and qty step from
   the registry, as BingX already does. Today Bybit reads
   `<trade store>/instruments.json`, a cache filled from the Bybit API once
   per symbol and never refreshed. The registry is refreshed by every
   «Синхронизировать» (step `refresh`):
   - Bybit: `instruments-info`, `priceFilter.tickSize` and
     `lotSizeFilter.qtyStep`;
   - BingX: `contracts`, a step of `pricePrecision` and of
     `quantityPrecision`.

   A change is journaled in `pair_changes`. A changed tick size or qty step
   recomputes the pair (user, 2026-10-20; see the contract below).
6. **Strategy name tag for Bybit too** (proposal, 2026-10-20). The
   active PerformanceDB (`performance-v2-test`) has no results and the
   registry has no finalist marks. So `BYBIT_` can be introduced now
   without renaming anything. This changes the name rule of ADR-0083, so it
   needs a new ADR.

## What the code does today (read 2026-10-20)

- `pair_segments(listing, end)` plans:
  - P2 periods over `[listing + 4, end − 20)`, the remainder joined to the
    last one (`split_periods`);
  - then a separate latest window `[end − 20, end)`.
- Every run plans the whole list, but only the segments that are not active
  in the store are simulated:
  - older P2 periods keep their dates, so they are skipped;
  - a new segment supersedes the active segments it overlaps.

  So the history is **not** recomputed from the start today. With one more
  day, two segments per pair change: the last P2 period (its end moves) and
  the latest window.
- When the last P2 period reaches 40 days, `split_periods` cuts it into
  20 + 20. This is the user's «two segments instead of one», but at the
  P2 level: the newest 20 days are always the separate latest window.
- **No interval:** with any new day every pair's plan row is checked.
- **Download inside the action:**
  - Bybit: `ensure_days` downloads only days without a file.
  - BingX: `verify_bingx_days` skips accepted days and verifies valid files
    on disk without a request; days without a file are requested.
  - The sync downloads only the BingX liquidity window (7 weekdays) and no
    Bybit days, so the first history run downloads the rest.

## Contract: instrument and up-to-date plan (implemented 2026-10-20)

**What tick size and qty step change in the simulator** (`mrs2_simulator`):
- the order price, SMA × multiplier, is rounded to the tick;
- the post-only shift is one tick away from the market;
- a limit fills only when the price trades at least one tick beyond it
  (ADR-0077);
- the position quantity is floored to the qty step.

They change the trades, so the segment metrics, the READY score and the
chosen structures. 2026-10-20: all 254 Bybit pairs with history were
simulated with the same values as the registry holds.

**The contract:**
- **Source:** the Bybit history takes the tick size and qty step from the
  registry (`registry_instruments`), as BingX does. A pair without them is
  skipped with a warning. Without a registry, the old
  `<store>/instruments.json` cache is used.
- **Recompute:** `run_history` compares each planned segment's stored
  `tick_size` / `qty_step` with the current ones (relative 1e-9). A catalog
  row without them counts as current, in the run and in the plan.
  - **A different active segment** is simulated again under the same id.
    - Its files get their own names (`instrument_variant`), so the active
      row's files are not touched.
    - The active row stays in use until `register(..., replace_active=True)`
      swaps it in one transaction. So the pair's active chain never has a gap
      (independent review 2026-10-20, MAJOR).
    - A failed simulation (`MISSING_DAYS`, …) keeps the old row, and the next
      run tries again.
    - A run killed after writing the new files leaves the old row in use.
      The next run writes the same files again and swaps the row.
  - **The tester segment of the same span is superseded in the same
    transaction as the swap** (`register(..., supersede_span_of="tester")`):
    it was tested with the old instrument, and a kill in between can never
    leave it active. The swap is journaled as `INSTRUMENT_CHANGED` with the
    old and new values.
  - **`tester_jobs import_window` never reactivates** a superseded tester
    segment whose tick size or qty step differ from the window's
    (`INSTRUMENT_CHANGED`); a new plan makes a new selection.
  - **A superseded segment of another instrument** is never reactivated.
  - **The files of the old instrument** stay on disk without a catalog row.
- **A busy registry stops the Bybit history** («held by another process»),
  instead of falling back to the old cache (review, MAJOR).
- **Readers:** a reader that took a row before the swap reads that row's
  files, which are not replaced. The old `replace_unregistered` path still
  rewrites a superseded row's files; readers never use a superseded row.
- **Plan («Пересчитать историю»):**
  - a pair with a stored segment of another instrument shows «tick или шаг
    лота изменились: пересчитать N периодов» and is checked;
  - a pair without a tick size or qty step in the registry shows «нет tick
    или шага лота в справочнике» and is not checked (the script skips it);
  - when no row is checked, the plan answers `up_to_date` with «Пересчёт
    не требуется: все пары с достаточной историей посчитаны до <day>
    включительно». The Panel shows it as the status, without a dialog.
  - Real data, 2026-10-20 (already computed that day): 254 pairs «без
    изменений», 20 «мало истории».

## Questions for the user

- The newest 20 days are a separate latest window today. Keep it, with the
  remainder joined to the P2 period before it? Or make the last segment
  «20 days + remainder», as described in requirement 1? READY scores the
  newest window, so this choice changes READY.
- Requirement 1 says «остаток > 2». Read here as «the last period holds at
  least two full 20-day periods» (40 days and more). Confirm.
- N: count it from the end day of the stored latest window, or from the
  time of the last run? What default?
- Fees in «Настройки»: should they also replace the fees in the tester
  templates? Otherwise the simulator and the tick test would disagree.

## Non-goals

- No change to the simulator's trade rules or to READY v4.
- No change to the supersede rule of the store.

## Acceptance evidence (to fill when implemented)

- a pair with history and fewer than N new days is not planned;
- a pair with N or more new days recomputes only its last segment(s); older
  segments keep their ids;
- the split at 40 days stores two segments and supersedes the old one;
- fees set in «Настройки» reach the simulator run settings of that exchange;
- Bybit tick size and qty step come from the registry.
