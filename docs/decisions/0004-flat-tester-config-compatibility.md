# ADR 0004: Accept Flat Tester Commission Config

## Status

Accepted

## Context

The immutable inbox capture contract requires the five tester commission
fields. The runner originally accepted only the nested `tester_config` JSON
shape, while the local Hamster Bot emits those fields at the JSON top level.
This caused a fully verified batch to fail during capture.

## Decision

Accept both shapes. If `tester_config` is present, it is authoritative and must
be an object. Otherwise, use the top-level JSON object as the commission
mapping. In both cases all five fields remain mandatory, finite, canonicalized,
and hashed; no defaults or substituted values are allowed.

## Consequences

Existing nested configurations remain compatible. Flat local tester configs can
produce the same immutable inbox contract. The recovery path remains the
existing resumable `tester-run` path: it reuses verified state and snapshots,
submits no strategies when none remain, and still validates the exact expected
name set before capture.
