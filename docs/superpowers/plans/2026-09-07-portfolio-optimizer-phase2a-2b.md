# Portfolio Optimizer Phase 2A-2B - fixture/fake implementation plan

**Date:** 2026-09-08
**Version:** Revision 6; P2AB-01..27 retained, P2AB-28..30 added.
**Status:** `PLAN_APPROVED`; approved by independent Advisor on Revision 6.

**Specifications:** [Phase 2A](../../specs/2026-09-07-portfolio-optimizer-phase2a-execution-research.md),
[Phase 2B](../../specs/2026-09-07-portfolio-optimizer-phase2b-live-account-monitor.md).
**Decision:** [ADR-0032](../../decisions/0032-portfolio-live-monitor-storage-and-reconcile.md).
**Chart notes:** [reference notes](2026-09-08-portfolio-optimizer-chart-reference-notes.md).

## 1. Entry state, scope, and prohibitions

M8 remains committed as `272f895` after its review and U1 remains committed as
`7ddbb94` after its review. Phase 2A is accepted at the fixture boundary after
Opus `CODE_REVIEW_PASS`; the Phase 2B server core is accepted at the fixture
boundary after its own final Opus `CODE_REVIEW_PASS`. Panel work remains
provisional until its separate verification and review.

This plan authorizes fixture/fake, read-only implementation work only. It does
not authorize real REST/WS, credentials, secrets, notifications, tester runs,
trading, order or resize mutation, emergency close, bot/config mutation, live
deployment, `READY`, `RECOMMENDATION_READY`, admission, or writes to
Performance DB or Portfolio DB. Production adapters remain deferred.

Phase 2A stays an independent accepted slice. This plan implements only the
Phase 2B server/store/read-model slice. Panel rendering is a forward contract
for a later separately approved diff and review. No fixture result is evidence
of production connector safety or a live trading strategy.

All persisted and API numeric values use `Decimal` or canonical decimal
strings. Every derived calculation uses decimal128 precision 34 and
`ROUND_HALF_EVEN`; Python `float` is forbidden. Canonical serialization,
source ordering, identity, and ambiguous-input handling are deterministic and
fail closed.

## 2. P2AB-01..11 approved baseline (retained)

These approved Rev2 slices remain in scope without widening Phase 2A:

1. **P2AB-01 deterministic evidence:** schema, nullable requested quantity,
   stable identity, duplicate/conflict/unknown events, recursive float
   rejection, and byte/digest-identical repeated builds.
2. **P2AB-02 lifecycle:** placement, acknowledgement, partial/full fill,
   cancel/replace, reduce-only, rejected events, latency, remaining quantity,
   revision, and reserve through `CANCEL_CONFIRMED`.
3. **P2AB-03 strata and curves:** symbol-side-timeframe strata, versioned
   regime only when present, sample/coverage/units/digest/version/applicability,
   and fail-closed small or incompatible samples.
4. **P2AB-04 liquidity and margin compatibility:** existing M2/M3 Decimal
   entry points, order loss, haircut, borrow, close fee, reserve, and stable
   canonical outputs/digests.
5. **P2AB-05 non-consumption:** research, calibration, confidence,
   applicability, and stress data never alter approved sizing, capacity,
   reserve, margin, admission, or readiness bytes.
6. **P2AB-06 stress/retest:** correlated shock and deepest fill use existing
   gates; sizing, capacity, reserve, or margin changes create immutable
   `NEEDS_RETEST` lineage.
7. **P2AB-07 persistence:** replay is idempotent, conflicting replacement is
   rejected, corrections carry provenance, and history is never updated.
8. **P2AB-08 store and manifest:** separate stdlib SQLite with WAL and foreign
   keys, append-only facts, path guard, secret-free manifest, and no Portfolio
   or Performance DB target.
9. **P2AB-09 reconcile:** complete REST plus buffered contiguous private WS,
   atomic checkpoints, reconnect and periodic reconcile, with explicit
   `UNKNOWN`/`INCONSISTENT` outcomes.
10. **P2AB-10 attribution and watchdog:** stable mappings only, no heuristic
    allocation, limiter/grace/L=0/priority-zero, drift, freshness, liquidity,
    and diagnostic recommendations.
