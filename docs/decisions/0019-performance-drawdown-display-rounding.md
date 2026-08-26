# ADR-0019: Tolerate display rounding in tester PnL and drawdown metrics

**Status:** Accepted

## Decision

During Performance DB import, compare tester display fields to their immutable
series-derived values after `ROUND_HALF_UP` using these quanta: absolute
`Total PnL` and `Max Drawdown` to the nearest integer; relative `Total PnL, %`
and `Max Drawdown, %` to one decimal percentage point. Keep the unrounded
derived values as the canonical metrics stored in the database.

## Rationale

The tester's HTML summary and sampled equity series can use different internal
precision. Small display drift must not quarantine an otherwise valid report,
while the importer must continue to reject differences that cross the selected
rounding boundary. Other metrics retain their declared decimal precision and
validation rules.

## Consequences

This changes only the Performance DB admission check. It does not alter source
HTML, strategy identity, stored series, or materialization contracts.
