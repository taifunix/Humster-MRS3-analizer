# MRS3 Source v6 and Stitched Surface Contract

**Status:** Accepted after Task 0 evidence and independent review
**Date:** 2026-08-18
**Decision:** [ADR-0010](../decisions/0010-source-v6-stitched-facts-and-surface-files.md)
**Implementation plan:** [Source v6 implementation plan](../superpowers/plans/2026-08-18-source-v6-stitched-surfaces.md)

## 1. Purpose

Define a compact, cumulative Source DuckDB v6 that can rebuild canonical MRS2
point evidence from disposable HTML reports, extend one point with overlapping
report periods, calculate exact required metrics over arbitrary continuous UTC
intervals, and publish separately named DuckDB surface files for later MRS3
analysis.

This specification supersedes the v5 report-replacement/source-materialisation
contract only for new Source v6 operation. It does not rewrite historical v4 or
v5 evidence and does not migrate it.

## 2. Dependencies and retained analytical rules

The following remain authoritative where this specification does not replace
their storage/source assumptions:

- [Canonical Phase 1](2026-08-16-mrs3-v07-canonical-phase1.md): canonical
  Shift grid, CloseMA readiness, Plateau geometry, frozen representatives,
  BASE and 1-4ORD rules;
- [Event filter and shortlist](v07-event-filter-and-shortlist.md): real-event
  floor and shortlist meaning;
- [Analysis shortlist/READY JSON](2026-08-18-analysis-shortlist-ready-json-contract.md):
  current non-mutating Pareto/shortlist and JSON admission rules;
- [Phase 2 draft](../superpowers/plans/2026-08-16-MRS3_v0.7_PHASE2_TZ.md):
  retained diagnostic requirements explicitly restated below.

When an older document assumes one report-period replacement, one central
Analysis DuckDB or source schema v5, this specification governs new v6 work.

## 3. Scope

### In scope

- fresh Source v6 creation from HTML on Windows or Debian;
- exact point identity independent of report index;
- fixed-lot compatibility and overlapping-period stitching;
- UTC daily coverage and version activation;
- lossless normalised closed cycles/actions and exact events;
- compact wallet/equity/UPNL timeline evidence;
- exact interval PnL, equity DD, realised DD, Profit Factor and trade counts;
- bridge-cycle handling and open-tail exclusion;
- gap preflight and exact missing-cell export;
- separately published surface DuckDB files with internal scope information;
- append-only analysis runs in a surface file;
- retained Phase 1 selection and selected Phase 2 audit/Plateau reporting;
- unified panel progress for import, coverage and surface publication.

### Non-goals

- v5 migration or v5 database merge;
- using different exchanges in one Source v6 database;
- selective pair deletion or physical compaction;
- retaining source HTML after verified import;
- exact tick-between-sample MAE/MFE;
- exchange margin simulation, order-book capacity or portfolio simulation;
- actual fill-order rejection;
- new hard Plateau, bridge, robustness or Pareto gates;
- automatic generation of missing test strategies from gap exports.

## 4. Definitions and identity

### 4.1 Point identity

```text
point_key =
  symbol
  + side
  + timeframe
  + open_ma_type + open_ma_source + open_ma_length
  + close_ma_type + close_ma_source + close_ma_length
  + shift_bp
```

Requirements:

- `side` is exactly LONG or SHORT;
- MA type and source are canonicalised non-empty report values;
- MA lengths and `shift_bp` are typed integers; bool is rejected;
- report number, filename, source path and report-local point index are absent
  from `point_key`;
- exchange is not a point key in this version; mixing exchanges is an operator
  error and unsupported.

### 4.2 UTC intervals

- All timestamps are timezone-aware UTC.
- A report header period is stored as a half-open interval `[start, end)`.
- A UTC day is `[00:00:00, next 00:00:00)`.
- An event exactly at midnight belongs to the new day.
- Coverage comes from validated report-header time, not the first or last trade.
- An absence of trades does not shorten coverage.

### 4.3 Minimal provenance

Source v6 may retain only the metadata required to prove import and stitching:

- stable fragment ID and SHA-256;
- point ID and validated header interval;
- fixed-lot compatibility fingerprint;
- normaliser/schema version;
- import job, disposition and failure reason.

It must not retain HTML bytes, HTML-relative paths as required runtime inputs,
screenshots or arbitrary unnormalised settings JSON.

