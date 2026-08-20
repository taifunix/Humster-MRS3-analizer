# Source v6 Stitched Surfaces Implementation Plan

**Status:** Historical вЂ” superseded for fresh compact artifacts by the 2026-08-20 contract; retained as evidence
**Date:** 2026-08-18
**Normative specification:** [Source v6 and stitched surfaces](../../specs/2026-08-18-source-v6-stitched-surfaces.md)
**Decision:** [ADR-0010](../../decisions/0010-source-v6-stitched-facts-and-surface-files.md)

## Goal

Build a fresh compact Source DuckDB v6 from disposable HTML, safely stitch
compatible Fixed-lot report periods, calculate correct point PnL/DD/trade
metrics over selected continuous intervals, and publish every surface as a
separate DuckDB file that accepts append-only analysis runs and a Plateau
report.

## Ponytail boundary

This plan deliberately chooses the smallest architecture that meets the
accepted contract:

- no v5 migration or generic migration framework;
- no pair deletion/compaction;
- no service process or external catalog database;
- no universal conflict-rule engine;
- no ORM, queue or new dependency;
- no speculative MAE/MFE, margin or portfolio implementation;
- reuse DuckDB, the current HTML parser/import job pattern, direct coverage,
  published-surface readers and analysis pipeline;
- one new Source v6 storage/resolution module is allowed only to avoid mixing
  v5 report-replacement semantics into v6.

The surface library initially scans a configured directory and reads each
file's internal manifest. Add a persistent catalog only if measured scan time
becomes unacceptable.

## Global execution rules

Each implementation task is one bounded change:

```text
narrow failing test
-> verify RED
-> minimum implementation
-> focused GREEN
-> relevant broader GREEN
-> git diff --check
-> inspect scoped diff
-> independent native Terra Reviewer (`gpt-5.6-terra`, medium)
-> fix/retest/re-review if required
-> one scoped conventional commit
```

Per `AGENTS.md`, the root owns implementation packets, integration,
verification and acceptance; this project does not invoke DeepSeek. The
independent Reviewer is the native Terra route specified in `AGENTS.md`.

Do not stage or commit real HTML, DuckDB files, local paths, `Input/`, `Output/`,
review scratch files or generated surface/report artifacts.

---

## Task 0: Accept the contract and freeze golden evidence

### Purpose

Resolve facts that must come from real reports before schema/code is allowed to
guess them.

### Files

- `docs/decisions/0010-source-v6-stitched-facts-and-surface-files.md`
- `docs/specs/2026-08-18-source-v6-stitched-surfaces.md`
- small synthetic/golden fixtures under `tests/fixtures/` only when sanitised
- focused contract tests, initially expected in `tests/test_source_v6.py`

### Checklist

- [x] Confirm the exact report-header start/end fields and UTC interpretation.
- [x] Confirm `Initial balance`, first wallet sample, `walletSeries`,
  `equitySeries` and `UPNL = equity - wallet` on 3-5 representative reports;
  real AAOI reports confirm Max Drawdown is peak-to-trough `equitySeries`,
  not `walletSeries`/Balance.
- [x] Action-column PnL excludes fees and is the sole Profit Factor input;
  net Total PnL comes from the canonical stitched Balance series. The frozen
  metric algebra (spec section 8, ADR-0010) records this without adding
  commission models.
- [x] At least one golden fixture carries a non-zero fee/adjustment so the
  implementation proves the intentional distinction: Balance-derived net PnL
  includes fees/adjustments, while action-PnL Profit Factor excludes fees.
- [x] Confirm normalised fields for action/cycle identity, partial decreases and
  a cycle open at report end.
- [x] Publish `execution_compatibility_fingerprint_v1`: exactly Pair, TF, Side,
  Shift, Open/Close MA type/source/length, report initial balance, fixed order
  balance and active-side balance percentage; typed canonical JSON + SHA-256,
  with missing/mismatch fail-closed and secondary settings non-blocking.
