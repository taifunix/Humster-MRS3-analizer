# Performance inbox autofill

## Purpose

When the tester workflow completes successfully, the panel should remember the
captured performance inbox path and prefill the "Completed performance inbox"
field automatically.

This removes the need to re-locate the verified inbox manually after a
successful test run.

## Non-goals

- Do not change how the tester workflow captures or validates the inbox.
- Do not change the DD5 calculation semantics.
- Do not replace manual path selection.
- Do not infer a path when no verified inbox exists.

## Input

- A completed `tester-run` workflow state that includes a verified `inbox_path`.

## Output

- The workflow state written by the tester run should include `inbox_path`.
- The panel should prefill `performance_inbox` from that state when the current
  field is empty or still at its default placeholder.

## Invariants

- Autofill must happen only for a verified inbox path produced by the workflow.
- The panel must never overwrite a manually chosen non-default inbox path.
- Failed or incomplete runs must not populate the field.

## Acceptance evidence

- A completed tester run writes `inbox_path` into its state file.
- The panel render logic uses that value to populate `performance_inbox`.
- Focused tests cover the state-file and panel autofill behavior.
