# Shortlist filters v2: explicit application, MA proximity and Pareto

Status: implementation in progress on `feat/shortlist-filters-v2`, authorized
by the operator after D3 `PLAN_APPROVED` from Opus 5/high on 2026-09-27.
Independent code review and final acceptance remain pending.

## Goal and boundaries

Replace the four independent Phase 2 controls in Shortlist and READY JSON with
three optional filters and an explicit `Пересчитать фильтры` button. Keep the
displayed cohort, audit and generated JSON consistent and reduce repeated work.

The controls are Source PRETEST A/B, opening-MA proximity, and one joint Pareto.
All default OFF. Representatives, plateaus, shifts, lots and materialization
remain governed by their existing contracts. No new candles, surface rebuild,
tester run, PerformanceDB migration or source-data mutation is required.
The MA rule limits parameter differences; it does not prove historical price
level nesting. Source metrics do not establish final MRS3 performance.

Dependencies:

- [Existing Phase 2](2026-08-25-panel-phase2-structural-filters.md).
- [Existing event/shortlist contract](v07-event-filter-and-shortlist.md), section 24.2.
- [PRETEST A/B](2026-09-20-source-pretest-ab-filter.md).
- [Canonical selection](2026-08-16-mrs3-v07-canonical-phase1.md), sections 12 and 16.
- [Plateau admission](2026-08-25-multi-order-plateau-admission.md).
- [Shared workers](2026-09-10-panel-fresh-analysis-settings.md).

This proposal supersedes the fresh Panel's four-control Phase 2 behavior only
when implemented and accepted. Legacy non-fresh APIs keep their old contract.

## Filter semantics

### 1. Source PRETEST A/B

Retain the existing first-order evidence, 14-day tail, strict decline >95%,
quiet-tail pass, insufficient-history pass and non-comparable pass. An old
analysis without PRETEST evidence remains usable with A/B OFF; A/B ON returns
the existing actionable rebuild error. Do not fabricate missing evidence.

### 2. Opening-MA proximity

For each additional order i, require `abs(open_ma_i - open_ma_1) <= 1`.
Order 1 is the existing ordered first representative. One-order structures pass.
Compare all additional orders with the first, not with the preceding order.
For example, 3/2/4 passes; 2/3/4 fails. Keep current GAP checks and shifts.
The user has not approved an additional GAP surcharge.

Label: `Лесенка: Open MA ±1 от первого ордера`, with short help explaining
that this is a proximity restriction, not a price-level guarantee.

### 3. Joint Pareto

Compare only within exact `(pair, side, timeframe, common_close_ma, order_count)`.
For A to dominate B, at EVERY corresponding order require:

- `source_pnl_pct_A >= source_pnl_pct_B`;
- `source_dd_pct_A <= source_dd_pct_B`;
- `plateau_point_count_A >= plateau_point_count_B`;
- `point_event_count_A >= point_event_count_B`.

At least one PnL or DD comparison must be strictly better. Improvement only in
events or plateau size is insufficient. Full equality keeps both candidates.
No sums/means of order PnL, economic tolerance, Top N, cross-CMA comparison,
same-first restriction or Shift criterion is introduced.

The rule applies to 1ORD as well as 2-4ORD, matching the old Pareto universe.
Plateau size is the stored number of member points, not event count or the
number of strategy variants. Close support and efficiency leave the fresh
Pareto calculation; their upstream uses are unchanged.

### Order of evaluation and evidence

Evaluate persisted READY eligibility, then A/B, then MA proximity, then Pareto.
Candidates rejected earlier cannot dominate later candidates. Deterministic
primary reasons follow this order; retain underlying evidence for audit.
Use a surviving Pareto dominator, chosen deterministically by structure/candidate
identity, so an explanation never points to an unavailable replacement.

Missing, nonfinite or malformed required metrics are data errors, not zeros or
silent passes. All switches are actual booleans. Keep exact numerical ordering
from stored decimal values and validate integer MA/count fields without truncation.

## Application state and user interaction

The browser maintains a draft selection and an applied snapshot. The three
checkboxes edit only the draft. Their change handlers send no recalculation.
Show a short pending-changes message when draft and applied selections differ.

