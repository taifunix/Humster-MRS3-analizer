# Performance v2 equity-quality M5: bounded real-data slice

Status: partial current-runtime measurement evidence, 2026-09-26. This does
not accept M5 or establish full-corpus speed/RSS. The user withdrew
prior-runtime comparison and relative-to-old-runtime budgets.
M0-M4 remain accepted; one-REPLACE timing is not performed because no exact
replacement report/inbox is available, and user speed acceptance is pending.

The user approved deriving a partial offline corpus from the existing
PerformanceDB. The new `scripts/create_performance_v2_equity_benchmark_slice.py`
opens the schema-v5 source read-only, deterministically selects at most 512
ACTIVE current-result strategies from one Pair+Side group, and publishes a new
schema-v6 database outside the repository. It copies only cohort-related
strategies/results, order-referenced plateaus, orders, actions, equity,
window metrics, tags and (for a v6 source) equity-quality facts. Import and
selection histories and optimizer-prepared inputs are intentionally omitted.
Source and target column signatures, source-scoped row counts, current-result
pairs, catalog coverage and source file identity are verified before exclusive
publication. No migration or write is issued against the source.

The real source was schema v5, 14,119,350,272 bytes. A one-strategy smoke
slice succeeded first (320 actions, 3,683 equity points, seven window rows).
The 512-strategy `FWDIUSDT/LONG` slice then succeeded with:

| Item | Count |
| --- | ---: |
| Strategies / current results | 512 / 512 |
| Actions | 288,464 |
| Equity points | 2,348,395 |
| Window metrics | 3,584 |
| Orders / order-referenced plateaus | 1,441 / 111 |

The published v6 file is 233,582,592 bytes. Selected strategy-ID SHA-256:
`d89a89980d514ff070e67eef5869256bfabba4730b95de7806ef2bb7b142567c`.
File SHA-256: `b46e51a49facd9cce605012f18c933d1c686d88ba2a4fe5151aafad4a06f2b83`.
The source size and mtime were unchanged after the smoke, 512 extraction and
benchmark. No temp/WAL/spill artifact remained beside the published slice.
The 14 GB source was not content-hashed: this is a file-stat and read-only-open
check, not a byte-for-byte source proof.

The existing copy-only benchmark ran one warmup and three measured all-warm
previews for each mode on scratch copies of this same slice, Top N=20:

| New-runtime mode | Median wall, s | Max wall, s | Peak parent RSS, MiB |
| --- | ---: | ---: | ---: |
| Legacy controls | 2.295 | 2.367 | 203.3 |
| Filter only | 1.952 | 2.120 | 206.1 |
| Equity rank only | 3.139 | 3.434 | 208.8 |
| Filter and equity rank | 2.036 | 2.141 | 208.0 |

Each mode had one stable decision signature across its measured repetitions,
zero cache writes and an unchanged input slice. The filter evaluated all 512:
7 GROWING, 171 WEAKENING and 334 DECLINING_OR_MIXED; it eliminated 334.
The measured preview SQL counter saw zero raw `strategy_equity` and
`strategy_actions` reads in all four modes. It saw 21 read queries per
legacy-controls repeat and 35 per equity-enabled repeat, including two
`window_metrics` reads and respectively three or six
`equity_quality_metrics` reads. These are process-level SQL/fetch counters,
not DuckDB physical-scan counts; the additional metadata reads are a possible
future optimization target only after end-to-end profiling.

These are observed decisions on one selected group, not a population estimate
or a claim that the filter improves trading results. JSONL evidence SHA-256:
`379c6effb4e62de8d98145c60a74b930b6a947b15507097643dc8fe55173089b`.
The four modes ran in a fixed order with separate Panel controllers but shared
OS/database-page caches. Only three repeats per mode were measured; their
medians do not establish a causal speed ordering or an enabled-feature penalty.

The slice creator and benchmark harness tests passed together (`30 passed`).
The creator received independent Opus 5 `CODE_REVIEW_PASS` after follow-up
reviews. The four modes above all use the new runtime; legacy-controls mode
is **not** an old-runtime baseline.

## Same-cohort cold, backfill and warm profile

A second frozen v6 slice from the same source and Pair+Side selected 64 current
strategies (selected-ID SHA-256
`9fdb617e385a4db04ae94610f6875cad959925c784ba45bce88fc29bc283aafc`).
It contains 294,397 equity points, 37,250 actions and 448 window rows. The
35,926,016-byte file SHA-256 is
`413f08080ccbd4314930610f4e74ab7f5e80f0086df9b0698ae792988907185f`.
The schema-v5 source stat remained 14,119,350,272 bytes with the same mtime;
its content hash was not computed. The frozen slices had no WAL sidecars. The
profile used one warmup and three measured runs per state and worker count,
each on a fresh temporary copy.
`cold` clears both derived caches. `old_warm_new_cold` retains all 448 window
rows and clears equity facts; "old" means the window-cache type, not an old
runtime. All measured runs used the current runtime and reported unchanged
source-copy stats. The cold setup had zero window/equity rows; backfill setup
had 448/zero in every repetition.