## 5. Fixed-lot admission

A fragment is `STITCHABLE_FIXED_LOT` only when:

```text
exchange.use_upnl == false
basic.use_fix == true
basic.my_fix_balance is present, finite and typed
basic.balance_percentage_long is present/typed for LONG
basic.balance_percentage_short is present/typed for SHORT
```

Two fragments may stitch only when their typed `my_fix_balance` values and
their active-side balance-percentage values are exactly equal. No additional
positive-value predicate is imposed by the stitch contract.

The stitch compatibility fingerprint is deliberately limited to the strategy
and sizing contract, not every secondary tester setting. For
`execution_compatibility_fingerprint_v1`, canonical input fields are exactly:
`symbol`, `timeframe`, `side`, `shift_bp`, open-MA `type/source/length`,
close-MA `type/source/length`, report `initial_balance`,
`basic.my_fix_balance`, and the active-side
`basic.balance_percentage_long` or `basic.balance_percentage_short`. Values
are typed and rendered as canonical JSON with sorted keys; the fingerprint is
the SHA-256 of that JSON and includes the version name. A mismatch in any
listed field is fail-closed; secondary exchange, fee, funding, filter, logging
and runtime settings do not by themselves block stitching. Missing or
malformed listed fields are non-stitchable.

`use_upnl=true` or `use_fix=false` fragments may be imported and retained as
legacy/non-stitchable facts, classified `NON_STITCHABLE_POSITION_SIZING`; they
are report-scope only and can never contribute to a stitched canonical v6
surface.

## 6. Normalised source facts

The logical schema must represent at least:

- database metadata and mutation generation;
- exact point configurations;
- compact source fragments/import jobs;
- UTC-day fact versions and their active/inactive disposition;
- normalised actions and closed cycles with stable IDs;
- exact independent events and point/day/event membership;
- compact timestamped wallet-change and UPNL/equity samples;
- overlap/bridge validation and manual resolutions;
- READY continuous intervals and gap details.

Physical table names and encodings belong to implementation design, but the
following losslessness requirements are normative:

1. Closed cycles and realised actions can be reconstructed in time order.
2. Exact distinct event IDs can be unioned across days and points.
3. Canonical Balance/equity can be reconstructed at every retained tester
   sample without a report starting-balance discontinuity (constant seam
   offsets).
4. Active and inactive conflicting versions remain distinguishable.
5. A source fragment can be deleted from disk after a committed readback proves
   all required normalised facts and hashes.
6. Ownership of every action, sample and event is exact at timestamp/interval
   granularity: a partial-day boundary never drops a fact, and UTC-day
   versioning never overrides exact ownership at that boundary.

## 7. Import, overlap and stitching

### 7.1 Preflight

Preflight recursively discovers HTML, parses without publication and returns:

- parsed/invalid/idempotent/stitchable/non-stitchable counts;
- exact point and interval inventory;
- fixed-lot compatibility failures;
- overlap duration;
- open-tail/bridge coverage status;
- real gap ranges;
- ambiguous incoming groups;
- a deterministic token bound to source database ID and mutation generation.

Start Import replays and validates this token before publication.

### 7.2 Standard extension

For the same compatible point:

```text
incoming_start < active_end
overlap >= 96 hours
```

Ninety-six hours is the minimum sufficient stitch overlap for automatic
stitching; it is a minimum, not a guarantee. The bridge-cycle rule below is
applied in addition to the overlap minimum.

If the active fragment has an open tail cycle, also require:

```text
incoming_start <= tail_cycle_open_time
```

and incoming evidence sufficient to reconstruct that cycle without mixing old
and incoming actions/samples.

When all checks pass:

- ownership switches at the exact `incoming_start` timestamp: facts strictly
  before `incoming_start` remain owned by the outgoing version, even inside
  an overlapping UTC day;
- facts at or after `incoming_start` within the incoming covered interval are
  owned by the incoming version;
- prior versions become inactive for their replaced interval, never
  physically overwritten in place;
- new intervals after `active_end` activate;
- the resulting continuous interval and metrics are recomputed;
- repeated import of the same fragment hash is idempotent.

Day-level activation (`USE_INCOMING_DAY`) remains valid where the incoming
fragment begins exactly at UTC midnight. It never overrides exact
timestamp/interval ownership at a partial-day boundary: an incoming fragment
starting mid-day (for example 14:00 UTC) must not drop or reassign outgoing
actions, samples or events that occurred earlier on the same UTC day.

