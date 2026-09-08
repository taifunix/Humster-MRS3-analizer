# Portfolio Optimizer chart reference notes

**Reference inspected:** local tester report
`my_test_run_001_of_001_system_3_8ca7713266a74d35a4bbb0ff4be553e8.html`.
This file is design input only. The [Phase 2B specification](../../specs/2026-09-07-portfolio-optimizer-phase2b-live-account-monitor.md),
[ADR-0032](../../decisions/0032-portfolio-live-monitor-storage-and-reconcile.md),
and [Phase 2A-2B plan](2026-09-07-portfolio-optimizer-phase2a-2b.md) are the
normative sources.

## Useful patterns

- compact candidate summary before the charts;
- equity and wallet context on one elapsed time axis;
- separate exposure/load panel;
- visibility controls, fit-to-range, and exact focus values;
- candidate selection with collapsed detail sections;
- period summaries for long runs.

## Problems to avoid

- loading a multi-megabyte report and every strategy array before selection;
- embedding a chart library or generating repeated JavaScript per strategy;
- dense trade labels obscuring the price series;
- mixing currency and percentage axes without a clear boundary;
- one DOM marker per source point;
- manufacturing timestamps for gaps in the browser.

## Shared route grammar

The only chart route is the bounded, read-only contract used by the other three
documents:

```text
GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PORTFOLIO
GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PAIR&symbol=<symbol>&side=<LONG|SHORT>[&contour=NONE|PORTFOLIO]
```

`PORTFOLIO` is the authoritative aggregate. `PAIR` accepts only a server-known
pinned `(symbol, side)` and may show a compatible portfolio contour. Unknown or
duplicate query fields, unknown pairs, unsupported methods, incompatible
metadata, gaps, and unavailable facts are explicit fail-closed states. See the
normative documents for exact numeric, accounting, source, and resource rules.
The first failing route stage wins: non-GET `405`; invalid path syntax `400`;
invalid query grammar `400`; unknown campaign/candidate `404`; unknown/unpinned
pair `404`; multiple live bindings `409 INCONSISTENT`; incompatible metadata
`409`; resource limit `413 RESOURCE_BOUND_EXCEEDED`; otherwise `200`, including
data-level unknown/gap with availability and provenance. Later stages are not
evaluated and every stage performs zero writes.
The server resolves zero or one active live deployment binding from the pinned
manifest; clients never select an account/deployment. Zero binding leaves TESTED
available and marks LIVE `UNKNOWN/NO_LIVE_BINDING`; multiple bindings are an
`INCONSISTENT` `409` response.

## Display direction

Keep four authoritative panels: net/cumulative PnL, equity, cashflow-adjusted
drawdown, and IM/MM load. Never merge incompatible units, currencies, time
bases, applicability, or TESTED/LIVE sources. Drawdown is a signed,
nonpositive path defined as `drawdown_path(t)=adjusted_equity(t)-high_water(t)`;
the percentage path is `drawdown_path(t)/high_water(t)*100` and is `UNKNOWN`
when adjusted equity is unknown or `high_water(t)<=0`. If any requested pct
point is unknown, `max_drawdown_pct` and any containing bucket's pct min/mean
are `UNKNOWN`; no known subset is used. Absolute DD remains independently
available when all absolute points are known. Known summary `max_drawdown` and `max_drawdown_pct` are
positive magnitudes `-min(path)` derived from exact path minima. Cashflows
cannot themselves create or erase the path or either magnitude. A compact
summary may show the tested interval, final PnL/equity, maximum DD, and peak
IM/MM only when facts are supplied; otherwise show `UNKNOWN`.
Here `adjusted_equity(t)=equity(t)-sum(signed_cashflow over (t_start,t])`, where
`t_start` is the first selected known equity point. Cashflow exactly at
`t_start` belongs to the baseline and is excluded; at later points a
same-timestamp cashflow precedes the equity sample under canonical source order. High water is seeded
only from the first known adjusted point, even when negative. Cashflow and equity
currencies must match; no FX conversion is allowed.

The server derives exact points before bounded `elapsed_bucket_v1` aggregation.
It uses the fixed seconds ladder
`(1,5,15,30,60,300,900,1800,3600,14400,21600,43200,86400,604800,
2592000,7776000,31536000)`, the smallest width producing at most 256 elapsed
buckets, half-open buckets aligned to series start, and at most 16
deterministic coarsening steps after provenance splits. An unsatisfied bound is
`RESOURCE_BOUND_EXCEEDED`, never truncation.
The response preserves first/last/min/max/mean/count, exact extrema UTC/source
IDs, and gap/provenance segments. For drawdown, the bucket `min` is the most
adverse signed path value; summary magnitudes come from exact pre-bucket points.
The later Panel slice renders mean and min/max envelope
paths plus one movable focus marker per panel using dependency-free native SVG.
It should not create one element per source point, use canvas or a chart library,
interpolate a gap, or invent a value. Focus shows source UTC, exact decimal
value, unit, source, availability, and provenance.

Load only the selected campaign/candidate. Clear the previous identity before a
new request and expose loading, empty, incompatible, stale, and `UNKNOWN`
states. Keep TESTED and LIVE independently hideable. Defer candlesticks,
trade-marker clouds, monthly heatmaps, and raw settings tables until a separate
 bounded, provenance-bearing contract is accepted.

For live entry-order projection, `observation_age` alone drives CURRENT/STALE;
`confirmation_age` is display metadata. Exactly `20.000s` is current and
`20.001s` is stale. This document does not place Panel code in the current
server/store implementation slice.
The current slice exposes only a pure `live_charts.py` request handler for direct
tests. It does not register the route in an existing served application; that
wiring belongs to the later Panel/application slice.
