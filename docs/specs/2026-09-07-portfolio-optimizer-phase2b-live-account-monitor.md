# Portfolio Optimizer Phase 2B - read-only Live Account Monitor

**Date:** 2026-09-08
**Status:** `PLAN_APPROVED`; fixture/fake-only contract approved with Revision 6.
It is not permission for a real account, tester,
trading, or live deployment.

**Related:** [main specification](2026-09-05-portfolio-optimizer.md) section 12,
[ADR-0025](../decisions/0025-portfolio-optimizer-evidence-and-phases.md),
[ADR-0031](../decisions/0031-portfolio-optimizer-panel-ui-and-campaign-boundary.md),
[ADR-0032](../decisions/0032-portfolio-live-monitor-storage-and-reconcile.md),
[Phase 2A-2B plan](../superpowers/plans/2026-09-07-portfolio-optimizer-phase2a-2b.md),
[chart notes](../superpowers/plans/2026-09-08-portfolio-optimizer-chart-reference-notes.md).

## 1. Scope and safety boundary

The monitor consumes a complete REST snapshot and contiguous private wallet,
position, execution, and order WS facts, reconciles them, and exposes an
append-only local history plus read models. It is read-only and diagnostic.
Fixture/fake inputs are mandatory for this phase. Real REST/WS, credentials,
secrets, notifications, exchange writes, orders, resize, emergency close,
bot/config mutation, tester runs, `READY`, `RECOMMENDATION_READY`, admission,
and live deployment are prohibited. Production adapters are deferred.

All persisted, calculated, and API numeric values use `Decimal` or canonical
decimal strings. The arithmetic context is decimal128 precision 34 with
`ROUND_HALF_EVEN`; float input, float coercion, and exponent-form output are
rejected. Canonical decimal strings have no exponent, no leading zero except
`0`, no trailing fractional zero, and serialize `-0` as `0`.

Facts use lexicographic source order:
`(effective_at_utc, observed_at_utc, source_kind, source_id, source_sequence,
canonical_payload_digest)`. Missing/incomparable ordering,
conflicting same-identity payloads, and ambiguous mappings fail closed as
`UNKNOWN` or `INCONSISTENT`; arrival order is not evidence.

## 2. Inputs, outputs, and identity

Inputs are a secret-free immutable deployment manifest, fixture complete REST
wallet/positions/orders/executions, private WS streams, frozen strategy
identities and mappings, versioned watchdog/margin/freshness/liquidity
settings, and a pinned tested baseline. The baseline pins
`portfolio_set`, `evaluation`, `run_id`, `attempt_id`, semantic digest,
series/metrics versions, and exact member composition.

The output is append-only account, position, order, execution, cashflow,
reconcile, checkpoint, and finding history plus read models for balances,
equity, cashflow-adjusted PnL/DD, realized and sampled unrealized components,
IM/MM, exposure, positions/orders/fills, limiter state, freshness, drift,
liquidity recommendations, and chart data. Recommendations never change
quantity or config.

The manifest contains schema/version IDs, public account identity, stable
strategy IDs, selected symbols, `orderLinkId` mapping digest, config/reference
digests, declared read-only permissions, and provenance. It contains no API
keys, secrets, tokens, passwords, cookies, or local secret paths. Secret-like
fields make the manifest `INVALID`. Every fact carries deployment and manifest
identity, source kind, observed time, schema version, and canonical digest.

Attribution requires an immutable mapping. In `PORTFOLIO` mode, metrics are
the authoritative account/candidate aggregate. In `PAIR` mode, metrics are
for one server-known pinned `(symbol, side)` member. When strategies share a
symbol, `orderLinkId` or an immutable manifest mapping is required; symbol-only
and heuristic allocation are forbidden. There is no heuristic pair equity or
account-DD attribution. Missing or ambiguous pair facts remain
`UNKNOWN`/`UNATTRIBUTED`.

## 3. LiveStore and reconciliation

Use a separate stdlib `sqlite3` LiveStore with WAL, foreign keys enabled on
every connection, and append-only tables for manifests, account/position/order
snapshots, execution events, cashflow events, reconciliations, checkpoints,
and findings. Performance DB and Portfolio DB canonical paths and aliases are
rejected before opening a connection. LiveStore never copies or becomes the
authority for the tested baseline.

