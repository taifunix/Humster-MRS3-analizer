# Windows strategy publication ACL recovery

## Goal

Keep generated strategy JSON readable after a Panel restart or a change of
Windows process identity.

## Scope

- The shared READY strategy publisher and the Performance v2 RETEST publisher
  create same-volume staging directories without Python 3.13's private Windows
  `0o700` ACL.
- Windows staging inherits the parent directory ACL. POSIX staging remains
  private (`0o700`).
- Existing atomic publication, rollback, manifest validation, and digest
  contracts remain unchanged.

## Non-goals

- The code change does not add or implicitly trigger tester execution.
- No change to generated strategy contents, names, or manifest hashes.
- No automatic deletion of an unreadable legacy output directory.

## Invariants

- Staging stays under the final output parent so publication remains a
  same-volume rename.
- A staging name is unique and publication cleanup remains bounded to that
  exact directory.
- Recovery preserves the unreadable legacy directory until its replacement is
  validated.

## Acceptance evidence

- Focused tests prove the platform-specific staging mode and both normal and
  RETEST publishers use the ACL-inheriting staging helper.
- Existing publication rollback tests remain green.
- A recovered real generation manifest validates all 171 strategy JSON files.