The cache workers use `ThreadPoolExecutor`, not child processes. Peak parent
RSS therefore includes their threads and DuckDB allocations; it does not
measure system-wide memory outside this process. The benchmark's process-wide
`duckdb.connect` wrapper covers connections opened by these worker threads,
as a four-thread targeted check confirmed (four connections/four SELECTs).
Pre-existing handles, subprocesses, and DuckDB physical scans are outside
the SQL/fetched-row counter scope.

| Workers | Cold median / max, s | Cold peak RSS, MiB | Backfill median / max, s | Backfill peak RSS, MiB |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 38.540 / 43.895 | 216.6 | 17.326 / 17.371 | 208.9 |
| 4 | 28.749 / 29.331 | 236.7 | 8.363 / 9.010 | 226.1 |
| 8 | 33.164 / 33.616 | 277.8 | 8.773 / 10.400 | 241.1 |
| 16 | 29.612 / 33.347 | 348.5 | 9.519 / 9.689 | 252.8 |

Each cold repeat issued 514 cache-write statements and read raw equity and
actions for 64 results. Each backfill repeat issued 66 cache-write statements,
read raw equity for 64 results and no actions. The SQL counter observed 525
write queries for cold and 77 for backfill including transaction statements;
these are not physical-scan counts. Backfill's 128 raw-equity SELECT statements
are two intentional reads per result: one full-history aggregate for exact
raw/invalid counts and one bounded 28-day path with edge sentinels. Cold instead
loads one full source equity series per result along with actions to rebuild
the seven window rows. This is an I/O tradeoff, not an accidental duplicate
fetch; the aggregate still scans history in DuckDB. Worker 4 had the lowest
median in this 64-result sample, while worker 16 had the highest observed RSS.
Cold time is non-monotonic across worker counts, and three measured runs per
cell do not establish a general tuning recommendation. The differences do not
justify changing the shared 16-worker default for other import/cache workloads
without a larger, representative measurement. The recalculation JSONL SHA-256
is `4f385af77e13cfb340285370c574485128bb28291340c4f33ee927c5ba6c0140`.

On the same 64-result slice, all-warm preview medians were 0.415 s
(legacy-controls), 0.455 s (filter-only), 0.485 s (equity-rank-only) and
0.393 s (both). Maxima were 0.438/0.480/0.563/0.433 s; maximum parent RSS
was 179.3/177.9/179.4/181.0 MiB. All four modes had stable decision
signatures, zero cache writes and zero raw-equity reads. JSONL SHA-256:
`3be46db7ec63ef45666e1e173f44579280ef5e5947ad188eb07d96c11e8eeff2`.
Some mode ranges overlap, others do not. With only three repeats and a fixed
mode order, these medians do not establish a causal speed ranking. The 64- and
512-result medians come from different run sets; they are not a measured
scaling curve.

The filter classified 3 GROWING, 21 WEAKENING and 40 DECLINING_OR_MIXED.
Against current-runtime legacy-controls Top 20, filter-only removed strategy
IDs 4959, 5842, 5848, 5944, 6623, 6773, 6955 because their H was
DECLINING_OR_MIXED and admitted 5283, 6150, 6583, 6598, 6779, 7144, 7413
to fill the slots. Equity-rank-only removed 4959, 5842, 5848, 5944, 6060,
6623, 6773, 6955 and admitted 5283, 5728, 6150, 6167, 6583, 6779, 7402,
7413 by the equity-quality Top N tuple, not by filter exclusion; the overlap
of seven removed IDs does not imply that filter and rank agree by construction.
Both controls selected the same 20 IDs as equity-rank-only in this cohort;
filter dispositions still
blocked the declining candidates. Top 20 is 31.25% of this 64-strategy pool;
replacements come from the other 44 and churn cannot be extrapolated to the
full corpus. These are deterministic selection changes, not evidence of better
future trading returns.

A separate combined 512-result replay was stopped after its warm phase to
avoid 32 large repeated recalculations; its warm output is not used above.
No exact replacement report/inbox was available for a one-REPLACE timing run.
Full-corpus timing and user acceptance of the observed speed remain open. The
old-runtime comparison and percentage-over-baseline gates are withdrawn, not
failed.

The post-measurement focused/broader Performance v2, Panel, cache lifecycle,
benchmark and slice-creator suite passed 844 tests with four Windows symlink
skips. It reported 44 pandas DataFrame-fragmentation warnings in existing
selection/review display paths; no runtime code or shared worker default was
changed in this measurement update.

The documentation and measurement interpretation received independent Opus 5
`CODE_REVIEW_PASS` on the second review round after source-scope, thread-counter,
backfill-query, statistical and status clarifications. This review passes the
bounded evidence update, not M5 full-corpus performance acceptance.
