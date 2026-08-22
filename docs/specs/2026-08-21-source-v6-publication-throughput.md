# Source v6 publication throughput — specification

**Status:** Implemented — measured on the Debian corpus; independent review pending
**Depends on:** [Source v6 high-throughput import plan](../superpowers/plans/2026-08-21-source-v6-high-throughput-import.md),
[ADR-0012](../decisions/0012-source-v6-fresh-compact-v1.md), [ADR-0014](../decisions/0014-source-v6-compact-publication.md)

## Problem

The sealed-segment import plan removed the parse bottleneck but left the
reduce/publish path proportionally dominant. Measured on the 5,859-report Debian
corpus (`/opt/hb1/debian-duckdb-importer/data/html/1`, 32 cores):

| Phase | Time | Bound by |
| --- | --- | --- |
| Parallel parse to 92 sealed leaf segments | ~4 min | CPU, 30 workers at ~92% |
| Hierarchical reduce + publish | dominant remainder | single parent, disk |

Three defects make the tail super-linear in corpus size.

1. **Per-fragment duplicate probe.** `import_fragment_batch` issues
   `select ... where source_sha256 = ? or fragment_id = ?` per fragment. The
   `OR` across two columns does not use an index, so each probe scans a table
   that grows with every insert — quadratic in fragment count. In the reduce
   path the target database is always freshly created, so the probe cannot
   match anything.
2. **Per-day row inserts.** `day_ownership` is filled by one `execute` per
   calendar day per fragment, producing on the order of one million individual
   statements for a single corpus.
3. **Per-fragment decode readback.** Each committed row is re-read, fully
   decoded through `_row_fragment`, and compared against the whole in-memory
   fragment. Decoding is the single most expensive operation in the importer
   and is already parallelised across workers; repeating it serially per
   fragment reintroduces the cost the segment design removed.

Separately, the fan-in tree re-materialises every payload at every level.
Measured write amplification on the same corpus: **6.8 GB of intermediate
segments for 1.5 GB of leaf data**.

At the user's production scale (10 batches of roughly 58,000 reports each) the
quadratic probe alone makes a 10x larger batch approximately 100x slower.

## Goal

Make reduce and publication linear in fragment count and keep payload bytes out
of Python, without changing canonical fragments, published
`source_content_digest`, quarantine/audit lineage, stitching, or atomic
publication.

## Non-goals

- Changing canonical fragment bytes, fragment identity, or the published
  `source_content_digest` definition.
- Changing leaf segment schema, worker failure semantics, or preflight.
- v5 migration, pair compaction, or analysis-side behaviour.
- Removing integrity checking. Cheaper checks replace redundant ones; nothing
  becomes unchecked.

## Contract changes

### C1 — Merge passes sealed rows through

Intermediate merges copy stored compact rows verbatim. They must not decode a
payload or rebuild a `SourceV6Fragment`. Integrity is enforced by comparing the
stored `header_sha256` and `payload_sha256` against SHA-256 of the stored
header and payload, and by the existing segment `compact_digest`.

Rationale: the bytes written by a merge are byte-identical to the bytes read;
decoding to re-encode the same bytes is redundant work.

### C2 — Reduce levels are parallel

Merges within one reduce level are independent: each reads its own consecutive
group and writes its own private output. They may run concurrently, bounded by
a `workers` argument. Level order is unchanged and the final Source DB writer
remains the parent alone.

Licensed by the existing proof that fan-in shape does not affect the result
(`test_hierarchical_fan_in_is_semantically_equivalent`).

### C3 — Segment reads do not decode

`_read_segment_outcomes` — the function the live reduce path uses — verifies
prepared-row correspondence from the stored `fragment_id` and `source_sha256`
columns against the sealed outcomes, plus the compact digest, not by decoding.

The outcome digest and the compact digest each seal one table, so neither can
detect that the two were zipped together wrongly; equal counts do not imply
equal pairs. The ordinal correspondence check is therefore explicit and
separate from both digests.

### C3a — Publication re-derives every fragment identity

