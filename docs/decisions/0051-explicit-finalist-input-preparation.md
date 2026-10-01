# ADR-0051: Explicit FINALIST optimizer-input preparation

Date: 2026-10-01. Status: accepted; implementation follows the approved OPT-01e plan.

## Context

PerformanceDB import spent most of its publication time building private
optimizer artifacts for every accepted report, although Stage 1 consumes only
the small current FINALIST set. The artifacts duplicate typed action and equity
facts already held in the database.

## Decision

Import stores only typed PerformanceDB facts. ADD writes no
`optimizer_prepared_inputs` row. REPLACE keeps its transactional deletion of
the row belonging to the exact replaced `result_id`, then writes no new row.
Valid legacy prepared rows are reusable and are not globally purged or
migrated.

The first Stage 1 action is the sole trigger for preparation. Its server job
resolves exact current FINALIST result IDs and invokes the existing preparer
with the single configured `duckdb_import.workers` value. The browser supplies
neither scope nor worker count. The preparer keeps the existing PerformanceDB
writer lock, transactional digest/current-result recheck, strict canonical
reader, and a bounded thread pool.  The pool receives no database connection;
its size is the configured worker value capped at 16.

Readiness and campaign launch use the same preparation-version/source-digest
validity predicate as the strict reader. Launch fails closed until every
required current FINALIST input is available; it does not prepare inputs as a
side effect. No new cache table, configuration field, background trigger, or
compatibility JSON fallback is introduced.

## Supersession

This ADR supersedes only ADR-0037, `Decision`, sentence beginning **“New
imports prepare within the result transaction.”** It no longer applies to ADD
or REPLACE. ADR-0037 remains immutable; its typed-source, digest,
availability, privacy, and lock contracts continue to apply.

## Consequences

The expensive work moves from an import of thousands of reports to a manual
job for a few dozen finalists. Import stays atomic and results remain
revision-bound. Stage 1 has a visible prerequisite and a retryable job instead
of a hidden delay at campaign launch.
