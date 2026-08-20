# ADR-0011: Source v6 Selected-Interval Boundary Cycles

**Status:** Accepted
**Date:** 2026-08-19

## Decision

When a user selects an interval that starts after a cycle opened, the cycle is
not counted unless its opening evidence is owned by the selected interval. A
cycle opened before the selected start is therefore excluded from PnL, DD and
trade counts even if its close action falls inside the interval. A bridge cycle
opened before an incoming report start is the explicit exception: it is admitted
only when the incoming fragment contains the complete atomic bridge membership
and the resolver has marked the bridge `RESOLVED`.

This policy prevents a selected surface from inventing a trade whose opening
state is outside the frozen evidence window while preserving safe overlap
reconstruction for an explicitly covered bridge.
