# Source v6 Analysis Handoff Implementation Plan

**Status:** Implementation evidence complete; acceptance gates remain
**Date:** 2026-08-19
**Reviewer corrections:** Task 1 and Task 6 each have `CODE_REVIEW_PASS` from their final reviewer calls. Overall handoff acceptance remains open.
The prior non-PASS wording for Task 6 is superseded by this correction.
The latest full-suite count supersedes earlier results: `1148 passed, 2 skipped, 1 warning`, with clean `git diff --check`.
Task 4 panel-to-tester generation/confirmation evidence is covered by a focused cross-module run (`338 passed, 1 skipped`); the bounded duplicate-DOM-input fix has `CODE_REVIEW_PASS`, but whole Task 4 acceptance remains open. Real timestamp normalization/import evidence is also confirmed: `1480` actions normalized and COMMITTED Debian artifacts produced.
**Review status:** Implementation evidence is complete for the currently exercised slices. Full suite: `1148 passed, 2 skipped, 1 warning`; `git diff --check` is clean. Task 1 and Task 6 have `CODE_REVIEW_PASS`; Task 4 has a bounded-fix `CODE_REVIEW_PASS`, not whole-task acceptance. The available fixture still produced no canonical READY intervals (and zero Plateau rows), blocking valid analysis/JSON/tester flow. Overall handoff acceptance remains open.
**Predecessor:** [Source v6 stitched surfaces](2026-08-18-source-v6-stitched-surfaces.md)
**Normative specification:** [Source v6 analysis handoff](../../specs/2026-08-19-source-v6-analysis-handoff.md)

**Start gate:** Task 1 may begin only after the predecessor's scoped changes are
committed, independently reviewed and reproducible from the committed tree.
This plan must not absorb an unreviewed predecessor diff into its first scoped
commit.

## Goal

Close the Windows production path without changing the Source v6 import
contract:

```text
raw HTML -> Source v6 DB -> stitched v6 surface -> immutable analysis run
-> READY JSON -> tester plan/run -> immutable tester inbox -> reconciled
Performance DuckDB evidence -> DD5 -> final shortlist
```

Every final row must retain its Source v6 surface identity, manifest hash,
analysis-run identity, generated JSON identity and tester batch identity.

## Ponytail boundary

Use Ponytail `full`: make the smallest integration of already-existing flows.

- Debian remains **import-only**: HTML -> fresh v6 DB -> optional surface and
  Plateau report. It never creates JSON strategies, starts the tester or runs
  DD5.
- Reuse the existing Source v6 DuckDB surface format, `PipelineInput`, analysis
  storage, analysis/shortlist/strategy APIs, tester runner and DD5 workflow.
- Do not migrate v6 facts into v5, rewrite HTML import, add a service, queue,
  ORM, catalog database, new dependency, portfolio/margin logic or speculative
  strategy metrics.
- Do not alter frozen Source v6 facts. Analysis runs are append-only and refer
  to a published surface by identity and manifest hash.
- Add only the narrow adapter, storage fields and panel endpoints strictly
  required for this handoff. Existing legacy/DUCKDB_DIRECT flows remain
  readable and behaviorally unchanged.

## Global execution rules

Each task follows the repository order:

```text
narrow failing test -> RED -> minimum implementation -> focused GREEN
-> relevant broader GREEN -> git diff --check -> scoped diff inspection
-> independent Terra review -> fix/retest/re-review -> one scoped conventional commit
```

Run every test through `.venv\\Scripts\\python.exe -m pytest`. Do not commit
local HTML, DuckDB files, generated surfaces/reports, `Input/`, `Output/`, or
local tester configuration.

---

## Task 0: Freeze the v6-to-analysis contract

- [x] Create an active specification defining the new handoff, inputs/outputs,
  non-goals, state machine and acceptance evidence.
- [x] Decide and document whether the existing central analysis DuckDB stores
  v6-backed analysis runs, or whether a v6 surface owns compatible append-only
  runs. Select one path; do not support two competing writers. The specification
  must compare both options against analysis-storage compatibility, immutable
  source consistency, query/scan performance, migration cost, legacy-read
  compatibility, operational recovery and the concrete follow-up work each
  option entails; record the rationale in an ADR when it changes the data
  ownership boundary.
- [x] Define immutable provenance fields: `source_surface_id`,
  `source_manifest_sha256`, selected READY scope/interval, `analysis_config`
  hash, `analysis_run_id`, strategy JSON hash/version and tester batch ID.
- [x] Define the exact mapping from frozen v6 point metrics/events to the
  existing pipeline inputs, including metric names, event mode and unsupported
  values; no silent defaulting.
