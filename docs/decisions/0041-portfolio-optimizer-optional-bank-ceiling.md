# ADR-0041: Optional per-profile bank ceiling and candidate-required bank

Date: 2026-09-22.

Status: Accepted by the user. The implementation plan received independent
Claude Opus 5 high `PLAN_APPROVED`; the implementation received independent
Claude Opus 5 high `CODE_REVIEW_PASS`. Neither approval authorizes a real
tester run.

## Context

Weighted search calculates `B_required` for every concrete portfolio candidate.
It also accepts an optional `B_available` ceiling. The previous Panel Campaign
contract nevertheless required `equity_usdt`, passed that value as the ceiling,
then reused the same value as executable `facts.B` and tester
`InitialBalance`. An additional `max_balance_usdt` launch field was accepted but
was not consumed by weighted search.

This mixed three different meanings: available capital, the calculated bank of
one candidate, and generated per-strategy `basic.max_balance`. It also prevented
the supported uncapped search mode.

## Decision

1. Each risk profile has one optional USDT Decimal field,
   `bank_available_usdt`. Missing or `null` means that weighted search receives
   `bank_available=None`; a supplied value must be finite and positive under the
   existing `DECIMAL(38,12)` boundary.
2. `bank_available_usdt` is a ceiling only. It is persisted in
   `campaign.launch.profiles` for search, audit, and display, but it never
   becomes executable `facts.B` or tester `InitialBalance`.
3. The actual bank of a candidate is its validated
   `metrics.required_bank_usdt`. With a ceiling, every admitted candidate must
   satisfy `B_required <= bank_available_usdt`; an over-ceiling candidate is
   excluded. If none remain for a profile, that profile reports
   `BANK_UNAVAILABLE`.
4. Every strategy wrapper inside one candidate uses that candidate's canonical
   `B_required` as `facts.B`; `q`, generated `basic.max_balance`, and executable
   identity are derived from the same value. Different candidates may have
   different banks.
5. Stage 2 prepares one committed candidate. It requires a finite positive
   `metrics.required_bank_usdt`, requires every committed wrapper `facts.B` to
   equal it, and sets tester `InitialBalance` to that value. Missing or mixed
   evidence fails closed.
6. `equity_usdt` and launch `max_balance_usdt` are removed from the Campaign
   contract without silent migration or fallback. Generated strategy
   `basic.max_balance` remains part of executable sizing and is not the removed
   launch override.
7. This decision changes preparation only. It does not run or authorize the
   tester, trading, exchange actions, or PerformanceDB writes.

## Consequences

The Panel can run the same risk profile either with a user-supplied capital
ceiling or without one. A ceiling of 5000 USDT with a candidate requiring 1800
USDT produces `facts.B = InitialBalance = 1800`, not 5000. The committed
candidate artifact, rather than mutable launch UI state, is authoritative for
Stage 2. Existing legacy Campaign payloads containing the removed fields are
rejected explicitly instead of being reinterpreted.

Stage 1 executable artifacts created before this decision do not contain the
new required metrics binding and therefore fail closed after upgrade; they must
be recalculated rather than treated as corrupted or silently migrated.