- [x] Confirm fully closed boundary-cycle policy for a cycle opened before a
  manually selected surface interval.
- [x] Add one sanitised fixed-lot overlap pair including a tail cycle and one
  incompatible/non-stitchable pair.
- [x] Confirm the legacy import path remains permissive for
  `use_upnl=true` / `use_fix=false` reports with a sanitised regression test.
- [x] Accept ADR-0010 and the normative specification after independent review.
- [x] Update `PRD.md` registry and `progress.md`; do not rewrite old ADRs/specs.

### Acceptance

- [x] A small reference calculation independently reproduces report PnL,
  `Max Drawdown, %`, Profit Factor and trade count for the golden full period.
- [x] The accepted documents contain no unresolved formula or identity field.

**STOP (provisional):** Initial Task 0 evidence permits bounded implementation;
final Task 0 acceptance remains pending the missing evidence listed by the Terra
audit.

---

## Task 1: Parse one HTML into one normalised v6 fragment

### Purpose

Reuse the existing structural parser and produce a pure in-memory fragment;
write no database yet.

### Likely files

- `src/mrs3/duckdb_import.py`
- one minimal new `src/mrs3/source_v6.py`
- `tests/test_source_v6.py`

Do not change `programs/РћР±СЂР°Р±РѕС‚С‡РёРє HTML-DuckDB/*`; it remains historical v3/v4
runtime.

### Checklist

- [x] Parse exact point identity including MA type/source/length.
- [x] Parse header interval, Fixed-lot settings and compatibility fingerprint.
- [x] Parse actions/cycles, exact independent events and open-tail state.
- [x] Parse synchronized wallet/equity samples and normalise to scaled integer
  or exact Decimal-safe values.
- [x] Calculate stable fragment, point, cycle and event IDs from canonical
  logical content, never file path or traversal order.
- [x] Classify malformed and UPNL-sized reports fail-closed.

### Acceptance

- [x] Windows paths and Debian paths produce identical canonical hashes.
- [x] Row/header ordering variations covered by the HTML contract do not change
  the fragment.
- [x] No HTML bytes are required after successful normalisation.

Evidence: `.venv\\Scripts\\python.exe -m pytest tests/test_source_v6.py tests/test_performance_import.py -q`
(`57 passed` in the current focused run), including source-name/path independence and reordered action rows.

---

## Task 2: Create and transactionally import a fresh Source v6

### Purpose

Persist only the minimum normalised facts and safe-deletion evidence.

### Likely files

- `src/mrs3/source_v6.py`
- `src/mrs3/duckdb_import.py`
- `tests/test_source_v6.py`
- `tests/test_duckdb_import.py`

### Checklist

- [x] Create only a new v6 database; reject v4/v5 as a v6 target.
- [x] Store database ID, schema/fingerprint and monotonic mutation generation.
- [x] Store point, compact fragment metadata, actions/cycles, exact source
  events and
  compressed wallet-change/UPNL samples.
- [x] Store active/inactive UTC-day ownership without duplicating identical
  normalised content; day-level markers never override exact `incoming_start`
  ownership at partial-day boundaries.
- [x] Make same-hash re-import idempotent.
- [x] Bind Start Import to database ID, generation and preflight token.
- [x] Commit all facts and import audit in one transaction.
- [x] Emit `safe_to_delete=YES` only after readback, hashes/counts and zero
  quarantine for that HTML.

### Acceptance

- [x] Forced failure/cancel leaves the prior database unchanged and HTML unsafe
  to delete.
- [x] A successful import can be reopened and reconstruct every golden fragment
  without HTML.

Evidence: `.venv\\Scripts\\python.exe -m pytest tests/test_source_v6_storage.py -q`
(`5 passed`).

---

## Task 3: Resolve overlap, bridge cycle and active timeline

### Purpose

Implement the one required precedence rule, not a generic resolver framework.

### Likely files

- `src/mrs3/source_v6.py`
- `tests/test_source_v6.py`

