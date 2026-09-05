# ADR-0023: Performance v2 typed-config deduplication

**Status:** Accepted
**Date:** 2026-09-04

## Decision

Performance DB v2 identifies an executable strategy by its symbol, side,
timeframe, Close MA, and the multiset of ordered Open MA, shift, and quantized
lot settings. Strategy names/IDs, analysis and candidate lineage, and plateau
diagnostics do not define executable behavior. `shift_bp` is authoritative;
`lot_x` uses twelve-place Decimal normalization. This deliberately keeps
`EQUAL` and `INCOME` variants separate.

ADD imports compare UTC effective intervals after the configured listing-date
plus 120-hour warm-up. Only a proper interval superset may replace the
canonical current result; equal, narrower, or shifted intervals are skipped.
For server-mapped RETEST replacement, an equal effective period is allowed, as
is a later-ending period whose effective duration is not shorter; its raw
reported start alone is not a reason to reject it. Cross-name matches reuse
the existing canonical row and never create a second strategy row.
If an explicit expected identity map is provided for RETEST replacement, it
must cover every target with the complete typed key; partial or malformed maps
are rejected.
The RETEST-clear flag applies only to a committed, mapped `REPLACE` whose
typed configuration matches; it never clears tags for skipped or rejected
entries.

One-time duplicate cleanup is soft-discard only: exact duplicates are audited
dry-run first, and an explicit apply marks non-survivors `DISCARDED` while
retaining facts. Only a confirmed merge marks the survivor `RETEST`.

## Consequences

- Intentional lot-allocation variants remain independently selectable and
  testable.
- A later report cannot hide an already-covered listing/warm-up interval.
- Legacy rows with null provenance can be compared without inventing a new
  period contract.
- Existing schema and atomic result replacement are reused; no fingerprint
  migration or alias table is needed.
- Discarded strategy rows and their historical facts remain recoverable, and
  every downstream reader must filter lifecycle status to `ACTIVE`.

An `ACTIVE` row whose typed key cannot be reconstructed (missing order rows,
invalid numeric fields, or an order-count mismatch) blocks the import rather
than allowing a silent duplicate. The collision check is intentionally global:
an ambiguous active key must be repaired or soft-discarded under a reviewed
cleanup before any later import can safely proceed.
