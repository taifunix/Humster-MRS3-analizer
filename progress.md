# Progress

**Updated:** 2026-08-12
**Current branch:** `main`
**Current feature:** [v0.7 DuckDB analysis storage and importer](docs/specs/2026-08-11-v07-duckdb-analysis-storage-and-importer.md)

## Verified repository baseline

- Root package: `src/mrs3`; tests: `tests`; project version: `0.7.0`.
- Latest complete repository suite: `478 passed, 2 skipped`
  (`.venv\\Scripts\\python.exe -m pytest -q --basetemp C:\\tmp\\humster-trusted-migration-full`, 2026-08-12).
  Both skips are Windows symlink-permission tests, not product failures.
- DuckDB v3/v4 importer pair is preserved in `programs/Обработчик HTML-DuckDB/`; v4 requires adjacent v3 codec.
- Local tester configuration exists outside Git as ignored `config.local.json`.
- Repository foundation, documentation model, importer/source preservation and Claude Code instructions are committed on `main`.

## Current verified external evidence

- The local v4 DuckDB was checked read-only: schema version `4`, compact payload schema and referential integrity are confirmed. Two malformed HTML reports are quarantined; the user accepted their exclusion from the current universe.
- The common UTC window is `[2026-07-15T00:00:00, 2026-08-06T00:00:00)`. The long CSV fully matches it; the short CSV contains one row with a shorter period and must be excluded by exact-period filtering.
- The v4 database contains `96,767` reports with the expected compact codecs. A source-HTML/Payload sample established the materializer formulas: wallet-change PnL, equity peak drawdown, and realised `closed`/`decreased` action metrics. The supplied CSV batch is a different grid and is not used as its reconciliation oracle; verification will use the source HTML referenced by DuckDB.
- The panel starts locally and answers HTTP 200. A one-strategy `tester-plan` succeeds when the JSON is supplied through a directory.
- Two approved real one-strategy smoke-runs completed. The second run completed the full runner lifecycle through `CSV_COMMITTED` and `COMPLETED` after the transient state-file lock fix; its result CSV is retained locally, while the configured report directory and both wizard logs were absent after cleanup. Only the root strategy JSON was changed; nested strategy folders were not touched.

## Current verified state

CSV and DuckDB source-package builders, v2 source verification, package loading,
the selector event gate and the panel source-package controls are implemented.
ADR-0003 makes `TotalTrades`, `WinRate` and `ProfitFactor` the fail-closed
full-horizon source gate; PnL/DD remain mandatory
`NOT_COMPARABLE_WINDOW_SCOPE` diagnostics. The DuckDB materializer uses bounded
reads and retains the complete cycle/exclusion audit.

Task 1 (`270249f`) of the approved DuckDB storage/importer plan rejects lossy
fractional/non-finite integer loader values, accepts only Python `None` and
`pd.NA` wins/losses values as zero, and rejects empty normalized input before
downstream processing. Focused evidence: `52 passed` for `tests/test_loader.py` and
`33 passed` for `tests/test_source_packs.py` with the local `.venv` on
2026-08-11. The full suite reaches `316 passed` but has eight independent
runner-test failures because three required HTML fixtures are absent from Git;
those paths are outside Task 1 and were not changed. Independent re-review is
approved; Task 1 is complete.

Task 2 (`f61ffc8`) validates `point_event_count` in eligibility before its `int64` cast:
fractional, negative and non-finite numeric/text values are rejected, while
the legacy missing-column proxy remains `trades`. Focused evidence is
`32 passed` for `tests/test_eligibility.py`; the full suite reaches
`324 passed` and has the same eight runner-test fixture failures outside this
task. Independent Terra review approved the staged implementation; Task 2 is
complete.

Task 3 (`bf899d3`) requires exactly one normalized UTC `report_start`/`report_end` pair
for raw CSV input before eligibility. Pair-history output now asserts that
invariant instead of synthesizing min/max endpoints; the audit records the
coherent window and its derived `effective_days`. Focused evidence is
`62 passed` for `tests/test_loader.py tests/test_pipeline.py`; the full suite
reaches `326 passed` and has the same eight runner-test fixture failures
outside this task. Independent Terra review and re-review are approved; Task
3 is complete.

Task 4 (`869bd70`) aligns Plateau Library eligibility ID tuples with the annotated
standalone/depth predicates. Per-point event hashes are no longer presented as
a plateau event union: only source-package event mappings can produce the real
union, while legacy retains `N/A_LEGACY_PROXY` and raw real-event input without
mappings fails closed. Focused evidence is `20 passed` for
`tests/test_plateau.py tests/test_pipeline.py`; the full suite reaches
`328 passed` and has the same eight runner-test fixture failures outside this
task. Independent Terra review approved the staged implementation; Task 4 is
complete.