`Пересчитать фильтры` is below all three controls. It freezes the draft for the
request; on successful completion the response and its options replace the
table and applied snapshot atomically. Later checkbox edits remain pending.
On error keep the previous table and applied options and show the error.

`Обновить shortlist` reloads the applied selection; it never applies pending
checkbox edits. Initial opening of an analysis loads all filters OFF and resets
draft/applied options, scope selections and analysis-specific request state.
Changing analysis invalidates the previous snapshot and outstanding responses.

Use a monotonically increasing request revision and analysis identity to reject
late responses. Repeated clicks cannot start duplicate recalculations. Disable
audit/generation while a replacement snapshot is pending or no snapshot exists.
After a failed same-analysis recalculation the old snapshot is usable and clearly
labelled. An old-analysis snapshot must never be used for the newly selected run.

Audit and Generate READY JSON always use the applied snapshot. Pending checkbox
edits alone do not invalidate it. Scope selection changes immediately select
from its READY rows. Prune selections for missing or zero-READY scopes.
Restore of an old generated batch must not overwrite a user's active analysis
or a newer shortlist request; keep generated-batch identity separate.

## API and reproducibility

Fresh requests use `filter_version=shortlist-v2` and the three top-level flags
`pretest_ab_enabled`, `ladder_enabled`, `pareto_enabled`. Default flags are false.
Enabled old `filters` entries are rejected with an actionable stale-client
message; never silently reinterpret individual old criteria as the new Pareto.
Empty/all-false legacy fields can be accepted as OFF only if unambiguous.
Legacy non-fresh routes and their Python filter functions retain their semantics.
For every fresh consumer, the four retired criteria are never evaluated or
applied, even when an old client supplies their names. A true value is an
error, not a hidden switch; false values are ignored for compatibility only.
The unused direct fresh legacy filter/list adapters are removed. This does
not change the independent non-fresh shortlist API.

A successful shortlist response contains canonical applied options, the filter
engine version, analysis identity, a content-bound `selection_token`, groups
and compact candidate statuses. The token binds the full artifact SHA-256,
analysis identity, engine version, options and sorted surviving READY identities.

Audit and fresh Panel generation require that token and the applied options.
The server recomputes or retrieves the matching verified result and checks token,
analysis and selected scopes before output. Browser candidate IDs do not authorize
generation. Generate exactly the surviving candidates in the selected scopes.
Tokens are reproducibility digests, not credentials or global per-user state.
Two tabs may hold different valid snapshots without overwriting one another.

Freeze options and token into the generation job before its background thread
starts, revalidate the artifact before publication, and record filter version,
options, token and selection identity in generated provenance. Changing source
bytes after application requires recalculation, not silent use of an old cache.
Existing historical manifests remain readable; bump the fresh generation
selection contract and update strict consumers together where required.

## Counts and audit

For each manifest scope include a TF row even when it has no candidates.
`plateau_count` counts distinct persisted geometric plateau IDs in that scope.
The pair/side row sums those counts across its TFs, including zero-READY TFs;
do not count a plateau again for each CMA, order or lot variant. This number
describes the analysis and does not change with shortlist switches.

Order buckets always count READY-after-filters structures. `ALL` counts persisted
structure rows and `DEFERRED = ALL - READY`, including persisted non-READY rows.
Thus `1ORD+2ORD+3ORD+4ORD = READY` and `READY+DEFERRED = ALL` even with flags OFF.
Show numeric zero for a verified zero plateau count; missing data is not zero.

Fresh audit uses Summary, READY and DEFERRED sheets with primary reasons,
per-order comparison values and dominator identity. Include A/B evidence,
MA difference evidence, applied options and selection identity. Do not calculate
four independent Pareto views just to populate old diagnostic sheets.

## Performance design

1. Prepare validated inputs once per cold analysis load. Batch reads by table,
   omit SQL ordering by raw payload JSON, and sort compact IDs only where needed.
   Compute scope facts in the same pass. Keep existing identity/event validation.
2. Replace whole-file `read_bytes()` hashing with streaming SHA-256. Reuse a
   digest within an action. Warm actions still verify content; file size/mtime
   alone are not a correctness key. Cold preparation needs before/after identity
   agreement; generation rechecks before publication.
