# ADR-0027: Mark accelerated collector runs explicitly

**Status:** Accepted for implementation.

## Decision

The collector health snapshot records `runtime_mode` (`production` or
`smoke_test`) and `accelerated_clock` (`false` or `true`). The normal `run`
command uses `production/false`; `--test-export-minutes` uses `smoke_test/true`.

Accelerated output keeps the production schema and file names for structural
testing, but it is diagnostic only and cannot be used as production sampling
evidence. Production sampling remains one boundary every five real seconds.

## Consequences

Operators can distinguish accelerated coverage from real-time coverage directly
from `health.json`. No reference endpoint, storage schema, or retention policy
changes.
