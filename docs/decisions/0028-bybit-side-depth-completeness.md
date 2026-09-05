# ADR-0028: Persist side-specific depth completeness

**Status:** Accepted for implementation.

## Decision

`liquidity_1m` schema version 2 keeps the existing combined
`depth_<band>bps_complete_ratio` fields and adds
`bid_depth_<band>bps_complete_ratio` plus
`ask_depth_<band>bps_complete_ratio` for every 10/25/50/100 bps band.

Each side ratio is the share of valid samples whose available `orderbook.1000`
levels reached that side's band boundary. The combined ratio remains the share
where both sides reached the boundary. Depth values remain the minimum observed
available depth and are never promoted to full-exchange capacity.

## Consequences

Downstream consumers can distinguish one-sided from two-sided depth coverage.
Existing combined fields remain available, while new Parquet files advertise
schema version 2. The reference collector, retention policy, and sampling
cadence do not change.