- [x] Fix v6-backed runs to the explicitly named `real_independent_events`
  event mode (or reject the handoff if the frozen contract proves otherwise).
  A run must never mix that mode with `legacy_trades_proxy`; legacy runs remain
  separately readable but cannot contribute rows, events or aggregates to a
  v6-backed run.
- [x] Define the role of listing dates and analysis config. Their hashes must
  be captured in the analysis run; publication-time `AlgorithmConfig.defaults()`
  cannot become a hidden production selection configuration.
- [x] State the final-list contract: only strategies with a verified tester
  batch and DD5 result may appear as tested; Source metrics remain diagnostic.
- [x] Record that Debian is import-only and its artifact handoff is a published
  v6 surface, not analysis/tester output.
- [x] Update PRD/progress with the approved scope; obtain independent review
  PASS before Task 1.

**Acceptance evidence:** a fixture v6 surface and a written field-by-field
mapping can be independently replayed into one deterministic analysis input.

## Task 1: Adapt one frozen v6 surface into a first-class analysis input

- [x] Use `load_source_v6_pipeline_input` (or a narrow replacement) to read
  only the published v6 surface, validate its manifest/frozen digest, and
  construct the existing `PipelineInput` without reopening Source v6 HTML/DB.
- [x] Require an explicit selected READY Pair+Side+TF interval; reject a gap,
  hash mismatch, unsupported event mode, incomplete point grid or absent
  required metric with a visible reason.
- [x] Preserve exact event membership/count, point identity, selected interval,
  tail/overlap diagnostics and immutable source identifiers in the adapter.
- [x] Cover JSON and DuckDB v6 surface readers if both remain public; otherwise
  fail closed for the retired reader rather than silently diverging.

**Tests:** valid v6 surface -> deterministic input; tampered manifest/frozen
facts rejected; same facts in reordered surface input keep identity; missing
coverage/event/metric fails with a precise code.

## Task 2: Publish an append-only v6-backed analysis run

- [x] Add one explicit operation that runs existing eligibility/refine/plateau/
  selection logic from Task 1's input and the user-selected config/dates.
- [x] Persist run metadata and complete output through the chosen Task 0 storage
  boundary. The run must reference all Task 0 provenance hashes.
- [x] Retain before/after rejection reasons, CloseMA profiles, BASE/1вЂ“4ORD/
  READY lineage, exact event unions and read-only tail/bridge diagnostics.
- [x] Refuse to append if the surface manifest/frozen hash changed before or
  during the transaction; never update frozen surface facts.
- [x] Make analysis library/detail/export discover v6-backed runs while keeping
  legacy rows readable.

**Tests:** same surface/config/dates is deterministic; altered config/dates or
surface hash creates a distinct run; run survives reopen; tampering is rejected;
legacy library/run behavior remains green.

## Task 3: Connect Source v6 and Analysis in the Windows panel

- [x] Add `Analyze v6 surface` to the Source v6 workflow, with surface-library
  selection, analysis config, listing dates and chosen READY scope/interval.
- [x] On success, populate the existing Analysis section with the resulting
  `surface_id`/`analysis_run_id`, status and provenance вЂ” never ask the user to
  copy an opaque ID between unrelated flows.
- [x] Add truthful long-job lifecycle: start/status/cancel semantics, real work
  units and recoverable failure states for import and analysis. Do not use a
  cosmetic timer as evidence of progress.
- [x] Surface all validation failures (gaps, stale preflight, invalid config,
  unsupported v6 mapping, manifest mismatch) in the status log.

**Tests:** panel integration from fixture HTML through published surface into a
v6-backed analysis run; cancellation/stale-token/error lifecycle; no regressions
to legacy analysis controls.

## Task 4: Generate READY JSON with end-to-end provenance

- [x] Let the existing shortlist and strategy generator consume the v6-backed
  analysis run without a legacy materialization step.
- [x] Generate only READY candidates and carry Task 0 provenance into strategy
  JSON and its generation manifest.
- [x] Populate the tester strategy directory from the successful generation;
  before tester execution require a user confirmation that displays the source
  surface ID/manifest hash, analysis run/config hash, READY strategy count and
  generated JSON manifest hash.
- [x] Reject generation when source surface/run/config hashes disagree or when
  no READY candidate exists.

**Tests:** v6 run -> deterministic READY JSON; JSON/manifest contain all
provenance; non-READY and hash-mismatched candidates are excluded; existing
legacy JSON generation remains compatible.

## Task 5: Bind tester and DD5 to the v6 provenance chain

- [x] Extend tester plan/run manifests to record the originating v6-backed
  analysis run and generated JSON hashes.
- [x] Require each completed tester batch to produce an immutable inbox with
  report HTML, strategy JSON and hashes; preserve existing tester
  resume/collision safety.
