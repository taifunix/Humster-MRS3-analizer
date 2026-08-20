# Source v6 Analysis Handoff

**Status:** Accepted — implementation evidence exists for Tasks 1–6; real Windows end-to-end execution and formal final review remain acceptance gates
**Date:** 2026-08-19
**Implementation plan:** [Source v6 analysis handoff](../superpowers/plans/2026-08-19-source-v6-analysis-handoff.md)
**Predecessor:** [Source v6 and stitched surfaces](2026-08-18-source-v6-stitched-surfaces.md)
**Decision:** [ADR-0010](../decisions/0010-source-v6-stitched-facts-and-surface-files.md)

## 1. Purpose

This contract closes the Windows workflow from an immutable published Source v6
surface to a tested MRS3 strategy result:

```text
published v6 surface -> v6-backed analysis run -> READY JSON
-> verified tester batch/inbox -> reconciled Performance DuckDB -> DD5 -> final shortlist
```

Source v6 PnL, DD and Profit Factor are source diagnostics. They are never
presented as tested MRS3 strategy results.

## 2. Scope and non-goals

In scope:

- one explicit adapter from a selected frozen v6 surface/scope/READY interval to
  the existing analysis semantics;
- append-only v6-backed analysis runs, deterministic strategy JSON provenance,
  tester-batch linkage and Performance DuckDB/DD5 linkage;
- a Windows panel handoff that keeps the chain visible without manual database
  surgery or copying opaque IDs.

Out of scope:

- modifying Source v6 HTML normalization, stitching, surface facts or their
  identities;
- migrating v6 data into v5 or adding a central v6 catalog/fact mirror;
- portfolio/margin/MAE/MFE work, a queue/service/ORM/new dependency, or a DD5
  tick retest;
- Debian analysis, JSON generation, tester or DD5 execution. Debian is strictly
  `HTML -> fresh v6 DB -> optional surface/report`.

## 3. Ownership decision

Each immutable Source v6 surface owns compatible append-only analysis runs.
This operationalizes ADR-0010: frozen facts and their analysis evidence remain in
one self-describing DuckDB file. The existing central Analysis DuckDB remains a
legacy reader/writer and is not a second v6 facts or runs store.

The chosen boundary avoids a duplicate v6 facts/events copy and post-failure
reconciliation. Local surface queries are bounded; a v6 library is rebuilt by
scanning the configured surface directory, not by a catalog. Cross-surface
comparison is deferred unless selected surface files can be read-only attached
without merging their facts.

### 3.1 Compatibility admission

A surface admits a v6-backed run only when one immutable manifest declares the
supported `surface_schema_version`, `metric_schema_version`,
`event_schema_version`, `readiness_schema_version`, frozen-facts digest
algorithm/version and `event_mode=real_independent_events`.  The manifest must
contain its `surface_id`, `manifest_sha256` and `frozen_facts_sha256`; the
reader recomputes the declared digest over the frozen tables before admission.
The selected scope and every point used by the adapter must have the same
declared versions.  Unknown, absent or mixed versions fail closed.  A legacy
surface or a v6 surface that cannot meet this whole tuple is readable only by
its legacy/diagnostic path, never by the v6 writer.

## 4. Inputs

An analysis start request supplies exactly:

- published surface path, `surface_id`, manifest SHA-256 and frozen-facts digest;
- one `symbol|side|timeframe` READY scope and one fully contained UTC interval;
- canonical `AlgorithmConfig`, algorithm version and their hash;
- a listing-date snapshot and hash.

The surface adapter admits only the selected scope and interval. It rejects a
missing/invalid manifest, frozen-digest mismatch, absent readiness, gap,
unsupported metric/version, missing exact events, or a request spanning scopes.

Publication-time diagnostics created with `AlgorithmConfig.defaults()` are not an
analysis run, READY authority or strategy-JSON input.

The selected interval is canonical UTC `[start, end)`, is non-empty, falls
wholly inside exactly one READY interval of exactly one selected scope, and
cannot be widened to fragment/report bounds.  Scope fields are canonicalised as
`symbol`, `side`, `timeframe`; no unordered multi-scope request is accepted.

