# ADR-0032: storage, reconcile, and read-only charts for Phase 2B

**Date:** 2026-09-08
**Status:** `PLAN_APPROVED`; approved with plan Revision 6. This ADR does not
grant real API, tester, trading, or deployment
permission.

**Related:** [Phase 2B spec](../specs/2026-09-07-portfolio-optimizer-phase2b-live-account-monitor.md),
[Phase 2A-2B plan](../superpowers/plans/2026-09-07-portfolio-optimizer-phase2a-2b.md),
[main optimizer specification](../specs/2026-09-05-portfolio-optimizer.md) section 12,
[ADR-0025](0025-portfolio-optimizer-evidence-and-phases.md),
[ADR-0031](0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md).

## Context

Live history must survive restarts and exchange retention. Performance DB owns
research facts and Portfolio DB owns campaign/evaluation facts; mixing live
snapshots into either would blur source authority and lifecycle. Private WS can
duplicate, arrive late, or lose sequence. Account cashflows can otherwise be
mistaken for trading PnL, and a wallet message cannot prove continuous
unrealized PnL or an entry-order resize.

Phase 2B is fixture/fake-only and read-only. Production REST/WS adapters,
secrets, notifications, tester execution, order mutation, bot/config mutation,
and live deployment remain deferred.

## Decisions

1. Use a separate stdlib `sqlite3` LiveStore with WAL, foreign keys enabled on
   every connection, and append-only tables for secret-free manifests,
   account/position/order snapshots, executions, cashflows, reconciliations,
   stream checkpoints, and watchdog findings. Reject Performance DB and
   Portfolio DB canonical paths and aliases before opening a connection.

2. Store immutable manifest and fact identity. The manifest includes public
   account identity, deployment/manifest versions, mapping/config/reference
   digests, read-only permissions, and pinned tested baseline identity. It never
   stores keys, secrets, tokens, passwords, cookies, or secret paths. A
   secret-like field is `INVALID`; an exact duplicate payload is idempotent and
   a same-identity payload conflict is `INCONSISTENT`.

3. Use complete REST for startup, reconnect, and periodic reconcile. Buffer
   private wallet/position/execution/order WS while the snapshot is read, then
   append the snapshot and checkpoint atomically and apply only the contiguous
   buffered sequence. Gaps, incomplete/stale snapshots, ambiguous ordering,
   and irreconcilable state remain explicitly `UNKNOWN`/`INCONSISTENT`; they are
   never silently repaired. Wallet WS is not an initial snapshot and is not
   continuous unrealized-PnL evidence.
   Completeness requires every page through an explicit terminal cursor,
   contiguous cursor linkage, no overlap/gap, and a checkpoint containing
   snapshot start/end observed times, terminal cursor, record count, and
   canonical digest. The manifest pins `snapshot_read_deadline_seconds`; the
   P2AB-20 open-order snapshot also has the 5-second completion bound.
   Truncation, cursor discontinuity, missing terminal marker, overlap/gap, or
   deadline overrun is `PARTIAL`, is not authoritative, and leaves dependent
   state `UNKNOWN`.

4. Use decimal128 precision 34 and `ROUND_HALF_EVEN` for all Decimal
   arithmetic, with canonical non-exponent decimal strings at every persistence
   and API boundary. Deterministic source order is the lexicographic tuple
   `(effective_at_utc, observed_at_utc, source_kind, source_id, source_sequence,
   canonical_payload_digest)`. Missing or incomparable order
   fields fail closed; arrival order is not a tie-breaker.

5. Calculate account trading PnL from equity change less signed deposits,
   withdrawals, and internal transfers. Retain a cashflow audit stream. A
   cashflow gap or unknown classification makes dependent PnL and drawdown
   `UNKNOWN`. With `t_start` equal to the first selected known equity sample,
   `adjusted_equity(t)=equity(t)-sum(signed_cashflow over (t_start,t])`.
   Cashflow exactly at `t_start` belongs to the baseline and is excluded; at a
   later equity timestamp, same-timestamp cashflow precedes the sample under
   canonical source order. `high_water(t)=max(adjusted_equity(s) for s<=t)`, seeded only from the
   first known adjusted point, including a negative point, and never from zero.
   Cashflow and selected equity currencies must match; no FX conversion occurs.
   A mismatch makes dependent PnL/DD `UNKNOWN`. Build the signed paths:
   `drawdown_path(t)=adjusted_equity(t)-high_water(t)` (always `<=0`) and
   `drawdown_pct_path(t)=drawdown_path(t)/high_water(t)*100`. A percentage point
   is `UNKNOWN` exactly when adjusted equity is unknown or `high_water(t)<=0`.
   If any percentage point in the interval is unknown, `max_drawdown_pct` is
   `UNKNOWN`; no known subset is used. Absolute `max_drawdown` remains defined
   independently when every absolute point is known. A bucket with any unknown
   pct member reports pct min/mean as UNKNOWN with availability. Positive known
   summary/admission magnitudes are
   `max_drawdown=-min(drawdown_path)` and, only when every pct point is known,
   `max_drawdown_pct=-min(drawdown_pct_path)`. Cashflows cannot themselves
   create or erase either path or magnitude. Do not add realized PnL, fees,
   funding, or sampled unrealized PnL to an equity-derived total unless a
   disjoint source partition is proven.

6. Recompute IM/MM on numerator or denominator changes using the latest
   compatible fresh facts at or before each event. Retain absolute IM/MM and
   the denominator alongside the ratio. Missing, stale, nonpositive, or
   incompatible facts produce `UNKNOWN`; no interpolation or silent zero is
   permitted.

