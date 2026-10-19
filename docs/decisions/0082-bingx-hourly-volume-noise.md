# ADR-0082: BingX hourly check — prices exact, volume within the measured noise

Date: 2026-10-19. Status: accepted.

Contract: [pair registry](../specs/2026-10-18-pair-registry.md), «BingX minute
verification»; [Panel for the simulation workflow](../specs/2026-10-18-panel-simulation-workflow.md),
«Stage 5a contract».

## Context

A BingX day with missing minutes is accepted only if its hourly klines prove
that the missing minutes had no trades (`VERIFIED_GAPS`). Each hour candle is
compared with the aggregate of its minutes. The rule was set on 2026-10-18 on
12 contracts' recent days: all five fields equal at a relative 1e-9.

On 2026-10-19 the first live BingX history run tested that rule:
- **The run:** KOPN, 146 days from 2026-05-26.
- **The result:** 37 days came out `SUSPECT`, and 6 of the 7 segments were
  refused.
- **The measurement over 864 hours of those 37 days:**
  - open, close, high and low were equal in every hour;
  - the volume of an hour candle differed from the sum of its minutes, even
    in hours where all 60 minutes were present;
  - the hour candle's volume was above the minute sum by at most +0.024 %
    in complete hours and +0.048 % in hours with gaps;
  - it was below the minute sum by down to −8.8 %.
- **Conclusion:** BingX's hourly volumes on older days are noisy aggregates.
  The difference is not lost trades.

## Decision

An hour equals its minutes when:
1. **Prices:** open, close, high and low are equal at a relative 1e-9.
2. **Volume:** the hour's volume exceeds the sum of its minutes by at most
   0.1 % of the hour's volume, about twice the measured maximum.
   - A minute sum larger than the hour is not a loss.
3. **An hour without minutes** still has no candle or zero volume.

A day stays `SUSPECT` only when a price differs or an hour holds more than
0.1 % volume the minutes do not show.

## Consequences

- Live run after the change, KOPN from scratch:
  - 146 days: 70 `OK` and 76 `VERIFIED_GAPS`, none `SUSPECT`;
  - all 7 segments written.
- Missing trades that make up less than 0.1 % of an hour's volume and never
  set a new hourly price extreme go unnoticed. That size is the scale of the
  exchange's own aggregation noise.
- The threshold is the constant `VOLUME_NOISE` in
  `pair_registry/minute_days.py`. Changing it needs a new measurement and a
  new ADR.
