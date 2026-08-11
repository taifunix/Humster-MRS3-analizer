# MRS3 v0.7 DuckDB Analysis Storage and Importer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Tasks 0–8 are complete; Task 9 is next. Task 6 was committed in `05369c2`; its
copied-real-HTML smoke established v3/v4/mrs3 adapter parity `PASS` and a
temporary v3 DuckDB import with `1` scanned, `1` imported and `0` quarantined
reports, `78` raw
actions, `646` equity samples and `79` wallet changes, without a production DB
write. Task 8 was committed in `b7dc23e`; its follow-up correction preserves
the raw input SHA for manifest/mutation evidence while the normalized v3/v4
semantic SHA drives report identity, `report_id`, deduplication and migration
compatibility. CRLF and LF inputs are identical under that semantic contract;
mutation and invalid UTF-8 fail closed. The related suite passes `66` tests.

**Goal:** Add a safely appendable source DuckDB, panel-managed HTML import,
immutable `DUCKDB_DIRECT` analysis surfaces and persistent plateau/candidate
lineage without changing legacy selection semantics.

**Architecture:** Migrate v4 out of place into a versioned source store with one
active payload per canonical report and immutable replacement audit. Materialize
bounded point facts into a separate analysis DuckDB; plateau algorithms consume
published surfaces and create independent analysis runs.

**Tech stack:** Python 3.11+, pandas, DuckDB, pytest, existing MRS3 CLI/panel and
v3/v4 compact HTML codecs.

## Global constraints

- The existing v4 database and HTML inputs are read-only during migration.
- Local paths, HTML, DuckDB files and generated reports never enter Git.
- Every task uses RED → minimal GREEN → focused regression → independent review
  → scoped conventional commit.
- `PointEventCount = TotalTrades` for every `DUCKDB_DIRECT` surface point.
- Plateau algorithm configuration belongs to `analysis_runs`, not `surface_id`.
- The [Optional CSV-DuckDB overlay](../../specs/2026-08-11-v07-optional-csv-duckdb-overlay.md)
  is explicitly outside this plan.

---

## External code-review remediation

### Task 0: Close the existing package-side and UTC slice

This blocking task finishes before Task 1. It must not absorb any DuckDB work.

**Files:**
- Modify only as required by the existing slice: `src/mrs3/loader.py`
- Modify only as required by the existing slice: `src/mrs3/package_loader.py`
- Modify only as required by the existing slice: `tests/test_loader.py`
- Modify only as required by the existing slice: `tests/test_package_loader.py`
- Modify: `progress.md`

- [x] Inspect the existing diff and remove no unrelated user changes.
- [x] Run `.venv\Scripts\python.exe -m pytest tests/test_loader.py tests/test_package_loader.py -q`.
- [x] Obtain independent review, apply only confirmed findings, rerun and re-review.
- [x] Update current evidence, run `git diff --check`, inspect the exact staged
  scope and commit separately: `fix: preserve package side and utc normalization`.

### Review triage