### Checklist

- [x] Existing active facts versus a compatible current import batch switch
  ownership at the exact `incoming_start` timestamp: facts strictly before it
  remain outgoing-owned, facts at/after it within the incoming interval become
  incoming-owned (`USE_INCOMING_DAY` only at UTC-midnight starts).
- [x] Require overlap `>= 96h` and separately require incoming start no later
  than the old tail-cycle open time.
- [x] Select and persist a bridge cycle atomically from incoming actions/samples/events.
- [x] Keep replaced day versions inactive with reason and winner reference,
  including the automatic `BRIDGE_NOT_COVERED` failure reason on
  unresolved/inactive bridge fragments.
- [x] Treat identical same-batch facts as one fact; conflicting ambiguous
  same-batch versions remain inactive and do not use filename/hash ordering as
  economic authority.
- [x] Implement `IGNORE_INCOMING` and `EXCLUDE_DAY_AS_GAP` as manual
  dispositions; add the automatic `BRIDGE_NOT_COVERED` failure reason only as
  a state/reason on unresolved/inactive fragments, not as a manual
  disposition.
- [x] Return `PARTIAL` only for a batch with committed and unresolved items.

### Acceptance matrix

- [x] Exact duplicate import: idempotent.
- [x] Compatible 96h overlap, no tail: incoming overlap plus extension active.
- [x] Mid-day incoming start (for example 14:00 UTC) with non-empty outgoing
  activity before the boundary and no open tail: every outgoing fact before
  `incoming_start` retained, ownership switches at the exact timestamp, no
  data loss.
- [x] 96h overlap with tail opened inside incoming: incoming cycle restored.
- [x] 96h overlap with earlier tail: fragment remains unresolved/inactive with
  automatic reason `BRIDGE_NOT_COVERED`, batch `PARTIAL`, no silent stitch.
- [x] Different Fixed-lot contract: inactive/non-stitchable.
- [x] Legacy `use_upnl=true` / `use_fix=false` report remains importable as a
  non-stitchable legacy fact and never enters a stitched v6 surface.
- [x] Real missing day: exact gap retained.
- [x] Incoming numeric differences with valid contract: incoming authoritative,
  not a conflict.

---

## Task 4: Recalculate canonical interval metrics

### Purpose

Make PnL, DD and trade count correct before coverage or UI work.

### Likely files

- `src/mrs3/source_v6.py`
- reuse helpers from `src/mrs3/performance_metrics.py` where semantics match
- `tests/test_source_v6.py`

### Checklist

- [x] Build the canonical Balance series from the first report's initial fixed
  balance, rebasing each later report's Balance by its constant seam offset.
- [x] Rebase Equity by the same seam offset, preserving each sample's
  `Equity - Balance` (unrealised PnL).
- [x] Derive `TotalPnL` and `TotalPnLPercent` from the canonical Balance series.
- [x] Calculate ProfitFactor from positive/absolute-negative action-column PnL
  after overlap deduplication; emit `n/a` when no losing PnL actions.
- [x] Calculate `TotalTrades`, wins, losses and WinRate from admitted closed
  evidence owned once.
- [x] Calculate primary `MaxEquityDrawdownPercent` as running peak-to-trough
  over the stitched canonical Equity, percent at the trough's peak.
- [x] Calculate separate `MaxRealizedDrawdownPercent` from canonical Balance.
- [x] Truncate both series immediately before an unresolved open tail.
- [x] Restore and recompute after a covering incoming fragment closes the tail.
- [x] Calculate exact distinct event membership/count; never sum daily unique
  counts.
- [x] Fail visibly when a primary metric is unavailable; never substitute the
  realised DD for equity DD.

### Acceptance

- [x] One long golden report and its equivalent overlapping fragments produce
  identical PnL, both DD values, Profit Factor, trade count and event IDs.
- [x] Changing only a report's wallet offset does not change the result.
- [x] Results match an independent Decimal reference calculation.