Checksum columns only prove a row is self-consistent, not that it is the row
that was sealed. Publication therefore re-derives the identity from the stored
bytes: `sha256(zlib.decompress(payload_blob)) == fragment_id`, which covers
every canonical field transitively.

The canonical document is then compared against **every** analytical field of
`header_json`, not only the fields the indexed columns duplicate. A header
carries `stitchability`, `initial_balance`, `fixed_order_balance`,
`balance_percentage`, `settings_fingerprint`, `metrics` and
`open_tail_cycle_ids` to analysis without any column to check them against, so
comparing header-to-column alone compares two halves of the same forgeable row
and settles nothing. Only the canonical bytes are content-addressed. A
resealed segment that edits such a field consistently — header, column and
`compact_digest` together — publishes silently unless this check is present;
`test_reduce_rejects_a_consistently_forged_header` covers all eleven fields.

This is not optional. Without it a tampered or bit-rotted segment column
publishes silently with a plausible `source_content_digest` and
`safe_to_delete=YES`, which is the gate that authorises deleting raw HTML.
Measured cost on the 62,208-report corpus: 88 s of a 1,764 s import (5%).

### C4 — Publication is set-based

`import_fragment_batch` must not perform per-fragment table scans, per-day
statements, or per-fragment decode readback. Required behaviour:

- Duplicate resolution uses one pre-pass over existing identity columns; when
  the target is fresh and empty, no probe is issued.
- `points`, `compact_fragments`, `fragment_origins`, `import_audit` and
  `day_ownership` are written set-based, one statement per table per batch.
  `executemany` does not qualify: DuckDB binds one row per call, measured at
  1,730 rows/s against 1,969,809 rows/s for the same rows inserted through a
  registered frame — 1138.9x. A single 62,208-report corpus emits ~1.2M
  `day_ownership` rows, so this is hours against seconds, not overhead.
  `_insert_frame` therefore registers a pandas frame and issues
  `insert … select … from <frame>`. Two properties of that helper are
  load-bearing rather than incidental: the frame is built with `dtype=object`
  so DuckDB receives the original Python objects — a `datetime.date` stays a
  DATE and `None` stays SQL NULL instead of pandas' `datetime64`/`NaN`
  coercions — and `unregister` runs in a `finally`, because a constraint
  failure inside the transaction must not leave the view bound. Each table
  keeps the conflict policy it had: `or ignore` where it had one, plain
  `insert` where a repeated key is a real fault.
- Readback verification compares the stored payload checksum, not a decoded
  fragment. A decode-based equality check may run at most once per batch when
  explicitly requested, never per fragment.

Committed rows, generation accounting, audit statuses, `safe_to_delete`
semantics and the final `source_content_digest` update are unchanged.

### C5 — Reduce publishes in one pass, without a Python payload round-trip

Reduce no longer builds a merge tree. It reads only segment manifests and
outcomes, resolves duplicates over metadata, then `ATTACH`es each sealed segment
and copies accepted rows straight into the target with a set-based insert.
Payload bytes never enter Python, and the corpus is written once instead of once
per tree level. `fan_in` is retained for contract compatibility and no longer
shapes a tree.

### C6 — Fragment decoding for stitching is parallel

The single-writer tail decoded every fragment serially. Decoding is independent
per fragment, so `iter_fragments_parallel` decodes explicit id slices across
processes and returns exactly what `iter_fragments` returns for the same
database. Verification that reads only `fragment_id` uses `fragment_ids`, which
decodes nothing.

### C7 — Stitch decisions are written as one batch

`persist_batch_resolution` must not open a connection and transaction per
decision. Decision fact rows are computed first, then applied through a single
`persist_fragment_resolutions` call in one transaction, in the same order
sequential writes would have applied them. Selecting the outgoing fragment uses
an ordered binary search rather than rescanning all active fragments per
decision, and incoming event ownership uses a set lookup rather than rescanning
accumulated fact rows.

### C8 — The merge verifies its staging file after the commit, before publication

