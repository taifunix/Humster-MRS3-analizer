# ADR-0063: Admit tester jobs only after durable journal writes

**Status:** Accepted
**Date:** 2026-10-09

## Context

Panel job admission writes a QUEUED record to `.panel-jobs.json` before it
starts the owned worker. On Windows, `os.replace` can temporarily fail while
another process holds the destination. The old submit path inserted the job
into in-memory state before saving; when the save failed, the HTTP request
closed without a response and the unsaved job continued to reserve its
resource. A repeated SINGLE_MODE start then reported `RESOURCE_BUSY` although
no tester had started.

## Decision

- Retry bounded Windows `os.replace` failures for access denied, sharing
  violation and lock violation. Do not retry other errors.
- If a new job still cannot be durably saved, remove that admission from
  in-memory state and return `JOB_PERSISTENCE_FAILED`; do not start its worker.
- Map this error to HTTP 503 with a safe, actionable message. Keep the original
  filesystem exception in the server log for diagnosis, including when a
  service wrapper converts the registry error.
- A failed save leaves the journal dirty; only a later successful save clears
  that flag. Registry state mutation, save, and admission rollback use the
  same lock.

## Consequences

- A transient Windows file lock can clear without requiring the user to submit
  the same operation again.
- A persistent journal failure fails closed without leaving a ghost resource
  reservation or launching a tester whose job cannot be recovered after a
  restart.
- The panel journal remains atomically replaced; this decision does not weaken
  file integrity or change ordinary job ownership.
- A retry window adds at most 0.4 seconds to one journal save, in addition to
  serialization, file write, and `fsync` time. SINGLE_MODE status polling uses
  volatile updates for ordinary progress within the same state and phase;
  durable sync occurs when state or phase changes, so ordinary progress polls
  do not repeatedly hold the registry lock through the retry window.
