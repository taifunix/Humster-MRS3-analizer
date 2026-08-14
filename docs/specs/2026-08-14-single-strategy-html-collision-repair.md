# Single-Strategy HTML Collision Repair

**Status:** Approved

## Goal

When a completed wizard result points to an overwritten HTML report, retest
only the strategy whose embedded report name differs.

## Contract

- After complete wizard results are observed, name-only reconciliation locates
  every mismatching strategy individually.
- A mismatch raises the existing `BatchHtmlCollision` recovery signal with only
  those names.
- Existing serial repair machinery reinstalls and tests only the affected name.

## Acceptance Evidence

- A focused test proves one name mismatch becomes a one-name collision signal.