Startup and reconnect first obtain a complete REST snapshot, buffer WS by
channel and source sequence while it is read, then atomically append the
snapshot/checkpoint and apply only the contiguous buffered sequence. Periodic
reconcile performs the same completeness and identity checks and appends a new
reconcile record. Exact duplicate payloads are idempotent. A gap,
out-of-order range that cannot be proven contiguous, conflicting duplicate,
stale/partial REST, or irreconcilable identity yields explicit
`UNKNOWN`/`INCONSISTENT`; it is never silently reconstructed. A missing
position does not prove that a strategy is absent, and forced close requires
unambiguous execution/order evidence.

A complete REST snapshot reads every page to an explicit terminal cursor with
contiguous cursor linkage and no page overlap or gap. Its checkpoint records
snapshot start/end observed times, terminal cursor, record count, and canonical
digest. The immutable manifest pins a versioned
`snapshot_read_deadline_seconds`; the P2AB-20 open-order snapshot must also
finish within 5 seconds. Truncation, cursor discontinuity, a missing terminal
marker, overlap/gap, or deadline overrun is `PARTIAL`; such a snapshot is not
appended as authoritative and dependent state remains `UNKNOWN`.

Private wallet, position, execution, and order WS are all appendable sources,
but wallet WS is neither an initial snapshot nor continuous unrealized-PnL
evidence. Unrealized PnL is only a sampled point when a compatible position and
mark fact exists. Cashflow events remain in an audit stream with identity,
direction, signed amount, currency, classification, and source provenance.

## 4. Cashflow-adjusted PnL and drawdown

For a selected equity series, signed cashflow is positive for deposits and
account inflows and negative for withdrawals and account outflows. Trading PnL
over a period is:

```text
trading_pnl = equity_end - equity_start - sum(signed_cashflow)
```

Let `t_start` be the first selected known equity sample. At every point:

```text
adjusted_equity(t) = equity(t) - sum(signed_cashflow over (t_start, t])
high_water(t) = max(adjusted_equity(s) for s <= t)
```

A cashflow exactly at `t_start` belongs to the baseline and is excluded. At each
later equity timestamp, same-timestamp cashflow is ordered by the canonical
six-field source order and applied before that sample is used. High water is
seeded only by the first known adjusted point, including when it is negative;
it is `UNKNOWN` before that point and is never seeded with zero. Each cashflow
currency must equal the selected equity-series currency. Phase 2B performs no
FX conversion; a mismatch makes the dependent PnL, paths, and summaries
`UNKNOWN`. The canonical signed paths are:

```text
drawdown_path(t) = adjusted_equity(t) - high_water(t)                 # <= 0
drawdown_pct_path(t) = drawdown_path(t) / high_water(t) * 100
max_drawdown = -min(drawdown_path)
max_drawdown_pct = -min(drawdown_pct_path)
```

`max_drawdown` and `max_drawdown_pct` are positive summary magnitudes for
summary/admission consumers; the chart paths remain signed and nonpositive.
The percentage point is `UNKNOWN` exactly when adjusted equity is `UNKNOWN` or
`high_water(t)<=0`. If any percentage point in the requested interval is
unknown, `max_drawdown_pct` is `UNKNOWN`; no minimum is taken over a known
subset. Absolute `max_drawdown` remains independently defined when every
absolute drawdown point is known. A bucket containing any unknown percentage
point reports percentage `min` and `mean` as `UNKNOWN` with an availability
marker, never a partial extremum.
Cashflows are removed from the adjusted equity before both paths are built, so
deposits, withdrawals, and transfers cannot themselves create or erase a
drawdown path or magnitude. Unknown, gapped, conflicting, or incomplete
cashflow classification makes dependent PnL, paths, and summaries `UNKNOWN`
until complete reconcile evidence closes the interval. Summary magnitudes are
derived from exact pre-bucket points, never bucket means.

An equity-derived PnL must not also add realized PnL, fees, funding, or sampled
unrealized PnL. A component view is valid only when its sources form a proven
disjoint partition; otherwise the component and dependent total are `UNKNOWN`.
Sampled unrealized values describe the observation and are not integrated
between samples.

## 5. Event-driven margin

