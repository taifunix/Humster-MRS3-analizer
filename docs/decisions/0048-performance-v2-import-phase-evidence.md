# ADR-0048: Diagnostic timing evidence for Performance v2 imports

**Date:** 2026-09-29
**Status:** Accepted
**Related:** [optimization spec](../specs/2026-09-28-heavy-database-optimization.md), [import-tail audit](../reports/2026-09-28-performance-db-import-tail.md).

## Context

The successful 409-report replacement reached `PUBLISHING 409/409` long before
the operator saw completion. That run has no stage timings, so its 14-GB
database size and the progress counter do not identify the slow stage. New
progress substages could cause repeated writes of the large shared Panel job
journal and distort the observation.

## Decision

Use the existing `PerformanceV2ImportResult.phases` mapping. Add an optional
`phases` object to `import_audit.v2.json` and an optional
`evidence.phase_seconds` object to the existing terminal Panel job. Audit
`schema_version` remains 2: every pre-existing key keeps its name, type and
meaning, and older audits simply lack `phases`. These objects contain at most
15 ASCII phase keys and less than 4 KiB each. Values are finite elapsed
seconds from `time.perf_counter()`, rounded to six decimal places. An absent
key means its phase was not entered; zero-valued placeholders are not emitted.
Phase names are diagnostic and unstable: consumers must not depend on a fixed
set, and a later spec or ADR may revise them without changing audit schema.

`PUBLISHING_THROUGH_CLEANUP_TOTAL` starts after the existing `PUBLISHING N/N`
callback and ends after parser staging cleanup. `UNACCOUNTED` is the total
minus only the entered through-cleanup component phases: `PUBLISH_ADMISSION`,
`PUBLISH_ROWS`, `PUBLISH_CHILD_READBACK`, `PUBLISH_PHASE8`, `PUBLISH_FINALIZE`,
`COMMIT`, `FAILURE_ARTIFACTS`, `POST_COMMIT_REPLACEMENT_READBACK`,
`CONNECTION_CLOSE` and `STAGING_CLEANUP`. Compute it before rounding and clamp
clock noise at zero. The total and residual are not additional components to
sum. `AUDIT_WRITE` and `WRITER_LOCK_RELEASE` are measured after the audit
snapshot and appear only in the returned result and terminal evidence.
`PANEL_READBACK` covers the existing schema/count readback connection lifetime.
`PANEL_TERMINAL_SYNC` cannot report its own duration in that same journal
write; one INFO log line records it when INFO logging is enabled and verified
before an operator run. INFO is not enabled by default, so that log is optional
operational evidence.

Only phase boundaries are timed, independent of report count. No new progress
event, database query, transaction, endpoint, callback signature, journal
publication or file is introduced. Timing failures cannot replace import,
rollback, audit or terminal-sync errors. The evidence guides a later bounded
optimization; it is not a claim about whole-import speed.