### 4.1 Run identity and idempotency

`analysis_run_id` is SHA-256 over canonical, sorted-key UTF-8 JSON with a
versioned identity label and exactly: `surface_id`, `manifest_sha256`,
`frozen_facts_sha256`, compatibility-version tuple, canonical selected scope,
half-open selected interval, `event_mode`, algorithm version, canonical
algorithm-config hash, and listing-date snapshot hash. It excludes surface path,
DuckDB bytes, wall-clock timestamps and publication-time defaults. Duplicate
semantic requests resolve to the same committed run; a collision with different
stored canonical identity bytes is an integrity failure, never a replacement.

## 5. Event-mode invariant

A v6-backed analysis run has exactly `event_mode=real_independent_events`.
Every point persists its exact distinct event IDs and satisfies:

```text
point_event_count == len(distinct point event IDs)
plateau_event_count == len(union of member point event IDs)
```

`legacy_trades_proxy` remains readable only in legacy storage. It cannot supply
rows, events, aggregates, comparisons, candidates or lineage to a v6-backed run.
Mixed-mode input fails before analysis.

An admitted v6 event ID is a stable source-v6 event identifier owned once by its
canonical facts. The adapter must retain the exact distinct IDs and a canonical
sorted-ID hash for every point. Only IDs whose event time belongs to the
selected `[start, end)` interval may enter it; duplicate IDs are deduplicated
before point and plateau unions. Missing IDs, an ID/hash disagreement, unknown
event schema or an event outside the selected interval reject the run.

### 5.1 Exact adapter mapping

The adapter builds the existing `PipelineInput(surface_id, points)` with no
implicit metric defaults. For every selected canonical point it maps:

| Frozen v6 field | Pipeline point field | Rule |
| --- | --- | --- |
| `point_key` | `point_id` | Preserve the full v6 key; do not parse it through the legacy six-field identity. |
| point facts `symbol`, `side`, `timeframe`, `shift_bp`, `open_ma_length`, `close_ma_length` | same scope fields, `shift_pct=shift_bp/100`, `open_ma`, `close_ma` | Typed canonical values only. |
| `TotalPnLPercent` | `pnl_pct` | Finite numeric value required; diagnostic only, never tester PnL. |
| `MaxEquityDrawdownPercent` | `dd_pct` | Finite numeric value required. The legacy-compatible alias `MaxDrawdownPercent`, if retained in a frozen schema, must be proven equal to this metric before mapping. `MaxRealizedDrawdownPercent` remains separately persisted audit evidence and cannot substitute. |
| `TotalTrades`, `Win`, `Los`, `WinRate`, `ProfitFactor` | `trades`, `wins`, `losses`, `win_rate_pct`, `profit_factor` | Typed exact values required; `ProfitFactor=null` is preserved only for its defined no-loss case. |
| exact `event_ids`, canonical event-ID hash, `point_event_count` | private `_event_ids`, `event_ids_hash`, `point_event_count` | Exact membership is mandatory; `point_event_count` must equal the distinct ID count. |
| selected READY interval | `report_start`, `report_end` | Convert the selected UTC `[start,end)` only; never derive min/max fragment bounds. |
| manifest event mode | `event_mode` | Must be exactly `real_independent_events`; a caller cannot override it. |

`TotalPnL`, `TotalPnLPercent`, both drawdowns, Profit Factor and exact trade
evidence remain attached to the immutable v6 run for audit. Fields not listed in
this mapping, a malformed number, an unavailable primary metric, a missing
canonical-grid point or an unsupported v6 metric schema reject the adapter with
a visible machine-readable reason. No v6 mapping may invent a `multiplier`,
replace events with trade counts, or use a publication-time diagnostic result.

## 6. Analysis-run state and immutability

A v6 analysis area uses append-only state:

```text
REQUESTED -> VALIDATED -> RUNNING -> COMMITTED
                              \-> FAILED | CANCELLED
```

Before and immediately before commit, the writer validates `surface_id`, manifest
hash, frozen-facts digest, schema/metric/event/readiness versions and selected
scope/interval. One transaction inserts a whole run or rolls back. No operation
updates the manifest, frozen point facts, frozen fragments or frozen events.

