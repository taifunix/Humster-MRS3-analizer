# ADR-0057: Manual PerformanceDB pair maintenance

Date: 2026-10-06. Status: accepted for the PerformanceDB maintenance feature.

## Context

The Strategy and DD5 Panel needs explicit pair-scoped operations for removing
heavy details from rejected strategies and for fully deleting selected pairs.
ADR-0055's cleanup marker and deletion timestamp conflict with the user's
decision to derive cleanup state from the remaining rows. The user also rejected
verified backups and requested a preview before deletion. PerformanceDB is
already at schema v9.

## Decision

- Keep schema v9. Add no cleanup marker, deletion timestamp, persistent
  maintenance log, backup, copy, staging database, snapshot or restore artifact.
- Delete Rejected physically removes only `strategy_actions`,
  `strategy_equity` and `optimizer_prepared_inputs` for strategies whose current
  effective User Status is exactly REJECTED. Preserve strategy identity and
  typed settings, compact result and metric rows, effective rejection evidence,
  and selection/review history as specified by the active feature contract.
- Delete fully physically removes all strategy and selection/review rows
  scoped to selected symbols, retaining only plateau rows still referenced by
  another strategy. It also clears the unscoped `import_files` and
  `import_runs` tables; this intentional global journal deletion is shown
  separately in preview and progress.
- Preview reports exact pair/table counts and requires a separate confirmation.
  Apply revalidates the preview targets while holding the shared writer
  coordination and uses one sequential DuckDB writer. Admission is atomic and
  only one maintenance job can be active. Preview tokens are single-use, expire
  after 15 minutes and are bounded to 32 in-memory entries. Progress is
  transient process memory and reports committed work. A later failure may
  leave earlier deletes committed; it must expose the specific database error
  and must not claim rollback.
- This decision supersedes only ADR-0055's cleanup marker and deletion-time
  requirement for this manual maintenance feature. It does not change the
  effective User Status or independent rejection-source lifecycle in ADR-0055
  and ADR-0056, or authorize changes to the existing prune operation.

## Consequences

Repeated previews derive remaining work directly from schema-v9 rows; no
persistent state indicates a previous cleanup. A later valid wider import may
rebuild removed details under the existing importer contract. Full deletion
intentionally removes import history for unselected pairs as well as selected
pairs, while their strategy/config rows remain and continue to govern typed
configuration deduplication. Since there is no backup or automatic restore,
the confirmation preview and actionable failure reporting are part of the
operator contract.

The implementation and acceptance evidence are defined by the
[PerformanceDB maintenance specification](../specs/2026-10-06-performance-db-maintenance.md).
