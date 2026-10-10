# SINGLE_MODE report collection

**Status:** Approved for implementation
**Date:** 2026-09-28
**Governing dependency:** `docs/specs/2026-09-03-performance-v2-retest-workflow.md`
**Commission exception:** [optional Performance v2 commission evidence](2026-09-30-performance-v2-optional-commission-evidence.md).
## Goal

Allow an operator to run several ordinary `SINGLE_MODE` tester batches, keep
their reports in the configured tester report directory, explicitly collect
only selected tester jobs, verify the collection, and import all registered
reports into Performance v2 with one Panel action.

The batches in one collection may have different test ranges, initial
balances, tester configuration hashes, commission contracts and analysis run
IDs. Those facts remain attached to the batch/report that produced them.

## Non-goals

- Do not import every HTML file found in the report directory.
- Do not infer membership from the newest jobs, filename timestamps or folder
  contents.
- Do not copy or archive HTML reports.
- Do not create a second Performance v2 importer or a new database.
- Do not allow two results for the same strategy name in one ADD import.
- Do not change RETEST or finalist-RETEST workflows.

## User flow

The ordinary tester card adds:

- checkbox `Объединять отчёты`;
- collection status `N пачек · M отчётов`;
- action `Очистить накопление`.

When the checkbox is selected, `Launch SINGLE_MODE tester` registers the new
tester job in the active collection before its worker starts. An unchecked
launch is an ordinary job and is never considered by collection verification,
import or cleanup.

A registered running job blocks collection verification. A registered failed
or cancelled job remains visible but contributes no expected report. Its
successful retry replaces it as the importable member. Only committed member
jobs contribute reports.

`Проверить` validates only the committed inboxes named by the active
collection. It freezes one immutable collection inbox and enables import for
that exact snapshot. Adding another checked launch after verification opens a
new collection generation containing the previous members plus the new job;
the older verified snapshot remains immutable and is no longer the active UI
target.

Successful import marks the verified collection imported and clears the active
accumulation. Failed import leaves it available for another verification and
retry. Explicit clear unregisters the active collection but does not delete
HTML or strategy JSON files.

## Durable registration

Collection membership is server-owned and persisted in the existing
`.panel-jobs.json` registry. A collection is a lightweight
`strategies.tester.collection` record with a generated collection ID and an
ordered member list. Each durable generation records a monotonic
`collection_revision`. Import admission records the exact worker job in
`import_in_progress` and the source revision in `import_generation_revision`.
Each member records:

- tester job ID and retry lineage;
- expected strategy names captured before tester execution;
- current job state;
- committed inbox path and snapshot hash when available.

Exact report filenames and report SHA-256 values come only from each member's
committed inbox. The collection verifier never glob-scans the report directory
to discover import candidates. Panel restart reconstructs the active
collection from the durable registry, not from directory contents.

## Heterogeneous batch contract

The collection inbox extends the existing immutable metadata-only handoff with
per-entry batch context:

- `test_start` and `test_end`;
- `tester_config_sha256`;
- optional paired `commission_contract` and `commission_contract_id`;
- per-strategy `analysis_run_id` and existing candidate provenance.

Legacy schema-version-1 inboxes remain readable without change. Collection
input uses one explicitly versioned extension and the existing Performance v2
parser, staging and transaction. Prepared entries carry their own range and
commission context; report validation and persisted `commission_rate` use the
entry context rather than a collection-wide value. The report remains the
authority for its parsed period and initial balance, while its member inbox
remains the authority for the tester config hash and any available commission
rate. An absent rate persists as SQL `NULL`; HTML fees and PnL remain authoritative.

## Invariants

1. A tester job without the active collection ID cannot contribute a report.
2. Collection verification reads only registered member inboxes and their
   exact manifest entries.
3. Missing, changed, linked, out-of-root or hash-mismatched source artifacts
   fail verification/import before database mutation.
4. Strategy names and report basenames are unique across one frozen
   collection.
5. Different batch dates, balance, config hashes, commissions and analysis run
   IDs are valid.
6. One frozen collection produces one deterministic collection digest and one
   Performance v2 import transaction.
