# ADR-0033: Portfolio Optimizer v2 adapter and liquidity-cap contract

**Date:** 2026-09-09

**Status:** `PLAN_APPROVED`; fixture/research implementation input only.

**Related:** [main optimizer specification](../specs/2026-09-05-portfolio-optimizer.md),
[Panel specification](../specs/2026-09-06-portfolio-optimizer-panel-ui.md),
[ADR-0030](0030-portfolio-optimizer-m2-admission-and-sizing-contract.md),
[ADR-0031](0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md),
[liquidity-capacity research](../superpowers/plans/2026-09-09-portfolio-optimizer-liquidity-capacity-research.md).

## Context

The D6 contract left the exact individual PnL/DD gates, profile ranking,
liquidity participation settings, and active sizing shape open. The prior
configuration model also exposed a monetary sizing grid even though the MVP
now has one liquidity-capped full position. Panel must migrate existing v1
documents without silently dropping unrelated accepted settings, while its CAS
must continue to protect the full document.

This ADR supersedes ADR-0030 only for the changed rules below. ADR-0030 is
preserved unchanged and remains authoritative for all other admission,
leverage, evidence, and risk rules.

## Decisions

1. Configuration schema v2 is strict. `search.sizing_mode` is fixed to
   `liquidity_cap_single`, `search.max_enumerated_combinations` defaults to
   `100000`, and scenarios contain one sizing upper bound without a monetary
   grid. A v1 load performs a deterministic in-memory migration: it removes
   `sizing.grid`, inserts the v2 fields, sets profile defaults and ranking
   descriptors, derives `inputs.bybit_minute_data_root` from the tester root,
   and preserves all other accepted values. Panel Save writes v2. Unknown v2
   keys fail closed.

2. Profiles use configurable `individual_max_dd_pct` with migration defaults
   AGGRESSIVE 30, BALANCED 20, and CONSERVATIVE 15, and
   `individual_net_pnl_min_exclusive` with default 0. The direct report
   `max_dd_pct` must be at or below the individual cap. Net PnL must be
   strictly above the profile floor. Ranking metrics are fixed as
   AGGRESSIVE `net_pnl DESC`, `recovery_factor DESC`, `max_dd_pct ASC`;
   BALANCED `recovery_factor DESC`, `net_pnl DESC`, `max_dd_pct ASC`;
   CONSERVATIVE `recovery_factor DESC`, `max_dd_pct ASC`, `net_pnl DESC`.
   These are preliminary profile ordering policies, not portfolio PnL.

3. Liquidity settings are explicit: participation integer 1..200 (default
   30), round-down USDT 50, market-reference maximum age 2 hours, weekend
   start/end defaults Saturday 00:00 UTC through Monday 00:00 UTC (each
   configurable as a valid weekday and HH:MM UTC pair with a non-empty weekly
   interval), archive publication lag integer 0..48 (default 6), and
   `backfill_write_enabled = false`. The Bybit
   minute root is a server-side local input. Disabled backfill reports gaps and
   remains read-only; when explicitly enabled, the adapter may atomically
   publish a downloaded complete daily CSV. Tests use an injected fetcher and
   temporary output root.

4. The liquidity cap covers the full accumulated directional position,
   including every opening, averaging, and DCA increase before the position
   returns flat. The maximum closing Limit quantity is recorded and must also
   fit the capacity evidence. The MVP has exactly one member size per profile:
   the calculated rounded-down full-position cap. It emits no 50/75/100 size
   variants and no partial-close recommendation. Multi-size calibration stays
   deferred Phase 8.

5. A future multi-pair `PortfolioCandidate` has a deterministic identity from
   sorted canonical pair/direction member identities plus profile and scenario.
   A Campaign freezes one validated market-reference snapshot and digest; all
   calculations in that Campaign use it. A changed snapshot starts a new
   decision Campaign.

6. Panel remains a thin facade for HTTP validation, full-document CAS,
   immutable Campaign capture, job lifecycle, progress, and artifact
   delivery. The portfolio package owns migration, identity, sizing,
   liquidity, gates, ranking, and calculations. Panel does not duplicate
   algorithm logic or write PerformanceDB.

7. The selected pair list is the candidate universe. Candidate composition
   chooses an explicit empty option or one admissible non-empty directional
   option for each selected symbol, then excludes the all-empty composition.
   Therefore each candidate contains a non-empty subset of the selected
   symbols, from one pair through all selected pairs; symbols without usable
   finalist options are simply absent from candidates. The stable failure
   `INSUFFICIENT_DIRECTIONAL_UNIVERSE` is emitted only when no usable option
   exists anywhere in the selected universe. If symbol `s` has `k_s` non-empty
   options, `total_combinations = product(1 + k_s) - 1`; when this count is at
   or below `max_enumerated_combinations`, every composition is returned for
   pre-test processing. The campaign profile's `max_candidates` is retained as
   a future post-joint-test selection limit and never truncates this universe.
   Enumeration is deterministic: sorted symbols, empty choice first, sorted
   non-empty options, product order, and no all-empty result.

## Consequences and non-goals

The v1 local example remains readable through migration, and a Panel save is
the explicit persistence point for v2. Existing portfolio-level research
risk values remain actual joint DD 20/10/5, free reserve 20/40/60, and MM load
50/35/20; the new 30/20/15 values are individual profile caps and must not be
substituted for those values.

This ADR does not authorize real exchange REST/WS, tester execution, trading,
recommendation readiness, or live deployment. Network access remains outside
this verification slice; enabled backfill is exercised only through the
injected fetcher contract. Multi-size calibration remains deferred.