`_copy_fragments_from_inputs` runs the C3a readback *after* `commit`, not
inside the copy transaction.

The reason is measured; the mechanism is only partly established, and this
section states no more than was proven. The two measurements below are on
different artifacts, not an A/B on one.

The readback windows rows 128 ids at a time — 493 windows for the
63,131-fragment merge. Against the **committed** 59,675-fragment input database
(4.3 GB of payload) one window costs 0.239 s, far too fast to be reading the
payload column, so the scan touches `fragment_id` and materialises payload only
for the 128 matches. Against rows still open in the copy transaction, the merge
of both corpora did not finish in 40 minutes, parked in
`_verify_published_identity` under `py-spy`. `EXPLAIN` returns the same
`SEQ_SCAN` + `HASH_JOIN (MARK)` plan in both cases, so index availability is
**not** the difference — an earlier revision of this section claimed it was and
was wrong. Transaction-local storage holding the merge's 4.7 GB of payload
(5,041,855,558 bytes, read from the published artifact) is the likeliest cause;
that has not been proven and is not asserted.

This leaves an open question rather than a closed one: the readback is a
sequential scan per window on both the merge and the import path, so it is
O(n²/128) scans in principle. It does not bite on the import path, which
commits `compact_fragments` per group and holds only metadata rows open at
verify time — measured at 5,000 fragments, 5.52 s committed against 5.47 s with
that transaction open. Replacing the 512 windows with a single streamed ordered
scan would remove the question entirely. A relation cursor is not that
replacement: `fetchmany` over the payload column grew resident memory by 2.9 GB
on a 4.3 GB corpus, so it materialises rather than streams. Finding a
bounded-memory single pass belongs to its own change.

The fail-closed guarantee is unchanged, because committing is not publishing.
The merge writes to a `.staging` file; publication is `compacted.replace(target)`
at the end of `merge_source_v6`, which is reached only if the readback raised
nothing, and the `finally` removes the staging file on any failure.
`validate_source_v6_database` already ran post-commit on the same staging file
for the same reason. No committed database ever becomes reachable unverified.

`fragment_origins` is not written by `_copy_fragments_from_inputs` at all:
`merge_source_v6` deletes and rewrites the whole table from the flattened
per-input lineage, so a write there is discarded unread. The surviving rewrite
is set-based under C4 and keeps `insert or ignore`.

### C9 — The merge readback runs across processes

`merge_source_v6` accepts `workers` and verifies the staging file with
`verify_published_identity_parallel(staging, workers=workers)`. The default is
`workers=1`, which takes the same serial path as before, so every existing
caller and test is unchanged; the panel passes the configured
`duckdb_import.workers`.

The split is measured, on the published 5.6 GB merge artifact (63,131
fragments, read-only). One 128-id window costs 2.460 s, and 494 windows
extrapolate to **1,215 s of the 2,080 s merge** — the readback is the single
largest phase, not a tail. Within one window the SQL fetch is **1.91 s against
47.56 s of Python for 20 windows, a 3.9% share**; the remaining 96.1%
decomposes as 64.3% `_assert_canonical_matches_columns` (a `json.loads` of the
canonical document, which averages 1.4 MB uncompressed), 20.3% `zlib.decompress`
and 15.4% `sha256`. A `limit 128 offset 5000` window returns in 0.017 s.

Two things follow. The work is CPU-bound Python with no shared state, so it
parallelises across processes exactly as C6's decoding does — and better, since
each worker returns a verdict rather than a decoded fragment, so none of the
pickling that capped C6 at 2.1x applies here. And the C8 open question is
narrowed rather than closed: the per-window sequential scan is real but is 3.9%
of the window at this corpus size, so a bounded-memory single pass would recover
a small fraction of what parallelism recovers. It stays a separate change.

The structure mirrors `iter_fragments_parallel`. `verify_fragment_slice(path,
ids)` opens its own read-only connection, windows the slice by `_VERIFY_BATCH`
and raises `SourceV6StorageError` on any mismatch; the parent runs the C3a
column check once — it is a single DuckDB query, already internally parallel,
measured at 59.3 s — collects the ids, and fans the slices out.