11. **P2AB-11 chart boundary:** pinned tested baseline through read-only
    PortfolioStore, separate LiveStore, decimal-string chart model, tested-only
    operation, compatible overlay, no thresholds, no interpolation, and no
    Panel integration in this slice.

## 3. P2AB-12..30 Revision 6 contract

12. **Canonical numeric and source contract.** A canonical decimal string is a
    finite decimal with no exponent, no leading zero except `0`, no trailing
    fractional zero, and `-0` serialized as `0`; all arithmetic runs in
    decimal128 precision 34 with `ROUND_HALF_EVEN`. Facts are ordered by
    `(effective_at_utc, observed_at_utc, source_kind, source_id,
    source_sequence, canonical_payload_digest)` in lexicographic order.
    Missing or incomparable ordering fields, equal
    identity with different payloads, or ambiguous mapping makes the dependent
    result `UNKNOWN` or `INCONSISTENT`; arrival order is never used.
13. **Portfolio and pair attribution.** `PORTFOLIO` means the authoritative
    account/candidate aggregate. `PAIR` means one server-known pinned
    `(symbol, side)` member. Shared symbols require `orderLinkId` or an
    immutable manifest mapping; symbol-only or heuristic allocation is invalid.
    There is no heuristic pair equity or account-DD attribution. No pair
    receives an aggregate PnL, DD, or margin value without pair facts.
14. **Cashflow-adjusted account metrics.** Trading PnL is the change in the
    selected equity series less signed deposits, withdrawals, and internal
    transfers over the same interval. Cashflow events are retained in an audit
    stream with identity, direction, amount, currency, and classification.
    Let `t_start` be the first selected known equity sample. Define
    `adjusted_equity(t)=equity(t)-sum(signed_cashflow over (t_start,t])`.
    A cashflow exactly at `t_start` belongs to the baseline and is excluded;
    at every later equity timestamp, same-timestamp cashflow is ordered by the
    canonical six-field source order and applied before that sample is used.
    `high_water(t)=max(adjusted_equity(s) for s<=t)`, seeded only by the first
    known adjusted point, including a negative point; it is `UNKNOWN` before
    that point and is never seeded with zero. Cashflow currency must equal the
    selected equity-series currency; Phase 2B performs no FX conversion.
    Missing, gapped, conflicting, differently denominated, or unknown cashflow classification makes
    dependent PnL and DD `UNKNOWN` until a complete reconcile proves it. Build
    the canonical signed paths from the adjusted equity:
    `drawdown_path(t)=adjusted_equity(t)-high_water(t)` (always `<=0`) and
    `drawdown_pct_path(t)=drawdown_path(t)/high_water(t)*100`; it is `UNKNOWN`
    exactly when `high_water(t)<=0` or adjusted equity is `UNKNOWN`.
    `max_drawdown_pct` is `UNKNOWN` for the requested interval if any percentage
    point is unknown; no known subset is used. Absolute `max_drawdown` remains
    independently defined when every absolute path point is known.
    Summary/admission known magnitudes are positive:
    `max_drawdown=-min(drawdown_path)` and, only when every percentage point is
    known, `max_drawdown_pct=-min(drawdown_pct_path)`. Cashflows cannot themselves
    create or erase either path or magnitude; unknown cashflows make both
    UNKNOWN. Fees, funding, realized PnL, and sampled unrealized PnL are not
    added to an equity-derived PnL twice; a component view requires a disjoint
    source partition, otherwise it is `UNKNOWN`.
15. **Event-driven margin.** Recompute IM/MM load when either numerator or
    denominator changes. At each event use the latest compatible, fresh fact at
    or before that event. Retain absolute IM, absolute MM, denominator, unit,
    currency, source ID, and freshness. Missing, stale, nonpositive, or
    incompatible numerator/denominator yields `UNKNOWN`; no interpolation or
    silent zero is allowed.
