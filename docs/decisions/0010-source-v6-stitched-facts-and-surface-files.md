# ADR-0010: Source v6 Stitched Facts and Per-Surface DuckDB Files

**Status:** Accepted
**Date:** 2026-08-18

**Affects:**

- [Source v6 and stitched surfaces specification](../specs/2026-08-18-source-v6-stitched-surfaces.md)
- [Canonical Phase 1 specification](../specs/2026-08-16-mrs3-v07-canonical-phase1.md)
- [DuckDB analysis storage and importer](../specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md)
- [Phase 2 draft](../superpowers/plans/2026-08-16-MRS3_v0.7_PHASE2_TZ.md)

## Context

Source schema v5 treats a whole HTML report period as the replaceable source
unit. That cannot safely extend an existing point with another partially
overlapping report, cannot build an arbitrary interval from several reports,
and makes report-level identity more important than the economic facts needed
by analysis.

The operational requirement has changed:

- Source data must be rebuilt from HTML into a fresh v6 database; v5 is not
  migrated or merged into v6.
- Reports for the same strategy point may overlap and extend one another.
- Deduplication is by exact point identity and UTC time, not report index.
- Fixed-lot reports make realised results comparable across report restarts.
- The original HTML files are disposable after verified normalisation.
- A user may build several independently named surfaces for different scopes
  and intervals without overwriting an older surface.

The accepted Phase 1 geometry, canonical Shift grid, event gate, frozen
selection facts and JSON rules remain analytical requirements. This decision
changes their source/storage boundary, not their mathematical meaning.

## Decision

### 1. Fresh Source v6

Source v6 is a new schema populated from source HTML. There is no v5-to-v6
migration and no two-database merge contract for v5. Existing v5 databases
remain historical evidence until the v6 rebuild is accepted, then leave the
new operational flow.

Source v6 stores normalised point, cycle/action, independent-event and compact
wallet/equity evidence. It does not store HTML bytes or require HTML paths for
later materialisation. Minimal source-fragment metadata and hashes may remain
only where required for import idempotency, overlap validation and audit.

### 2. Exact point and time identity

The canonical point identity is:

```text
symbol + side + timeframe
+ open_ma_type + open_ma_source + open_ma_length
+ close_ma_type + close_ma_source + close_ma_length
+ shift_bp
```

The exchange is not part of this v6 identity. The operator must not combine
reports from different exchanges in one Source v6 database until a later
contract explicitly adds exchange identity.

Logical time is UTC. Day facts use half-open intervals `[00:00, 24:00)`.
Report-local indexes are never deduplication keys.

### 3. Fixed-lot stitching contract

Automatic stitching requires both reports to satisfy:

```text
exchange.use_upnl = false
basic.use_fix = true
basic.my_fix_balance is typed-equal
active-side balance percentage is typed-equal
```

The active-side key is `basic.balance_percentage_long` for LONG and
`basic.balance_percentage_short` for SHORT. Positivity of
`my_fix_balance` is not an additional compatibility rule.

The compatibility fingerprint is intentionally limited to the strategy and
sizing contract. `execution_compatibility_fingerprint_v1` contains exactly
`symbol`, `timeframe`, `side`, `shift_bp`, open-MA `type/source/length`,
close-MA `type/source/length`, report `initial_balance`,
`basic.my_fix_balance`, and the active-side balance percentage. It is hashed
from typed canonical JSON with sorted keys. A mismatch or missing/malformed
listed field is fail-closed; secondary exchange, fee, funding, filter, logging
and runtime settings do not independently prevent stitching.

Reports using UPNL-based sizing (`exchange.use_upnl = true`) or not using
fixed-lot sizing (`basic.use_fix = false`) remain importable as non-stitchable
legacy facts, are report-scope only, and cannot participate in a stitched
canonical surface.

### 4. Overlap, bridge cycle and activation

An extending report must overlap the active interval by at least 96 hours.
Ninety-six hours is the minimum sufficient stitch overlap; it is a minimum,
not a guarantee: if the old fragment ends with an open cycle, the incoming
report must start no later than that cycle's open time and contain the
evidence required to close or continue it.

For a compatible, sufficiently covering incoming fragment, ownership switches
at the exact `incoming_start` timestamp: facts strictly before it remain
outgoing-owned, and facts at or after it within the incoming covered interval
become incoming-owned. Day-level `USE_INCOMING_DAY` activation remains valid
only where the incoming fragment begins at UTC midnight and never overrides
exact timestamp/interval ownership at a partial-day boundary, so ordinary
actions, samples and events earlier on an overlapping UTC day are not dropped
or reassigned. Old versions remain inactive audit facts. An old open tail
contributes no PnL, DD or trade count; metrics stop immediately before its
opening. When an incoming fragment contains the complete subsequently closed
cycle, the incoming cycle and time series restore it and the combined interval
is recalculated.

