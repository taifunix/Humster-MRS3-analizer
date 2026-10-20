# ADR-0084: Every strategy name carries its exchange tag

Date: 2026-10-20. Status: accepted (user, 2026-10-20: «меняй и приводи к
единому виду, в будущем может добавиться 3 и 4я биржи, алгоритм должен это
учитывать»).

Amends [ADR-0083](0083-one-performancedb-for-both-exchanges.md), decision 5.
Contract: [Panel for the simulation workflow](../specs/2026-10-18-panel-simulation-workflow.md),
«Stage 5e contract», and the
[history spec draft](../specs/2026-10-20-panel-history-incremental.md),
requirement 6.

## Context

- ADR-0083 tags only non-Bybit names (`BINGX_…`). Bybit names stay untagged,
  because the live database and the finalist marks used them.
- On 2026-10-20 the active PerformanceDB (`performance-v2-test`) holds no
  results, and the registry holds no finalist marks. There is nothing to
  rename.
- More exchanges may come. A rule with Bybit as the exception would have to
  be repeated for each of them.
- In the code, «has a tag» also meant «needs an API symbol». That holds for
  BingX only.

## Decision

1. **The tag of an exchange is its canonical name in upper case plus `_`**,
   for every known exchange: `BYBIT_`, `BINGX_`, and any exchange added to
   `KNOWN_EXCHANGES` later. There is no exception.
2. **Every generated strategy name starts with the tag of its exchange:**
   - READY strategies (`build_ready`);
   - the tick batch (`prepare_tick_batch`);
   - the retest copies, which keep the stored names.

   The older generators that the Panel no longer uses (fresh analysis, the
   portfolio adapter, `tester_run_files`, the plateau research scripts) stay
   untagged; their Bybit names are still accepted as legacy names (decision
   3). A folder of READY built before this change is listed as «пересоберите
   READY» by `list_ready_folders`, not only refused later by the batch.
3. **The import enforces it both ways** (`EXCHANGE_NAME_TAG`):
   - a strategy of a known exchange E needs the tag of E;
   - a name with the tag of E must be of E.

   **One exception, historical:** an untagged name is accepted for Bybit
   only (`LEGACY_UNTAGGED_EXCHANGE`). Bybit is the only exchange that existed
   before the tags, so the names made then are untagged; every later exchange
   is born tagged. Everything this code generates carries the tag.
   (Strict import for Bybit too would break 77 of the 144 import tests,
   whose fixtures use names like `alpha`, for no gain: an untagged name can
   only ever be Bybit.)

   A report that names no known exchange (an unmarked legacy database, a
   `tester` report without a strategy exchange) has nothing to enforce except
   the second rule, as before.
4. **The API symbol is a separate per-exchange property, not the tag.** An
   exchange whose tester symbol differs from the human ticker (BingX) needs
   the registry's API symbol (`exchange_needs_api_symbol`). Bybit trades the
   human ticker. In the code a tag no longer stands for «needs an API
   symbol».

## Consequences

- **READY folders of the trade store built before this change** have
  untagged Bybit names. The tick test refuses them and asks to rebuild READY,
  as it already does for untagged BingX folders.
- **Databases with untagged Bybit results** (the live v1 database, old test
  databases) keep working: they open, read and take new imports of untagged
  Bybit names.
- **A new Bybit batch is tagged** (`BYBIT_...`), so a Bybit strategy that is
  tested again gets a second, tagged name next to its untagged one. This
  happens only when an old untagged database is used with a rebuilt READY.
  The user does not use old databases (2026-10-20): a new protocol-v2
  database is created on «Настройки».
- **A third exchange** is added by one entry in `KNOWN_EXCHANGES` (tag and
  import rule follow) and, if its tester trades another symbol than the
  human ticker, one entry in `API_SYMBOL_EXCHANGES`.
- No schema change and no migration.
