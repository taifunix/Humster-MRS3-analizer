# PerformanceDB: why an incremental import takes hours

**Date:** 2026-09-30. **Scope:** read-only inspection of the live schema-v6
database and the `perf/heavy-db-optimization` importer. No import or database
mutation was performed for this audit. The existing
[import-tail audit](2026-09-28-performance-db-import-tail.md) covers prior
changes and their synthetic checks.

## Measured behavior

`import_runs.started_at_utc` is written after parsing and admission, when
`_publish` starts row publication. `finished_at_utc` is written before its
`COMMIT`; neither timestamp includes connection close, staging cleanup or the
Panel journal. The UI emits `PUBLISHING N/N` before `_publish`, so its apparent
100% precedes this entire expensive interval. The live run ledger and
`import_files` give:

| Run | Imported reports | Equity samples | Actions | Time between DB timestamps |
| --- | ---: | ---: | ---: | ---: |
| 13 | 2,426 | 8,488,424 | 826,866 | 34m 13s |
| 14 | 3,744 | 20,880,392 | 3,227,847 | 1h 25m 57s |
| 15 | 3,369 | 20,874,717 | 4,144,417 | 2h 36m 57s |

Runs 14 and 15 inserted almost the same number of equity samples. Run 15 had
28% more actions and about 4% more combined child rows, yet took 1.83 times as
long inside the transaction. Report and row counts alone do not explain this
difference, but report complexity, memory pressure and database growth remain
uncontrolled factors; these observations do **not** identify which statement
slowed down. The most recent audit file was
written about 15 minutes after run 15's `finished_at_utc`, but that interval
combines commit, close, cleanup and audit publication and is not an isolated
commit measurement. All these runs predate terminal `phase_seconds`; no
historical per-phase attribution exists.

The previous 409-report replacement also predates phase timings. Its long tail
cannot honestly be assigned to any one check. However, runs 13–15 are `ADD`,
so replacement-specific checks and DELETEs cannot explain their measured hours.
Prepared-input persistence was introduced on 2026-09-17, before these runs; no
comparable pre-change timing is recorded, so the user's reported regression
cannot be quantified from Git history alone.

## Cost centers in the current code