The current import batch is incoming relative to already active facts. Within
one batch, conflicting fragments for the same point/day may auto-collapse only
when their normalised content is identical or one is the unambiguous compatible
extension selected by validated report intervals. Traversal order and filename
never establish authority; otherwise the group remains inactive/ambiguous.

### 7.3 Atomic cycle boundary

A cycle is never assembled from half old and half incoming action membership.
For a bridge cycle, actions, samples and event membership come from the
incoming version. Day versioning may be the storage boundary, but cycle
selection is atomic. The same lossless guarantee applies to ordinary actions,
samples and events: every fact is owned exactly once by timestamp/interval
relative to `incoming_start`, and no fact is dropped because a UTC day crosses
a partial-day boundary.

### 7.4 Open tail

Selected-interval boundary policy is recorded in
[ADR-0011](../decisions/0011-source-v6-boundary-cycle-policy.md): a cycle
opened before the selected interval is excluded unless it is an explicitly
resolved atomic bridge cycle supplied by the incoming fragment.

If a selected interval ends with an open cycle:

- set `open_cycles_at_window_end` and preserve its open timestamp;
- exclude that cycle from net PnL, Profit Factor and trade counts;
- exclude its UPNL and later samples from equity-DD computation;
- calculate Balance and equity metrics only through the sample immediately
  before the cycle opens;
- mark the point/report output with an explicit truncated-tail diagnostic.

When a later incoming fragment fully covers and closes the cycle, the incoming
cycle and samples enter the recomputed interval normally.

### 7.5 Non-standard outcomes

`USE_INCOMING_DAY` is the default compatible overlap action. The remaining
entries are failure reasons attached to fragments that remain
inactive/unresolved:

```text
USE_INCOMING_DAY       default compatible overlap action
BRIDGE_NOT_COVERED     automatic failure reason: an incoming fragment that
                       satisfies the overlap minimum but whose start cannot
                       cover the outgoing tail cycle opened earlier remains
                       unresolved/inactive and is not stitched
IGNORE_INCOMING        manual rejection of an inactive incoming version
EXCLUDE_DAY_AS_GAP     manual declaration that a day remains unavailable
```

`BRIDGE_NOT_COVERED` is an automatic failure reason attached to an
unresolved/inactive fragment; it is not a new manual disposition alongside
`IGNORE_INCOMING` or `EXCLUDE_DAY_AS_GAP`, and it contributes to a `PARTIAL`
batch outcome.

`PARTIAL` describes an import batch in which at least one fragment committed
and at least one remained inactive/unresolved. Mere numeric difference between
old and incoming overlap facts is not a conflict when all compatibility and
bridge checks pass: incoming is authoritative.

## 8. Metric algebra

Metrics are calculated from canonical facts over the selected interval and
never by averaging report summaries. Every overlapping timestamp/interval has
exactly one owner: ownership switches at the exact `incoming_start` timestamp
(`USE_INCOMING_DAY` applies only at UTC-midnight boundaries), so canonical
samples, actions and events are never double-counted or dropped at a
partial-day boundary. The three metric families use explicit sources: net PnL
comes from the canonical Balance series, Max Drawdown from the stitched Equity
series, and Profit Factor from action-column PnL.

### 8.1 Canonical Balance and net PnL

The canonical Balance series begins at the first report's initial fixed balance
and is continuous across report seams:

```text
canonical_balance[0] = first report's initial fixed balance
seam_offset(first report) = 0
seam_offset(incoming report) = canonical_balance[seam_start]
                               - incoming_report_balance[seam_start]
canonical_balance[t] = owner_report_balance[t] + seam_offset(owner report)
```

`seam_start` is the first timestamp owned by the incoming report. Each
incoming report is rebased by its single constant seam offset, so the series
remains continuous and a report-local balance reset does not shift later PnL.

The positive `canonical_initial_balance` is stored separately from the
fixed-lot settings; golden reports must prove the first report's initial
fixed balance / first-wallet-sample relationship before the seam-offset
formula is enabled.

Net Total PnL is derived from the canonical Balance series, so it includes
commissions, funding and other balance-affecting adjustments that action PnL
does not carry:

```text
TotalPnL = final canonical Balance - initial canonical Balance
TotalPnLPercent = TotalPnL / canonical_initial_balance * 100
```