| # | Disposition | Evidence / action |
| --- | --- | --- |
| 1 | Confirmed in `loader.py` and `eligibility.py` | Fractional integer fields are truncated by `astype("int64")`; add exact-integer guards. `source_packs.py` already rejects fractional trades and is unchanged. |
| 2 | Confirmed audit defect | Only genuine pandas `NA`/Python `None` may become `0` for legacy compatibility. Blank strings, non-empty malformed, negative, fractional and non-finite wins/losses raise `InputError`. |
| 3 | Confirmed audit defect | Plateau Library member-ID columns must use the same predicates as annotated eligibility. Selection itself already uses the annotated flags. |
| 4 | Confirmed edge case | Empty/all-service input must raise `InputError`, never reach `iloc[0]`. |
| 5 | Rejected as stated | `prepare_batch_files` checks planned and staged hashes before cleanup/install and has rollback tests; it does not install changed input before validation. |
| 6 | Intentional contract | Unknown post-test strategies remain fail-closed because no audited lots/JSON lineage exists. |
| 7 | Confirmed common-window gap | Compatibility raw CSV can mix periods and `_pair_history` can report a non-existent composite interval. Reject mixed data windows. |
| 8 | Confirmed helper/audit defect | `build_plateaus` must not publish eligible-point count as a true event union; package pipeline union remains authoritative and legacy remains `N/A_LEGACY_PROXY`. |
| 9 | Confirmed UX defect | Explicit config `null` must raise a stable field-specific `ValueError`, not incidental `InvalidOperation`/`AttributeError`. |
| 10 | Harmless cleanup | Remove the unused `_side_keys` sign only if that function is touched by another scoped task. |
| 11 | Intentional contract | Persistent lock inode is race-safe and explicitly tested; do not delete it on unlock. |
| 12 | Stale finding | Only `src/mrs3/lots.py` exists at runtime; do not create another lots module. |

### Task 1: Reject lossy loader values and empty normalized input

**Files:**
- Modify: `src/mrs3/loader.py`
- Test: `tests/test_loader.py`

**Interface:** Keep `load_points(...) -> tuple[pd.DataFrame, InputAudit]`; all
validation failures use `InputError` before constructing point IDs.

- [x] Add parameterized RED tests for fractional/non-finite `run_id`, `open_ma`,
  `close_ma` and `trades`.
- [x] Add RED tests proving only genuine pandas `NA`/Python `None` becomes `0`;
  blank strings, non-empty nonnumeric, negative, fractional and non-finite
  wins/losses raise `InputError` before coercion.
- [x] Add RED tests for zero rows and all-service rows.
- [x] Run RED: `.venv\Scripts\python.exe -m pytest tests/test_loader.py -q`.
- [x] Add one reusable exact-integer parser, then guard `data.empty` before any
  event-mode `iloc[0]`.
- [x] Run GREEN and `tests/test_source_packs.py`; the latter must remain unchanged.
- [x] Review and commit: `fix: reject lossy loader values`.

### Task 2: Validate eligibility event counts

**Files:**
- Modify: `src/mrs3/eligibility.py`
- Test: `tests/test_eligibility.py`

- [x] Add RED tests for numeric/text fractional, negative and non-finite
  `point_event_count` reaching `annotate_eligibility` directly.
- [x] Run RED: `.venv\Scripts\python.exe -m pytest tests/test_eligibility.py -q`.
- [x] Reuse the exact non-negative integer rule before casting; preserve legacy
  proxy behavior.
- [x] Run GREEN, review and commit: `fix: validate eligibility event counts`.

### Task 3: Enforce one raw-CSV period and coherent pair history

**Files:**
- Modify: `src/mrs3/loader.py`
- Modify: `src/mrs3/pipeline.py`
- Test: `tests/test_loader.py`
- Test: `tests/test_pipeline.py`

- [x] Add RED raw-CSV tests with two distinct `(report_start, report_end)` pairs;
  expect `InputError` before eligibility.
- [x] Add a GREEN-path assertion that every `01_Pair_History` row reports the
  single accepted window and derives `effective_days` from coherent rows.
- [x] Run RED: `.venv\Scripts\python.exe -m pytest tests/test_loader.py tests/test_pipeline.py -q`.
- [x] Validate one normalized UTC window in `load_points`; make `_pair_history`
  assert rather than synthesize min/max endpoints.
- [x] Run GREEN, review and commit: `fix: enforce one csv analysis window`.

### Task 4: Align Plateau Library eligibility and event provenance

**Files:**
- Modify: `src/mrs3/plateau.py`
- Modify: `src/mrs3/pipeline.py`
- Test: `tests/test_plateau.py`
- Test: `tests/test_pipeline.py`

