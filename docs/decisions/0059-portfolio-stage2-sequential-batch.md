# ADR-0059: Portfolio Stage 2 sequential candidate batch

Date: 2026-10-07.

Status: Accepted for fixture/fake-only implementation after manual Advisor
`PLAN_APPROVED` of plan v2. This decision does not authorize a real tester run.

Related: [Portfolio Optimizer UI specification](../specs/2026-09-06-portfolio-optimizer-panel-ui.md),
[main optimizer specification](../specs/2026-09-05-portfolio-optimizer.md),
[ADR-0040](0040-portfolio-optimizer-phase7-off-only-local-stage2.md), and
[implementation plan](../superpowers/plans/2026-10-07-portfolio-stage2-sequential-batch.md).

## Context

ADR-0040 established an off-only local Stage 2 baseline for one prepared
candidate. The committed Stage 1 executable artifact can contain several
ordered portfolio candidates, but the existing Stage 2 path prepares and runs
only the first. Candidate batch execution must preserve the artifact's exact
order and payloads while reusing the existing tester target lock, staged
configuration restoration, job registry, and report naming.

## Decision

1. A single `portfolio.stage2` job processes every candidate in committed
   artifact order, sequentially, with no re-sort, batch-wide truncation, retry,
   or resume. Each per-candidate tester payload is produced by the existing
   `_stage2_material(candidate)` path.
2. Prepared receipts and full payloads remain in worker memory. The job registry
   stores compact artifact bindings and ordered results in
   `runtime["stage2_results"]`; it remains the only durable job store. Reports
   remain in `tester/report/<candidate_id>/`; Panel does not copy or delete
   them.
3. A candidate becomes completed only after valid result readback and a
   successful existing `LocalTestingService.stop()`. That method owns restore
   and restore verification. If cancellation is observed during polling after
   a valid result was obtained, successful stop is followed by syncing that
   result before terminal `CANCELLED`; no next candidate starts.
4. A failure stops the batch and preserves already completed results. The
   existing target lock is released by `stop()` between candidates; if another
   actor acquires it before the next fill, the existing lock path safely fails
   the job without losing completed results.
5. The Stage 1 runtime stores committed artifact `executables_count` beside
   its digest and removes it during failed-runtime cleanup. UI submission is
   offered only for the currently projected successful Stage 1 Campaign with
   a positive count; the server does not gain a latest-Campaign restriction.
6. The server requires the exact confirmed request body, committed successful
   Stage 1, and the existing committed artifact digest/shape validation.
   Provider presence is not an authorization gate. Repeated submission of a
   Campaign returns its existing job in every state; another run requires a
   new Stage 1 Campaign.
7. Restart projects a nonterminal job as `INTERRUPTED` without resuming it.
   Public results and batch progress survive failure, cancellation, and
   interruption; the singular `result` is only a derived alias for a fully
   successful batch.
8. Historical single-candidate Stage 2 journal records without
   `stage2_results` are not a compatibility target. No real Stage 2 runs exist,
   so a migration/fallback is unnecessary.
9. This decision does not authorize real tester execution and does not close
   real-evidence, PnL, Q06/HTML import, ranking, recommendation, or admission
   gates. Implementation and tests are fixture/fake-only. Real execution
   requires M5/M6 readiness and separate fresh user authorization after
   independent code review.

## Consequences

- The Stage 2 route becomes available to submit a confirmed batch through the
  existing local Panel flow, while one active Portfolio job and the existing
  target lock continue to serialize tester access.
- Completed candidates remain visible if a later candidate fails, is cancelled,
  or is interrupted; partial batches are not represented as successful results.
- A newer current Portfolio projection can make an older successful Campaign
  unavailable to this minimal UI, although server authorization remains based
  on the submitted Campaign and its committed evidence.
- ADR-0040 remains unchanged and continues to define the off-only baseline
  constraints not superseded by this sequential batch amendment.