---

## Task 5: Add READY coverage and exact gap export

### Purpose

Reuse current canonical CloseMA/Shift readiness on v6 facts and stop before any
missing-strategy generator.

### Likely files

- `src/mrs3/duckdb_direct.py`
- `src/mrs3/source_v6.py`
- `tests/test_duckdb_direct.py`
- `tests/test_source_v6.py`

### Checklist

- [x] Read coverage from report-header UTC days, including no-trade days.
- [x] Adapt the existing six-CloseMA/19-Shift readiness helper instead of
  creating another readiness algorithm.
- [x] Return continuous READY bounds per Pair+Side+TF after integrating the
  inherited six-CloseMA/19-Shift readiness grid.
- [x] Accept a user interval inside those bounds independently per selected
  Pair+Side+TF.
- [x] Exclude a timeframe with any required point/day gap in the requested
  interval.
- [x] Export exact missing point/day cells to stable CSV and JSON, with filters
  for all scopes or individual pair/timeframes.
- [x] Keep Source gap export separate from Refine missing-neighbour artifacts.

### Acceptance

- [x] UTC-midnight, leap/day boundary, empty-trade day and one deliberately
  missing canonical cell have exact expected results.
- [x] Export is deterministic and contains everything later strategy grouping
  needs, without implementing that grouping.

---

## Task 6: Publish one self-contained surface file per build

### Purpose

Replace central overwrite behavior with immutable frozen surface facts and a
directory scan library.

### Likely files

- `src/mrs3/analysis_storage.py`
- `src/mrs3/published_surface.py`
- `src/mrs3/panel.py` only for a thin library read API
- `tests/test_analysis_storage.py`
- `tests/test_published_surface.py`
- `tests/test_panel.py`

### Checklist

- [x] Use one configured surface directory; do not add a catalog DB.
- [x] Generate collision-resistant filenames containing UTC creation time and
  point count.
- [x] Store frozen point facts, exact events, per-scope intervals, overlap/tail
  decisions, versions and logical hashes in an internal manifest.
- [x] Publish staging -> validate/readback -> close -> atomic rename for the
  required DuckDB surface format.
- [x] Scan files and read manifests for the library and `РЎРІРµРґРµРЅРёСЏ`.
- [x] Ignore/diagnose staging, malformed and hash-mismatched files in the
  required DuckDB surface library.
- [x] Permit transactionally appended analysis runs while preventing updates to
  frozen surface tables.
- [x] Base identity on canonical facts, not file bytes/path/time/catalog order.

### Acceptance

- [x] Two builds create two selectable files and never overwrite each other.
- [x] Appending an analysis run leaves frozen surface hashes unchanged.
- [x] Reopen and directory reordering do not change surface identity or details.

---

## Task 7: Adapt analysis and export the Plateau report

### Purpose

Preserve accepted Phase 1 behavior and add only the retained Phase 2 audit facts.

### Likely files

- `src/mrs3/analysis_storage.py`
- `src/mrs3/analysis_exports.py`
- existing `plateau.py`, `selection.py`, `analysis_strategies.py` only where an
  input adapter or missing audit field is required
- corresponding focused tests

### Checklist

- [x] Read frozen v6 surface facts without reopening Source v6, including the
  published DuckDB surface format.
- [x] Preserve canonical geometry, `PointEventCount >= 3`, equivalence-before-
  event-filter, frozen CMA/BASE facts and current shortlist/Pareto semantics.
- [x] Persist exact BEFORE/AFTER rejected-state trail using the same geometry.
- [x] Persist complete CloseMA profiles including no-representative reasons.
- [x] Persist required Plateau counts, membership, spans, exact event union,
  BASE/1-4ORD/READY lineage and Refine requests.
- [x] Keep bridge/robustness diagnostics read-only and non-gating.
- [x] Export deterministic CSV artifacts and one `plateau_report.xlsx` with
  `Plateaus`, `Plateau Members`, `Before After`, `CloseMA Profiles`, `Lineage`
  and `Diagnostics`.