16. **Bounded chart aggregation.** Derive exact points first, then apply
    bounded server-side `elapsed_bucket_v1` aggregation. Its fixed seconds
    ladder is `(1,5,15,30,60,300,900,1800,3600,14400,21600,43200,86400,
    604800,2592000,7776000,31536000)`. Choose the smallest width for which
    `ceil(elapsed_span/width)<=256`; buckets are half-open `[start,end)` and
    aligned to the exact series start on the elapsed axis, with the final
    boundary inclusive of the final point. Each bucket retains
    `first`, `last`, `min`, `max`, `mean`, `count`, exact UTC timestamps and
    source IDs for extrema, and segment gap/provenance. For each drawdown path,
    `min` is the most adverse signed path value with its exact UTC/source ID;
    summary magnitudes come from exact pre-bucket points, never bucket means. A
    bucket containing any unknown percentage point reports percentage `min` and
    `mean` as `UNKNOWN` with an availability marker, never a partial extremum.
    Source, unit, currency,
    time basis, applicability, availability, or TESTED/LIVE changes split a
    bucket. Apply these splits after width selection. If a trace then exceeds
    256 records, move deterministically to the next coarser ladder width, at
    most 16 escalations. If no ladder width satisfies the bound, fail closed
    with `RESOURCE_BOUND_EXCEEDED`; gaps and adverse spikes survive and raw
    arrays are not returned.
17. **Read-only chart route.** The sole route/query grammar is:

    ```text
    GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PORTFOLIO
    GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PAIR&symbol=<symbol>&side=<LONG|SHORT>[&contour=NONE|PORTFOLIO]
    ```

    `campaign_id` and `candidate_id` are opaque URI-safe path values of 1-128
    characters. Query parameters occur once; unknown, duplicate, empty,
    path-like, or out-of-scope values fail closed. `symbol`, `side`, and
    `contour` are forbidden in `PORTFOLIO`; `symbol` and `side` are required in
    `PAIR`. Pair `contour` defaults to `NONE`; `PORTFOLIO` contour is returned
    only when compatible. The pair must be server-known in the candidate's pinned
    composition. Apply this ordered validation pipeline and stop at its first
    failure: (1) non-GET => `405` before service access; (2) invalid path-ID
    syntax => `400`; (3) query grammar, including missing/unknown/duplicate/
    empty/path-like/out-of-scope parameters and mode-conditional fields =>
    `400`; (4) unknown campaign or unknown/unpinned candidate => `404`; (5)
    unknown/unpinned pair => `404`; (6) multiple active live bindings => `409`
    `INCONSISTENT`; (7) incompatible contour/metadata => `409`; (8) resource
    breach => `413` with machine-readable
    `reason=RESOURCE_BOUND_EXCEEDED`; (9) otherwise `200`, including data-level
    `UNKNOWN`/gapped series with explicit in-body availability and provenance.
    No later stage is evaluated after failure; every stage performs zero writes.

    A candidate has at most one active live deployment binding, resolved
    server-side from the immutable manifest; it is never selected by a client
    query. With zero binding, TESTED traces remain available and LIVE traces are
    absent with an explicit `UNKNOWN/NO_LIVE_BINDING` marker. More than one
    active binding is `INCONSISTENT` and returns `409` without choosing one.
18. **Resource and rendering bounds.** Per request, return at most 256 output
    records per trace, 32 traces, and 128 pair options; inspect at most
    1,000,000 raw source facts and emit at most 8 MiB encoded response. Exceeding
    any bound fails closed; the client cannot request truncation or resolution
    changes to bypass a bound. The Panel slice uses dependency-free native SVG
    mean and min/max envelope paths plus one focus marker per panel; it creates
    no per-point DOM nodes, canvas, chart library, client thresholds, or client
    baseline authority.
19. **Live source and store contract.** Startup and periodic reconcile require
    a complete REST snapshot. Completeness means every page was read to an
    explicit terminal cursor with contiguous cursor linkage and no page
    overlap/gap; the checkpoint records snapshot start/end observed times,
    terminal cursor, record count, and canonical digest. A versioned manifest
    pins `snapshot_read_deadline_seconds`; the P2AB-20 open-order snapshot is
    additionally bounded by 5 seconds. Cursor discontinuity, truncation,
    missing terminal marker, overlap/gap, or deadline overrun is `PARTIAL`; it
    is not appended as authoritative and dependent state is `UNKNOWN`.
    Private wallet, position, execution, and order WS
    facts are accepted only through contiguous checkpoints and are appended to
    LiveStore. Wallet WS is neither the initial snapshot nor continuous
    unrealized-PnL evidence. Production adapters, secrets vault, and live
    deployment remain deferred. Every reconcile, gap, cashflow, and provenance
    disposition is append-only.
