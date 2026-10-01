# PerformanceDB lossless compaction

Status: implementation authorized; independent Advisor v3 `PLAN_APPROVED`.
User priority is
shrinking the existing database without additional D capacity.

## Goal and scope

Reduce prepared-artifact storage and reclaim reusable blocks by constructing
a verified fresh database. Preserve every report, financial fact, identifier,
history row, required empty table and database instance identity.
The [storage investigation](../reports/2026-09-30-performance-db-storage.md)
contains measurements, limitations and expected gains.

This supplements the [heavy database contract](2026-09-28-heavy-database-optimization.md)
and [optional commission contract](2026-09-30-performance-v2-optional-commission-evidence.md).
Their calculation/admission rules remain applicable. The only new storage
semantics are the envelope and schema-v8 compatibility gate below.

Implementation and tests are repository-only until the reviewed live cutover.
Do not deploy new code against the live v6 file or invoke its initialization:
legacy in-place migration paths are exercised only on fixtures/copies. The
real source stays v6 throughout rehearsal; only the fresh candidate is v8.

Non-goals: cycles-only cache redesign, removing raw action evidence, changing
PnL/fees, tester execution, modifying the source during rehearsal, automatic
cutover or backup deletion, and a new compression dependency/configuration.

## Inputs, outputs and compatibility

The application reads exact supported v5/v6/v7/v8 catalogs. Writers require
v8. Schema v8 has the v7 relational schema; its marker declares that prepared
payloads may be compressed. Upgrade v7 to v8 in a transaction after strict v7
validation; validate v8 before commit. Preserve legacy v2-v6 migration steps
through v7 before that marker upgrade. Old executables reject v8 using their
existing schema gate. Test this with the actual pre-change store module.

`PreparedOptimizerInput.to_json()` and its canonical digest contract do not
change. Prepared data is persisted only by the explicit finalist-scoped
`performance_v2_optimizer.prepare_current_optimizer_inputs` path; import stores
the typed source facts and does not write a prepared artifact.

The existing v7 table-rebuild migration copies payload strings opaquely.
The offline copier transforms them using the same codec. Fixture/raw SQL
in tests may intentionally contain legacy or corrupt payloads; tests must
verify behavior, not count source-code call sites.

## Lossless envelope

Stored text is `mrs3-zlib-v1:<raw-byte-count>:<sha256>:<base64>`.
Use stdlib zlib level 1 on the exact original UTF-8 bytes. The decoder accepts
legacy JSON and this envelope. Decoded canonical JSON, never the envelope,
feeds semantic parsing and source-digest validation.

Reject malformed fields, unknown versions, invalid base64, bad zlib,
truncated or trailing streams, invalid UTF-8, length/hash mismatches and
declared sizes above `PREPARED_MAX_BYTES` (16 MiB). Bound decompression to
16 MiB + 1 and reject output exceeding the limit. Bound encoded input before
base64 allocation as well. The encoder compresses raw JSON or fully validates
and returns an existing envelope unchanged. Nested compression is forbidden.

Strict reads raise `OptimizerIntegrityError` on corruption and never silently
rebuild. The explicit existing preparation/repair operation keeps its
documented ability to rebuild malformed cache rows from typed facts.
Mixed legacy/compressed rows and unavailable NULL payloads remain supported.

## Fresh-file construction

The offline tool builds, verifies and reports only. It opens the source
read-only, requires a supported exact v6/v7/v8 catalog and zero WAL, refuses
an existing/aliased target, and never checkpoints or changes source attributes.
Record source file identity, size, mtime and SHA-256 before/after construction.

Use native `COPY FROM DATABASE ... (SCHEMA)` to retain the source catalog,
defaults, indexes and sequence state. Copy schema markers, detach source
before existing catalog validation, and run migrations only on the EMPTY
target. Copy all data in FK order with explicit columns. Do not copy raw
prepared JSON and update it later: encode before inserting into the target.
Preserve instance identity and sequence next values, including unused IDs.

Use bounded batches, the shared `duckdb_import.workers` configuration,
and one DuckDB writer. Do not introduce a separate worker setting or change
the existing loader fallback. Compare one versus configured compression workers;
retain parallelism only with measured benefit. Use native durability defaults,
pin spill to a dedicated C TEMP directory, and report chosen settings/bounds.
Checkpoint, close and reopen before accepting the candidate. Failed builds
are never accepted; cleanup only owned temporary paths.

## Verification and capacity

Compare counts and every typed row in bounded ordered passes, including all
non-payload prepared columns. Compare every recovered payload byte-for-byte
with source. Exact typed equality plus exact payload equality proves preserved
digest inputs; do not reread all source facts once per result. Validate catalog,
constraints, current-result references and representative public reader paths.
Record raw UTF-8 totals/distribution during the existing pass, not an extra
full scan. Do not use logical characters as physical table-byte measurements.

Recheck capacity before and during each large phase. Candidate construction
must retain 10 GiB on C. Candidate admission to D requires measured candidate
size no greater than D free minus 5 GiB. Original backup to C requires original
size plus 8 GiB free, after the C candidate has been safely copied elsewhere.
If a gate fails, stop with the original retained; never lower a reserve or
delete unrelated files. Candidate size estimates are not admission evidence.

## Deployment and acceptance

Deploy compatible code and v8 database together under maintenance. The manual
runbook must recheck source identity/WAL and exclude writers before cutover.
Copy candidate to D, verify hash and read-only catalog/reader checks, then
remove only its redundant C copy. Copy original to C; verify hash, read-only
catalog and representative reads before replacing names on D. Preserve the
D original through successful application smoke checks. Remove it only with
the verified C original retained. C backup deletion needs later explicit
authorization. A changed source invalidates the candidate.

Record the original application's code revision and launch path before
maintenance. Every database rollback also restores that compatible runtime;
never let new code automatically migrate the restored original. Rehearse
old-runtime write capability on a disposable fixture/copy before cutover,
without adding smoke-test financial rows to the live database.
Preserve existing uncommitted main-checkout changes. A commit ID alone does
not identify that runtime: keep the original checkout intact and record the
relevant file hashes/launch environment; rollback must not use `git reset`.

Acceptance: failing-before codec/version tests; corruption/limit/Unicode and
mixed-row tests; unchanged digests and import idempotence; both writer paths;
atomic marker rollback; old-runtime refusal; fresh-copy completeness and
sequence tests; no-overwrite/alias/low-space/corrupt/source-change failures;
focused plus relevant broader tests using `.venv` and cleaned C TEMP;
independent `CODE_REVIEW_PASS`; full read-only rehearsal with actual candidate
size, timings, exact verification and source unchanged. Observe physical and
payload growth on subsequent operator imports; historical doubling remains
unproven.
