# ADR-0017: Source v6 facts and metrics v2

**Status:** Accepted

## Decision

New Source v6 artifacts use a fresh facts-only payload v2. Canonical payload
identity excludes `cycles`, compatibility action-events and open-tail ids; they
are reconstructed from facts on decode. Existing compact-header derivative
caches remain and must match reconstruction. The physical DuckDB schema and
zlib codec do not change, but payload, source, segment, import-token and surface
fingerprints change together. Older artifacts are rejected and rebuilt from
retained HTML.

Metrics are calculated once in the materialization worker and placed in the
existing compact analysis-row JSON. Analysis reads that JSON without raw payload
decode. W6 retains both blob checksum and decompressed canonical-id validation.

Total PnL is taken from the raw merged/windowed balance before rebasing; returned
series remain rebased to the declared initial balance. Import validates declared
PnL, fees and Profit Factor at the token's precision, with conditional Recovery
Factor consistency. A mismatch quarantines that report; any quarantine blocks
surface materialization until a clean fresh rebuild.

## Consequences

- Derivative-rule changes do not alter factual `fragment_id`, but still require
  a new fingerprint and rebuild when their semantics change.
- No migration, dual read, normalized event store, new table or new analysis
  pass is introduced.
- Performance is accepted only after a comparable three-run baseline/candidate
  measurement; W6 is not weakened.

## Related

[ADR-0006](0006-undefined-profit-factor-contract.md),
[ADR-0016](0016-source-v6-zero-activity-runs.md),
[metric contract](../specs/2026-08-23-source-v6-metric-contract.md).
