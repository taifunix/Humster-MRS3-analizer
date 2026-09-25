# ADR-0042: Startup migration for external Campaign snapshots

Date: 2026-09-23.

Status: Accepted for the Campaign snapshot implementation.

This ADR supersedes only the snapshot storage and retention clause of
ADR-0031. ADR-0031 remains authoritative for the Panel/Campaign boundary,
API behavior, and tester boundary.

## Context

Older `.panel-jobs.json` records may embed the complete frozen Campaign,
including finalist series and weighted input rows. New records keep that input
in a deterministic external gzip snapshot, but the registry currently performs
restart recovery before the Portfolio service can migrate old records. That
ordering can persist a large journal before the external snapshot exists.

## Decision

1. The Panel constructs `PanelJobRegistry` with recovery deferred. The
   Portfolio service migrates legacy `portfolio.stage1` records under the
   registry lock and then invokes one idempotent interruption recovery pass.
   Other job kinds retain the existing `FAILED`/`INTERRUPTED` behavior.
2. Before mutation, migration writes a journal-adjacent
   `.panel-jobs.snapshot-migration.bak` with an fsynced file and checks free
   space for `2 * len(journal_bytes)`, the sum of canonical compressed snapshot
   sizes for all nonterminal or successful candidates, and a 64 MiB reserve.
   If that preflight is short by even one byte, migration, recovery, backup,
   and journal state remain unchanged. An existing valid backup is never
   overwritten blindly.
3. `COMMITTED` embedded Campaigns are copied, geometry-repaired by exact
   finalist identity, written and verified as external snapshots, and replaced
   in the journal by descriptors. `FAILED` and `CANCELLED` records drop the
   embedded input while retaining only compact campaign ID/input/config
   bindings and bounded diagnostics. A per-job migration failure leaves that
   job unchanged and is retried at a later startup.
   By operator sign-off dated 2026-09-23, once an unrepairable legacy job is
   marked `FAILED` and compacted, its full embedded payload is permanently
   deleted. The retained campaign ID and input/config digests plus compact
   diagnostics identify the non-executable payload; no quarantine file is
   retained.
4. Migration saves atomically, reloads and validates all job IDs/states, and
   hydrates retained descriptors before deleting the fixed adjacent backup. On failure the
   original in-memory registry is restored and the backup remains available.
5. After terminal state is durable, startup retries descriptors marked
   `deleting`; it removes only the exact snapshot file when no nonterminal job
   references the path. Missing files count as deleted, permission failures
   remain retryable, and successful Campaign snapshots are retained. After a
   successful unlink it makes a best-effort nonrecursive `rmdir` of the
   campaign directory; missing, nonempty, permission, and sharing errors are
   tolerated and never trigger recursive deletion.

## Consequences

Startup no longer persists a full legacy Campaign before migration. The
compact journal is bounded independently of finalist-series size, while a
legacy journal remains readable if one record cannot be migrated. Terminal
Campaign jobs have no retry endpoint; a new submit creates a new Campaign and
snapshot rather than rerunning a terminal one.

The steady-state persistence budget is at most 360 journal writes per hour per
active job, plus bounded substage-transition and completion writes. A read-only
projected total journal of 4,678,721 bytes includes unrelated tester/retest
jobs occupying about 4.63 MiB; the portfolio contribution falls from about 81
MiB to tens of KiB. The decision therefore does not claim a total journal
smaller than 2 MiB.

Progress telemetry keeps ETA fail-closed: the backend publishes a numeric ETA
only with an exact positive total, at least two completed units, and at least
two seconds elapsed; missing or inconsistent totals publish `null`. The Panel
UI suppresses numeric ETA for heartbeat age over 30 seconds and for terminal
jobs, showing `stalled / ETA unknown` while retaining an informational
determinate bar when available. The progress-state lock may nest the registry
lock for persistence; registry-locked code never acquires the progress-state
lock.