- [x] Add RED regression where geometric members include economic-, sample-,
  history- and event-ineligible points; library ID tuples must equal the
  annotated `standalone_eligible`/`depth_eligible` sets.
- [x] Add RED regression proving `build_plateaus` cannot label hashes-per-point
  as a true `PlateauEventSet` union.
- [x] Run RED: `.venv\Scripts\python.exe -m pytest tests/test_plateau.py tests/test_pipeline.py -q`.
- [x] Derive both ID tuples from the same predicates as annotations. Publish real
  union count/hash only in `_apply_package_event_unions`; keep legacy values
  `N/A_LEGACY_PROXY` and fail closed if real mappings are absent.
- [x] Run GREEN, review and commit: `fix: align plateau audit eligibility`.

### Task 5: Stabilize null configuration errors

**Files:**
- Modify: `src/mrs3/config.py`
- Test: `tests/test_config.py`

- [x] Add RED cases for `base_rate_tf: null`, null values and non-object nested
  configuration.
- [x] Run RED: `.venv\Scripts\python.exe -m pytest tests/test_config.py -q`.
- [x] Validate mapping/value types before `Decimal`; raise field-specific
  `ValueError` with the original exception chained.
- [x] Run GREEN, review and commit: `fix: validate null algorithm config`.

---

## Core DuckDB delivery

### Task 6: Expose a tested compact-importer parity contract

**Files:**
- Modify: `programs/Обработчик HTML-DuckDB/mrs3_html_compact_importer_v3.py`
- Modify: `programs/Обработчик HTML-DuckDB/mrs3_html_parallel_compact_importer_v4.py`
- Modify: `src/mrs3/duckdb_events.py`
- Create: `tests/test_duckdb_events.py`
- Create: `tests/fixtures/duckdb_import/report_a.html`
- Create: `tests/fixtures/duckdb_import/report_b.html`

- [x] Add RED parity tests comparing codec metadata, payload bytes/counts, source
  text SHA and error classification for representative compact HTML.
- [x] Define one immutable compact-record value contract consumed by the v4
  worker and `mrs3` adapter; v4 must still load the adjacent v3 codec.
- [x] Run RED/GREEN: `.venv\Scripts\python.exe -m pytest tests/test_duckdb_events.py -q`.
- [x] Verify on a copied real sample without writing the production database.
- [x] Review and commit: `refactor: expose compact importer contract`.

### Task 7: Add source schema v5 and out-of-place migration

**Files:**
- Create: `src/mrs3/duckdb_source_schema.py`
- Modify: `src/mrs3/duckdb_events.py`
- Create: `tests/test_duckdb_source_schema.py`

**Interfaces:**
```python
ensure_source_schema(connection) -> int
validate_source_database(connection) -> SourceValidationResult
migrate_source_database(source_path, target_path) -> SourceMigrationResult
canonical_report_key(metadata) -> str
normalize_source_shift(value, contract_version) -> int
```

- [x] Add RED tests proving source/target paths differ and the old DB remains
  byte-for-byte unchanged after success or any validation/transaction failure.
- [x] Keep Task 7 independent of configuration; configured-path activation is
  owned and tested only by Task 9.
- [x] Validate schema/constraints, rows, canonical report keys, active hashes,
  decoded payloads, references and row/payload hashes.
- [x] Prove `0.99` and `0.9900` normalize to one canonical point and the
  time-grid hash is integrity evidence, not report identity.
- [x] Prove one active SHA cannot belong to a second active canonical report;
  historical audit may repeat hashes outside the active uniqueness constraint.
- [x] Add RED normalized-contract metadata and incompatible-setting tests that
  fail before target preflight/write.
- [x] Implement the exact spec model: one active payload per canonical
  point+period; active hashes separate from replacement history.
- [x] Run GREEN: `.venv\Scripts\python.exe -m pytest tests/test_duckdb_source_schema.py tests/test_duckdb_events.py -q`.
- [x] Review and commit: `feat: add versioned source duckdb schema`.