7. A new member cannot silently extend an already verified snapshot.
8. Non-member reports and strategies are neither read nor deleted by
   collection actions.
9. Ordinary unchecked SINGLE_MODE behavior remains unchanged.
10. RETEST paths never join an ordinary report collection.
11. Collection mutations use the durable revision as a compare-and-set token;
    verification performs long artifact reads outside the registry lock and
    publishes only if the generation is unchanged.
12. Import admission atomically claims one VERIFIED generation. Clear and
    successor registration cannot invalidate an in-progress claim; failed or
    cancelled import releases it and restores retryability, while committed
    completion marks that exact generation IMPORTED.
13. On controller startup or each collection access, an import claim is
    reconciled against the process-local import workers. A live worker keeps
    its claim; an absent or interrupted worker releases it for retry, while a
    durable COMMITTED result completes the generation as IMPORTED. If live
    worker enumeration is unavailable, retain claims and defer reconciliation.
    Reconciliation holds the controller claim lock through its registry update
    and drops pending IDs whose tracked import job is missing or terminal;
    a durable COMMITTED import completes the generation as IMPORTED. The
    collection service is initialized once under the same controller lock.
14. A SINGLE_MODE launch is admitted only after its QUEUED job is durably saved
    in `.panel-jobs.json`. If persistence fails, the in-memory admission is
    rolled back, the tester runner is not called, and the Panel returns an
    explicit persistence error. Bounded retries apply only to transient
    Windows replace denials; exhausted retries remain fail-closed. Wrapping
    Panel services preserve the machine-readable `JOB_PERSISTENCE_FAILED`
    code and HTTP 503 status.
15. The Windows Panel launchers disable delayed expansion before handling
    paths. `start_panel.bat` prepends its checkout's `src` directory to
    `PYTHONPATH` without an empty path component and preserves existing entries.
    `start_new_panel.bat` and `restart_new_panel.bat` delegate to it without
    overriding that path or launching Python themselves. An editable install
    pointing at another checkout must not select backend code for the current
    checkout's configuration and database.

## Failure behavior

- No committed registered members: verification is rejected.
- A registered job is still active: verification is rejected with its job ID.
- Duplicate strategy/report identity: verification is rejected.
- Member inbox or source artifact unavailable: verification is rejected and
  database state is unchanged.
- Import failure: collection remains verified and retryable.
- Registry restart: an open or verified collection is restored exactly.
- A stale import claim never blocks restart forever: missing, interrupted or
  terminal non-COMMITTED jobs are released, while ambiguous states remain
  fail-closed until a durable completion record is available.
- Concurrent register/verify, clear/verify, and clear/import operations fail
  closed or serialize at the durable revision boundary without losing a
  member or overwriting a committed import.
- A journal replace that keeps failing does not leave an in-memory job holding
  `strategies.tester`; the start request returns HTTP 503 and no tester starts.
- A transient Windows journal replace denial that clears within the bounded
  retry window saves the job and proceeds through the ordinary SINGLE_MODE
  flow.
- Starting Panel from any supported batch launcher imports the package from
  its checkout, even when `.venv` has an editable install for a different
  worktree; the launcher's working copy retains CRLF line endings.
- A journal persistence failure exposed through a service wrapper, including
  portfolio job submission, remains HTTP 503 with
  `JOB_PERSISTENCE_FAILED` rather than being reclassified as a client error.

## Acceptance evidence

- Two registered batches with different ranges, balances, commissions,
  config hashes and analysis run IDs verify and import together.
- An unregistered committed tester job with valid HTML in the same report
  directory is ignored.
- A running registered job blocks verification; failed/cancelled jobs do not
  add expected reports; a committed retry replaces its source.
- Reload restores membership and counts without scanning HTML.
- Changing or removing one registered HTML fails before DB mutation.
- Duplicate strategy names or report basenames fail closed.
- Adding after verification creates a new generation and invalidates UI import
  authorization for the older snapshot.
- Clear does not delete report files.
- Successful collection import leaves non-member files untouched and closes
  the active collection.
- Ordinary single-job verify/import and RETEST regression suites remain green.
- No real tester is launched as implementation evidence.