- [x] Import that inbox into Performance DuckDB and require committed,
  reconciled, zero-quarantine evidence before DD5. A runner CSV is audit/parity
  evidence only and is never sufficient DD5 input.
- [x] Produce a final shortlist/report that joins Source surface, analysis run,
  strategy, tester batch/inbox, Performance DuckDB import and DD5 evidence.
  Mark untested or unreconciled candidates as such; never present Source PnL as
  tested strategy PnL.
- [x] Keep DD5 calculation-only semantics and do not imply a DD5 tick retest.

**Tests:** mocked tester plan/run plus a reconciled zero-quarantine Performance
DuckDB import reaches final shortlist; wrong batch/JSON hash/missing inbox or
quarantine is rejected; CSV-only data cannot enter DD5 or cross batches.

## Task 6: Keep Debian import-only and prove the boundary

- [x] Make the v6 bundle's documented command recursive or explicitly reject
  nested-report expectations; retain fresh-v6 safety and per-file result output.
- [x] Emit a machine-readable import/surface handoff manifest with relative
  paths, surface ID/hash, coverage and failure/safe-delete results.
- [x] Do not add analysis, JSON, tester or DD5 code to the Debian bundle.
- [x] Test the generated bundle on the fixture flow: import -> stitch ->
  optional surface/report -> handoff manifest.

## Task 7: Final end-to-end evidence and rollout documentation

- [ ] Execute the real Windows fixture flow:
  `HTML -> v6 surface -> analysis run -> READY JSON -> tester plan/run -> Performance DuckDB import -> DD5 -> final shortlist`.
- [ ] Verify all identities/hashes form one reproducible lineage and that the
  final list contains only reconciled zero-quarantine tester evidence.
- [ ] Run focused, relevant broader and full `.venv` suites; run
  `git diff --check`; complete independent Terra review.
- [ ] Update the active specification, PRD, progress and public launch/runbook
  only after acceptance. State Debian's import-only boundary plainly.

## Task 8: Fresh-only compact Source v6 and parallel multi-scope surfaces

**Status:** proposed replacement-format work; not accepted and no production
format change is implied by the earlier Task 1вЂ“7 evidence.

- [ ] Create a superseding active specification and ADR before implementation.
  The new entry points are **fresh-only**: they accept only the new Source DB
  and surface format, with no v3/v4/v5 migration, dual reader, or legacy
  compatibility mode. Raw HTML is re-imported. Existing historical artifacts
  are not deleted as part of this task.
- [ ] Keep one published surface file for the complete, user-preflighted
  selection of Pair+Side+Timeframe scopes. Within that file, plateau/refine
  processing is isolated by `(symbol, side, timeframe)`; it must not emit one
  physical file per CloseMA or per scope.
- [ ] Preserve every factual eligible cell in each selected scope:
  canonical shifts Г— available OpenMA Г— available CloseMA. A complete
  OpenMA witness is readiness/coverage evidence only and must never filter the
  factual grid used for Shift/OpenMA/CloseMA neighbour geometry, rejection
  audit, plateau construction, or refinement.
- [ ] Benchmark and implement deterministic parallel surface construction.
  Read-only workers may decode/materialize independent point work; a bounded,
  canonically ordered parent remains the sole publisher/writer and verifies
  the semantic digest before atomic publication. Measure 1/4/8/16 workers on
  real reports, including wall time, peak memory, output and WAL size.
- [ ] Benchmark and implement deterministic parallel analysis by complete
  `(symbol, side, timeframe)` logical surface. A worker must receive the full
  scope so it retains Shift/OpenMA/CloseMA neighbours; the parent performs a
  stable merge and append-only publication. Demonstrate scaling using at least
  two complete real scopes and prove equality of rows, reasons, plateaus,
  provenance and hashes between one worker and the selected worker count.
- [ ] Compare compact lossless Source layouts on real HTML (fragment-level and
  interval-addressable sample chunks) against the current format for import
  rate, final DB/WAL size, selected-window surface-build rate, surface size,
  surface-load rate and analysis rate. Select the format only after complete
  reconstruction and frozen-contract compatibility checks pass.

**Acceptance evidence:** full factual-grid neighbour tests; one-file
multi-scope publication test; fresh-only rejection tests; 1-vs-N semantic
digest equality; real-report benchmark matrix for import, surface build,
surface load and analysis; all required source facts/events/coverage and
reproducibility hashes retained.

## Definition of Done

- [ ] A Windows user can complete the full v6 chain in the panel without
  manual database surgery or copying opaque IDs.
- [ ] Every final tested strategy resolves back to immutable v6 and tester
  provenance.
- [ ] A v6 gap/tamper/unsupported metric fails before JSON generation.
- [x] Debian delivers only import artifacts and never claims tester capability.
- [x] Legacy workflows remain available and their regression suite stays green.
