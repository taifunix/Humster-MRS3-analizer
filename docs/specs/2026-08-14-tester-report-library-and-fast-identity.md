# Verified Tester Report Library And Fast Identity

**Status:** Approved design

## Goal

Preserve every fully reconciled ONUSDT tester HTML in a durable library beside
the configured live `my_test` directory, record the exact strategy evidence,
and avoid false missing-report retries caused by parsing complete reports just
to obtain the strategy name.

## Scope

- The library is `ONUSDT_reports` beside configured `tester/report/my_test`.
- A report is accepted only when its embedded strategy name, one-strategy
  wizard result, and complete metric reconciliation agree.
- The library retains one SHA-256-addressed record per strategy name and an
  audit manifest with the source path, size, mtime and validation result.
- `tester-report-library` audits existing artifacts without mutation by
  default; its explicit `--apply` mode publishes verified copies and removes
  only eligible byte-identical duplicates.
- A live HTML is removed only after an identical verified library copy exists
  and the manifest records `safe_to_delete=YES`.
- The snapshot collector extracts only the embedded strategy identity while a
  report is being written. Full `parse_html_report()` remains mandatory for
  reconciliation and final result publication.

## Non-goals

- Do not infer a completed strategy from HTML without a matching wizard result.
- Do not delete snapshots, existing saved evidence, DuckDB data, or a live
  report that has no verified library copy.
- Do not change strategy JSON, tester scheduling, or metric tolerances.

## Invariants

1. Library filenames are deterministic `<strategy-name>.html`.
2. Duplicate content for the same strategy may be removed from `my_test` only
   after SHA-256 equality with the verified library file.
3. Conflicting reports for one strategy are retained for diagnosis and marked
   `safe_to_delete=NO` in the manifest.
4. The fast identity extractor returns `None` for incomplete or malformed
   reports; it never treats an unparsed report as a valid identity.
5. `parse_html_report()` remains the sole source of metrics and trade rows.

## Acceptance Evidence

- Tests prove a burst collector identifies reports through the light identity
  extractor without calling full reconciliation parsing.
- Tests prove library publication accepts only a fully reconciled report,
  records SHA-256 evidence, and refuses conflicting duplicates.
- A read-only audit lists every live report by embedded name and shows the
  reconciled strategy count, duplicate count, quarantine count and deletion
  eligibility before any deletion.