The parent's **write** connection must be closed before the fan-out, because a
DuckDB file open for writing cannot be opened by another process. So the
readback moves out of `_copy_fragments_from_inputs` to its caller, immediately
after that function returns and its connection is closed. This preserves C8
exactly: verification still happens after the copy commits, still on the
`.staging` file, and still before the `compacted.replace(target)` that
publishes. Both halves are pinned by test — that the copy connection is
released and committed before verification, and that a readback failure
publishes nothing.

Worker count does not change the artifact. Slices are disjoint id sets, each
worker only reads, and the merge's ordering guarantees are unaffected, so
`workers=N` and `workers=1` must produce the same `source_content_digest`.

Measured A/B on the same 8,192 ids of the merged corpus: **134.2 s serial
against 19.0 s at 16 workers, 7.1x**. It plateaus there — 8 workers give 5.7x
and 24 give 6.8x, slightly worse than 16, so the 34 logical processors are not
the binding constraint and the default is not raised past what was measured.
The whole verification over all 63,131 fragments runs in **113.9 s at 16
workers**.

Those two do not reconcile cleanly, and the gap is left open rather than
explained away. Taking the serial readback as 1,215 s, the full-corpus payload
speedup implied by 113.9 s is far above the 7.1x the A/B measured on a subset.
Process-pool startup was the obvious candidate and was measured out: spawning
16 workers that import `mrs3` costs 1.1 s, about 6% of the 19.0 s subset run,
nowhere near the difference. The likelier explanation is that the 1,215 s
figure was extrapolated from windows timed on a cold page cache while the
parallel runs read a warm one, which would make the serial baseline too
pessimistic — but the controlled full-corpus A/B that would settle it was not
run. Treat 7.1x as the measured speedup and 113.9 s as the measured cost of the
phase; do not multiply the two into a claim about the whole merge.

That 113.9 s includes the 59.3 s column check, which is now more than half of
the phase and is the next lever rather than this one. It is a single DuckDB
statement, so it is DuckDB's own parallelism to improve, not a fan-out.

Memory is bounded per process, not globally: one window is 128 compressed
payloads plus the single canonical document being checked, so the peak is that
times the worker count — a few hundred MB at 16 workers and the 1.4 MB average
canonical size.

Re-running the full two-corpus merge with `workers=16` took **543.7 s against
the 2,080.1 s serial run**, publishing the identical
`source_content_digest` `a26c00b965680ab50afb72874bd89cb087441b1ca3433d2d1551b6cd4cc4c814`,
the same 63,131 accepted and 2,403 duplicates. Roughly 1,100 s of that 1,536 s
difference is the verification saving (1,215 s serial against the 113.9 s
measured above). The rest is not controlled: the serial run was the first read
of those 6.1 GB of inputs and the C9 run was not, so the page cache differs.
The end-to-end number is reported as what the merge now costs — about 9 minutes
where it was about 35 — not as this change's isolated speedup.

Equivalence is evidence, not inference: every published table was digested
inside DuckDB and compared between the two artifacts — `compact_fragments`
63,131, `points` 63,131, `fragment_origins` 65,534, `day_ownership` 1,205,395,
`import_audit` 63,131, and the three empty tables — all identical. `schema_info`
differs in `database_id` alone, which `create_v6_database` generates fresh per
merge; `schema_version`, `fingerprint`, `mutation_generation` and
`source_content_digest` match. The published byte sizes differ by 256 KB
(5,643,972,608 against 5,643,710,464), which is DuckDB block allocation and not
content.

`_verify_fragment_payloads` now also rejects a window that returns fewer rows
than it asked for, as `decode_fragment_slice` already did. That is not
reachable today — both reads see the same committed snapshot — but on the
parallel path the ids are scanned on a different connection than the one
reading payloads, and a silently short window here would mean unverified rows
behind a `safe_to_delete=YES`.

### C9 does not close the gap between the verified file and the published file

