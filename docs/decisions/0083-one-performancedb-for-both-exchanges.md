# ADR-0083: One PerformanceDB for both exchanges

Date: 2026-10-19. Status: accepted.

Contract: [Panel for the simulation workflow](../specs/2026-10-18-panel-simulation-workflow.md),
«Stage 5e contract». Supersedes the exchange binding of the P7 multi-database
design: each database was bound to one exchange by an immutable `schema_info`
marker.

## Context

- Under P7 a protocol-v2 PerformanceDB is bound to one exchange:
  - the `schema_info` marker `exchange`;
  - `require_database_identity` refuses a configuration of another exchange
    (`EXCHANGE_MISMATCH`);
  - the import rejects a report of another exchange (`_identity_rejection`).
- BingX would therefore need its own database.
- On 2026-10-19 the user decided that this split only gets in the way, since
  there are now few strategies. There is one database, with an exchange
  identifier.

**Audit, 2026-10-19:**
1. **The exchange is already stored per strategy.**
   - `strategy_results.exchange VARCHAR NOT NULL` is written on every
     import: from the report's `exchange.name`, else from the manifest /
     strategy `exchange_name`.
   - The value was stored raw. The real tester writes `exchange.name =
     "tester"` into its HTML, while the strategy JSON names the real
     exchange.
   - `strategy_results.strategy_id` is `UNIQUE`, so each strategy has
     exactly one result row and therefore one exchange.
   - Strategies are inserted only together with their result.
2. **The binding to one exchange is not in the data.** It lives only in the
   marker and in the two checks above.
3. **A schema change would cost a lot for no gain.** Schema v13 would touch
   about 30 modules and scripts with exact catalog checks (store, compaction,
   maintenance, pruning, research scripts) and give nothing that
   `strategy_results.exchange` does not already give.
4. **Names would collide.** `strategies.strategy_name` is `UNIQUE`, and the
   generated name (`SYMBOL_TF_SIDE_nORD_CMAx_<structure>_<method>`) is the
   same for the same structure on both exchanges.
5. **The live database** (read-only, 2026-10-19) is protocol v1, schema 6,
   with no exchange marker. All 1,805 of its results hold
   `exchange = 'tester'`, not an exchange name.

## Decision

1. **No schema change** (the current v12; the live v6 database migrates to it as before).
   - The exchange identifier of a strategy is the `exchange` of its result
     (one per strategy). It is compared case-insensitively.
   - A stored `Bybit` / `BingX` counts as that exchange.
   - Any other stored value (the legacy `tester`) counts as the database's
     home exchange: its marker, or Bybit without one
     (`records.result_exchange_sql`). So every existing result stays Bybit.
2. **The effective exchange of a report** (`performance_v2_import.effective_exchange`):
   - The report's exchange if it is known.
   - If the report names no exchange or `tester`: the exchange of its
     hash-verified strategy JSON / manifest.
   - Any other unknown name stays unknown.
   - Two known exchanges that differ are `EXCHANGE_CONFLICT`.
   - The result stores the canonical name (`Bybit`, `BingX`), never
     `tester`.
3. **The database accepts reports of every known exchange**
   (`KNOWN_EXCHANGES = Bybit, BingX`, the exchanges with a profile in
   `templates/exchanges/`).
   - In a marked database a report must name a known exchange
     (`EXCHANGE_MISSING`, `EXCHANGE_UNKNOWN`). An unmarked v1 database keeps
     accepting unnamed reports, as before.
   - The `exchange` marker of an existing database stays as its home
     exchange, for history.
   - It no longer refuses a configuration or a report of another exchange.
   - The evaluation protocol check (`v2`) is unchanged.
4. **Readers filter by exchange** wherever a user works on one exchange:
   - the «Стратегии» view and the registry imports (tick tests, finalists),
     by the Panel's exchange switch;
   - the optimizer and its strategy selection, by a new exchange selector
     on «Портфели»;
   - retest and finalist retest, by the exchange of the finalists.
   Maintenance, compaction and pruning stay database-wide.
5. **Names of strategies on an exchange other than Bybit carry the exchange.**
   - Bybit names are unchanged, so existing strategies, results, finalists
     and marks keep their identity.
   - BingX names start with `BINGX_`.
   - The import enforces this both ways in every database, marked or not
     (`EXCHANGE_NAME_TAG`): a BingX strategy needs the tag, and a tagged name
     must be BingX.
   - So a name determines its exchange, and a replacement never moves a
     result to another exchange.

## Consequences

- No migration, and no change to stored data. Existing databases keep
  working as they are.
- A database created for BingX under P7 is not merged into the Bybit one by
  this decision. None exists: only the default Bybit database is
  configured.
- Every query that lists strategies for a user must state its exchange. A
  query without one sees both exchanges. That is right only for
  maintenance and for whole-database counts.
- **Stage order:** the readers that are still home-exchange or
  whole-database have no exchange filter yet:
  - the selection candidates;
  - the optimizer finalists before the registry seed;
  - the XLSX export;
  - the finalist retest without a registry.
  Stage 5e-2 adds the filter, so the BingX tick test (5e-3), which first
  produces BingX results in the product, ships only after 5e-2.
- The `unified_performance_v2.exchange` setting stays the default exchange
  of the evaluation target (its templates). A BingX tick test chooses the
  BingX profile itself (stage 5e-3).
