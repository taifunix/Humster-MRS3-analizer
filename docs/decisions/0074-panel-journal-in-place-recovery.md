# ADR-0074: Recover the Panel journal after an interrupted in-place save

**Status:** Accepted
**Date:** 2026-10-15

## Context

[ADR-0073](0073-panel-journal-locked-replacement-fallback.md) permits an
in-place journal overwrite when Windows blocks atomic replacement but still
allows writing the existing file. An interruption during that write can leave
invalid JSON, so recovery must preserve the last known-good journal.

## Decision

- Before the in-place overwrite, persist and fsync the existing journal and
  intended payload in a temporary sibling file, then atomically publish it as
  `.recovery`; fsync the parent directory where supported before changing the
  journal.
- Fsync and verify the replacement bytes and parsed JSON before removing the
  recovery file.
- On startup and before each later save, compare the journal with both saved
  payloads. Remove the sidecar if the journal matches the previous or intended
  bytes; otherwise restore the previous JSON before any newer journal write.
- Continue to read the previous format-1 sidecar when it includes the intended
  payload digest. Reject malformed or unknown records with an actionable error
  and preserve both files for manual recovery. Restore the previous bytes when
  the journal is missing; if it is unreadable, fail closed and preserve the
  sidecar.
- Fsync the parent directory after atomic replacement and recovery-file changes
  where supported. Unsupported directory fsync is a no-op on Windows. A failed
  sync after removing a verified sidecar or staging file is safe: the journal is
  already fsynced and any sidecar that remains is reconciled against both full
  payloads.
- If recovery cannot be validated or restored, fail startup rather than loading
  an empty job registry.

## Consequences

- A process interruption during the fallback can recover the previous valid
  job journal, or preserve a fully written intended journal, on the next Panel
  start.
- The sidecar exists only during the locked-replacement fallback and is removed
  after either a verified save or a successful recovery.