3. A bounded controller-owned cache holds one compact prepared analysis and up
   to eight small evaluation results (three booleans). Key by artifact content,
   analysis and engine version; do not retain full event-list/DataFrame copies.
   Apply a 256 MiB total retained-memory cap and fall back to uncached request-local
   evaluation for oversized input. Cache failure/eviction cannot change results.
4. Coalesce simultaneous preparation of identical input and bound concurrent
   shortlist work in one controller. Long calculations must not hold the general
   Panel lock. Old/new analyses must not share mutable preparation state.
5. Evaluate Pareto once. Encode exact per-column Decimal ordering as integer
   ranks and use bounded NumPy blocks, preserving strict economic comparisons.
   Never allocate a complete candidates-by-candidates-by-metrics tensor.
6. Parallelize independent comparison groups with a bounded thread pool; workers
   consume immutable numeric arrays and do not share DuckDB connections.
   Read `duckdb_import.workers` through the existing Settings path at each action.
   Serial execution for small work avoids scheduling overhead. Bound temporary
   memory across the whole pool (64 MiB temporary-array budget), not independently
   per worker.
7. Reuse the verified selection for list/audit/generation. Generation may load
   selected point records once for existing strategy validation; it must not
   reload/revalidate/filter the complete universe a second time.

No new concurrency setting, process farm, persistent derived database or new
dependency is required. Speedup is accepted only after measurements; worker
count must not affect decisions, reason ordering, token or generated identities.

## Acceptance

Meaningful unit, controller, HTTP and browser-state tests cover the rules,
draft/applied boundary, stale replies, artifact changes, A/B compatibility,
scope sums, audit/generation agreement and deterministic parallel execution.
Compare optimized Pareto with a small exact Decimal oracle and permuted input.
Measure cold/warm shortlist, audit-selection reuse and generation-selection
reuse separately, including validation/hash costs and peak memory.

Read-only real-analysis probes and temporary fixtures are sufficient for
implementation acceptance. Do not launch a real tester or recalculate a live
PerformanceDB as part of this task.

## Implementation detail: persisted fields and exact comparisons

The following paths are relative to parsed `payload_json` in the named table.
The SQL `scope_key` must agree with the payload's symbol/side/timeframe and the
manifest. A scope is unique by `(symbol, side, timeframe)` within the analysis.

| Value | Persisted path | Interpretation / required use |
| --- | --- | --- |
| Group | `structures.symbol`, `.side`, `.timeframe`, `.common_close_ma`, `.order_count` | Canonical scope, positive integral CMA, integral order count 1..4; always validate count |
| Ordered representatives | `structures.orders[i]` | Persisted list order; required for READY structures; length equals order count |
| Open MA | `structures.orders[i].open_ma` | Positive integral period; required by proximity |
| Source PnL | `structures.orders[i].source_pnl_pct` | Finite Decimal percent; required by Pareto |
| Source DD | `structures.orders[i].source_dd_pct` | Finite nonnegative Decimal percent magnitude; required by Pareto |
| Plateau size | `structures.orders[i].plateau_point_count` | Positive integral member-point count; required by Pareto; not root-level tuple |
| Point identity | `structures.orders[i].point_id` -> `points.point_id` | Existing validated reference; never use an unverified event count copy |
| Point events | joined `points.point_event_count` | Nonnegative integer, checked against exact `_event_ids` and `event_ids_hash` |
| A/B evidence | joined `points.pretest_ab` for first order | Existing versioned evidence contract |
| Geometric plateau | `plateaus.plateau_id` within SQL `scope_key` | Distinct scoped identity `(scope_key, plateau_id)` |

`selection._order_from_point` persists source metrics as JSON numbers from
`point.pnl_pct` and `point.dd_pct`. Decode to `Decimal(str(value))` without
inventing precision lost upstream. Do not replace the persisted source metric
with a newly recomputed strategy metric. DD is a positive percentage magnitude
(the upstream equity drawdown/peak times 100), not a signed loss or a fraction.
Reject negative/nonfinite DD, never take abs() or guess scaling from its size.
Zero DD is valid. Unit correctness follows the supported producer/fingerprint;
a small positive number alone cannot prove a fraction/percent mismatch.