- [x] Generate the report from persisted facts, without analysis rerun.

### Acceptance

- [x] Existing accepted Phase 1 regression suites remain green through the new
  adapter.
- [x] Row permutation yields identical Plateau IDs/membership/profiles/READY and
  report content hashes.
- [x] Legacy surfaces remain readable where already supported but cannot enter
  new v6 operational builds.

---

## Task 8: Simplify the panel around the v6 workflow

### Purpose

Expose proven backend operations only after Tasks 1-7 are stable.

### Files

- `src/mrs3/panel.py`
- `tests/test_panel.py`

### Checklist

- [x] Steps 1-5 right panel: current action, one real progress bar, status log,
  then forming/selected surface information and library.
- [x] Wire truthful work-unit progress for Preflight, Start Import, Check
  Coverage and Build Surface.
- [x] Remove duplicate left-side status counters/messages.
- [x] Make available start/end dates active controls per Pair+Side+TF, bounded
  by READY coverage.
- [x] Add `РЎРІРµРґРµРЅРёСЏ`, gap downloads and Plateau-report download.
- [x] Remove legacy source-pack/MRS2 construction and manual legacy candidate
  controls from the operational panel.
- [x] Keep Test plan/tests/DD5 in steps 6-8 with its own right-side status; do
  not display its statistics on steps 1-5.

### Acceptance

- [x] Endpoint and DOM tests cover action lifecycle, cancellation, stale-token
  clearing, two surface files, details, interval validation and tab isolation.
- [x] No cosmetic timer is used as progress evidence.

---

## Task 9: Package the v6 Debian importer and run final evidence

### Purpose

Reuse the existing headless runner/bundle shape but replace its v5 runtime with
the accepted v6 entry point.

### Likely files

- `scripts/import_html_duckdb_debian.py`
- `scripts/import-html-duckdb-debian.sh`
- `scripts/build-debian-import-bundle.py`
- `debian-duckdb-importer/` generated only by its tracked builder policy
- focused Debian runner/bundle tests

### Checklist

- [x] Keep one Python command and one POSIX wrapper; no web framework.
- [x] Bundle only the v6 runtime closure and pinned minimum dependencies.
- [x] Use relative config/output paths and no workstation-local paths.
- [x] Produce identical canonical hashes on Windows and Debian fixtures.
- [x] Run a real fresh-v6 import -> overlap stitch -> coverage -> separate
  surface -> analysis -> Plateau-report smoke.
- [x] Measure import time and resulting DB size; report them without promising a
  speedup or compression ratio not demonstrated by the evidence.
- [x] Run relevant broader tests and `git diff --check`.
- [x] Complete independent reviewer and maintainer checks.
- [x] Update `progress.md`, PRD status and public launch docs only after the
  verified command actually works.

### Definition of done

- [x] Exact PnL, equity DD, realised DD, Profit Factor and trade count reconcile
  for a point stitched from at least two real compatible reports.
- [x] Open-tail exclusion and later restoration are evidenced.
- [x] A deliberate gap produces the exact missing export and excludes only the
  affected timeframe/requested interval.
- [x] Two surface builds remain separately selectable and self-describing.
- [x] An appended analysis exports a reproducible Plateau report.
- [x] HTML deletion remains gated by per-file `safe_to_delete=YES` evidence.
- [x] No v5 migration, pair deletion, margin/portfolio work or new diagnostic
  hard gate entered the implementation.

## Deferred ledger

- Pair deletion and physical compaction: add only after real v6 size/retention
  evidence shows worthwhile savings.
- Exchange in point identity: add before any database is allowed to mix
  exchanges.
- Missing-strategy generator: build later from the accepted gap CSV/JSON.
- Exact tick MAE/MFE, margin exposure and portfolio simulation: require their
  own input contracts.
- Persistent surface catalog: add only if directory-scan performance is
  measured as insufficient.
