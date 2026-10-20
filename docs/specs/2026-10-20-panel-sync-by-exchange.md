# Panel «Синхронизировать»: only the selected exchange, candle check, faster BingX data

**Status:** Part 1 approved by the user 2026-10-20 («чини и сразу пересчитай
ликвидность… нужен режим отдельно пересчитать ликвидность») and implemented:
requirements 1 and 4 and «Пересчитать ликвидность» («Part 1 contract» below).
Part 2, the parallel BingX download, was approved («внедряй параллельную
закачку») and is pending.
**Depends on:** [Panel for the simulation workflow](2026-10-18-panel-simulation-workflow.md)
(main screen, stage 5), [pair registry](2026-10-18-pair-registry.md) («Minute data
layer», «BingX minute verification»), [BingX data downloader](2026-10-10-bingx-data-downloader.md).

## Requirements (user, 2026-10-20)

1. **One exchange per sync.** «Синхронизировать» works only for the exchange
   selected at the top of the main screen, not for both.
2. **BingX: check, do not re-download.** The sync checks that every needed
   day is present and complete, and downloads a day only when it is missing or
   incomplete. It must not download the last day again every time.
3. **BingX: faster data loading.** Study how to speed it up. The old screener
   loaded BingX data almost instantly; now it takes hours. Find out why and
   what the old way was.
4. **Bybit: check and fill.** With Bybit selected, the sync checks the candle
   days and downloads the missing ones.
5. **«Пересчитать ликвидность»** (user, 2026-10-20): recompute the liquidity
   of the selected exchange without the simulation.

## Part 1 contract (implemented 2026-10-20)

- **CLI `sync --exchange bybit|bingx|all`** (default `all`, the old order for
  manual runs). The steps:
  1. `pull`;
  2. `refresh` of the chosen exchanges;
  3. per exchange `minute_days`, `liquidity`, `pipeline` (with `_bingx` for
     BingX);
  4. `workbook` (both sheets, from the registry);
  5. `push`.

  One step failing never stops the next one, as before.
- **`ensure_window(con, settings, exchange, first_today, today)`**, the
  `minute_days` step. For every TRADING pair it ensures the liquidity window
  days, from the window of `first_today` to `today − 1`:
  - Bybit: the missing days from the Bybit trade archive (`ensure_days`);
    never a day after `last_archived_day(now)`;
  - BingX: download and verification as before (`ensure_bingx_window` is now
    its BingX form);
  - a day already on disk is not downloaded again;
  - one pair failing (any error) never stops the others.
- **CLI `liquidity --exchange E`** ensures the window days of E first (until
  now only BingX did). The output has `minute_days`.
- **The archive lag for Bybit too:** `refresh_liquidity` writes nothing when
  the window end is after the last archived day (Bybit, 9 h) or the last
  available day (BingX). It answers `not_yet_available`, so an early run
  keeps the last rows.
- **Panel:**
  - «Синхронизировать» passes the selected exchange (`--exchange`); the
    plan note names it.
  - The new action `liquidity`, «Пересчитать ликвидность» next to
    «Синхронизировать», for both exchanges. Its plan dialog has no rows,
    only a note (window length; history not touched). It runs one step,
    `python -m mrs3.pair_registry liquidity --exchange E`.
- **Non-goals:**
  - the liquidity formulas (ADR-0079) stay as they are;
  - the liquidity table is not synced between machines, so the liquidity
    action does not push.

## What the code does today (read 2026-10-20, not yet run)

- The Panel starts `python -m mrs3.pair_registry sync --config …` for the
  action `sync` and **does not pass the exchange** (`panel_main.py`, `run`).
  `plan("sync")` says «Обновить справочник обеих бирж…».
- The CLI sync runs fixed steps for both exchanges: pull, refresh (Bybit and
  BingX), liquidity Bybit, liquidity BingX, pipeline Bybit, pipeline BingX,
  workbook, push (`pair_registry/cli.py`).
- The BingX liquidity step calls `ensure_bingx_window`: for every TRADING
  BingX pair, one after another, `ensure_minute_days` over the liquidity
  window (weekdays) up to yesterday.
- Bybit: that step does not ensure candle files. Whether the Bybit liquidity
  refresh reports missing days is **not yet checked**.
- BingX day check (`minute_days._verify_day`, mode `incremental`, which is the
  default; `config.local.json` has no `bingx` section):
  - a day with an accepted record (`OK`, `VERIFIED_GAPS`) and the same file
    hash is skipped with no request;
  - a valid file on disk without a record is verified, not downloaded;
  - otherwise the day is requested (1 request), plus 1 hourly request when
    the minutes have gaps, plus up to 3 refetches for a `SUSPECT` day;
  - a `SUSPECT` day is not accepted, so it is requested again on every run.
- The BingX client is sequential: one request at a time and a 0.2 s pause
  before each (`BingXClient`, `request_delay_seconds`). `ensure_bingx_window`
  loops over pairs in one process with no parallelism.