Task 5 (`c579801`) validates `base_rate_tf` object and scalar value types before Decimal
conversion. Null, non-object and non-scalar values now yield field-specific
`ValueError` with the original cause chained. Focused evidence is `15 passed`
for `tests/test_config.py`; the full suite reaches `334 passed` and has the
same eight runner-test fixture failures outside this task. Independent Terra
review and re-review are approved; Task 5 is complete.

Task 6 (`05369c2`) has a committed, independently Terra-reviewed compact-importer parity
contract: v3 exposes an immutable compact record, v4 workers return that same
contract while dynamically loading the adjacent v3 codec, and the `mrs3`
adapter decodes its compact payloads. Focused evidence is `4 passed` for
`tests/test_duckdb_events.py`. A copied-real-HTML smoke established v3/v4/mrs3
adapter parity `PASS` and a temporary v3 DuckDB import without writing the
production database: schema `v3`, scanned `1`, imported `1`, quarantined `0`,
raw actions `78`, equity samples `646`, and wallet changes `79`. Task 6 is
complete.

Task 7 (`e2bcde9`) adds a versioned source DuckDB schema v5 and an out-of-place v4
migration. The v5 contract persists normalization metadata, identifies a
point by normalized shift and MA pair plus its period, uses the time-grid hash
only as integrity evidence, and keeps active payload hashes separate from
replacement history. Migration rejects same paths and incompatible contracts
before target writes, validates schema constraints, row counts, references,
canonical keys, compact payloads and row/payload hashes, then atomically
publishes a separately validated target. Focused and relevant evidence is
`48 passed` for `tests/test_duckdb_source_schema.py tests/test_duckdb_events.py
tests/test_source_packs.py`; the full suite reaches `350 passed` with the same
eight absent-runner-fixture failures. Independent Terra review found and the
re-review verified the report-period/time-grid-bounds integrity check; Task 7
is complete.

Task 8 (`b7dc23e`) adds the recursive HTML-to-source-DuckDB import boundary. It snapshots
every input without moving, rewriting or deleting HTML; parses read-only in
parallel; groups canonical identities before a single-writer publication; and
records deterministic hashed manifest/checklist evidence. Insert, identical
skip, new period/shift, `A -> B -> A` replacement history, quarantine,
cancellation and safe retry are covered. Any same-batch canonical ambiguity
now stops the entire job before staging or DuckDB access, so neither an
existing target nor a new target can be published. The RED regression parsed
`1` report and quarantined `1` valid Windows CRLF input because it compared the
raw-byte snapshot SHA with the normalized codec SHA. The corrected contract
keeps raw input SHA as manifest and mutation evidence; normalized v3/v4 semantic
SHA drives report identity, `report_id`, deduplication and migration
compatibility. CRLF and LF inputs are identical under the semantic contract;
mutation and invalid UTF-8 fail closed. The related suite passes `66` tests.
Task 8 is complete.

Task 9 is complete (`feat: manage duckdb import from panel`). The panel manages
saved import settings, preflight, start, cancellation and out-of-place migration
activation. A per-resolved-source writer lock and path-free preflight token reject
concurrent or stale work; progress reports `parsed`, `inserted`, `replaced`,
`identical`, `ambiguous` and `quarantined`. Evidence, hashes and JSON are
revalidated before `COMMITTED`/`safe_to_delete=YES` or download; errors stay
path-free. The root suite is `87 passed, 1 skipped` (Windows symlink privilege),
and independent Terra review approved after three fix rounds.

Task 10 is complete (`224a039`): the analysis DuckDB schema has 13 focused
passing tests and an independent clean review.

Task 11 DUCKDB_DIRECT materialization and panel orchestration is complete.
The panel keeps materializer constants server-side, freezes the full preflight
snapshot behind a token, selects usable symbols by default and renders missing
coverage as noninteractive warnings. Build uses distinct read-only source and
writable analysis connections, revalidates the complete source contract before
publication, supports cancellation and publishes no raw source data. The relevant
suite passes `69` tests, and independent Terra review approved the implementation.

Task 12 atomic immutable publication is complete (`f72aae3`). All identity
inputs and order-independent source hashes are covered. New same-period/scope
surfaces form an immutable child chain; repeated older or newer inputs deduplicate
without rewriting parentage. Every publication table is written in one
transaction with rollback, without a source connection or raw-data copy. Current
raw reproducibility is derived from persisted source hashes and the supplied
active-hash set. The focused suite passes `29` tests, and independent Terra
review approved after the fix rounds.

