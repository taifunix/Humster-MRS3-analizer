# PerformanceDB storage investigation

Read-only evidence collected on 2026-09-30 from the configured main-checkout
database. No checkpoint, migration, update or deletion was performed. Local
database paths and report contents are intentionally excluded.

## Measured state

| Measurement | Result |
| --- | ---: |
| Physical file | 43,546,324,992 bytes / 40.556 GiB |
| Schema | v6; strict read-only catalog accepted |
| Allocated used blocks | 27.431 GiB |
| Reusable free blocks inside file | 13.125 GiB / 32.36% |
| WAL | absent / zero bytes |
| Import runs | 15 |
| Strategies / results / prepared inputs | 23,887 each |
| Actions | 14,753,892 |
| Equity samples | 112,188,510 |
| Import file ledger | 24,046 |

The ledger reconciles: 23,887 imported, 148 skipped, 11 rejected with
`NO_EFFECTIVE_TRADE`. All imported HTML hashes are distinct; there are no
`REPLACED` entries. All current-result references match their strategies.
All full typed strategy keys are distinct, and order counts agree. Repeated
`candidate_identity` values alone are not evidence of duplicate strategies.

Seven required tables are empty: `equity_quality_metrics`, `window_metrics`,
`selection_results`, `selection_review_imports`, `selection_review_rows`,
`selection_runs`, `strategy_tags`. Their presence belongs to the schema;
removing them would break the catalog and cannot explain a 40 GiB file.

## Verified sources of excess size

`optimizer_prepared_inputs.prepared_json` contains canonical actions, equity
and reconstructed cycles. Actions and equity duplicate typed table content.
All 23,887 rows contain available artifacts. The sum of character lengths is
18,083,324,749 (16.841 GiB if one byte per character); maximum length is
3,065,424. DuckDB reports the VARCHAR segments as `Uncompressed`.

`strategy_actions.raw_action_json` contains another 1,640,845,295 characters;
these strings use FSST compression. They still have consumers, including
historical migration and report export, so deletion is not justified.

Logical character counts are not exact physical table sizes. In particular,
distinct `block_id` values from `storage_info` omit long-string overflow
allocation. Do not derive per-table physical bytes from that count.

DuckDB free blocks can be reused, but ordinary VACUUM does not shrink the
file. A fresh database copy is the documented compaction route; checkpoint
only partially reclaims deleted rows. See official [space reclamation](https://duckdb.org/docs/current/operations_manual/footprint_of_duckdb/reclaiming_space)
and [VACUUM](https://duckdb.org/docs/current/sql/statements/vacuum) documentation.

## Lossless compression probe

Sixty evenly distributed result IDs were read by scalar primary-key queries.
No probe files were created. Compression used Python stdlib zlib and base64.

| Payload | Bytes | Fraction of original | Compression time |
| --- | ---: | ---: | ---: |
| Original UTF-8 | 52,363,350 | 100% | - |
| zlib level 1, base64 included | 9,848,340 | 18.81% | 0.688 s |
| zlib level 6, base64 included | 7,860,460 | 15.01% | 1.320 s |

Level 1 is the initial choice: roughly 81% less prepared payload on this
sample, without changing the decoded artifact. These are sample timing and
logical payload measurements, not an end-to-end import benchmark or a
guarantee of the final file size. A 14-18 GiB rebuilt database is a planning
estimate requiring a full rehearsal.

A repeated thread probe used the same 60 payloads and the actual shared
configuration, `duckdb_import.workers=30`. Serial times were 0.5410,
0.4944, 0.5045 s (median 0.5045); 30-thread times were 0.1632, 0.1338,
0.1172 s (median 0.1338). Encoded totals were identical at 9,848,340 bytes.
This is 73.5% lower compression wall time on the sample, not a whole-import
gain. The probe retained payloads only in memory; source size/mtime stayed
unchanged and no temporary files were written.

## Growth claim and limits

No historical physical-file snapshots are available, so successive doubling
of physical growth is not established. The last two batches are not equal
in stored content: run 14 imported 3,744 reports, 3,227,847 actions and
20,880,392 equity samples; run 15 imported 3,369 reports, 4,144,417 actions
and 20,874,717 equity samples. Prepared JSON grew by 3,565,466,118 versus
4,118,420,160 characters (+15.51% despite fewer reports).

The evidence supports eliminating oversized prepared storage and reclaiming
free blocks. It does not support removing reports or financial facts.

## Next acceptance

Implement a backward-reading lossless storage codec and a source-read-only
fresh-file copier. Validate every copied table and recovered payload; measure
the final physical size before planning live replacement. Retain all IDs,
instance identity, sequences, indexes, constraints and required empty tables.
Pair any replacement with compatible application code and a verified original
backup. Recalculate C/D free space immediately before each large operation.
Tests use dedicated C TEMP directories and remove them after completion.
