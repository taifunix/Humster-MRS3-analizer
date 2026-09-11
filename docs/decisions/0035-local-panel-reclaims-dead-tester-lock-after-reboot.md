# ADR-0035: Local Panel Reclaims a Dead Tester Lock After Reboot

**Status:** Accepted

## Context

The local Panel keeps a target-wide tester lock between **Prepare files** and
Start/Stop. A host reboot terminates the owner but also changes the recorded
boot and native-runtime identities. The default lock policy treats that drift
as unverifiable, so a valid lock left by the previous boot blocks the next
preparation indefinitely.

## Decision

`LocalTestingService.fill()` opts into reclaiming a previous-boot lock only
when all of the following are true:

- the owner record is valid and names the same canonical target and lock kind;
- host, machine and PID-namespace identities match the current machine;
- the recorded boot identity differs from the current boot; and
- the recorded PID/process-start pair is proven not to be the live owner.

The default `TesterTargetLock` policy remains fail-closed. Live owners,
unknown process probes, same-boot container drift, foreign machines, malformed
records and changed targets are never reclaimed by this opt-in.

## Consequences

The first local Panel preparation after a reboot replaces a dead lock
atomically and proceeds. Other tester consumers retain the stricter default,
and concurrent preparations remain mutually exclusive.