Task 13 common-pipeline adaptation and persistent lineage is complete. Analysis
schema v2 transactionally migrates v1 candidates to explicit multi-plateau
membership without changing published surfaces or points. Published surfaces
are loaded from the analysis DB only; explicit listing dates are scoped and
hashed into analysis-run identity. Runs, plateau members, 2–4ORD candidates and
explicit cross-period lineage are atomic and deterministic; repeated comparison
targets add idempotent lineage without copying points. Focused verification is
`115 passed`; the full suite is `454 passed, 2 skipped`, with both skips limited
to unavailable Windows symlink privilege. Independent Terra review approved
after two fix rounds.

Task 14 library, statistics and deterministic exports is complete. Analysis
schema v3 stores immutable per-run point/eligibility/plateau/READY facts and
atomically migrates v1/v2 history as explicitly unavailable rather than
inventing eligibility counts. The panel exposes an explicit writable v3
initialization/migration action; library, compare and export then remain
read-only, while re-run analysis reuses the same immutable surface without
opening the source DB. Refine requires an explicit existing parent surface.
CSV/XLSX exports are byte-stable generated artifacts with a hashed manifest,
not canonical storage. Focused evidence is `104 passed`; independent Terra
review approved after one fix round.

Task 15 final verification is complete. The copied-real-data smoke imported
one report into a small v4 database, migrated it out-of-place to a valid v5
database, append-imported one more report with `COMMITTED`,
`safe_to_delete=YES` and zero quarantine, then revalidated `2` reports, `2`
points, `2` grids and `2` payloads. DUCKDB_DIRECT published one immutable point
with `458` trading events into analysis schema v3. The complete repository
suite passes `467` tests with two Windows symlink-permission skips. Smoke data
and generated databases remain outside Git; the optional overlay remains
unimplemented.

Read-only v2 materialization then completed: `96,767` reports, `8,050`
coverage-accepted points, `88,717` coverage-rejected reports and `4,932,780`
included cycles. The package has `source_summary_status=VERIFIED` and
`window_metrics_status=DERIVED_FROM_VERIFIED_SOURCE`. An exploratory real LONG
selector run then completed after the package-side and UTC-normalization
integration slice:
`3,730` normalized points, all with 24-346 events, but `0` economic/event-
eligible points, `0` plateaus and `0` READY structures under the current
economic thresholds. This is a result, not a source-verification failure.
It is not directly comparable to the separately supplied LONG CSV grid: that CSV has
`36,288` points on the same window and produces `3,432` economic-pass points
under the same thresholds, whereas the imported DuckDB LONG slice has `3,730`
points. Core v0.7 will therefore publish direct DuckDB surfaces into a separate
analysis DuckDB. A possible CSV-base overlay is isolated in the
[optional/deferred specification](docs/specs/2026-08-11-v07-optional-csv-duckdb-overlay.md)
and does not block the core delivery.

Trusted-v4 migration performance is implemented and independently reviewed.
It streams v4 metadata and exact matching payload batches through one read-only
transaction; detached workers prepare records while one DuckDB writer commits
them. Raw v4 report payloads are not decoded, and the staged v5 database is
validated structurally before an atomic no-replace publish. Focused evidence:
`67 passed` for `tests/test_duckdb_source_schema.py tests/test_panel.py`.
The 96,767-report production migration has not yet been rerun, so no duration
claim is recorded.

## Next required work

The core specification and its 16-task plan (Task 0 plus Tasks 1–15) are
approved. Task 0 is represented by the existing `d76b985` package-side/UTC
slice; Tasks 1–15 are complete.

1. Run the trusted-v4 production migration and record its observed duration,
   counts and structural-validation result. Before direct panel analysis, fix
   the separate preflight path so it does not perform full payload validation.
2. Use the panel to initialize the chosen production analysis DuckDB, publish
   required real surfaces and run their plateau analyses.
3. Continue the READY-candidate → tester → DD5 product loop; source metrics
   remain diagnostic until those real tests exist.
4. Keep CSV/DuckDB overlay deferred unless the user activates its separate ТЗ.

## Blockers

- CSV/DuckDB overlay is explicitly deferred and no economic threshold changes
  are implied.

## Queued module hook

**Анализатор Портфеля:** [спецификация v0.4](docs/specs/2026-08-09-portfolio-analyzer-v04.md) передана отдельной команде. Начинать с проверки входных контрактов; до trade timestamps и определённого limiter допускается только Layer A, без симулятора и рекомендаций по сету.

## Update protocol

Replace this file’s verified-state and next-action sections whenever a commit changes the operational state. Keep only the present blocker set; durable decisions belong in ADRs and feature requirements belong in specs.
