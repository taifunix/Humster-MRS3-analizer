# ADR-0026: Preserve initial Bybit snapshots and fail closed on missing data

**Status:** Accepted for implementation.

## Decision

The collector reports a WebSocket transport as connected immediately after the
socket is created, before subscription ACK processing. Bybit can send the first
`orderbook.1000` snapshot before that ACK; the runtime must apply it while the
connection is live and must not invalidate it when the handshake completes.
Disconnect/reconnect still invalidates the book and requires a new snapshot.

Transport connectivity and data health are separate contracts. After a 60-second
startup grace, an active symbol without a synchronized book or valid sample makes
health `DEGRADED`. Five minutes without a valid sample makes health `ERROR`.
Health records per-symbol synchronization, last book update, last valid sample,
recent valid-sample count, and recent coverage.

## Consequences

The collector no longer publishes a false healthy state when the sampling loop is
running but every book is invalid. Initial snapshots are retained, so the normal
Bybit handshake produces valid liquidity samples without synthetic data. The
reference REST pipeline and hourly publication contract are unchanged.