- The old screener ran the tester with `UpdateData: true`, so the **tester's
  own downloader** loaded BingX data (git: `914c28c^`,
  `panel_testing.py` / the screening templates). That downloader is in the
  external bot, not in this repository. Today the templates use
  `UpdateData=false` and this repository downloads through its own client.

## Measured 2026-10-20 (read-only: a copy of the registry, public kline requests)

- **Registry journal of today's two syncs.** The BingX window step itself is
  not journaled; the times below are read from the rows around it.
  - First sync, 08:09–08:56: about 46 min to download and verify
    3,830 BingX days (10-09..10-18, about 380 pairs), so about 0.7 s per day.
  - Second sync, 09:07–09:14: about 7 min for 394 days, all of 10-19. That
    day became available after 09:00 UTC (9 h lag). It was a **new** day, not
    a re-download: the 3,830 accepted days made no request.
- **One BingX kline request** (1 day of 1m, 1,440 rows): 0.45–0.92 s, mean
  0.66 s, plus the client's fixed 0.2 s pause. An hourly request: 0.23 s.
- **Parallel requests.**
  - 10 requests one by one: 6.6 s. With 5 threads: 1.7 s. With 10 threads:
    0.9 s.
  - No rate-limit answer at 10 threads.
  - One answer of 1,055 rows was a real gap: the registry has the same day
    as `VERIFIED_GAPS` with 1,055 candles.
- **The BingX history span** (TRADING pairs, listing day..10-19):
  - 398 pairs, 44,868 pair-days;
  - 26,034 day files on disk;
  - registry records exist for 4,224 days (only 10-09..10-19).

  The first BingX «Пересчитать историю» would verify about 26k files
  locally and request the remaining ~18.8k days. Many of those are
  no-trade days of stock contracts, and each of them also costs an hourly
  request. Estimate: 1–1.3 s per day, so 6–7 h one by one, or about 3 h
  with the two pairs prepared at a time. This is where «hours» come from.
- **Bybit** (all times UTC; file times on disk are local, UTC+2):
  - The sync ran at 09:06–09:17. Its Bybit liquidity step, at 09:07, wrote
    `LIQUIDITY_MODEL_WINDOW_UNAVAILABLE` («missing weekday files») for all
    274 pairs. The window was the 7 weekdays 10-09..10-19.
  - The Bybit data folder was empty then. Every file of
    `tester/data/bybit/AAPLUSDT` (175 days) was written at 09:26–09:28 by
    the first «Пересчитать историю» run (`20261020T092600`: 09:26–11:21,
    254 pairs, 1,059 segments).
  - The sync does not download Bybit candles. Its liquidity step reads only
    the files on disk. The code assumes that a Bybit collector writes
    yesterday's file (`liquidity.py`), and here no one did.
  - The history run is followed by the pipeline only, so the Bybit liquidity
    stays UNAVAILABLE until the next sync.
  - Not the current day: liquidity ends on `today − 1`, the history on
    the last archived day (`now − 9 h − 1 day`), BingX on
    `last_available_day` (also 9 h).
  - **Fix (requirement 4):** in the sync, ensure the window days of the
    selected exchange first, then compute the liquidity.

**Speed-up candidates** (proposal, not decided):
- parallel requests across pairs, within the shared `duckdb_import.workers`
  limit; the client keeps its retry/backoff on the rate-limit codes, and the
  fixed pause becomes a shared rate limit;
- in the sync, check only the days the next step needs.

The 1m request is already one request per day (`limit 1440`), so a longer
range per request is not possible.

## Open questions to answer before the implementation

- Which part is slow: the number of requests (days × pairs, extra hourly
  requests), the 0.2 s pause, the missing parallelism, or repeated work on
  days that never become accepted? Measure one sync on BingX (requests per
  pair, seconds per request, days re-requested) before changing anything.
- Why does the last day look downloaded every time? From the code, an
  accepted day is skipped. Candidates: the last day is `SUSPECT` or its record
  hash differs, or the registry record is not written (registry held by a
  writer). Confirm on real data.
- Does the BingX API allow parallel requests at the rate needed, and what are
  its limits (the client already retries on rate-limit codes)?
- How does the bot's tester downloader get its speed (parallel requests,
  longer ranges per request, no per-day verification)? It is outside this
  repository: ask the user for its description or read the bot's source.
- Should the Bybit sync only report missing days, or download them (the
  user's wish: download)? Which window: the liquidity window, or the history
  span of the pair?

## Non-goals

- No change to the MRS2 simulator, READY, tick test or retest.
- No new BingX verification rule (ADR-0082 stays).

## Acceptance evidence (to fill when implemented)

- the sync of one exchange touches only that exchange (tests: the command line
  the Panel builds; the CLI steps);
- a second BingX sync with nothing new makes no kline request;
- measured BingX sync time before and after, on the same pairs;
- Bybit sync downloads a missing day and reports it.
