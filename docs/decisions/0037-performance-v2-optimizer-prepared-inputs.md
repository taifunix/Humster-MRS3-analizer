# ADR-0037: Typed Performance facts and prepared optimizer inputs

Date: 2026-09-17. Status: Accepted.

## Context

Performance v2 already stores typed actions and equity. Optional action
Price/Cost and source sizing settings are retained only in bounded JSON for
compatibility, while Portfolio Optimizer reconstructs cycles and normalized
inputs from the same current rows for each Campaign. Phase 8 requires permanent
analytics without changing the accepted report/import contract.

## Decision

Upgrade Performance v2 additively from schema v4 to v5. Persist nullable typed
action Price/Cost, six nullable WS1.1 sizing facts, and one private, versioned,
digest-bound, per-result prepared optimizer input. Keep raw JSON as provenance
and migration input, not as the v5 typed reader contract.

New imports prepare within the result transaction. Existing v4 rows are
backfilled only from exact saved JSON; ordinary analysis prepares missing/stale
current artifacts. Unknown evidence stays unknown and does not block unrelated
Performance import or selection.

The normative contract is
[Performance v2 optimizer prepared inputs](../specs/2026-09-17-performance-v2-optimizer-prepared-inputs.md).

## Consequences

Portfolio calculations can reuse verified source preparation without rereading
HTML or reparsing compatibility JSON. ADD/REPLACE, current `result_id`, reviews,
A/B windows and XLSX behavior remain unchanged. The database gains one table and
eight nullable columns; no new service, store, public payload or policy is added.
Old rows may remain unavailable until exact evidence exists and normal analysis
prepares them. Price/Cost remain execution facts and never become planned size.