### Task 8: Implement recursive append/replacement import

**Current status:** Complete. The RED regression parsed `1` report and
quarantined `1` valid Windows CRLF input before the semantic-SHA correction;
the related suite now passes `66` tests.

**Files:**
- Create: `src/mrs3/duckdb_import.py`
- Modify: `src/mrs3/duckdb_events.py`
- Create: `tests/test_duckdb_import.py`
- Reuse: `tests/fixtures/duckdb_import/*.html`

**Interface:**
```python
import_html_tree(request: ImportRequest, progress_callback) -> ImportJobResult
```

- [x] Add RED tests for insert, active identical skip, new period/shift append,
  `A -> B -> A`, parser quarantine, transaction rollback and safe retry/resume.
- [x] Repeat the canonicalization/integrity/active-hash invariants from Task 7
  through the actual import boundary.
- [x] Add RED same-batch ambiguity test: two different HTML files with one
  canonical point+period produce `AMBIGUOUS_BATCH_DUPLICATE` and no replacement.
- [x] RED-test deterministic recursive discovery at multiple nesting depths.
- [x] Snapshot every discovered HTML path and byte hash; assert success,
  quarantine, cancellation and failure never move, rename, rewrite or delete an
  input file.
- [x] RED-test a deterministic per-job manifest with canonical relative paths,
  input hashes, report classifications, parity results, counts and final state;
  link a deterministic quarantine/deletion checklist and hash both artifacts.
- [x] Set `safe_to_delete=YES` only for committed schema-v4 import with complete
  manifest, zero quarantine and successful parity/validation for every HTML;
  cancellation, ambiguity, quarantine or incomplete/failing evidence is `NO`.
- [x] Treat the checklist as evidence only: import never deletes source HTML.
- [x] Include manifest/checklist paths and hashes in `ImportJobResult`.
- [x] Assert progress/final telemetry includes parsed, inserted, replaced,
  identical, ambiguous and quarantined counts.
- [x] Define `discover_compact_reports(root_path) -> tuple[Path, ...]`; quarantine
  is a database result, never a filesystem move.
- [x] Implement parallel read-only parsing and one coordinator writer; group all
  decisions before writes and append old/new hashes to immutable audit.
- [x] Run GREEN: `.venv\Scripts\python.exe -m pytest tests/test_duckdb_import.py tests/test_duckdb_events.py -q`.
- [x] Review and commit: `feat: import html into source duckdb`.

### Task 9: Integrate the importer into the panel

**Files:**
- Modify: `src/mrs3/config.py`
- Modify: `src/mrs3/panel.py`
- Modify: `src/mrs3/duckdb_import.py`
- Test: `tests/test_config.py`
- Test: `tests/test_panel.py`
- Test: `tests/test_duckdb_import.py`

- [ ] Add RED tests for saved source DB/default HTML/audit roots, workers and
  batch size plus the separate analysis DB path and native directory pickers.
- [ ] Require panel progress/final state to expose `parsed`, `inserted`,
  `replaced`, `identical`, `ambiguous` and `quarantined` explicitly; matching
  active content is classified only as `identical`.
- [ ] Add RED progress-phase, cancellation, success and stable-failure tests.
- [ ] Successful jobs expose verified manifest/checklist links and
  `safe_to_delete`; failed/cancelled jobs show explicit failure, finalized
  evidence links when present and always `safe_to_delete=NO`.
- [ ] Never expose success/`YES` before both artifacts are finalized and hashes
  verified; the panel never performs deletion.
- [ ] Add RED single-writer and stale-preflight rejection tests.
- [ ] Define `DuckDBImportSettings`, `DuckDBImportProgress` and panel job state;
  keep parsing/schema logic out of HTML/JavaScript.
- [ ] Persist `source_duckdb_path` and `analysis_duckdb_path` only in ignored
  `config.local.json`; never put real paths in tracked templates or docs.
