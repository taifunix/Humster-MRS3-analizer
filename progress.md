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
defect 8, **1346 passed, 2 skipped** with defect 9, **1347 passed, 2 skipped**
with C10 (ADR-0015), and **1363 passed, 2 skipped** with ADR-0016.

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

## Zero-activity runs are imported (2026-08-22)

Contract: [zero-activity spec](docs/specs/2026-08-22-source-v6-zero-activity-runs.md),
[ADR-0016](docs/decisions/0016-source-v6-zero-activity-runs.md).

The 144 `walletSeries`-empty quarantines of `my_test_CX_GE_fixed` were complete
reports of runs in which no trade occurred, not defects. They are now admitted,
but only on affirmative evidence: `Total Trades` and `Total transactions
(buy/sell)` present and zero, corroborating metrics consistent where present,
and the seven undefined ratios as the literal `n/a`. Absence of data is never
accepted as evidence of emptiness, because a truncated report has none either.
Opt-in per caller; only `normalize_source_v6` opts in, so ADR-0006's DD5
candidate contract is untouched.

Four defects were found by review across two rounds, each reproduced against
the repository's own fixtures before being fixed. A zero-activity outgoing fragment triggered
ADR-0013 seam exclusion and deleted the incoming fragment's only cycle while
reporting the batch `COMMITTED` — seam exclusion de-duplicates, and an empty
fragment has nothing to de-duplicate. And for an identical window the empty
fragment took ownership by `fragment_id` sort order, flagging the fragment with
four real actions as `AMBIGUOUS_INCOMING`. Round two found the mirror image of
the first — an empty *incoming* deletes the outgoing's open tail, one cycle and
two actions, also under `COMMITTED` — now `BRIDGE_NOT_COVERED`/`PARTIAL` as
ADR-0010 already specified; and that the new tie-break had desynchronised
`resolve_batch` from `persist_batch_resolution`, which re-derives the outgoing
side by bisecting its own ordering. A further one was found by the tests
themselves: a report with actions but empty series was admitted, which is not a
run where nothing happened but one whose samples did not render.

The 145th quarantine, an optimizer summary page, still fails. Re-importing the
CX_GE corpus is required to gain the 144 points; nothing is migrated.

## Surface publication throughput (2026-08-22)

Contract: [surface throughput spec](docs/specs/2026-08-22-source-v6-surface-throughput.md).

Measured on `my_test_CX_GE_fixed`, scope `CXMTUSDT|LONG|15m`, 648 fragments,
43 MB of payload: metadata + readiness 4.9 s, hydration 49.8 s,
`materialize_source_v6` 0.1 s, publication **100.4 s**, resident memory 42 MB to
2,450 MB. That is 239 ms per fragment; the whole 38,160-fragment corpus
extrapolates to ~2.5 h and ~74 GB resident, so it could not be published at all.

The preflight needs nothing: `preflight_source_v6` returns in 0.00 s and reads
no HTML, and `canonical_ready_intervals` costs 1.7 s over 38,160 fragments —
both already work from metadata. `folder1` correctly reports 0 READY scopes
because its widest grid is 12 of the required 114 point variants; `CX_GE`
reports 55 of 56.

Publication carried three defects already fixed elsewhere: it re-encoded the
sealed payload (59 ms each, and the result is byte-identical to what is stored —
120/120 on payload, codec and `payload_sha256`), inserted one statement per row
(736 rows/s), and ended by decoding every fragment again to check ids it could
derive directly (48.1 s of the 100.4 s). Payloads are now copied by SQL from the
source database, rows are written through `_insert_frame`, and publication
validates the C3a identity instead of reconstructing objects.

**Publication 100.4 s to 3.0 s (33x)**, same `surface_id`, and the two artifacts
compared directly: manifest, scope manifests, factual rows and the payload bytes
of all 648 fragments identical. The file is 43% smaller as a side effect of the
set-based insert.

Hydration is now the dominant cost and is untouched: `materialize_source_v6`
still rejects metadata views, so the caller decodes every fragment although
nothing on the publication path needs a decoded object.