20. **Entry-order resize projection.** For a pair outside a position, fixture
    evidence must observe bot-driven entry-order resize within a worst-case
    20-second window. Primary evidence is private order WS; one account-wide
    complete REST open-orders snapshot runs on an interval `<=10s`,
    with completion, validation, and append within 5 seconds. The chart GET
    is served within 5 seconds. Triggers are coalesced to one in-flight
    reconcile and use fake clocks. `observation_age` is measured from the last
    successful WS-contiguous or REST-reconciled observation covering the pair;
    only it drives freshness. `confirmation_age` is measured from the last
    entry-order confirmation and is display metadata only. Observation age
    `<=20.000s` is current; age `>20.000s` is `STALE`, and current quantity,
    notional, state, and order list are hidden. The `10+5+5` worst-case budget
    is measured against observation freshness, so exactly 20 seconds remains
    current.
    Entry role is explicit and never inferred. The projection contains position,
    active entry quantity/notional, observation and confirmation times/ages, state
    `IN_POSITION|PENDING_ENTRY|NO_POSITION`, `WS/REST_RECONCILED`, and
    deterministic constituent order IDs, revisions, and digest. Multiple orders
    aggregate with exact Decimal arithmetic. Missing price/quantity is
    `UNKNOWN`; cancel-replace/revision gaps or conflicts fail closed, while a
    complete REST snapshot can recover the projection. A wallet change
    recomputes margin immediately but does not confirm resize. The monitor never
    mutates orders or configuration.

21. **Pinned live binding.** Test zero, exactly one, and multiple active
    server-side deployment bindings; clients cannot select deployment/account.
22. **Equity boundary.** Test `(t_start,t]`, same-timestamp source ordering,
    first-known high-water seeding, negative initial equity, and mixed currency.
23. **Bucket determinism.** Test ladder selection, half-open alignment,
    provenance-split escalation, and terminal `RESOURCE_BOUND_EXCEEDED`.
24. **Freshness semantics.** Test idle-but-fresh pairs, exactly `20.000s`, and
    `20.001s`; confirmation age never drives hiding.
25. **HTTP contract.** Test every status/body rule above and zero writes for all
    rejection paths.
26. **REST completeness.** Test terminal pagination, truncation, cursor
    discontinuity, overlap/gap, recorded boundaries/count/digest, and deadline
    overrun.
27. **Server/Panel boundary.** This slice changes server/store/read-model code
    only. SVG rules constrain a later Panel slice. Existing Panel tests are
    regression gates and must pass unchanged.
28. **Partial percentage-DD availability.** Unknown adjusted equity or
    nonpositive high water makes that percentage point unknown; any unknown pct
    point makes interval `max_drawdown_pct` and bucket pct min/mean unknown.
    Absolute DD remains independently available when its entire path is known.
29. **HTTP precedence.** The ordered nine-stage pipeline above determines the
    first response; tests include unknown campaign/candidate and two multi-fault
    precedence cases.
30. **Pure route seam.** `live_charts.py` exposes a self-contained pure request
    handler accepting method, path, query mapping, and headers and returning
    status plus canonical body. `tests/test_portfolio_live_charts.py` calls it
    directly. No existing served app receives route registration in this slice;
    framework/application wiring is deferred to a separate approved diff.

## 4. TDD slices and evidence

The existing P2AB-01..11 tests remain the first failing-test sequence. Add
focused fixture tests for each P2AB-12..30 rule before implementation:

- canonical decimal strings, precision/rounding, source-order ties and
  ambiguous mappings;
- portfolio/pair shared-symbol `orderLinkId` mapping and fail-closed absence;
- deposit/withdrawal/transfer audit, cashflow gaps, fee/funding partition,
  signed nonpositive drawdown paths, positive summary magnitudes, and
  cashflow non-creation/erasure;
