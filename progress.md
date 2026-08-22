# MRS3 — current verification

**Updated:** 2026-08-22
**Current branch:** `main`
**Current feature:** Source v6 high-throughput import and merge — implemented,
measured on both real corpora, `CODE_REVIEW_PASS`. The merge readback is now
parallel (C9): 2,080 s to 544 s on the two-corpus merge, identical artifact.

## Source v6 import throughput (2026-08-21)

Contract: [publication throughput spec](docs/specs/2026-08-21-source-v6-publication-throughput.md)
and [high-throughput import plan](docs/superpowers/plans/2026-08-21-source-v6-high-throughput-import.md).

Measured on Debian `46.4.84.220`, `/opt/hb1/debian-duckdb-importer`, corpus
`data/html/1` (5,859 reports), 32 cores, `workers=30`:
**886 s to 299 s (2.96x)**, published digest unchanged
(`c85fdb8372b2cce51d2a0e4aff537eb951e781eff5b4704a74d63c7163611b90`).

Defects found and fixed, each measured rather than assumed:

1. Leaf scheduling capped in-flight chunks at `segment_writer_limit`, so 30
   workers ran 4-wide (12.5% of 32 cores). The writer limit now bounds only
   segment writes, via a pool semaphore.
2. Merges decoded and re-encoded sealed payloads at every tree level; segment
   reads decoded every row to compare two stored columns.
3. Reduce built a fan-in tree, writing 6.8 GB of intermediates for 1.5 GB of
   leaves. It now `ATTACH`es segments and copies rows in one SQL pass.
4. Publication issued a per-fragment duplicate probe (quadratic), one statement
   per calendar day, and a per-fragment decode readback. All are now set-based.
5. `decode_fragment` rebuilt the canonical document to re-derive the identity —
   51.8% of all decode time. The decompressed bytes are that document, so the
   id is `sha256(raw)`.
6. The tail decoded all 5,859 fragments to serve consumers that read metadata
   only. Metadata now comes from indexed columns; payloads are decoded only for
   surface publication or a point with a real overlap to persist.

7. Metadata publication bound one row per call. DuckDB `executemany` was
   measured at 1,730 rows/s against 1,969,809 rows/s for the same rows inserted
   through a registered frame — **1138.9x**. One corpus emits ~1.2M
   `day_ownership` rows, so this was the unexplained "last stage" cost on both
   the import and the merge path. Both now go through `_insert_frame`.
8. The merge ran its identity readback inside the copy transaction. Committed,
   one 128-id window costs **0.239 s** against the committed 59,675-fragment
   input database; with the rows still open in the copy transaction the merge
   of both corpora did not finish in 40 minutes (`py-spy` parked it in
   `_verify_published_identity`). These are two artifacts, not an A/B on one.
   The mechanism is not settled — `EXPLAIN` gives the same `SEQ_SCAN` plan
   either way, so it is not index availability; transaction-local storage of
   the merge's 4.7 GB of payload is the likeliest cause and is recorded as
   unproven. The readback
   now runs after the commit and before publication — on the `.staging` file,
   which `merge_source_v6` publishes only by `compacted.replace(target)`, so
   nothing committed ever becomes reachable unverified.

