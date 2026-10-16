# ADR-0073: Overwrite a writable Panel journal when Windows blocks replacement

**Status:** Accepted
**Date:** 2026-10-14

## Context

Panel job admission writes a complete temporary journal and atomically replaces
`.panel-jobs.json`. On Windows, another process can keep the existing journal
open without delete sharing while still allowing read/write access. In that
state every `os.replace` attempt fails with `WinError 5`, so SINGLE_MODE is
rejected before the tester starts even though the completed journal payload can
be written safely to the existing file.

## Decision

- Keep temporary-file plus atomic replacement as the primary persistence path.
- Retry transient Windows replacement failures for the existing bounded window.
- After the final retry, use an in-place overwrite only when the journal already
  exists and can be opened for writing. Flush and fsync the completed payload
  before admitting the job.
- Keep missing journals and failed direct writes fail-closed with
  `JOB_PERSISTENCE_FAILED`.

## Consequences

- A persistent no-delete-share lock no longer prevents ordinary SINGLE_MODE
  admission when the existing journal remains writable.
- The fallback is not atomic across a process or machine crash, so it is used
  only after the preferred atomic path is exhausted.
- Other persistence failures still roll back the in-memory admission and do not
  start the tester.