The pass-through is opt-in and **the panel does not pass it yet**. Its single
production call site, `panel.py:1837`, was being edited by another session, so
switching it was left out rather than conflict. Until that one argument is
added the application still takes the 100.4 s path.

## Empty result combinations (2026-08-22)

Contract: [empty result spec](docs/specs/2026-08-22-source-v6-empty-result-combinations.md).

A "point" is a parameter combination — shift, open MA and close MA over one
symbol, side and timeframe — and since ADR-0016 one of them can be tested and
produce no trades. `calculate_metrics` raises for a combination with no samples,
and every consumer called it in a bare loop, so one such combination aborted the
whole build. Demonstrated: ten healthy combinations published, the same ten plus
one idle one raised and all eleven were lost.

Such a combination now keeps its cell in the canonical grid and carries the flat
result the tester itself declared — PnL 0, drawdown 0, no trades, every ratio
`None` under ADR-0006 — and is recorded under `empty_result_points`.

An earlier revision of this change excluded the cell instead, and that was wrong.
Exclusion published a 113-of-114 grid, which `load_source_v6_pipeline_input`
rejects with `INCOMPLETE_GRID` one stage later, naming neither the reason nor the
cell — a loud publish-time failure turned into a quiet artifact that dies later.
The objection to keeping it (that `build_persisted_analysis_facts` defaults a
missing metric row to 0% return at 0% drawdown, an outstanding risk-adjusted
result that never happened) does not apply: `annotate_eligibility` runs before
plateau geometry and rejects the cell with `REJECT_PNL_NONPOSITIVE` and
`REJECT_DD_NONPOSITIVE`. Verified — `plateau_id: None`, `role: UNASSIGNED`. It is
visible and unselectable, which is what a tested-and-idle combination should be.

A window that hides a *measurable* combination is still an error and raises,
naming the combination; that is a different fact and must not be flattened into a
zero.

The multiscope path needed the same rule one stage earlier: it stores facts, not
metrics, so it published happily and `run_multiscope_analysis` aborted
afterwards. `materialize_source_v6` now measures each scope over that scope's
READY witness — the same window the analysis measures over.

Coverage is not lost: the tested days remain in the source database as
`ACTIVE_EMPTY` under Z4.

## Next

1. Re-import `my_test_CX_GE_fixed` to pick up the 144 zero-activity runs and
   confirm `safe_to_delete` is no longer held at `NO` by them. The surface
   blocker that stood here is fixed — see "Empty result combinations" above.
2. Pass `source_database` at `panel.py:1837`, the only production caller of
   `publish_multiscope_surface`. One argument; it is what makes the 33x
   reachable from the application.
3. Let `materialize_source_v6` work from metadata. Readiness already does, and
   after the pass-through above nothing on the publication path needs a decoded
   fragment — so hydration is pure waste, and it is what makes a full-corpus
   surface need ~74 GB resident.
4. Close the same published-file gap on the import path. ADR-0015 scoped
   itself to the merge deliberately: `_publish_segments_single_pass` verifies
   its reduce target and then publishes a repack that receives neither the C3a
   payload readback nor `fragment_metadata`'s header pass, so the import
   publishes with weaker evidence than the merge now does — under the same
   `safe_to_delete=YES`. It needs its own change, not an assumption from
   ADR-0015's wording.
5. Remove the orphaned segment-merge path (`merge_source_v6_segments`,
   `_merge_segment_contents`, `_read_source_v6_segment`, `import_fragment`,
   `import_fragment_batch`) in its own `refactor:` commit — C5 left them
   without a production caller, and ~50 tests still reference them.
6. Open question from C8: the identity readback is a sequential scan per
   128-id window on both paths, so O(n²/128) in principle. It does not bite on
   import (5.52 s committed against 5.47 s with the metadata-only transaction
   open, at 5,000 fragments). A relation cursor is not the fix — `fetchmany`
   grew resident memory by 2.9 GB on a 4.3 GB corpus. A bounded-memory single
   pass is its own change.