9. The merge's identity readback ran serially and was the single largest phase
   of it, not a tail: **1,215 s of the 2,080 s merge**, measured on the
   published 5.6 GB artifact at 2.460 s per 128-id window over 494 windows.
   Within a window the SQL fetch is only **3.9%** (1.91 s against 47.56 s of
   Python over 20 windows); the Python side is 64.3%
   `_assert_canonical_matches_columns`, 20.3% `zlib` and 15.4% `sha256`. It is
   therefore CPU-bound, per-fragment and shares no state. `merge_source_v6` now
   takes `workers` and calls `verify_published_identity_parallel`. A/B on the
   same 8,192 ids: **134.2 s serial against 19.0 s at 16 workers, 7.07x**;
   it plateaus there (8 → 5.74x, 24 → 6.82x). The full 63,131-fragment
   verification runs in **113.9 s**, of which 59.3 s is now the single-statement
   column check — that is the next lever, and it belongs to DuckDB's own
   parallelism rather than to a fan-out.

   Re-run end to end with `workers=16`, the two-corpus merge took **543.7 s
   against 2,080.1 s** — about 9 minutes where it was about 35. Roughly 1,100 s
   of that difference is the verification saving; the rest is uncontrolled,
   because the serial run was the first read of those inputs and this one was
   not, so the page cache differs. The subset A/B is the isolated measurement,
   and it does not reconcile cleanly with the full-corpus figure — see C9,
   where the discrepancy is recorded open rather than explained away.
   Equivalence was checked rather than assumed: every published table digested
   inside DuckDB and compared, all identical, with `schema_info` differing only
   in the per-merge `database_id`.

Verified equivalent: full-database dump comparison across all published tables
including `mutation_generation` and `import_audit`; surface publication fails
closed on metadata-only fragments.

Verification for all of the above, including C9:
`.venv\Scripts\python.exe -m pytest -q` — **1341 passed, 2 skipped** through
defect 8, and **1346 passed, 2 skipped** with defect 9.

## Merge of the two real corpora (2026-08-22)

`merge_source_v6` over `data/databases/1_3/` — 5,859 + 59,675 = **65,534 input
fragments, 6.1 GB** — completed in **2,080 s** and published 5,643,710,464
bytes with `source_content_digest`
`a26c00b965680ab50afb72874bd89cb087441b1ca3433d2d1551b6cd4cc4c814`. Read back
from the artifact: 63,131 `compact_fragments` (2,403 inputs were cross-corpus
duplicates), 5,041,855,558 bytes of payload, 1,205,395 `day_ownership`, 65,534
`fragment_origins` (one per *input* fragment — lineage keeps the duplicates
publication drops), 63,131 `points`, 63,131 `import_audit`. The same
merge previously could not complete at all: it was aborted twice, once parked
on the `day_ownership` bind (defect 7) and once on the in-transaction readback
(defect 8).

Merge order does not matter and cannot be chosen: `merge_source_v6` forbids the
target from existing or being an input, so there is no base to merge *into*.
The duplicate winner is the smallest `(source_sha256, source_name)`, publication
order is `(point_key, report_start_ms, fragment_id)`, copy order is by sorted
input path, and origins are re-sorted before the rewrite — so the artifact is a
function of the input set alone.

## Debian corpus `my_test_CX_GE_fixed` (2026-08-21)

Imported on `46.4.84.220` with `workers=30`: 38,305 HTML reports (36 GB) in
~15 min — **38,160 COMMITTED, 145 QUARANTINED** (144 × non-empty `walletSeries`
required, 1 × exactly one complete settings JSON object required).
`safe_to_delete=NO`, so the raw HTML must not be deleted.

All 145 were identified and inspected: `quarantine` stores no file name, so the
`source_sha256` values were exported and matched against a parallel sha256sum of
all 38,305 server-side HTML files — all 145 matched, confirming `source_sha256`
is the sha256 of the raw report. They are 102 × `CXMTUSDT_5m`, 36 ×
`CXMTUSDT_15m`, 6 × `CXMTUSDT_4h` (run ids 30577–38298) plus one stray optimizer
summary page, `report_optimizer_my_test_auto_x_auto_y_20260820_232555.html`,
which is not a run report at all. An earlier note here called all of these
source defects. That was wrong for the 144: `my_test_run_30577_of_38304_
CXMTUSDT_5m_2026-07-29.html` is a complete 1,183,513-byte report that emits
`const walletSeries = [];` and `const equitySeries = [];` because no trade
occurred in that shift window. They are valid zero-activity runs the importer
rejects — see Next. Published:
`compact_fragments` 38,160, `points` 38,160, `day_ownership` 766,702,
`import_audit` 38,305, max `generation_after` 38,160, 3,215,208,448 bytes.
Downloaded to `data/databases/`; byte size matches. The server-side original is
untouched. This run predates defects 7 and 8 above, so re-importing on the
fixed runner should be materially faster.