- [ ] Add migration activation that runs every Task 7 validation and atomically
  switches the ignored source path only after success; failure retains both the
  old file and old configured path.
- [ ] Run GREEN: `.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_panel.py tests/test_duckdb_import.py -q`.
- [ ] Review and commit: `feat: manage duckdb import from panel`.

### Task 10: Create the analysis DuckDB schema

**Files:**
- Create: `src/mrs3/analysis_storage.py`
- Create: `tests/test_analysis_storage.py`

**Interface:** `ensure_analysis_schema(analysis_connection) -> int`.

- [ ] RED-test bootstrap/version/foreign-key/unique constraints and rollback for
  exactly: `surfaces`, `surface_sources`, `surface_pairs`, `surface_timeframes`,
  `surface_points`, `coverage_issues`, `dedup_decisions`, `analysis_runs`,
  `plateaus`, `plateau_members`, `candidates`, `plateau_lineage`.
- [ ] Implement only schema metadata, those tables and required indexes.
- [ ] Use explicit distinct source/analysis connections in fixtures and assert
  the analysis DB contains no raw payloads, actions, equity or wallet data.
- [ ] Run GREEN, review and commit: `feat: add analysis duckdb schema`.

### Task 11: Materialize a `DUCKDB_DIRECT` surface

**Files:**
- Create: `src/mrs3/duckdb_direct.py`
- Modify: `src/mrs3/duckdb_events.py`
- Modify: `src/mrs3/pipeline.py`
- Modify: `src/mrs3/panel.py`
- Create: `tests/test_duckdb_direct.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_panel.py`

**Interface:**
```python
preflight_duckdb_direct(source_connection, request) -> DirectPreflight
materialize_duckdb_direct(
    source_connection, analysis_connection, request, cancellation
) -> DirectSurface
run_panel_direct_build(
    source_connection, analysis_connection, request, cancellation,
    progress_callback
) -> PublishedSurface
```

- [ ] RED-test UTC `[start,end)` whole-report coverage, selected symbols,
  timeframe exclusion, manifest and `OBSERVED_GRID_CONTRACT` completeness,
  missing/conflicting cells and canonical point uniqueness.
- [ ] Assert every final point has `point_event_count == TotalTrades` and the
  surface declares one trades-proxy mode; real event IDs stay diagnostic only.
- [ ] Implement active-report reads and deterministic coverage/dedup facts; do
  not route through or implement the deferred CSV overlay.
- [ ] Add panel RED tests for usable symbols checked by default, noninteractive
  unavailable warnings, cancellable build and stale-preflight rejection after
  active source hashes change.
- [ ] Make the panel action invoke exactly preflight → materialize → publish;
  unavailable selection blocks materialization, cancellation blocks publication
  and hashes are revalidated immediately before publication.
- [ ] Use this action only for the initial direct build and a source-backed
  **Refine** that intentionally creates a child surface.
- [ ] Require distinct source/analysis connections and prove no raw source data
  is copied into the analysis store.
- [ ] Run GREEN: `.venv\Scripts\python.exe -m pytest tests/test_duckdb_direct.py tests/test_duckdb_events.py tests/test_pipeline.py tests/test_panel.py -q`.
- [ ] Review and commit: `feat: materialize duckdb direct surfaces`.

### Task 12: Publish immutable surfaces atomically

**Files:**
- Modify: `src/mrs3/analysis_storage.py`
- Modify: `src/mrs3/duckdb_direct.py`
- Test: `tests/test_analysis_storage.py`
- Test: `tests/test_duckdb_direct.py`

**Interface:**
`publish_surface(analysis_connection, surface) -> PublishedSurface`; it never
receives a source connection.

- [ ] RED-test deterministic digest inputs, identical-build deduplication,
  transaction rollback, immutable parent surfaces, raw-reproduction status
  after source replacement and stable ordered reads.
