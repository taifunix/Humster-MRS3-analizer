# PerformanceDB lossless compaction implementation plan

> Use subagent-driven-development or executing-plans, with independent review
> after each accepted implementation slice. Root owns deployment decisions.

**Goal:** shrink PerformanceDB while preserving exact data and preventing
oversized future prepared writes.
**Architecture:** lossless prepared-payload envelope, schema-v8 compatibility
gate, native schema copy into a fresh file and bounded verified data copy.
**Stack:** Python stdlib, existing DuckDB, existing pytest environment.
**Spec:** [lossless compaction](../../specs/2026-09-30-performance-db-lossless-compaction.md).
**Version:** Planner revision v3 from v2; independent Claude Opus 5/high
`PLAN_APPROVED`. Existing authorization covers source-read-only rehearsal;
only the final live cutover needs a new approval.

## Constraints and review focus

- Original DB stays read-only through rehearsal; no in-place rewrite.
- Repository-only implementation; no new runtime deployed against live v6
  until cutover. Legacy in-place migrations run only on fixtures/copies.
- All tests and spill use dedicated C TEMP paths; clean after each run.
- All Python runs use `.venv`; standalone scripts explicitly select worktree
  `src` instead of accidentally importing main's editable install.
- Shared `duckdb_import.workers` is authoritative; no new worker setting.
- Legacy/mixed/UNAVAILABLE artifacts, corrupted streams, schema-gate rollback,
  unused sequence IDs and interrupted builds have explicit acceptance tests.
- C build reserve 10 GiB, D staging reserve 5 GiB, C original-backup reserve
  8 GiB; remeasure before each large phase. Never lower reserves to continue.

## Review ledger: v1 to v2

| ID | Disposition |
| --- | --- |
| F1 | Use native schema-only COPY and empty-target migration; reject source chmod and store-module import ban. Explicit read-only source plus hash/stat/WAL guards suffice. |
| F2 | Adopt schema v8 gate; actual v6/v7 runtime refusal verified on tiny fixtures. |
| F3 | Paired local code/database rollout with v8 gate replaces a flag and two releases. |
| F4 | Both writers share codec; valid envelope is validated/pass-through; nested payload fails. Opaque migration copy and legacy fixtures remain valid. |
| F5 | Bound encoded input and inflate, check length/hash/base64/zlib/EOF/trailing/UTF-8/version. |
| F6 | Canonical JSON and source digests remain unchanged. |
| F7 | Bounded exact typed-row and recovered-byte comparison; no giant EXCEPT or per-result full rereads. |
| F8 | Offline tool only builds/verifies/reports; manual cutover. |
| F9 | Fixed capacity reserves; retain D original and stop if C backup cannot fit. |
| F10 | UTF-8 totals/distribution collected during existing copy pass; size estimate remains a hypothesis. |
| F11 | Native DuckDB durability and bounded batches; one writer, configured workers; checkpoint/reopen. |
| F12 | Source stat/hash/WAL before/after build and before cutover; exclude writers. |
| F13 | Backup verification includes read-only catalog and sample reads after hash. |
| F14 | Observe subsequent import growth; cycles-only redesign deferred. |
| F15 | Repository-only task1, no live initialization/deployment; real source stays6 through rehearsal. |
| F16 | Rollback pairs original database with recorded original runtime; prove write smoke on disposable fixture/copy only. |

## Task 1: codec, schema v8 and all writers

Files: `src/mrs3/performance_v2_optimizer.py`, `performance_v2_import.py`,
`performance_v2_store.py`, the version gate in `performance_v2_selection.py`,
two Performance benchmark scripts, corresponding tests, spec/ADR/progress.

Interfaces: `encode_prepared_storage(payload: str) -> str` and
`decode_prepared_storage(payload: str) -> str` in optimizer module; shared
semantic decoder unwraps before its current validation. Public `to_json`
remains canonical plain JSON. Existing valid envelopes are not recompressed.

- [ ] Write and run failing codec, integration and schema-gate regressions.
- [ ] Implement zlib-level-1 envelope with bounded strict decoding.
- [ ] Implement transactional v7-to-v8 marker upgrade; preserve historical
  migrations, and require v8 for writers/read v5-v8 without repair.
- [ ] Update both prepared writers and all discovered version gates; retain
  legacy data and explicit COR01 repair behavior.
- [ ] Verify both writers, Unicode/limits/corruption, mixed rows, digests,
  import idempotence, marker rollback and current/historical version tests.
- [ ] Run focused/relevant broader tests, cleanup C TEMP, diff check,
  independent `CODE_REVIEW_PASS`, scoped commit with current documentation.

## Task 2: source-read-only fresh-file builder

Files: smallest separate offline script/module and its focused tests; reuse
existing validators, native COPY and shared worker configuration.

- [ ] Write failing tests for source6/7/8 copying, complete catalog/history,
  empty tables, sequence gaps, aliases/no-overwrite, corruption, source change
  and insufficient space.
- [ ] Implement build/verify/report entry point: record source identity,
  reject WAL/unknown catalog, create unique target with no overwrite.
- [ ] Native schema COPY, copy schema markers, detach source, migrate EMPTY
  target to8; reattach source read-only and copy explicit columns in FK order.
- [ ] Transform prepared rows before insertion using shared codec; bounded
  worker queue and single writer; collect byte totals/distribution in pass.
- [ ] Pin spill to C, enforce capacity, checkpoint/close/reopen, then compare
  all typed rows and recovered payload bytes in bounded PK ranges. Source
  stat/hash/WAL must match. Never consume source sequence values.
- [ ] Benchmark one versus configured compression workers, test failure
  cleanup and reproducible verification, review and scoped commit.

## Task 3: full candidate and measured acceptance

- [ ] Recheck C/D capacity; invoke tested builder with explicit worktree code.
- [ ] Verify all17 tables, 15runs, 23,887results/prepared, 14,753,892actions,
  112,188,510equity rows against the source snapshot; exact content equality.
- [ ] Confirm instance identity, current references, full typed keys, imported
  hashes, defaults/constraints and sequence state; reopen representative public
  read paths and prove source unchanged.
- [ ] Record actual physical/used/free sizes, raw/encoded UTF-8 totals, build
  and decode timing, memory/temp usage, hashes and cutover capacity calculation.
- [ ] Independent review of rehearsal evidence; candidate must fit current
  D free minus5 GiB. Expected14-18 GiB is not acceptance evidence.

## Task 4: manual cutover after concrete approval

- [ ] Present verified candidate, code review, actual space/downtime and
  backup/rollback sequence; obtain approval for the live operation.
- [ ] Stop writers, hold exclusive maintenance, verify unchanged source/WAL.
- [ ] Record original code revision and launch path; prove old-runtime write
  capability on a disposable fixture/copy for paired rollback.
- [ ] Copy Ccandidate to D.next; hash and read-only catalog/reader verification.
- [ ] Remove redundant Ccandidate only after D.next is verified; copy original
  D database to C after capacity check; hash, catalog and representative reads.
- [ ] Recheck source, rename Dlive to rollback and D.next to live, deploy
  compatible code, smoke. Restore D original AND its recorded compatible
  runtime immediately on failure; never auto-migrate the restored original.
- [ ] Only after smoke and verified C original, remove redundant D original.
  Retain C original until later explicit authorization to delete it.
- [ ] Record final sizes; observe file/blocks and payload growth on subsequent
  operator imports without adding a monitoring table.