Stated here because C9's framing invites the assumption that it does. The C3a
payload readback runs on `staging`. `compact_v6_database` then rewrites those
bytes into `compacted`, and `compacted` is what `replace(target)` publishes.
What `compacted` does get is narrower than the readback but not nothing:
`validate_source_v6_database` (schema version and fingerprint), a per-table row
count, an id-set and digest comparison, and — through `fragment_metadata` —
`sha256(header_json) == header_sha256` plus header-against-column agreement for
every fragment. Exactly one column escapes: `payload_blob`. It is never
decompressed, its `fragment_id` is never re-derived from it, and its
`payload_sha256` is never recomputed on the published file.

This is pre-existing and unchanged by C9, and it is recorded rather than fixed.
It is worth revisiting precisely because C9 changes the economics: verifying
`compacted` too would have added a second serial ~1,150 s phase to a 2,080 s
merge, and now costs 114 s. Whether the raw-HTML deletion gate should require
it is a decision, not a refactor, so it belongs to its own spec.

## Invariants

- Published `source_content_digest` for a given input set is unchanged from the
  pre-change importer.
- Fragment identity, canonical bytes and codec are unchanged.
- Winner ordinals and the first-wins duplicate policy are unchanged across
  merge levels.
- **Amended:** duplicate inputs within one import now produce a
  `duplicate_fragment` quarantine row and an audit row, where the pre-change
  importer returned `IDEMPOTENT` with no quarantine. A corpus containing a
  duplicated report therefore reports `safe_to_delete=NO` instead of `YES`.
  The direction is deliberately fail-safe — a duplicate is now visible in the
  artifact rather than silently absorbed — but it does change the
  HTML-deletion gate, and it changes the `quarantine` and `import_audit`
  tables relative to the pre-change implementation.
- A corrupted header or payload still fails closed on segment read, on merge
  and on publication.
- `workers` affects only scheduling, never the published artifact.

## Materialization compatibility

The published database must remain a legitimate input to the existing
materialization/surface contract. This is not assumed; it is proven by a
whole-database equivalence test.

- The schema is untouched: `create_v6_database` is unchanged, including the
  `compact_fragments.fragment_id` primary key and `source_sha256` unique
  constraint that already enforce duplicate rejection at the storage layer.
- For an identical input set, every table must be row-for-row identical
  between the pre-change and post-change publication paths: `schema_info`,
  `compact_fragments`, `points`, `fragment_origins`, `day_ownership`,
  `import_audit` and `quarantine`.
- `mutation_generation` accounting is the highest-risk detail. Generation
  currently advances once per committed fragment and each `import_audit` row
  records its own `generation_before`/`generation_after`. Bulk publication must
  reproduce that numbering exactly, or lineage diverges.
- `validate_source_v6_database` continues to run at the end of reduce.

## Acceptance evidence

1. Focused RED-then-GREEN tests for C1–C7, including:
   - a whole-database equivalence test asserting every table is row-for-row
     identical to the pre-change publication path, `mutation_generation` and
     `import_audit` included;
   - merge performs zero `decode_fragment` calls and produces byte-identical
     rows to the decoding implementation;
   - reduce performs zero merges, writes no artifact other than the target, and
     is independent of the declared `fan_in`;
   - parallel decoding returns exactly the serial result, and an incomplete id
     slice fails closed;
   - bulk resolution writes through exactly one connection and leaves the
     database identical to sequential single writes;
   - `reduce_source_v6_segments` rejects a segment whose `report_start_ms`,
     `report_end_ms`, `stitchability`, `source_name` or `point_key` column was
     tampered with, and rejects a payload substituted together with its own
     checksum;
   - a hydrated and a metadata-only import of the same inputs produce
     row-for-row identical `compact_fragments`, `points`, `fragment_origins`,
     `day_ownership`, `quarantine`, `fact_ownership` and
     `fragment_resolutions`.
2. Full `.venv\Scripts\python.exe -m pytest` suite green.
3. Real Debian corpus run on the 5,859-report set: successful reopen/readback,
   zero quarantine, and a published digest equal to the pre-change digest for
   the same inputs.