`analysis_run_id` is derived from the semantic inputs: surface ID, manifest hash,
frozen digest, selected scope/interval, algorithm version/config hash and
listing-date snapshot hash. It must not depend on DuckDB bytes, paths, timestamps
or publication-time defaults.

Persisted run evidence includes canonical identity bytes, compatibility tuple,
input hashes, state timestamps, eligibility/refine/plateau results, exact event
unions, before/after reasons, CloseMA profiles, BASE/1–4ORD/READY lineage and
read-only bridge/tail diagnostics. A transaction creates one immutable run
record plus its complete output, or no record. Revalidation failure rolls back
the attempted output and records a separate append-only failed/cancelled attempt
with reason and attempt ID; it cannot alter a committed run. A later retry is a
new attempt that either resolves to the existing identical committed run or
commits the same deterministic run once. The surface contains no mutable
``current`` pointer.

## 7. Strategy, tester and DD5 provenance

READY JSON and its generation manifest carry: source surface ID, surface
manifest hash, frozen-facts digest, compatibility tuple, selected scope/interval,
analysis run ID, canonical analysis identity/config/listing-date hashes,
candidate identity, generator schema/version and per-JSON SHA-256. The generator
accepts only a committed run whose stored identity agrees with the still-verified
surface. The panel displays these values and the READY count for an explicit user
confirmation before a tester run.

The tester plan and batch manifest record the analysis-run ID, the full source
tuple above, generation-manifest SHA-256 and the exact set of strategy JSON
hashes. The immutable inbox records its batch ID, copied report/strategy hashes
and the same batch manifest SHA-256. Performance import persists the inbox and
batch IDs plus verified hashes; it rejects a cross-batch strategy or any mismatch
with the analysis provenance tuple. A final row becomes `tested` only after the
matching immutable tester inbox is committed and reconciled with
`quarantined_count=0` in Performance DuckDB. Runner CSV is audit/parity evidence
only.

DD5 reads committed zero-quarantine Performance DuckDB evidence only, remains
`CALCULATION_ONLY`, and does not constitute a second tick test. The final shortlist
joins source surface, analysis run, JSON, tester batch/inbox, Performance import
and DD5 result by their recorded IDs/hashes; it rejects a missing, mismatched or
cross-surface join. Non-Source artifacts remain in their existing strategy,
tester-inbox and Performance DuckDB stores; only immutable references are copied
into the v6 surface run, so no v6 mirror or catalog is created. Missing or
reconciled-false evidence is visibly untested.

## 8. Windows panel contract

The Source v6 surface library exposes a selected surface to `Analyze v6 surface`.
The action collects its required config/date/scope inputs, shows truthful progress
and cancellation states, and on commit selects the resulting v6-backed run in the
analysis/shortlist/strategy workflow. Errors remain visible and actionable; no
cosmetic timer may stand in for work progress.

Existing legacy/DUCKDB_DIRECT analysis remains readable and behaviorally
unchanged. It is never silently combined with a v6-backed run.

## 9. Acceptance evidence

1. A frozen fixture surface maps deterministically to one analysis input; a
   cross-surface request, incompatible schema/version tuple, tampered
   manifest/frozen digest, event ID/hash, readiness or interval fails closed.
2. A v6-backed run persists/reopens deterministically; canonical rerun is
   idempotent, a retry cannot overwrite history, and no run can alter frozen
   surface facts.
3. A legacy-proxy or mixed-mode fixture is rejected before analysis, while
   legacy storage remains readable outside the v6 path.
4. Panel evidence covers the complete fixture chain from v6 HTML import to
   READY JSON, tester batch/inbox, zero-quarantine Performance import, DD5 and
   final tested shortlist.
5. A wrong JSON/generation-manifest hash, tester batch, inbox/Performance
   provenance tuple, quarantine result, event mode or gap is
   rejected before it can be labelled tested.
6. Debian bundle evidence ends at v6 import/surface/report and contains no
   analysis/tester/DD5 capability.

## 10. Start gate

The predecessor/start gate is historical for this working tree: implementation
evidence is now present, but release acceptance still requires reproducible
committed-tree review and the real Windows fixture chain.