7. Keep tested baseline authoritative in read-only PortfolioStore and live
   facts in LiveStore. Build exact chart points first, then bounded
   `elapsed_bucket_v1` points. Use the fixed seconds ladder
   `(1,5,15,30,60,300,900,1800,3600,14400,21600,43200,86400,604800,
   2592000,7776000,31536000)`, selecting the smallest width with
   `ceil(elapsed_span/width)<=256`; align half-open `[start,end)` buckets to the
   series start and include the final point at the final boundary. Points retain first/last/min/max/mean/count, exact
   extrema UTC/source IDs, and segment gaps/provenance. Drawdown `min` retains
   the most adverse signed path value for absolute and percentage paths; summary
   magnitudes are derived from exact pre-bucket points, never bucket means.
   Apply metadata/provenance splits after width selection; if they exceed 256
   records, advance at most 16 times through coarser ladder widths, then fail
   with `RESOURCE_BOUND_EXCEEDED`. Never return raw arrays
   or merge incompatible source, unit, currency, time basis, applicability, or
   TESTED/LIVE segments.

8. Standardize the read-only endpoint and query grammar:

   ```text
   GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PORTFOLIO
   GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PAIR&symbol=<symbol>&side=<LONG|SHORT>[&contour=NONE|PORTFOLIO]
   ```

   Path IDs are opaque URI-safe 1-128 character values. Query parameters are
   single and strict. `PORTFOLIO` forbids symbol/side/contour; `PAIR` requires
   a server-known pinned symbol/side and may request `contour=PORTFOLIO`.
   Resolve at most one active live deployment for the candidate server-side
   from the immutable manifest. Zero bindings preserves TESTED and marks LIVE
   `UNKNOWN/NO_LIVE_BINDING`; multiple bindings are `INCONSISTENT` and return
   `409` without choosing one. Apply the ordered first-failure pipeline: (1)
   non-GET `405`; (2) invalid path-ID syntax `400`; (3) invalid query grammar
   `400`; (4) unknown campaign or unknown/unpinned candidate `404`; (5)
   unknown/unpinned pair `404`; (6) multiple bindings `409 INCONSISTENT`; (7)
   incompatible contour/metadata `409`; (8) resource breach `413` with
   `reason=RESOURCE_BOUND_EXCEEDED`; (9) otherwise `200`, including data-level
   UNKNOWN/gaps with availability/provenance. Stop after the first failure; no
   later stage is evaluated and every stage performs zero writes. Bound each request to 256 records per trace, 32 traces,
   128 pair options, 1,000,000 raw facts, and 8 MiB encoded output. Exceeding
   a bound fails closed without truncation or client override.

9. Keep `PORTFOLIO` aggregate metrics and `PAIR` member metrics separate.
   Shared-symbol strategies require `orderLinkId` or immutable mapping; no
   heuristic PnL/DD/margin allocation is permitted; there is no heuristic pair
   equity or account-DD attribution. Pair data that cannot be proven remains
   `UNKNOWN`.

10. For P2AB-20, observe explicit entry-order resize for an out-of-position
    pair from primary private order WS. Schedule one complete account-wide REST
    open-orders snapshot on an interval `<=10s`; complete validation and append
    within 5 seconds; serve the chart GET within 5 seconds; coalesce triggers
    to one in-flight run under fake clocks. `observation_age` comes from the
    latest successful WS-contiguous or REST-reconciled observation covering the
    pair and alone drives freshness; `confirmation_age` comes from the latest
    entry-order confirmation and is display-only. Observation age `<=20.000s`
    is current; older data is `STALE` and hides current
    quantity/notional/state/order list. The `10+5+5` budget is measured against
    observation freshness, so exactly 20 seconds is current. Aggregate
    multiple orders with exact Decimal values and retain order IDs, revisions,
    observation and confirmation times/ages, state, reconcile marker, and digest. Missing
    price/quantity, revision gaps, cancel-replace conflicts, and ambiguous entry
    roles fail closed. Complete REST may recover. Wallet change can recompute
    margin but cannot confirm resize. Monitor never mutates.

11. This Phase 2B implementation owns only server storage, reconciliation,
    monitoring, and chart read models. Native SVG rules are forward constraints
    for a later separately approved Panel diff. `panel_portfolio.py` and its
    Panel tests are byte-identical regression gates in this slice.
    `live_charts.py` exposes a pure request handler taking method, path, query
    mapping, and headers and returning status plus canonical body; its test calls
    it directly. No route registration is added to an existing served
    application; framework wiring is deferred.

## Consequences

The local history is auditable, append-only, and independent of research and
campaign lifecycles. Reconcile surfaces uncertainty instead of presenting a
healthy empty account. Cashflow adjustment prevents deposits and transfers from
changing reported trading performance. Bounded server aggregation and native
SVG keep the chart response and DOM finite while preserving spikes and
provenance.

The design requires local retention/backup and future schema migration work.
Fixture/fake evidence cannot establish production connector safety. Panel
integration, production sources, credentials, tester runs, trading actions,
recommendation readiness, admission, and live deployment remain separate gates.

## Verification

Acceptance requires focused fixture tests for path guard, WAL/FK/append-only
history, secret-free manifest, source ordering, duplicate/conflict/gap,
cashflow PnL/DD, signed drawdown path and positive summary assertion,
event-driven margin, attribution, bounded `/charts`, and
P2AB-20 resize timing and stale hiding. Run the `.venv` test commands in the
Phase 2B specification, including `all(p <= 0 for p in drawdown_path)` and
`max_drawdown == -min(drawdown_path)` assertions, and `git diff --check`.
Independent review must produce
`CODE_REVIEW_PASS` before the ADR or plan status advances.