7. The parse phase is now the dominant remaining cost. Compression level was
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

## Parallel panel work (2026-08-22)

Static Control Panel v1 is implemented and independently reviewed
`CODE_REVIEW_PASS`. It replaces the root shell, keeps `/legacy`, and covers
local testing, guarded remote profile operations, Source DB import/merge,
READY-only immutable surfaces, fresh analysis → local tester → Performance DB
and `CALCULATION_ONLY` DD5, plus local settings. Job terminal snapshots and
tester inbox lineage survive controller restart; interrupted remote importers
are rehydrated solely for a safe stop attempt. Portfolio remains disabled.

Latest verification: `1523 passed, 2 skipped, 1 warning` via
`.venv\Scripts\python.exe -m pytest -q`; focused panel suite: `68 passed`.
Contract and visual evidence: `docs/specs/2026-08-22-panel-static-frontend-v1.md`.

## Selected-scope surface materialization (2026-08-22)

The panel now keeps preflight metadata-only and hydrates only payload fragments
belonging to the explicitly selected READY scopes.  Hydration is deterministic
and parallel within the existing bounded worker limit; the whole-source lineage
digest still comes from validated Source DB metadata.  `materialize_source_v6`
remains hydrated and witness-based, because E1 requires `calculate_metrics` to
distinguish an actually idle point from data hidden by the selected window.

Evidence: `39 passed in 7.92s` over selected serial/parallel storage readers,
empty-result E1--E5, materializer, surfaces service and static panel tests;
independent review `CODE_REVIEW_PASS`.  The heavier throughput suite was not
claimed as evidence: its terminal invocation exceeded the local tool limit and
only its own child processes were stopped.  Source backup before this work:
`backups/surface-contract-before-metadata-materialization-2026-08-22.zip`.

Surface output now defaults to an editable `{pair...}_{start}_{end}` filename;
the immutable `surface_id` remains only in its manifest.  The Strategies/DD5
Analysis DB field derives an editable filename below saved `analysis_db_root`
and that full target is passed to the analysis runner.  Repeated explicit names
fail closed; automatically generated conflicting analysis names receive a
readable numeric suffix.  Evidence: `33 passed in 8.99s`, `node --check
src/mrs3/panel_web/app.js`, `git diff --check`; independent review
`CODE_REVIEW_PASS`.

The Strategies/DD5 selector now reloads every manifest-validated published
surface recursively from configured `source_v6_surface_dir`, falling back to
`data/surfaces`; full payload validation remains immediately before analysis,
not on panel bootstrap. `surface_target_path` is an approved, persisted panel
default, so the publication-card save button writes it to `config.local.json`.
Evidence: `49 passed in 15.64s`, JS syntax check, diff check, live local
catalog: one surface in `2.4s`; independent review `CODE_REVIEW_PASS`.

Fresh analysis now immediately reports its real entry phase, an indeterminate
bar and elapsed time; it does not invent a percent because the synchronous
analysis contract exposes none. The control is restored on every terminal
result. Evidence: `47 passed in 16.86s`, JS syntax check, diff check,
independent review `CODE_REVIEW_PASS`.

## Approved: Source v6 facts and metrics v2 (2026-08-23)

Contract and implementation plan are approved for a fresh-only rebuild:
[metric contract](docs/specs/2026-08-23-source-v6-metric-contract.md),
[ADR-0017](docs/decisions/0017-source-v6-facts-and-metrics-v2.md), and
[minimal rebuild plan](docs/superpowers/plans/2026-08-23-source-v6-minimal-rebuild.md).

**Next step:** record the three-run v1 baseline specified in Stage 0, then begin
the atomic facts-only v2 boundary. No v2 performance or correctness claim is
valid until a post-M7 fresh import has zero quarantines and Stage 4 evidence.

Current repository verification: `1597 passed, 2 skipped, 2 warnings` via
`.venv\Scripts\python.exe -m pytest -q`. The warnings are the existing tar
deprecation and unavailable Windows pytest cache; two symlink tests skip on
this host.