- TDD assertion: `assert all(p <= 0 for p in drawdown_path)` and
  `assert max_drawdown == -min(drawdown_path)` (and the equivalent percentage
  assertion only when every pct point is known; otherwise assert the pct summary
  is `UNKNOWN`);
- event-driven IM/MM numerator/denominator updates, freshness, and unknowns;
- exact pre-bucket points, `elapsed_bucket_v1`, extrema source IDs, gap
  segments, adverse spikes, native SVG path/focus bounds;
- strict `/charts` query, server-known pair, GET-only ordering, zero mutation,
  and all resource ceilings;
- startup/periodic REST, contiguous wallet/position/execution/order WS,
  append-only LiveStore, and wallet-WS limitations;
- P2AB-20 fake-clock resize timing, WS-first path, REST recovery, coalescing,
  stale hiding, Decimal multi-order aggregation, revision conflicts, and
  no-mutation assertions.
- P2AB-21..27 binding cardinality, equity/currency boundaries, bucket ladder
  escalation, split ages, HTTP codes/body, paginated REST completeness, and
  unchanged Panel regression files.
- P2AB-28..30 partial pct-DD availability, first-failure HTTP precedence, and
  direct pure-handler tests with no served-app registration.

Phase 2A remains verified with its existing focused suite. Phase 2B tests are
run only after this plan is approved. Panel tests are regression-only and their
files remain unchanged. Every test uses fixtures/fakes;
no real endpoint, secret, order, or tester run is permitted.

## 5. Verification and acceptance gates

```powershell
.venv\Scripts\python.exe -m pytest tests/test_portfolio_execution_research.py tests/test_portfolio_liquidity.py tests/test_portfolio_margin.py tests/test_portfolio_store.py -q
.venv\Scripts\python.exe -m pytest tests/test_portfolio_live_store.py tests/test_portfolio_live_reconcile.py tests/test_portfolio_live_monitor.py tests/test_portfolio_live_charts.py -q
.venv\Scripts\python.exe -m pytest tests/test_panel_portfolio.py tests/test_panel_static_ui.py -q
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m compileall -q src/mrs3/portfolio src/mrs3/panel_portfolio.py
git diff --check
```

Acceptance requires focused and broad tests, byte compilation, import/dependency
scan, fixture secret/action scan, resource-bound assertions, and independent
review. No status advances without actual `CODE_REVIEW_PASS`. The revision
does not authorize implementation, tester/runtime, production adapters,
`RECOMMENDATION_READY`, admission, or live use. Any later Panel work remains a
separate approved slice with its own diff and review.

The Phase 2B implementation diff may touch only
`src/mrs3/portfolio/live_store.py`, `live_reconcile.py`, `live_monitor.py`,
`live_charts.py`, their named `tests/test_portfolio_live_*.py` tests and
`tests/fixtures/portfolio/live/`, plus this feature's spec/ADR/plan/evidence,
`PRD.md`, and `progress.md`. `src/mrs3/panel_portfolio.py`,
`tests/test_panel_portfolio.py`, and `tests/test_panel_static_ui.py` must have
zero diff and remain byte-identical in this slice. No route registration may be
added to any existing served application.

## 4. Phase 2B implementation status

- [x] **2B-1 LiveStore/manifest:** accepted after Opus `CODE_REVIEW_PASS`;
  [evidence](2026-09-08-portfolio-optimizer-phase2b-store-evidence.md).
- [x] **2B-2 reconcile:** complete paginated REST, buffered contiguous WS and
  atomic checkpoint recovery.
- [x] **2B-3 monitor/read models:** attribution, cashflow-adjusted metrics,
  margin timeline and watchdog.
- [x] **2B-4 order projection:** explicit ENTRY role and 20-second freshness.
- [x] **2B-5 exact charts:** deterministic bounded overview and pure handler;
  [evidence](2026-09-09-portfolio-optimizer-phase2b-server-evidence.md).
- [ ] **2B-9 Panel visualization:** separate implementation and review after
  accepted server evidence.