- [ ] Ensure plateau algorithm settings do not affect `surface_id`.
- [ ] Mutation-test every identity input: build mode, UTC period, side, selected
  symbols/timeframes, source hashes, grid/normalization contracts, materializer
  version and point-materialization configuration.
- [ ] Publish all surface/source/pair/timeframe/point/coverage/dedup rows in one
  transaction after validation.
- [ ] Run GREEN, review and commit: `feat: publish immutable analysis surfaces`.

### Task 13: Adapt published points to the common pipeline and persist lineage

**Files:**
- Modify: `src/mrs3/analysis_storage.py`
- Create: `src/mrs3/published_surface.py`
- Modify: `src/mrs3/plateau.py`
- Modify: `src/mrs3/pipeline.py`
- Test: `tests/test_analysis_storage.py`
- Create: `tests/test_published_surface.py`
- Test: `tests/test_plateau.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
```python
load_published_surface(analysis_connection, surface_id) -> PipelineInput
publish_analysis_run(analysis_connection, result) -> PublishedAnalysisRun
```

- [ ] RED-test deterministic run identity from surface+algorithm version/config,
  reuse of one point set across algorithm variants and complete candidate →
  plateau → member → surface → source lineage.
- [ ] RED-test `CONTINUED`, `SPLIT`, `MERGED`, `NEW`, `DROPPED` without merging
  metrics from different periods.
- [ ] Prove the read-only adapter never opens the source DB or copies surface
  points, and algorithm variants/repeated runs reuse the identical stored set.
- [ ] Write each run and lineage atomically without copying surface points.
- [ ] Run GREEN, review and commit: `feat: persist analysis runs and lineage`.

### Task 14: Expose library, statistics and deterministic exports

**Files:**
- Modify: `src/mrs3/panel.py`
- Modify: `src/mrs3/analysis_storage.py`
- Create: `src/mrs3/analysis_exports.py`
- Test: `tests/test_panel.py`
- Test: `tests/test_analysis_storage.py`
- Create: `tests/test_analysis_exports.py`

- [ ] RED-test library filtering, build/analysis progress, coverage and point/
  eligible/plateau/READY counts, parent/source lineage and final state.
- [ ] Add panel workflows and tests for **Refine**, **Re-run analysis** and
  **Compare periods**, keeping period metrics separate.
- [ ] Implement **Re-run analysis** through `load_published_surface` and
  `publish_analysis_run`: no source DB, rematerialization or new surface; RED
  tests preserve `surface_id` and every `surface_points` row.
- [ ] Implement **Compare periods** as read-only comparison of published runs:
  no source read, materialization, publication or metric combination.
- [ ] Show separate unique-point, economic-eligible, event-eligible, plateau,
  READY, coverage-reason, parent/source-lineage and final-state facts.
- [ ] RED-test byte-stable CSV/Excel exports of one immutable surface/run with a
  manifest; exports never become canonical storage.
- [ ] Implement read-only published-generation queries and
  `export_analysis_run(connection, run_id, output_path) -> ExportResult`.
- [ ] Run GREEN, review and commit: `feat: expose analysis duckdb results`.

### Task 15: Final verification and documentation

**Files:**
- Modify: `progress.md`
- Modify: `PRD.md` only if feature status changes
- Modify: `README.md` only after verifying a changed public command
- Modify: active spec only to record acceptance evidence, not rewrite history

- [ ] Run all focused files from Tasks 1–14, then
  `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`.
- [ ] Perform a copied-real-data migration/import/direct-surface smoke test; keep
  paths, databases and output artifacts outside Git.
- [ ] Run `git diff --check`, inspect staged scope and scan for local paths.
- [ ] Obtain final independent review and re-review after any correction.
- [ ] Record exact evidence and confirm the optional overlay remains unimplemented.
- [ ] Commit: `docs: record duckdb analysis storage evidence`.