### 8.2 Canonical Equity and Max Drawdown

Equity is rebased by the same seam offset as Balance, preserving each sample's
`Equity - Balance` (unrealised PnL):

```text
unrealised_pnl[t] = report_equity[t] - report_balance[t]
canonical_equity[t] = canonical_balance[t] + unrealised_pnl[t]
```

Max Drawdown is the running peak-to-trough over the stitched canonical Equity
series; the percent uses the running peak value at each trough:

```text
running_peak[t] = max(canonical_equity[0..t])
MaxEquityDrawdown = max over t of (running_peak[t] - canonical_equity[t])
MaxEquityDrawdownPercent = max over t of
    (running_peak[t] - canonical_equity[t]) / running_peak[t] * 100
```

`MaxEquityDrawdownPercent` is the primary backward-compatible point DD and
includes in-moment unrealised PnL even though `use_upnl=false` is used for
position sizing.

`MaxRealizedDrawdownPercent` uses the same running-peak-to-trough formula on
`canonical_balance` and is stored as a separate metric. It must not silently
replace the primary equity DD.

### 8.3 Action-column Profit Factor and trade counts

Profit Factor uses the report action-column PnL, which excludes fees and other
balance-affecting adjustments. This matches the source tester metric; fees are
never added to the numerator or denominator. Sums use only admitted actions
owned once after overlap deduplication:

```text
GrossProfit = sum(positive action-column PnL)
GrossLoss = abs(sum(negative action-column PnL))
ProfitFactor = GrossProfit / GrossLoss
```

When there are no losing PnL actions (`GrossLoss = 0`), Profit Factor is
explicitly `n/a`; no finite number is invented.

```text
TotalTrades = exact admitted realised-action count, each owned once
WinRate = wins / TotalTrades * 100
```

### 8.4 Event algebra

`PointEventCount` and `PlateauEventCount` use exact sorted distinct event IDs.
Daily unique counts must never be summed as a substitute for a set union.

### 8.5 Future metrics

Compressed samples preserve tester-sample MAE/MFE and mark-to-market return
research. Exact between-sample/tick excursions, exchange margin usage and
pending-order occupancy are not claimed.

## 9. Coverage, READY intervals and missing export

Coverage is evaluated independently for each exact
`symbol + side + timeframe`, using the complete canonical Shift/CloseMA
readiness contract inherited from Phase 1.

The panel may select an interval inside the available continuous READY range
for each selected pair/timeframe. One surface may contain several scopes with
different selected intervals; each interval is recorded explicitly.

If any required point/day cell is absent:

- preflight reports exact missing UTC days and point parameters;
- the affected timeframe does not enter the requested surface;
- the operator either imports additional reports, shortens the interval or
  explicitly preserves the day as a gap;
- stable CSV/JSON export contains enough typed fields to group future tester
  strategies for all scopes or selected individual pair/timeframes.

Source-coverage missing cells and analysis Refine-neighbour requests are
different artifacts and must have different names/reason codes.

## 10. Surface file and library

### 10.1 Publication

Each Build Surface produces a new file such as:

```text
surface_20260818_143015_123456p_<short-id>.duckdb
```

The exact safe filename function is deterministic and collision-resistant.
The panel display label needs only creation date/time and point count.

Publication sequence:

```text
staging file
-> write frozen facts and internal manifest
-> validate/read back/count/hash
-> close file
-> atomic rename
-> refresh rebuildable catalog
```

Cancellation or failure before rename leaves no published surface. Orphan
staging files, missing catalog entries, missing surface files and manifest
hash mismatches have explicit recovery/diagnostic behavior.

### 10.2 Immutability and appended analysis

Frozen surface fact tables and the surface manifest never change after
publication. Analysis runs may be appended transactionally to the same file.
They cannot mutate frozen facts or another run.

Surface identity uses canonical logical content, not raw DuckDB file bytes,
filesystem path, file modification time or catalog ordering.

### 10.3 `РЎРІРµРґРµРЅРёСЏ`

The internal manifest supplies:

- surface ID, creation time and point count;
- pair/side/timeframe scopes and selected interval per scope;
- canonical grid/readiness/event/metric/schema versions;
- tail truncation and other warnings;
- latest analysis run status and artifact counts.

## 11. Analysis and Phase 2 evidence

The accepted Phase 1 algorithm is adapted, not redesigned. In particular the
canonical event floor remains `PointEventCount >= 3`, economic equivalence is
formed before that filter, and rejected points remain auditable.