## Next

1. Decide whether zero-trade runs are valid Source v6 fragments. The 145
   `my_test_CX_GE_fixed` quarantines were inspected on the server and are not
   defective reports: the tester emits `const walletSeries = [];` and
   `const equitySeries = [];` for a shift window in which no trade occurred.
   The importer rejects that as a parse failure, which holds `safe_to_delete`
   at `NO` for the whole corpus and so blocks raw-HTML deletion. Accepting them
   needs a contract for what a fragment with no samples means for
   `report_start_ms`/`report_end_ms`, `day_ownership` and coverage, so it is a
   spec, not a patch. Re-importing that corpus is not required for correctness.
2. The C3a payload readback runs on `staging`, never on the `compacted` file
   that is actually published; `compacted` gets only schema, row-count and
   digest checks. Pre-existing, but C9 makes closing it cheap (114 s rather
   than a second ~1,150 s phase). It changes the deletion gate, so it needs its
   own spec.
3. Remove the orphaned segment-merge path (`merge_source_v6_segments`,
   `_merge_segment_contents`, `_read_source_v6_segment`, `import_fragment`,
   `import_fragment_batch`) in its own `refactor:` commit — C5 left them
   without a production caller, and ~50 tests still reference them.
4. Open question from C8: the identity readback is a sequential scan per
   128-id window on both paths, so O(n²/128) in principle. It does not bite on
   import (5.52 s committed against 5.47 s with the metadata-only transaction
   open, at 5,000 fragments). A relation cursor is not the fix — `fetchmany`
   grew resident memory by 2.9 GB on a 4.3 GB corpus. A bounded-memory single
   pass is its own change.
5. The parse phase is now the dominant remaining cost. Compression level was
   measured and rejected. Swapping the raw-markup cross-check to the lexbor
   engine was measured at 3.9x on that step and then **reverted**: lexbor is an
   HTML5 tree builder and performs the same implicit-close recovery as lxml, so
   the two parsers stopped being independent. The cross-check must stay a
   tokenizer; no faster second parser has been found that preserves it.

## Prior feature: Source v6 fresh compact multi-scope

STAGE_1_GATE=ACCEPTED_BY_ROOT; date=2026-08-20; reviewer=CODE_REVIEW_PASS compact-publication and gate-checker final re-reviews; evidence=.codex/stage1-acceptance-ledger.md,.codex/task5-real-corpus-report.md,.codex/task6-merge-evidence-report.md,.codex/task6-recovery-overlap-report.md,.codex/task6-debian-recovery-report.md

## Verified implementation

- Fresh compact Source → multi-scope surface → separate analysis pipeline is complete.
- Panel supports multiple READY scopes; the analysis worker limit is
  `duckdb_import.workers` and `gap_rules` is part of analysis identity and
  selection.
- Independent review: `CODE_REVIEW_PASS`.
- Latest full local verification: `1206 passed, 2 skipped, 1 warning` via
  `.venv\Scripts\python.exe -m pytest -q`.

## Next: manual verification

1. In the panel, import the intended raw HTML set and select one or more READY
   `symbol|side|timeframe` scopes.
2. Confirm a new `.surface-v6.duckdb` appears under
   `Output/surfaces-v6-compact/`, then run analysis with the intended listing
   dates and configuration.
3. Confirm the `.analysis-v6.duckdb` appears under
   `Output/analysis-v6-compact/`; repeat after changing `gap_rules` and verify
   that it produces a distinct analysis artifact and expected structure result.
4. Before syncing Git, review the scoped diffs/commits; local `Input/`,
   `Output/` and `Data/` must remain untracked.