1. **Large indexed child tables, highest-priority hypothesis.** Each new equity
   or action row maintains a `(result_id, ordinal)` primary key, a foreign key,
   and an explicit `(result_id, timestamp_utc)` ART index. The live database has
   112.2 million equity rows and 14.8 million action rows. DuckDB says ART
   indexes slow bulk loading, can consume considerable memory, and **multi-
   column indexes are ineligible for index scans**. The two explicit timestamp
   indexes are therefore suspect write amplification. This is a hypothesis,
   not proof they caused run 15; catalog/reader and representative import
   comparisons are needed before changing the schema. Read-only `EXPLAIN` on
   the live catalog reported sequential scans for one-result equity read,
   equity count and action read; this confirms those three plans did not use
   the explicit indexes, not that no other reader does. See the
   [DuckDB indexing guide](https://duckdb.org/docs/guides/performance/indexing).
2. **The importer is bulk for children but serial elsewhere.** Action/equity
   rows use bounded 20,000-row DataFrame appends. Each report still updates or
   inserts `strategy_results` individually and constructs/writes one prepared
   optimizer payload. `PUBLISH_ROWS` and `PUBLISH_PHASE8` run under one writer
   transaction. `Phase 8` recreates a canonical document and digest for every
   accepted report; all 3,369 results in run 15 are `AVAILABLE`. A second digest
   call in `_persist_phase8_prepared` duplicates one done by
   `build_prepared_input`. The prepared object is also encoded to JSON once to
   enforce its byte limit and again to persist it. These duplicate full-payload
   passes are direct code evidence; their wall-time share is still unknown.
3. **Input and memory overhead before publication.** The parser already uses
   the shared configured process count, but submits every report and retains
   every parsed result. The inbox reader parses each strategy JSON twice. Report
   staging hashes and copies HTML, then parser processes read it again; these
   cross distinct trust boundaries and should not simply be deleted. Bounded
   parsing can control memory and stalls, but cannot by itself explain the
   2h37m recorded *after* parsing.
4. **Admission and startup are unmeasured.** `_load_existing` builds a large OR
   predicate from every incoming typed prefix. Before parsing the importer
   always calls `initialize_performance_v2`. On a v6 database, the new v7
   commission migration would rebuild parent and child tables once. This did
   **not** cause historical v6-code runs 13–15. The planned verified v8
   fresh-file cutover avoids that first-import migration. These stages need
   their own clocks so the next run does not conceal them.

The existing branch has already reduced 409 replacement child DELETE
statements from 2,045 to five, post-commit current-result reads from 409 to
one, and timestamp-only updates from 409 to one. The Panel journal also skips
unchanged status rewrites. Repeating these optimizations would not address the
measured `ADD` slowdown. The separate commission-contract simplification makes
tester commission evidence optional; it is a correctness/usability change, not
an explanation for multi-hour row publication.

## Ordered implementation and expected effect

Percentages below are **hypotheses for a comparable batch**, not measured
end-to-end gains. They must not be summed; the next real import's phase timings
and exact row counts decide which work remains justified. Keep one DuckDB writer
and the single Panel/config worker setting.

| Step | Change and gate | Tentative whole-import effect |
| --- | --- | ---: |
| 0 | Finish the lossless v8 cutover; capture `phase_seconds`, stage and wall times on the next normal import. This prevents a one-time full-table migration; it does not by itself prove faster steady-state inserts. | Migration avoided; steady-state unknown |
| 1 | Reuse the validated strategy object, prepared digest and validated JSON bytes; bound parser submissions/results by report count and bytes without changing manifest/hash boundaries. | 0–15%; memory stability may matter more |
| 2 | Compare equivalent append-only cohorts on a verified disposable copy with and without the two explicit compound timestamp indexes. Audit all query plans, child counts and exact outputs. If the indexes provide no read benefit, remove them in a new schema version with an explicit migration/rollback. Keep PK/FK initially. | 20–60% if index maintenance dominates; otherwise near zero |
| 3 | If `PUBLISH_ROWS` remains dominant, replace per-report wide result updates and DataFrame conversion hot spots with set-based staging/`INSERT SELECT` while preserving current IDs, atomicity and per-row Decimal values. Measure each substep separately. | 15–50% if row publication dominates |
| 4 | If `PUBLISH_PHASE8` dominates, prepare bounded payloads in workers before the final write where IDs/revision are known, or redesign the prepared-input lifecycle under a new contract. Keep all readers fail-closed until prepared data is present. | 15–50% if preparation dominates |

The operative goal is a batch of roughly 20 million equity samples in **tens
of minutes instead of hours**; a single-digit-minute promise would require a
measured order-of-magnitude improvement and may require a different large-child
storage contract. A 409-report batch can plausibly finish in minutes, but its
size distribution must be specified before setting an acceptance threshold.
DuckDB supports concurrent writer threads **within one process**, but the
current import is intentionally one atomic publication under a cross-process
writer lock. Splitting that transaction across writers changes failure and
rollback behavior; asynchronous scheduling alone does not make indexed inserts
faster. Prefer bounded CPU preparation or larger set-based SQL after the phase
evidence; consider concurrent writer connections only with an explicit atomic
publication design. See [DuckDB concurrency](https://duckdb.org/docs/current/connect/concurrency).

## Verified compact-database cutover (2026-09-30)

The live PerformanceDB was rebuilt from schema v6 into the schema-v8 compact
prepared-payload format and cut over at the canonical path. The database
instance ID and every normalized table, constraint, index and sequence were
preserved; only the reviewed schema marker/commission-nullability/storage-codec
changes were admitted.

| Evidence | Result |
| --- | ---: |
| Old file size | 43,546,324,992 bytes |
| Deleted v6 SHA-256 | `78d6eb1f7eafc1b9bef5bc88637a526132bf83af0f8c26025783ac3a7b96ba19` |
| Live compact file size | 18,201,718,784 bytes |
| File reduction | 58.2% |
| Strategies / results / prepared rows | 23,887 / 23,887 / 23,887 |
| Actions / equity rows | 14,753,892 / 112,188,510 |
| Decoded prepared UTF-8 | 18,083,324,749 bytes |
| Encoded prepared storage | 3,366,475,413 bytes |
| Compact SHA-256 | `adab2caea9242ffc3b48be1bc64ff6f8c7f14570116adc90af59cf620a4399c3` |

The verifier compared every non-floating table by its enforced primary key and
every non-key value, routed `selection_results` through the signed-zero-aware
Python comparator, decoded and compared every prepared payload, checked public
reader output, then repeated source/candidate stat, WAL and SHA checks. The
copied D: candidate passed a separate schema/count/public-reader smoke before
the live rename. The old 43.55-GB file and temporary C: candidate files were removed
only after live schema-v8 smoke passed. The active database now occupies the
canonical `data/performance-v2/strategy_performance.duckdb` path.
No v6 database artifact remains in the project data or backup directories;
recovery to v6 would require a full re-import or an external backup.

## Verified 2,070-report import (2026-10-01)

The listing source was corrected to `input/bybit_tradfi_liquidity.xlsx`; it
covers all six symbols in the batch. The retried ADD import committed all 2,070
reports with zero skipped or rejected rows. The parser used 30 worker processes
and reached 2,070/2,070 without a progress regression. The database grew from
18,201,718,784 to 19,393,163,264 bytes and now contains 25,957 strategies and
results, 57,115 orders and 3,112 plateaus.

The first complete phase evidence moves the main priority from parsing to
Phase 8 preparation:

| Phase | Elapsed |
| --- | ---: |
| `PUBLISH_ADMISSION` | 1.372 s |
| `PUBLISH_ROWS` | 242.858 s |
| `PUBLISH_CHILD_READBACK` | 0.129 s |
| `PUBLISH_PHASE8` | 963.364 s |
| `PUBLISH_FINALIZE` | 0.003 s |
| `COMMIT` | 45.777 s |
| Publication through cleanup | 1,254.552 s |

Phase 8 alone consumed 76.8% of the measured publication interval. Its current
serial loop rebuilds optimizer source objects, prepares the optimizer input,
creates canonical JSON, compresses and base64-encodes it, hashes it, then issues
one SQL insert for every result. Peak private memory reached about 12.5 GiB
because all parsed reports and transient Phase 8 representations coexist.

OPT-01e therefore requires exclusive substep attribution before selecting
serial batching, threads or processes. Exact stored rows, digest branches,
ordering and the single transaction remain frozen. A mechanism is retained only
when the whole publication median improves without moving the cost into
`PUBLISH_ROWS` or `COMMIT`; the final Phase 8 target is at least 20% below the
accepted reproducible baseline. Independent plan review returned
`PLAN_APPROVED` for revision v4. The operator-started all-pairs cache
recalculation remains active, so importer edits, tests and benchmarks wait for
its terminal status.

## Evidence required before the index decision

Collect `PUBLISH_ADMISSION`, `PUBLISH_ROWS`, `PUBLISH_CHILD_READBACK`,
`PUBLISH_PHASE8`, `PUBLISH_FINALIZE`, `COMMIT`, `CONNECTION_CLOSE`, cleanup and
terminal Panel sync for one normal import. Add lightweight clocks around schema
initialization, staging and parsing. Record rows, input bytes, elapsed wall
time, peak RSS, database/WAL growth and worker count. Compare equal report
cohorts at equal starting database size, with identical child rows, prepared
payload digests, import ledger and representative reader outputs. No further
long-running synthetic suite should delay the active database cutover.
