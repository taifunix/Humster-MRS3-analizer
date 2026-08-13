# Безопасный smoke-test runner v0.6

**Статус:** Approved design — implementation pending  
**Зависит от:** [Repository foundation](2026-08-10-mrs3-v07-repository-foundation.md)  
**Блокирует:** реальный smoke-test панели и runner перед реализацией v0.7

## Цель

Проверить веб-панель и runner на одном реальном JSON стратегии, не затрагивая вложенные каталоги рабочего `settings_strategy`.

## Контракт каталогов

- Рабочий каталог стратегий остаётся тем же; сам каталог не заменяется и не переименовывается.
- Runner изменяет только файлы `*.json`, лежащие **непосредственно** в корне рабочего каталога стратегий.
- Вложенные каталоги и всё их содержимое не перечисляются для изменения, не копируются, не перемещаются и не удаляются.
- Перед batch-run root-level JSON заменяются только после успешного staging и backup; затем в корень копируется только валидированная входная пачка.
- Не-JSON файлы в корне также не изменяются.
- Источник входной пачки не может быть корнем рабочего каталога стратегий или его вложенным путём: это исключает самоуничтожение источника при очистке root-level JSON.

## Реальный smoke-test

1. Пользователь выбирает один JSON из защищённого вложенного каталога.
2. Панель или CLI копирует его во временный staging-каталог вне рабочего каталога стратегий.
3. `tester-plan` только читает конфигурацию и staging-пачку, ничего не изменяя.
4. После явного подтверждения `tester-run` создаёт чистое состояние: очищает только настроенный каталог отчётов и два wizard-лога, затем заменяет только root-level JSON и запускает бот. Очистка должна завершиться до запуска бота, чтобы stale артефакты не попали в результат нового batch.
5. Runner собирает результат и атомарно пишет итоговый CSV, state, progress и audit вне `bot_root`.
6. После успешной записи CSV runner останавливает проверенный процесс бота и ещё раз очищает только настроенный каталог отчётов и два wizard-лога.

## Не входит в scope

- Изменение логики бота или формата JSON стратегии.
- Materializer DuckDB, CSV-пакеты, independent events и selector v0.7.
- Удаление, перенос или изменение файлов во вложенных каталогах стратегий.

## Ошибки и безопасность

- Если в корне стратегий есть каталог, символьная ссылка или файл, не являющийся JSON, runner оставляет его без изменений.
- Если staging-источник расположен внутри рабочего каталога стратегий, runner завершает preflight ошибкой.
- CSV, state, progress и audit должны находиться вне `bot_root`; небезопасные output destinations отклоняются до изменения файлов.
- Замена root-level JSON транзакционна: сначала валидируются и staging-копируются все новые JSON, затем backup-ятся только прежние root-level JSON, и только после этого устанавливается новая пачка. При любой ошибке до запуска бота runner восстанавливает прежние root-level JSON. Если rollback не завершился, backup остаётся рядом с каталогом стратегий под явно распознаваемым recovery-именем, а runner завершает работу ошибкой без запуска бота.
- План показывает точное число JSON, которые будут удалены в корне, и имена JSON входной пачки.

## Acceptance evidence

- Unit tests доказывают: вложенный каталог не меняется, root-level JSON заменяются, не-JSON root files сохраняются, вложенный источник отклоняется, а read-only plan не меняет файлов.
- Панельный тест передаёт каталог, а не одиночный JSON, и отображает безопасное действие плана.
- Реальный smoke-test одной стратегии проходит `tester-plan`, затем — только после явного подтверждения — `tester-run`; итоговый CSV и audit подтверждают один обработанный JSON.
- `git diff --check`, focused tests и независимый code review выполнены перед commit.

## Protected strategy visibility

- The tester may expose strategies loaded from protected nested directories such as
  `settings_strategy/Bybit`; their presence is compatible with a clean root batch.
- Before submission, every strategy in the installed root batch must be visible in
  `TEST` state. Additional visible strategies are ignored and must never be submitted
  or monitored by the batch runner.
- If any failure occurs after the runner starts the bot, the runner must attempt to
  stop that verified bot process before recording `FAILED`. Reports and wizard logs
  remain preserved for diagnosis.

## Panel tester log

- The panel shows concise English tester lifecycle, progress, metric and report
  messages. It suppresses bot localization lines and undecodable text rather than
  displaying mojibake.
- The unmodified stdout captured by the panel is written beside the configured
  tester result CSV as `<results-stem>.raw.log` and is available as an artifact.

## Controlled batch submission and recovery

- The runner must not enqueue the whole strategy directory at once. It submits
  at most `max_parallel_submissions` distinct strategies that have not yet
  reached a verified result; the local default is `10`.
- Consecutive tester launch requests are spaced by
  `submission_delay_seconds`; the local default is `0.2` seconds.
- A freed slot is filled only after the completed strategy has a tester Result
  row, one matching wizard-result entry and a stable report HTML file.
- A Result row without a matching HTML is held for
  `result_report_grace_seconds` (default `15`) before it can be retried. The
  stability poll count is not a retry timeout.
- A submitted strategy which was observed in `RUNNING` and then
  returns to the tester `TEST` row without a verified result is retried
  automatically. It cannot consume an additional window slot.
- A tester `RESULT` row with a matching wizard entry but no report HTML after
  two consecutive polls is also retried automatically. This covers tester runs
  whose summary is written but whose chart report is lost.