Canonical order construction is ascending `(shift_bp, point_id)` in
`selection.build_structures`, then `id=1..N`. Preserve the persisted array;
validate that ordering, never sort it into a new strategy at filter time.
If order `id` is present it must match its 1-based position; its absence in
otherwise valid historical payloads is accepted. Corresponding orders mean
the same array position, not a matching MA or plateau ID between strategies.

Integer fields accept only finite integral numeric values, not booleans or
fractional numbers. MA must be positive; counts have the bounds above.
An MA difference of +1 or -1 passes, +/-2 fails; a difference of 1.0000001
implies malformed fractional MA and is a data error, not an approximate pass.

Ordinal ranks use numeric Decimal equality and ordering: 1.10 equals 1.1,
1E+1 equals 10, and -0 equals 0. Do not deduplicate by string/repr or round
through a Decimal context. Test encoded comparisons against the exact oracle.

Dominator order is ascending lexicographic `(structure_id, candidate_id)`.
Canonical IDs are nonempty strings; historical integral IDs convert to decimal
strings, never numerical sort. Reject booleans, containers, null and blank IDs;
do not case-fold. Candidate ID falls back to structure ID only when absent.
Reject duplicate/colliding canonical candidate identities. Select only among
surviving dominators; reasons and tokens are input-order/worker invariant.

Plateau hashing already includes symbol/side/timeframe and core point IDs in
`plateau._plateau_id`. Scope-qualified identity makes the counting contract
explicit: identical raw IDs in different TFs represent different scoped
plateaus. Distinct per-scope counts therefore sum without counting CMA/order
variants. Validate scope membership and test totals for all eight flag settings.

## Compatibility, empty input and errors

Before this matrix validate that `filters`, if present, is an object with only
the four recognized old names and actual boolean values. All present new flags,
including `pretest_ab_enabled`, must also be actual booleans.

| filter_version | Legacy filters | ladder/pareto fields | Result |
| --- | --- | --- | --- |
| Unknown value (including null) | Any | Any | 400 unsupported version |
| Absent | Any | Present, even false | 400 stale client; supply shortlist-v2 |
| Absent | Absent, empty, or all false | Absent | Accept new Pareto/proximity OFF; existing A/B boolean retained |
| shortlist-v2 | Absent, empty, or all false | Absent or present | Accept canonical flags, missing flags false |
| Absent or shortlist-v2 | Any old flag true | Any | 400 stale client; explicit new selection required |

This matrix governs fresh list, list-with-audit, dedicated audit, JSON generation
and RUNS. Audit/generation additionally require the valid applied token even for
an accepted version-absent request. Old non-fresh APIs are not changed.

There is exactly ONE A/B request spelling in both existing and v2 clients:
the top-level `pretest_ab_enabled` boolean. It is not a key in `filters`, is
not a newly introduced ladder/Pareto field, and has no separate legacy alias.
This is verified in `PanelController._pretest_ab_enabled` and the current JS
request builders. Do not invent an alias or reject this field in v2.

| Request detail (no enabled old criteria) | Canonical applied (A/B, ladder, Pareto) |
| --- | --- |
| Version absent, pretest_ab_enabled=true, ladder/pareto absent | (true, false, false) |
| Version absent, pretest_ab_enabled=false or absent, ladder/pareto absent | (false, false, false) |
| shortlist-v2, pretest_ab_enabled=true, ladder/pareto false or absent | (true, false, false) |
| shortlist-v2, pretest_ab_enabled=false or absent, ladder/pareto false or absent | (false, false, false) |

Identical canonical options against the same artifact produce identical tokens
regardless of whether the accepted request omitted `filter_version`. An A/B
field inside `filters` is an unknown old key and errors. No alias precedence
or alias migration exists. Test these cases across all five fresh consumers.

The old parser also accepts the four old criterion names at top level when
`filters` is absent. V2 checks recognized old criteria at BOTH locations:
all present values must be booleans; any true causes the stale-client error,
including top-level true plus nested false. False values are accepted under
the same matrix. They must never silently enable old filtering or be masked.

Missing/malformed required PRETEST is a whole-request evidence error with an
actionable rebuild message, never a per-candidate quiet pass. Preserve existing
validation: supported v2 artifacts require complete valid point evidence even
with A/B OFF; legacy v1 permits absence with A/B OFF, but validates any evidence
that is present. A/B ON on unsupported v1 still errors. Test mixed evidence.

