# Panel «Синхронизировать»: only the selected exchange, candle check, faster BingX data

**Status:** Part 1 approved by the user 2026-10-20 («чини и сразу пересчитай
ликвидность… нужен режим отдельно пересчитать ликвидность») and implemented:
requirements 1 and 4 and «Пересчитать ликвидность» («Part 1 contract» below).
Part 2, the parallel BingX download, was approved («внедряй параллельную
закачку») and is implemented («Part 2 contract»). The BingX rate limit
bounds its gain.
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

## Part 2 contract: parallel BingX download under the BingX rate (implemented 2026-10-20)

- **The request rate, measured on the klines** (2026-10-20, distinct pairs,
  1m days):
  - 60 requests at 2/s: no error;
  - at 3/s the 41st request, and at 5/s the 32nd, got `100410` («请稍后重试»,
    retry later). That is about 30 requests per 10 s per IP.
  - BingX's own pages disagree:
    - the 2025-10-16 announcement gives 500 per 10 s for all market data;
    - the Coin-M klines reference gives 1/s.
  - The measurement decides: `REQUEST_INTERVAL_SECONDS = 0.5` (2 requests/s).
  - Also seen: after more than 10 errors `109425` within 15 min, BingX
    refuses that API for 15 min (`109429`). Staying under the rate avoids
    it.
- **`BingXClient` is thread-safe and spaces its requests.**
  - `request_delay_seconds` (default 0.5) is the interval between the starts
    of two requests of the client, over all its threads, in call order.
  - It is no longer a pause added before every request. One by one, a
    request (0.45–0.9 s) is therefore not slowed down any more.
  - Retries and backoff on the rate-limit codes are unchanged.
- **The sync / liquidity window** (`ensure_window`, BingX):
  - the pairs run in parallel threads (`duckdb_import.workers`) over one
    shared client;
  - the registry is read before and the new records are written after, in
    the calling thread;
  - one pair failing never stops the others.
- **One pair's days** (`verify_bingx_days(..., threads)`): days in parallel.
  - The result is in day order and the same as one by one.
  - Used with `threads = duckdb_import.workers` by the tick test / retest data
    step and the history.
- **History:** two pairs are prepared at the same time in two processes.
  Each process's client takes half of the rate (`rate_share =
  min(PREPARE_IN_FLIGHT, workers)`), so the run as a whole stays at 2/s. With
  one worker, one pair is prepared at a time at the full rate.
- **The registry in the window:**
  - each pair's records are written as soon as the pair is done, so an
    interrupted run keeps the pairs already done;
  - a pair without an API symbol, or with a failing write, is a failed pair.
- **The rate is per process.** Two BingX downloads running at the same time
  in separate processes would together exceed it, and then get `100410`
  retries; `109429` days are refused and verified again on the next run.
  Such downloads are a sync or liquidity task, the history, the tick-test /
  retest data step and `tester_jobs`. The Panel's task area runs one task
  at a time, but a tick test runs as a controller job beside it, so do not
  start one during a BingX sync.
- **Measured with the code** (a copy of the registry, an empty folder, 40
  BingX pairs, the 7-weekday window up to 10-19, 437 days):
  - 238 s, all 437 accepted, no error, about 0.54 s per day. The old client
    took about 0.72 s per day (the first sync's journal).
  - A second run: 0.2 s, no request.
- **Consequence:**
  - the BingX rate, not the code, is the limit;
  - the sync gains about 1.3×;
  - the history gains nothing: its two processes already made about 2.3
    requests/s together, so they were at the limit, with retries.
  - **Proposed next** (not decided): check the hours of a pair's whole span
    in one 1h request (`limit 1440` = 60 days) instead of one per day with
    gaps. Every no-trade day (stock weekends) costs such a request today.

## Part 3 contract: one hourly request per 40 days (implemented 2026-10-20)

The user approved it («Эти проверки можно собрать в один запрос на 60 дней
по паре… делай»). BingX answers at most 1,000 candles per request, so a
request takes 40 days of 1h candles (960), not 60.

- **`HourBatches`** (`minute_days`) serves the 1h candles of the days of one
  `verify_bingx_days` call:
  - the days of the call, in order, are cut into chunks of
    `HOUR_DAYS_PER_REQUEST = 40` consecutive days;
  - a chunk is requested **only when one of its days has a gap to check**,
    and once: the days of a chunk, also in parallel threads, wait for the
    same request;
  - a failed request is not remembered. Its day is refused as before, and
    the next gap day of the chunk asks again.
- **The rule does not change:** a day with gaps is compared with the hour
  candles as in ADR-0082 (`differing_hours`); only where the candles come
  from changes. `_verify_day` without batches asks per day, as before.
- **Measured** (40 BingX pairs, 437 days, 2026-10-20): 437 minute requests
  and 3 hourly ones; 226 s against 238 s before. On the 7-weekday window the
  hourly requests were only about 9 % of all, so the gain there is small.
  The full history of a pair, with the no-trade stock weekends, should gain
  more. That is not measured yet.

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
