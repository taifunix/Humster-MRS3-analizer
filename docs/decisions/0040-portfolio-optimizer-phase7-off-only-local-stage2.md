# ADR-0040: Phase 7 off-only baseline and local Stage 2 boundary

Date: 2026-09-21.

Status: The scoped policy is recorded under explicit user authorization. The
revised pre-run package **P7-R4** received independent Opus high
`PLAN_APPROVED` on 2026-09-22. This approval covers fake-only implementation
and the stop-before-execution runbook. The resulting fake-only implementation
received final Claude Opus 5 high `CODE_REVIEW_PASS`; this is not tester-result
evidence.

## Context

Phase 9 accepted WS1.2 directional candidate search, but the bot's
`open_positions_limiter` is not operational. The weighted-search lower-level
limiter model remains implemented and tested, while current production Stage 1
must not rank or admit candidates using `L>0` or limiter replay.

With limiter off, `position_priority` has no operational effect. The existing
strategy template still requires `mrs.position_priority`, and the candidate
payload wrapper is part of frozen local executable identity. That wrapper is
not tester readback.

## Decision

1. Current production Stage 1 policy is `LIMITER_DISABLED_OFF_ONLY` until
   Phase 13 bot behavior is separately authorized, implemented, and verified.
2. The production adapter calls weighted search with explicit `L=0` and a
   canonical `priorities` mapping assigning 1 to every member. No `L>0`
   limiter model or replay participates in current Stage 1 ranking/admission.
3. Keep the lower-level weighted-search limiter APIs, math, tests, and history
   intact for Phase 13. Do not change their defaults or remove them.
4. Keep `mrs.position_priority=1` in each executable strategy JSON for the
   template contract. The payload wrapper includes
   `account.open_positions_limiter=0` as frozen internal candidate identity;
   it does not represent tester readback.
5. Phase 4 is closed only for this off-only production boundary. Its prior
   live-evidence item is moved to Phase 13; bot cancellation, excess market
   close, and real IM release have not been proven. Until real evidence,
   `limiter_release_status` remains `UNKNOWN`.
6. Phase 7 is reframed as an off-only joint baseline. All Phase 7 execution
   items remain open. Limiter runtime, release/replay evidence, and same-size
   off/main/neighbor comparisons belong to Phase 13.
7. This decision is not blanket authorization for Stage 2 or tester execution.
   Separately, on 2026-09-21 the user authorized a bounded local-only off-only
   tester baseline. No run or result is complete; execution remains gated by
   implementation, focused tests, and review. Exchange actions, trading, and
   production PerformanceDB writes are not authorized.
8. A successful weighted Stage 1 privately persists the exact eligible off-only
   executable payloads and explicit member identities in one canonical,
   digest-bound artifact beside its workbook. The artifact shares workbook
   publication/rollback, is not exposed through summary/API/XLSX, and may be
   loaded after restart without recomputing Stage 1.

## Consequences

The current adapter has one eligible limiter state, `L=0`, and a stable inert
priority value. Campaign public output and the strategy-template contract remain
unchanged; the exact executable set is retained only as a private result
artifact. Future limiter work requires the Phase 13 evidence and review listed
in the [weighted-search plan](../superpowers/plans/2026-09-12-portfolio-optimizer-weighted-search-discussion.md).