- An exact resume preserves the validated report HTML and wizard artifacts
  through preparation; a clean batch remains destructive only for the exact
  configured report directory and wizard files.
- On an exact resume, a remaining strategy may initially appear as either
  tester `TEST` or stale tester `RESULT`; both states are eligible for a new
  launch. A clean batch still requires `TEST` rows only.
- Each strategy may be submitted at most `max_strategy_attempts`; the local
  default is `4` across the whole runner process, including bot restarts. When
  an additional retry would exceed this limit, the batch
  fails with the exact exhausted names. Reports and wizard logs are preserved.
- Progress retains the count of distinct submitted strategies and additionally
  exposes retry attempts, so the panel can distinguish a throttled queue from
  a lost tester job.
- A transient tester HTTP failure or batch stall restarts the bot process at
  most `max_bot_restarts` times; the local default is `30`. The installed JSON
  batch is not replaced again during these process restarts.
- Before each process restart, the runner stops the bot and validates every
  current one-strategy wizard entry against its HTML report. Validated results
  are retained in memory, and the next process receives only names which do
  not yet have a valid result.
- A row already in `RESULT` before launch and its existing wizard `runId` are
  baseline evidence only. They cannot satisfy the new attempt; completion
  requires a fresh `runId` and a snapshot captured after launch.
- Process restarts do not retry configuration, installation, reconciliation or
  metric mismatch errors. When the restart limit is exhausted, the batch fails
  with the number and names of the remaining strategies, preserving raw
  reports and wizard logs.
- Progress is cumulative across process restarts and exposes
  `bot_restart_count`; completed work must not return to zero in the panel.

## Deferred panel plan summary

- The next panel iteration must render a concise result of `tester-plan`:
  total current JSON, verified reusable results, strategies to retest, and the
  reason reuse is unavailable.
- Before `tester-run`, the panel must show the exact number prepared for the
  run: all current strategies for a clean batch, or only the remaining
  strategies for a resume batch.

## Temporary closed-tester HTML collision recovery

- The closed tester derives a report filename from its shared symbol, timeframe
  and process-wide period. It does not use the already generated wizard `runId`.
- The runner keeps the full global `max_parallel_submissions` window, including
  strategies with the same symbol and timeframe. This avoids serializing a
  large single-timeframe batch for a rare writer collision.
- A report is accepted only after the existing verified-result contract: Result
  row, matching one-strategy wizard entry, matching HTML content and stable file.
- A collision therefore leaves the affected strategy unverified and sends only
  that strategy through a sequential repair run. The repair run keeps only its
  small group of verified HTML copies outside the bot report directory, so a
  repeated shared filename cannot overwrite the report used for final metrics.
- Existing reports are reusable only through the existing exact-resume
  validation; an orphaned, unstable, missing or mismatching HTML is retried
  within its strategy attempt budget and is never counted as complete.

## Bounded strategy-directory batches

- `tester_runner.strategy_batch_size` defaults to `50`.
- The complete generated JSON directory stays immutable; only the next
  unresolved chunk is installed into the bot `settings_strategy` directory.
- Every chunk follows `stop bot -> replace root JSON -> start bot -> verify the
  chunk -> stop bot`. The next chunk is not installed until the prior bot
  process has stopped.
- Reports and wizard logs are preserved between chunks for exact resume. Only
  runner-installed root JSON files are replaced for the next chunk.
- A `output_csv`-scoped exclusive runner lock rejects a second live process
  before it can change state, start the bot or replace files. A lock left by a
  dead PID is reclaimed automatically.
- Until the closed tester includes `runId` in the report name, a background
  collector polls the active report directory every 50 ms. After two unchanged
  observations it parses the embedded strategy name and atomically saves a
  per-strategy snapshot outside the bot directory. The final reconciliation
  prefers that snapshot over the shared `chartUrl` path. Snapshots remain after
  failures for resume and are removed only after the result CSV is committed.
- The runner never renames report HTML in place. Existing historical
  `*.html.saved` files may be read as legacy resume evidence, but no new files
  with that suffix are created. New interruption evidence is stored only as
  immutable per-strategy snapshots outside the tester report directory.
- Normal resume never synthesizes a completed wizard result from HTML alone.
  A one-time legacy migration may import already validated historical HTML only
  when it writes an explicit provenance manifest; new runtime evidence always
  requires the Result row, one-strategy wizard entry and matching HTML.

## Audited interruption and restart contract

- `tester-run` must publish fresh `PRECHECK`/resume progress and stop the
  verified bot before parsing or hydrating archived HTML. An old bot must never
  continue tests while resume evidence is being rebuilt.
- Every monitor exit closes its snapshot collector, including HTTP transport
  errors and unexpected exceptions. Process restart must not leak collector
  threads.
- Before a process restart, recovery validates both current report paths and
  per-strategy snapshots already captured by the interrupted monitor. Verified
  names are persisted immediately and removed from the next submission set.
- Retry counts and process-restart diagnostics are cumulative for the whole
  run. Progress exposes the exception class/message for the last restart and a
  count grouped by restart reason; a restart must never silently reset these
  fields to zero.
- `strategy_batch_size` and `max_parallel_submissions` are independent bounds.
  A final partial chunk is valid, and a configured chunk of `250` with a
  submission window of `35` must obey the same lifecycle as smaller chunks.
