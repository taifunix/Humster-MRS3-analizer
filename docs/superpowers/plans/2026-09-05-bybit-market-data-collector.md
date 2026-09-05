# Executable plan — Bybit market-data collector, Revision 2

**Status:** Approved for implementation. No runtime changes now.

## Documentation scope

This revision edits only this plan, the paired
[specification](../../specs/2026-09-05-bybit-market-data-collector.md), and
[ADR-0024](../../decisions/0024-bybit-market-data-collector-archive.md). It does
not edit code, dependencies, PRD, or progress because the worktree has unrelated
user changes.

## Future delivery phases

1. **Configuration.** Narrow failing tests for the exact TOML, 30-second reload,
   restart-only root, and symbol isolation; then minimal configuration code.
2. **Order-book core.** Snapshot/delta/reset/delete, silence-not-stale,
   explicit invalidation, one connection, acknowledged subscription batches,
   ping/pong, and reconnect tests.
3. **Minute aggregation.** UTC five-second scheduling, clock reanchor, partial
   minutes, counts/nullability, p05/p50/p95 interpolation, depth/completeness,
   and exact `liquidity_1m` schema tests.
4. **SQLite spool.** WAL/NORMAL, duplicate policy, bounded write retries,
   `published_hours`, advisory lock, and restart-safe row tests. Prove no
   WebSocket/book/sample filesystem persistence.
5. **Hourly archive.** UTC `[H,H+1)` plus 120-second eligibility, same-directory
   tmp/fsync, deterministic validator, no-clobber publish, marker-authoritative
   reader, ascending catch-up, late row, and invalid-final tests.
6. **Reference data.** Symbol-specific pagination, normalized instruments/risk
   schemas, raw JSON.gz without sidecars/hashes, daily atomic publication, and
   symbol-events tests.
7. **Health, CLI, locking.** 60-second atomic health, warning/critical disk,
   four commands and exits, archive verification, and lock ownership tests.
8. **Windows Scheduler.** Task scripts for `MRS_BybitMarketCollector`, SYSTEM,
   30-second startup delay, project `.venv`, restart/no-limit/no-parallel.
9. **Integration and soak.** One live public WS for 20–30 symbols, lifecycle,
   REST, restart, marker-based concurrent DuckDB reader, disk-failure behavior,
   then 3 symbols for 2h/24h and 20 for 24h/7d plus Windows boot.

Each phase starts with one narrow failing test, then minimum code and a focused
suite. Final checks use `.venv\\Scripts\\python.exe -m pytest`, entry/wheel smoke,
`git diff --check`, and independent code review.

## Required recovery matrix

Tests inject every point below and assert health plus no archive/raw/spool
overwrite or deletion: crash during tmp write; after fsync before publish; after
final before marker; after marker; valid/invalid unmarked final; existing-final
no-clobber; marker without final; current-run tmp disposal; orphan scratch cleanup;
late marked-hour row; forward/backward clock changes; SQLite busy retries/fatal
write; validator across DuckDB writer versions; and reader exclusion of unmarked
or missing paths.

Manual evidence is redacted and ignored under `artifacts/bybit_collector/<run_id>/`:
health snapshots, marker/file verification, resource samples, logs, and soak
summary. Pass requires no unexplained minute keys, no delta accepted after
invalidation before snapshot, every reader-selected file structurally valid, and
at least 99% valid slots outside logged exchange/network/disk degradation.

## Revision 2 resolution ledger

| Review concern | Resolution |
| --- | --- |
| Undefined stale book / fresh snapshot | spec **One WebSocket and valid book**; phases 2–3 |
| Max-ten topology | one public connection; phases 2 and 9 |
| Instruments/risk/raw REST contract | spec **Reference data**; phase 6 |
| Hourly simpler archive | spec **SQLite, hourly archive, and recovery**; phase 5 |
| Reader safety after crash | `published_hours`, no-clobber, recovery matrix |
| Late rows, clock boundaries, tmp/marker faults | spec archive section; recovery matrix |
| Disk, health, CLI, lock, Windows, RPI | spec **Operations**; phases 7–8 |
| Removed legacy mechanisms | spec **Revision 2 changelog** |

Documentation acceptance is review-only: diff limited to the three named docs,
`git diff --check` clean, and no v1 manifest/hash/quarantine/max-ten grouping
contract remains. The exact source schema, one-WebSocket decision, reader index,
and nine phases must remain.
