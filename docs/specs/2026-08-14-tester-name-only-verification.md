# Tester Name-Only Verification

**Status:** Approved operational change

## Goal

Prevent the closed tester's completed reports from being retried because the
runner parses every metric table and every trade row while the report is still
being written or is expensive to read.

## Contract

- A runner result requires a one-strategy wizard entry, a stable HTML file and
  an exact match between the expected strategy name and the HTML embedded
  settings name.
- Runner completion, resume and CSV publication must not call
  `parse_html_report()` or inspect HTML metric/trade tables.
- Runner CSV rows are marked `verification_mode=strategy_name_only`; HTML
  metrics and trades are intentionally unavailable for this mode. DD5 lots are
  resolved from the immutable JSON strategy source recorded in the completed
  runner state, not by parsing report HTML.
- DD5 derives `effective_days` from the persisted wizard period for name-only
  rows. Profit Factor remains unavailable rather than being inferred.

## Non-goals

- This does not claim metric reconciliation or make a DD5/post-test conclusion.
- A missing, malformed or mismatched embedded strategy name remains invalid.

## Acceptance Evidence

- A focused test proves reconciliation accepts a matching embedded name while
  `parse_html_report()` is made to fail.
- A focused panel/post-test test proves an empty `strategy_settings_json`
  column falls back to the completed runner's immutable strategy source.