`PARTIAL` is not the normal stitching mode. It is a batch/import outcome when
some fragments publish while other fragments remain inactive because of a
real gap, insufficient overlap, uncovered bridge cycle, malformed evidence,
incompatible fixed-lot settings or ambiguous incoming versions. A fragment
whose start cannot cover the outgoing tail cycle is marked
`BRIDGE_NOT_COVERED`: an automatic failure reason attached to the
unresolved/inactive fragment that contributes to `PARTIAL`. Manual resolution
is limited initially to `IGNORE_INCOMING` and `EXCLUDE_DAY_AS_GAP`;
`BRIDGE_NOT_COVERED` is not a manual disposition.

### 5. Canonical metrics

Metrics are recalculated for the selected interval; report-summary metrics are
not joined or averaged. Every overlapping timestamp/interval has exactly one
owner, so canonical samples, actions and events are never double-counted. The
three metric families use explicit sources:

- net Total PnL is derived from the canonical stitched Balance series, so it
  includes commissions, funding and other balance-affecting adjustments;
  Total PnL percent divides by the canonical initial Balance;
- Profit Factor is positive over absolute negative action-column PnL; action
  PnL excludes fees, matching the source tester metric, and overlap actions
  are deduplicated; Profit Factor is `n/a` when there are no losing PnL
  actions;
- `MaxEquityDrawdownPercent` is the running peak-to-trough over the stitched
  Equity series and retains the floating-UPNL meaning: Equity is rebased by the
  same seam offset as Balance, preserving each sample's `Equity - Balance`.
  Max DD therefore includes in-moment unrealised PnL even though
  `use_upnl=false` is used for sizing;
- `MaxRealizedDrawdownPercent` is calculated separately from the canonical
  Balance curve;
- exact closed trade/action count (each owned once), win/loss counts and
  distinct event count complete the primary evidence.

The canonical Balance series begins at the first report's initial fixed
balance; every subsequent report's Balance is rebased by its constant seam
offset so the series is continuous. `my_fix_balance` is a lot-compatibility
setting, not an assumed account-equity anchor; the validated
`canonical_initial_balance` is stored separately. Report-local balance offsets
are removed via seam offsets, never by averaging report summaries. Source v6
retains compressed timestamped wallet-change and UPNL/equity evidence for
future sampled MAE/MFE and mark-to-market research without retaining HTML.

### 6. Coverage and gaps

Coverage is determined per exact `symbol + side + timeframe` and point
identity from report-header intervals, independently of whether trades occur
on a day. Only continuous READY intervals can enter a surface. A real gap
blocks that timeframe for the requested interval.

Gap preflight exports exact missing point/day cells in a stable machine-readable
form suitable for future strategy-generation tooling. That later generator is
not part of this decision.

### 7. Per-surface DuckDB publication

Every Build Surface operation creates a new DuckDB file; it never overwrites a
previous surface. The visible name contains creation time and point count, and
the file stores its full scope, per-scope selected intervals, versions,
fingerprints and counts for the panel's `Сведения` action.

Surface fact tables are immutable after publication. The same file may receive
append-only analysis runs and their diagnostic artifacts. Surface identity and
content hashes cover frozen surface facts, not the physical bytes of the whole
DuckDB file.

A rebuildable catalog may index surface files for the panel, but the surface
file and its internal manifest remain the source of truth. Publication uses a
staging file, validation, close, atomic rename and catalog refresh.

### 8. Analysis evidence and Plateau report

Analysis retains exact point-event membership, rejected states, BEFORE/AFTER
event-filter evidence, CloseMA profiles, frozen BASE/1ORD and 2-4ORD lineage,
and deterministic intermediate decision facts. These facts are a diagnostic
trace, not copies of the source HTML.

Each analysis run can export a dedicated Plateau report with summary,
membership, BEFORE/AFTER, CloseMA profiles, lineage and research diagnostics.
Diagnostics do not silently become production gates.

### 9. Panel boundary

The Import-to-JSON tab gets one action progress indicator and status stream for
Preflight, Start Import, Check Coverage and Build Surface, followed by current
surface information and the surface library. Legacy source-pack controls and
the manual legacy candidate flow leave the operational panel. Test plan,
tester and DD5 remain in their own 6-8 tab and right-side status view.

### 10. Deferred work

Physical deletion/compaction of selected pairs, v5 migration, exchange-aware
identity, actual fill-order diagnostics, margin simulation and portfolio
simulation are deferred. No new Plateau/Core/Pareto hard gate is introduced by
this decision.

## Consequences

- PnL, floating equity DD, realised DD and trade counts are reproducible over
  an interval assembled from compatible reports.
- Long bridge cycles cannot be silently lost merely because a nominal 96-hour
  overlap was present.
- Exact independent-event union remains possible after HTML deletion.
- Source storage grows by canonical facts and compressed time series rather
  than duplicate report payloads.
- Existing Phase 1 analysis must be adapted to the new source and per-surface
  file boundaries without changing its accepted selection semantics.
- Historical v4/v5/legacy surfaces may remain readable but are not accepted as
  new Source v6 operational evidence.