4. Recorded wall-clock split by phase before and after.
5. Independent code review returning `CODE_REVIEW_PASS`.

## Measured outcome

Debian corpus `/opt/hb1/debian-duckdb-importer/data/html/1`, 5,859 reports,
32 cores, `workers=30`.

| | Before | After |
| --- | --- | --- |
| Total wall clock | 886 s | **299 s (2.96x)** |
| Parse to sealed segments | ~240 s | ~240 s |
| Reduce to staging | ~60 s | ~60 s |
| Tail (decode + publish) | ~575 s | ~10 s |

Published artifact is unchanged: `source_content_digest`
`c85fdb8372b2cce51d2a0e4aff537eb951e781eff5b4704a74d63c7163611b90` matches the
pre-change run, with identical `compact_fragments` (5,859), `day_ownership`
(113,104), `points` (5,859), `import_audit` (5,859), `mutation_generation`
(5,859), zero quarantine, and identical reported coverage.

Supporting measurements that shaped the work:

- Tail split: decode 568.8 s, `compact_v6_database` 6.4 s, `fragment_ids` and
  both validations 0.0 s. Compaction was never a bottleneck.
- Decode internals over 400 fragments: canonical re-serialisation for the
  identity check 43.2 s (51.8%), `_fragment_from_payload` 35.2 s (42.1%),
  `json.loads` 4.0 s, `zlib.decompress` 1.1 s. Replacing the re-serialisation
  with `sha256(raw)` costs 0.23 s and yields the same id.
- Parallel decode scaled only 2.1x across 30 processes (1202 s to 569 s),
  because decoded fragments must be pickled back to the parent. Not decoding at
  all is worth far more than decoding faster.
- Compression level was measured and rejected: level 6 compresses 2.97x faster
  than level 9 for 3.7% more size, but compression is only ~4.6% of the parse
  phase, so the whole change would save about 7 s of 299 s while invalidating
  every stored payload. Level 9 stays.

Second corpus, `data/html/3`, 62,208 reports: **1,764 s**, 59,675 committed and
2,533 quarantined for genuine parse failures (2,532 empty `walletSeries`, one
malformed settings JSON), 4.9 GB published. Both published databases pass the
C3a identity verification with zero mismatches.

Merge of both corpora, 65,534 input fragments and 6.1 GB of input: **2,080 s**,
publishing 5,643,710,464 bytes with `source_content_digest`
`a26c00b965680ab50afb72874bd89cb087441b1ca3433d2d1551b6cd4cc4c814`. Read back
from the published artifact: 63,131 `compact_fragments` (so 2,403 inputs were
cross-corpus duplicates), 5,041,855,558 bytes of payload, 1,205,395
`day_ownership`, 65,534 `fragment_origins` — one per *input* fragment, because
lineage keeps the duplicates publication drops — 63,131 `points` and 63,131
`import_audit`. Before C4 and C8 this merge could not complete and was aborted
twice.

## Rejected

- Lowering the zlib level (see above).
- Making `compact_v6_database` conditional: it costs 6.4 s.
- Parallelising the reduce tree: the tree itself was removed instead.

## Verification

All tests use `.venv\Scripts\python.exe -m pytest`. No `Input/`, `Output/`,
`Data/`, HTML, DuckDB, segment, benchmark or generated bundle artifact may be
committed.

## Known follow-up: orphaned segment-merge path

C1, C2 and C4 were implemented against `merge_source_v6_segments` and its
helpers `_merge_segment_contents` and `_read_source_v6_segment`. C5 replaced
that path with `_publish_segments_single_pass`, leaving those three functions —
and `import_fragment` / `import_fragment_batch` — without any production
caller. Their tests still pass but describe a path no import or merge takes, so
their measured evidence does not describe the shipped runtime. Removing them
touches ~50 test references and is a behaviour-preserving cleanup, so it
belongs in its own `refactor:` commit rather than this one.