Each analysis run persists:

- exact input surface ID and algorithm/config fingerprint;
- Plateau IDs, members, core/support roles and spans;
- `AllPointCount`, `CoreSize`, `SupportedSize`,
  `EventEligiblePointCount`, `PointEventCountSum` and exact distinct
  `PlateauEventCount`;
- BEFORE/AFTER event-filter trail using the same Plateau geometry;
- CloseMA profile for every expected CloseMA, including no-representative and
  blocked reasons;
- frozen representatives, BASE, 1ORD, 2-4ORD and READY lineage;
- missing Refine neighbours and affected points;
- non-mutating Pareto/shortlist reasons;
- diagnostic-only robustness and event-bridge results when implemented.

No diagnostic becomes a gate without a new decision/specification.

### 11.1 Plateau report

Analysis exports one `plateau_report.xlsx` with at least:

- `Plateaus`;
- `Plateau Members`;
- `Before After`;
- `CloseMA Profiles`;
- `Lineage`;
- `Diagnostics`.

CSV equivalents and their row counts/content hashes are recorded in the
analysis manifest. The report is generated from persisted facts without
rerunning analysis.

## 12. Panel behavior

### Import-to-JSON tab (steps 1-5)

- One right-side current-operation title, progress bar and status stream covers
  Preflight, Start Import, Check Coverage and Build Surface.
- Duplicate per-action status blocks are removed from the left workflow.
- Below progress: forming/selected surface information, library and `РЎРІРµРґРµРЅРёСЏ`.
- Date bounds shown per pair/timeframe are active controls constrained to the
  available READY interval.
- Legacy MRS2 DuckDB/source-pack creation and manual legacy candidate controls
  are absent.

### Test plan/tests/DD5 tab (steps 6-8)

- Keeps test plan, tester and DD5 workflows.
- Uses a dedicated right-side status view.
- Its strategy/test/DD5 statistics are not shown while steps 1-5 are active.

Progress values come from backend work units; timers or cosmetic progress are
not acceptance evidence.

## 13. Safety and deletion

HTML is eligible for deletion only after committed v6 readback, zero
quarantine for that file, required row/sample/event counts, hashes and an
explicit `safe_to_delete=YES` checklist.

Pair deletion and physical compaction are deferred. Source v6 implementation
must not add a logical delete button that promises disk-space reduction.

## 14. Determinism and acceptance evidence

Required regression evidence includes:

- row-order-independent normalisation and stitching;
- identical semantic result for one report versus compatible overlapping
  fragments covering the same evidence;
- report balance resets do not alter canonical Balance/equity/DD;
- overlap exactly 96 hours passes only when bridge coverage also passes;
- a bridge opened earlier than incoming start fails `BRIDGE_NOT_COVERED`;
- old open tail is excluded and later restored by a covering incoming report;
- mid-day incoming start (for example 14:00 UTC) with non-empty outgoing
  activity before the boundary and no open tail keeps every outgoing action,
  sample and event before `incoming_start` and switches ownership at the exact
  timestamp (no data loss);
- exact PnL, equity DD, realised DD, Profit Factor and trade-count golden cases;
- event set union is not daily-count addition;
- gaps at UTC midnight and empty-trade days obey coverage rules;
- same surface logical content has the same identity regardless of input row
  order, physical path, creation time and catalog order;
- frozen surface facts remain identical after appended analysis and reopen;
- Plateau report is reproducible from persisted facts;
- Windows and Debian normalisation produce the same canonical hashes;
- bounded real-data import/storage benchmark and one complete real v6
  import-to-surface-to-analysis smoke.

## 15. Definition of done

- ADR-0010 and this specification are accepted after independent review.
- Fresh v6 import passes golden and real HTML evidence with safe deletion
  disabled until readback succeeds.
- At least one point is stitched from overlapping reports and independently
  reconciled for PnL, both DD values, Profit Factor and trade count.
- Gap export identifies an intentionally removed exact point/day cell.
- Two Build Surface operations publish two separately selectable files.
- A published file exposes correct `РЎРІРµРґРµРЅРёСЏ`, accepts an append-only analysis
  run and exports the Plateau report.
- The redesigned panel reports truthful backend progress for all four actions.
- Relevant focused and broader tests, `git diff --check` and independent review
  pass for every implementation task.