An empty survivor set is a successful shortlist with a token and zero READY.
Audit remains available and writes Summary, READY headers and DEFERRED evidence.
Generation with no READY candidates in selected scopes returns
`EMPTY_READY_SELECTION` and publishes no files/batch/manifest. Empty or invalid
scope selections error explicitly. Zero-READY scopes are not selectable in UI.

## Bounded work, ownership and timing

Count retained Python object graphs using `sys.getsizeof` recursively, tracking
visited object identities to avoid double counting. Account for ndarray-owned
buffers (`nbytes`) plus headers, not shared views twice. Include all retained
evaluation vectors in the 256 MiB cap and check before insertion. This is a
retention-accounting limit, not a promise that process RSS stays under 256 MiB.
Measure RSS separately. A read-only sizing probe of 11,816 full structures plus
38,304 scalar point tuples measured 76,144,082 bytes (~72.6 MiB). PRETEST in that
probe was still JSON text, so actual parsed/Decimal/rank/result storage must be
measured too; do not treat the estimate as an implementation measurement.

Budget all concurrent NumPy temporary arrays from their dimensions/dtypes,
including masks and intermediates, against the shared 64 MiB limit. Derive
block size and effective concurrency from that budget and configured workers.
Do not cache copied candidate payloads per switch combination. Stream/batch
cold parsing so event-array intermediates can be discarded after validation.

Keep one prepared-analysis slot deliberately: two tabs have independent correct
tokens, but alternating different analyses may require cold reloads. This is an
accepted bounded-memory tradeoff, not a claim of multi-analysis warm performance.
One heavy shortlist action runs per controller. Coalesce identical in-flight
requests, at most four waiting followers with a 30-second wait timeout; other
work or saturation returns `SHORTLIST_BUSY` (409, Retry-After: 1), no hidden
unbounded queue. Release the slot/followers on errors. A coalesced result must
match the requested options; shared preparation never implies shared flags.

A warm list uses an immutable cached snapshot verified against the artifact
at action start; this is its consistency point, not a guarantee that a file
cannot change after return. Any observed mid-action identity mismatch fails
the action without partial UI publication. Cold preparation checks both sides;
audit and generation additionally check before atomic output publication.
Failure leaves previous same-analysis UI/output intact and temporary output
unpublished. Test mutation during cold load and audit/generation publication.
If the start-of-action digest differs from the cached one, list invalidates
the slot and cold-prepares a new snapshot/token. Audit/generation requests with
the superseded token fail as `STALE_SHORTLIST_SELECTION` without publication.

Read-only streaming SHA-256 of the 750,792,704-byte analysis took 2.985, 2.897
and 2.813 seconds (median 2.897, 1 MiB buffers, OS-warm runs). This is a real
latency floor, not yet optimized shortlist timing. Technical target for the
new warm-list median is <=5 seconds on the same host/corpus, not a promised SLA.
Record hash time separately. If it dominates, keep content verification and
show its stage/time; if the target is missed, report it as unmet rather than
weaken integrity checks or claim instantaneous filtering.

The separate liquidity plan is also design-only and may touch shared Panel
files. Before implementation, root rechecks agents/worktree and sequences
ownership of `panel.py`, `panel_web/app.js` and `panel_web/index.html`: a single
Executor owns this contour, with no concurrent liquidity edits to those files.

Current Panel uses neither localStorage nor sessionStorage for these switches.
Do not clear unrelated browser storage or add a migration framework. Remove
all old checkbox-ID references and explicitly reset checked state on new analysis
open (including browser-restored form state). Test stale DOM state and retain
the existing no-localStorage/sessionStorage invariant.

Executable browser-state coverage lives in
`tests/test_panel_static_ui.py::test_shortlist_v2_state_machine`, using the
existing `subprocess.run(("node", "-e", script), ...)` pattern with Node's
built-in assertions/VM and controlled DOM/request fakes. It must fail, not
skip, if Node is unavailable. Run it explicitly through `.venv` pytest and
include its result in implementation review evidence; `node --check` alone
is only a syntax check. Update stale old-checkbox assertions in that same slice.
