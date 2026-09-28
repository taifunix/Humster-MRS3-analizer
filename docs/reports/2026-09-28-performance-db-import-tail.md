# PerformanceDB: delay after the last parsed report

**Date:** 2026-09-28. **Code baseline:** fetched `8f59c2c`.
**Method:** read-only code trace and bounded synthetic fixtures; no live DB
mutation or tester run. The implementation follows the
[active optimization contract](../specs/2026-09-28-heavy-database-optimization.md).

The last local audit records a successful `REPLACE` of 409 reports, with zero
failures/skips/rejections. The local DB file is approximately 14 GB. Existing
job/audit records have no stage timings, so the production time distribution
is unknown. File size alone does not establish a bottleneck.

`import_performance_v2` emits `PUBLISHING N/N` before `_publish` begins. Work
still remaining is replacement deletion, action/equity append, grouped child
count verification, optimizer preparation, commit, connection close, parser
staging cleanup and the Panel's schema/count readback. Thus apparent 100%
does not mean that the transaction has committed.

The concrete repeated work is five child-table DELETE statements per admitted
replacement result. For 409 replacements this is 2,045 statements. T13a uses
the resolved replacement IDs for one set-based DELETE per table, reducing that
count to five while keeping the same transaction and result identities.
Index presence does not establish the scan plan. DuckDB 1.5 ART scans require
a single-column index; the explicit result/timestamp compound indexes are not
eligible, while foreign-key indexes can provide the eligible result index.
Verify the actual query plan rather than infer it from index names.
[DuckDB indexing documentation](https://duckdb.org/docs/current/guides/performance/indexing.html#art-index-scans).

A root probe on the actual synthetic schema (250 results/250,000 actions,
DuckDB 1.5.5) observed an Index Scan for scalar equality and a SEMI HASH_JOIN
with Sequential Scan for the UNNEST batch predicate. One deletion took 0.0078 s;
the three-result batch took 0.0119 s in single samples. This is not a median or
production gain. T13a must compare selective/scattered cohorts and preserve the
single-result path, in addition to reducing statement counts.

Other tail work remains material and must be measured separately:

| Stage | Current mechanism | Required integrity |
| --- | --- | --- |
| Child append | DataFrame append in row batches; column metadata read per flush | Exact Decimal values and target columns |
| Count readback | Two grouped child-count queries | Do not remove verification |
| Optimizer preparation | Synchronous CPU/JSON work inside publication | Final IDs, source digest and exact prepared bytes |
| Commit and close | DuckDB changed-page/index persistence | Atomic publication |
| Staging cleanup | Remove copied reports after readers close | Windows file ownership |
| Panel readback | Schema validation and four counts | Verified terminal state |
| Terminal job publication | Whole shared journal serialization under registry lock | Durable terminal state and private recovery data |

Window recalculation and equity-cache publication are separate explicit paths,
and are not invoked by the import service. Failure CSV/XLSX generation is
conditional; the successful zero-failure batch does not take that path.

A tiny 16-report available-input fixture took 2.527 s overall, including
1.8644 s in publication and 1.5315 s in optimizer preparation. These are nested
timings and include lazy startup. They are not a production fraction or a
throughput estimate. A warmed 1,000-cycle source (2,000 actions/3,000 samples)
took 0.823177 s in `prepare_optimizer_input` in one probe. It performed two
source digests and five source-document constructions. One digest assignment
is unused; removing it is the smallest additional CPU optimization. Larger
document caching needs separate ownership/revision evidence.
OPT-01a removes that unused assignment. Available, missing-fact and unsupported-
sizing regressions pin two digest calls before to one after, frozen source
digests and prepared JSON identity. Optimizer tests passed 28 and strict import
checks passed 4. A warmed 1,000-cycle comparison measured median 1.413928 s
before and 1.159939 s after, about 18% lower for preparation alone. The baseline
helper emulates the extra pre-change digest; three samples per variant matched
1,371,304 prepared bytes and SHA-256. This remains synthetic evidence;
independent Opus 5/high returned `CODE_REVIEW_PASS`; OPT-01a is accepted.

The original shared Panel job journal contains 109 jobs and 86,687,437 bytes
(82.67 MiB). A read-only, in-memory probe measured three repetitions of the
same deep-copy and sorted compact serialization used by `_save`: median
1.438544 s for `json.loads(json.dumps(jobs))` and 4.064874 s for `json.dump`
to `StringIO`. No journal or database was changed. This is about 5.5 s of CPU
per full save on this machine; filesystem write, fsync, atomic replacement,
lock waiting and other per-job copying are additional, unmeasured costs.

The dominant retained field is `runtime.campaign`: 80,953,127 encoded bytes
across 14 `portfolio.stage1` jobs. Tester evidence contributes about 5 MB;
all `result` fields together are about 70 KB. Import-job records total about
93 KB. The existing campaign stripping rule requires a verified campaign
snapshot beside the inline payload; none of these 14 entries has both, so it
cannot safely discard their inline data. Whole-journal deletion would remove
history, restart data and artifact links. Retention/compaction requires a
separate policy and has not been performed. The existing portfolio startup
migration already handles these legacy records with a backup and verified
snapshots: ten are failed jobs eligible for compact runtime, and four are
committed jobs. A read-only pure preflight repaired all four successfully;
their canonical 13,750,280 bytes compress to 1,433,391 bytes. The measured
free-space gate would pass. This supports reusing the existing migration,
but no live migration or journal cleanup was run.

The worker callback and each status poll invoke `_record_special_job`. Before
J4, even an unchanged import snapshot rewrote the complete journal. J4 now
skips identical clean terminal snapshots while the first/changed publication,
volatile changes, failed-save retries and filtered-load normalization remain
durable. Three identical terminal polls made zero journal replacements in a
controller test; a changed result made one and survived reload. A disposable
80,893,843-byte/109-job synthetic journal measured one first write, zero writes
for three repeated polls and one changed write (1.067 s, 0.000095 s total and
1.053 s respectively). Its repeated-string payload is unlike the real journal,
so these timings are mechanism evidence, not a production speed estimate.

The parser progress callback itself is in-memory only and causes zero journal
writes. Before J5, each unchanged RUNNING/PUBLISHING status
poll still wrote the full journal. J5 is the separately approved extension
for that path. J4 passed independent review and committed as `b31b37e`.
J5 now opts in unchanged RUNNING/PUBLISHING import snapshots too: a first
change saves once, three exact repeats save zero times, and changed state,
phase, progress or runtime still saves. A pending volatile update in another
job forces the whole journal to save. The full Panel suite passed 122 tests
with 4 Windows skips; integrated RETEST passed 68 with 1 Windows skip. J5-R2
independent review returned `CODE_REVIEW_PASS`. These counts establish journal replacement
behavior in private controller fixtures, not a production tail percentage.
The repository search found no consumer of the journal file's mtime or size:
the only production path constructors are Panel and Portfolio Panel, registry
load/save use its contents, and portfolio startup migration reads its bytes.
The private `_peek` has one production caller, `_record_special_job`, which
reads but does not mutate its nested values. Public get/list/runtime return
JSON-detached values; sync JSON-clones incoming progress, error, evidence,
result and runtime before updating the stored job under its lock, and the
callback does not mutate them after sync.
The import worker publishes only COMMITTED or FAILED, and there is no public
Performance v2 import cancel route. CANCELLED is a generic registry state but
not an ordinary repeated import-tail status; a future cancellation path would
need its own persistence/count check. The old terminal tester callback was
dead: current jobs store the tester link as a resource key,
not a request; optional legacy records reached an invalid empty-status sync
which changed no stored flag. Public import consumes verification before its
worker starts. Immutable `c1a5e0e` characterization passed three tests, and
J4 preserves current false and legacy true flags while removing that invalid
attempt. No live journal migration, cleanup or database write was run.

Review added two negative gate checks: identical RUNNING/PUBLISHING import polls
and identical COMMITTED callbacks of another job kind still save on every poll
in J4. Invalid/stale volatile payloads carrying otherwise valid phase, progress
and evidence leave the job and dirty flag unchanged; transition/expected-state
validation precedes all assignments. The only production volatile caller is
the tester hot path in `_record_special_job`: native tester snapshots allocate
their nested progress/evidence values, while RUNS documents allocate progress
and have no evidence. The callback returns immediately after volatile sync, so
it does not retain a mutable nested reference for later updates.
Independent J4 re-review also checked every registry mutator against the global
dirty flag. Restart recovery already saves its FAILED projection before a poll
can skip; a new regression verifies that disk state and zero extra replacements
on an identical recovered import poll. The audit found one real side path:
an unserializable runtime reservation previously left an empty in-memory
runtime object without marking it dirty. Serialization now precedes mutation;
its failing-before regression passes. The shallow volatile payload ownership
contract is explicit in code and spec. The focused registry suite passed 24
tests, and the independent J4-R4 review returned `CODE_REVIEW_PASS`.

The first broad J4 run used a coarse selector and omitted two additional
metadata checks; both passed/skipped identically on immutable base and current
branch (`1 passed, 1 skipped`). The corrected combined run deselected exactly
the seven baseline-failing RETEST nodes and passed `443`, skipped `7`,
deselected `7`. Both previously omitted metadata checks were included.
The immutable-base command used the project venv with `PYTHONPATH` and
`pytest -o pythonpath` pointing at exported `c1a5e0e` Panel modules, then:

```powershell
.venv\Scripts\python.exe -m pytest -o "pythonpath=$baseline" tests/test_panel_performance_v2_retest.py -q -k 'metadata_retest_inbox or retest_start_reuses_oldest_valid_inbox or committed_native_retest_verify_survives_restart' --tb=line
```

It returned the same seven failures (`strategy_path is missing` five times,
the reusable-inbox date guard once, and missing committed RETEST source
artifacts once), plus one pass and one Windows symlink skip. These are tests
written for old strategy roots; runtime continues to require trusted `Output`.
The complete repository suite remains the final integrated core gate after
those test fixtures are aligned in a separate scoped change.

T13a's real-schema, in-memory comparison used 1,000 results, 500,000 actions
and 32 scattered result IDs, with three repetitions per variant. The preserved
benchmark times DELETE alone: the median was 0.123736 s for scalar deletion
and 0.064546 s for batch deletion, about 47.8% lower. An earlier probe measured
32.5%; these small synthetic timings vary and are not a production promise.
Both variants left 484,000 rows, matched retained-row signatures and each
rollback restored 500,000. Single-result scalar deletion remains unchanged.
This is one child table on synthetic data; it does not measure the complete
import, journal publication or the production DB.

The failing-before regression observed ten child DELETEs for two admitted
replacements; the new path uses five. It preserves exact action/equity rows
and all five child tables of a rejected sibling, including seeded caches.
Singleton and multi-result cohorts are covered. The final importer suite
passed 84 tests; upstream Panel bootstrap/metadata checks
passed 9 with 2 skipped, and the supported-v4 migration check passed again.
Root separately confirmed four batch/rollback/strict-prepared regressions.
Independent Opus 5/high returned `CODE_REVIEW_PASS` in round 2 after guard and
read-order enumeration plus singleton characterization. T13a is accepted;
the broader IMP-02 task and final integrated full suite remain open.

T13b now reads constrained action/equity column metadata once per used table
within a publication and flushes each writer buffer as soon as its 20,000-row
cap is reached, even within one report. A second publication on the same
connection re-queries. At private caps 20,000, 2 and 1, a persistent regression
compared complete DuckDB action/equity rows, NULL and exact Decimal values,
prepared source digest/JSON and import-file ledger; all matched. A failure in
the second equity batch rolled back earlier writes. The importer suite passed
91 tests, selected Panel bootstrap/Output checks passed 7 with 1 skip, and
supported migration checks passed 3. Independent Opus 5/high re-review returned
`CODE_REVIEW_PASS`. This establishes query and buffer bounds, not an elapsed
speedup for the 409-report production import. Per-result publication metadata,
Phase8 preparation and commit still need separate measurement and work.