IM/MM load is recomputed on every numerator or denominator change. At each
event, select the latest compatible fresh facts at or before the event time.
The model retains absolute IM, absolute MM, denominator, ratio/load, unit,
currency, source IDs, and freshness metadata. A missing, stale, nonpositive, or
unit/currency-incompatible numerator or denominator makes the corresponding
load `UNKNOWN`; no interpolation, fabricated zero, or cross-event carry is
allowed after freshness expires. Margin recomputation from a wallet change is
immediate, but does not confirm an order resize.

## 6. Chart data and bounded read route

Exact derived points are built first. The server may then apply only the
versioned `elapsed_bucket_v1` aggregation. Its fixed seconds ladder is
`(1,5,15,30,60,300,900,1800,3600,14400,21600,43200,86400,604800,
2592000,7776000,31536000)`. Select the smallest width with
`ceil(elapsed_span/width)<=256`. Buckets are half-open `[start,end)`, aligned to
the exact series start on the elapsed axis, with the final boundary inclusive
of the final point. Each output bucket contains
`first`, `last`, `min`, `max`, `mean`, `count`, exact UTC timestamps and source
IDs for extrema, plus segment gap and provenance. For drawdown paths, `min`
must preserve the most adverse signed path value (and its exact UTC/source ID)
for both absolute and percentage paths. Source, provenance, unit,
currency, time basis, applicability, availability, or TESTED/LIVE changes split
a bucket. Apply these splits after width selection. If a trace then exceeds
256 records, select the next coarser ladder width deterministically, for at most
16 escalations. If the largest width still exceeds the bound, return
`RESOURCE_BOUND_EXCEEDED`. Adverse extrema are preserved; raw point arrays are
not returned.

The canonical bounded route is:

```text
GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PORTFOLIO
GET /api/v2/portfolio/campaigns/{campaign_id}/candidates/{candidate_id}/charts?mode=PAIR&symbol=<symbol>&side=<LONG|SHORT>[&contour=NONE|PORTFOLIO]
```

Path IDs are opaque URI-safe values, 1-128 characters. Query parameters occur
once; missing/unknown `mode`, unknown, empty, duplicate, path-like, or
out-of-scope values fail closed.
`PORTFOLIO` forbids `symbol`, `side`, and `contour`; `PAIR` requires `symbol`
and `side` and validates them against the candidate's server-known pinned
composition. Pair `contour` is optional with default `NONE`; `PORTFOLIO` is
accepted only when portfolio and pair units, currency, time basis, and
applicability match.
The server resolves at most one active live deployment binding for the candidate
from the immutable manifest; no deployment/account query selector exists. Zero
bindings keeps TESTED traces available and returns LIVE as
`UNKNOWN/NO_LIVE_BINDING`. Multiple bindings are `INCONSISTENT` and fail closed
without choosing one.

Apply this ordered pipeline and stop at the first failure: (1) non-GET => `405`
before service access; (2) invalid path-ID syntax => `400`; (3) query grammar,
including missing/unknown/duplicate/empty/path-like/out-of-scope parameters and
mode-conditional fields => `400`; (4) unknown campaign or unknown/unpinned
candidate => `404`; (5) unknown/unpinned pair => `404`; (6) multiple active
bindings => `409 INCONSISTENT`; (7) incompatible contour/metadata => `409`;
(8) resource breach => `413` with machine-readable
`reason=RESOURCE_BOUND_EXCEEDED`; (9) otherwise `200`, including data-level
UNKNOWN/gapped series with explicit availability and provenance. No later stage
is evaluated after failure. All stages have zero mutation and perform zero
writes.

In this slice `live_charts.py` exposes a pure request handler accepting method,
path, query mapping, and headers and returning status plus canonical body.
`tests/test_portfolio_live_charts.py` invokes it directly. No route is registered
in an existing served application; framework/application wiring is deferred to
a separate approved diff.

Every request is bounded to at most 256 output records per trace, 32 traces,
128 pair options, 1,000,000 raw source facts, and an 8 MiB encoded response.
Exceeding a bound fails closed; no truncation or client resolution override is
allowed. The four authoritative views are net/cumulative PnL, equity,
cashflow-adjusted drawdown, and IM/MM load. All values retain exact decimal
strings, source UTC, elapsed-from-start, source, availability, and provenance.
Missing series remain `UNKNOWN`; tested and live are independently hideable.

Panel rendering, when separately approved, uses dependency-free native SVG mean
and min/max envelope paths and one focus marker per panel. It creates no
per-point DOM nodes, canvas, chart library, client thresholds, interpolation,
or client baseline authority. Panel integration is outside this phase until
plan approval and its own review.

