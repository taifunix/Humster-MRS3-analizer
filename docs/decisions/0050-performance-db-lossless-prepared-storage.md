# ADR-0050: Lossless prepared storage and fresh-file compaction

Date: 2026-09-30. Status: accepted; independent Advisor v3 `PLAN_APPROVED`.

## Context

The inspected PerformanceDB occupies 40.556 GiB with 13.125 GiB of reusable
free blocks. Prepared JSON duplicates typed actions/equity; 60 distributed
artifacts compress to 18.81% of original UTF-8 bytes with zlib level 1 and
base64. See the [investigation](../reports/2026-09-30-performance-db-storage.md)
and [contract](../specs/2026-09-30-performance-db-lossless-compaction.md).

## Decision

Keep the complete prepared artifact and canonical public JSON interface.
Persist its exact UTF-8 bytes as
`mrs3-zlib-v1:<raw-byte-count>:<sha256>:<base64>` using Python stdlib zlib
level 1. The shared strict reader accepts legacy and encoded values, bounds
decompression, validates integrity and then performs unchanged semantic
validation. Both normal persistence paths use the same encoder.

Advance the relational schema marker to v8 without additional table changes.
This is an application compatibility gate: older readers and writers must
refuse the file before processing encoded rows. New code reads supported
v5/v6/v7/v8 catalogs, writes only v8, and supports mixed payloads in v8.
Upgrade v7 to v8 by transactional marker change after strict validation.

Compact by copying the schema natively into a fresh file, migrating its empty
catalog, then copying all rows with prepared compression before insertion.
The source stays read-only. Exact typed-row and decoded-payload equality
are acceptance requirements. No automatic cutover is part of the tool.

## Evidence and consequences

Small C TEMP probes using DuckDB 1.5.5 demonstrated that schema-only COPY
preserves the 17-table catalog, instance marker and sequence next values
after checkpoint/close/reopen. Probes covered the actual main v6 runtime and
the pre-change worktree v7 runtime (commit `0933a72`).

Both old runtimes reject a small fixture with a v8 marker. The v7 reader
reports `Performance database schema version requires upgrade`; its writer
gate reports `Performance database does not have schema version 7`; its
initializer reports `Performance database has an unsupported schema version`.
The fixture SHA-256 stayed unchanged. Temporary files were removed.
Standalone probes explicitly select worktree `src`, because the local
editable environment otherwise imports main-checkout code. Pytest already
sets `pythonpath = ["src"]`.

Deployment must pair compatible application code and database. The original
file, verified on C before removing its redundant D copy, is the rollback
artifact. No feature toggle or two-stage decoder deployment is needed for
this local paired rollout. A new application can still read old files.

All implementation stays in the isolated repository until reviewed cutover;
the live v6 file must never be passed to the new initialization path during
rehearsal. Legacy in-place migrations are tested only on fixtures/copies.
Record the original runtime revision and launch path. Rollback restores both
that runtime and the original database, with old-runtime write capability
verified on a disposable fixture/copy. Do not auto-migrate the restored file.

Cycles-only storage would require a changed reconstruction contract and is
deferred. Removing raw action JSON would break existing consumers and is
excluded. A plain compact copy would reclaim free blocks but leave oversized
future prepared writes unchanged. In-place rewriting would retain allocation
and risks running out of space. Level 6 saves somewhat more bytes but doubled
compression time in the sample; level 1 is the measured initial choice.