## 7. P2AB-20 entry-order resize projection

For a pair outside a position, the fixture monitor observes bot-driven entry
order resize within a worst-case 20-second window. Primary evidence is the
private order WS. One account-wide complete REST open-orders snapshot runs on an
interval `<=10s`, completes validation and append
within 5 seconds, and can recover WS revision gaps. The chart GET completes
within 5 seconds. Fake clocks are mandatory and triggers coalesce to one
in-flight reconcile.

`observation_age` is measured from the last successful WS-contiguous or
REST-reconciled observation covering the pair; only it drives freshness.
`confirmation_age` is measured from the last entry-order confirmation and is
display metadata only. Observation age `<=20.000s` is current. At age
`>20.000s`, status is `STALE` and current quantity, notional, state, and order
list are hidden. The `10+5+5` budget is measured against observation freshness,
so exactly 20 seconds remains current. Entry role is an
explicit manifest/order field and is never inferred. The projection includes
position, active entry quantity/notional, observation and confirmation timestamps and ages,
`IN_POSITION|PENDING_ENTRY|NO_POSITION`, `WS/REST_RECONCILED`, and a
deterministic list of constituent order IDs, revisions, and canonical digest.
Multiple orders aggregate with the decimal contract. Missing price or quantity
is `UNKNOWN`; cancel-replace/revision gaps/conflicts fail closed. A wallet
change may trigger margin recompute but never proves resize until order
confirmation. The monitor never mutates an order, bot, or configuration.

## 8. TDD and acceptance

Write focused failing fixture tests before implementation for storage/path
guard, WAL/FK/append-only history, REST/WS buffering and gaps, duplicate and
conflict, stable/ambiguous attribution, cashflow adjustment, signed
nonpositive drawdown paths, positive `max_drawdown`/`max_drawdown_pct`
summaries, cashflow non-creation/erasure, component double-count prevention,
event-driven margin, exact chart buckets, route
grammar and bounds, native SVG output, and the complete P2AB-20 fake-clock
resize scenario. Run the project suites only through `.venv`:

The fixtures additionally cover zero/one/multiple deployment bindings;
same-timestamp cashflow order, negative first equity, and mixed currency;
bucket-width selection/escalation/terminal failure; idle fresh, exactly
`20.000s`, and `20.001s` order projections; every HTTP code/body and zero-write
rejection, unknown campaign/candidate and two multi-fault first-failure cases;
and truncated/discontinuous/overlapping/deadline-exceeded REST pages.

The drawdown test must assert
`all(p <= 0 for p in drawdown_path)` and
`max_drawdown == -min(drawdown_path)`, with the equivalent percentage assertion
only when every percentage point is known; otherwise assert
`max_drawdown_pct` is `UNKNOWN`. A negative initial adjusted-equity fixture must
retain a known absolute magnitude while the percentage summary is `UNKNOWN`.

```powershell
.venv\Scripts\python.exe -m pytest tests/test_portfolio_live_store.py tests/test_portfolio_live_reconcile.py tests/test_portfolio_live_monitor.py tests/test_portfolio_live_charts.py -q
.venv\Scripts\python.exe -m pytest tests/test_panel_portfolio.py tests/test_panel_static_ui.py -q
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m compileall -q src/mrs3/portfolio src/mrs3/panel_portfolio.py
git diff --check
```

Acceptance must show exact `UNKNOWN`/`INCONSISTENT` provenance, no duplicate
execution/PnL, no secret/action fixture content, no target DB access, no
mutation on GET/non-GET rejection, all bounds, and no false healthy state after
gaps. Independent review is required. This specification does not authorize
real connectors, tester/runtime, recommendation readiness, admission, or live
use.

This slice may change only `src/mrs3/portfolio/live_store.py`,
`live_reconcile.py`, `live_monitor.py`, `live_charts.py`, their named
`tests/test_portfolio_live_*.py` tests and `tests/fixtures/portfolio/live/`, plus
this feature's spec/ADR/plan/evidence, `PRD.md`, and `progress.md`.
`src/mrs3/panel_portfolio.py`, `tests/test_panel_portfolio.py`, and
`tests/test_panel_static_ui.py` remain byte-identical regression gates. No route
registration is added to an existing served application.
