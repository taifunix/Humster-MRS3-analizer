# Performance DB v2 — CHECK & RETEST и пакетный REPLACE

**Статус:** Active implementation contract
**Дата:** 2026-09-03
**Зависимости:**
[Unified Performance Analytics v2](2026-08-28-unified-performance-analytics-v2.md),
[Selection review import](2026-09-02-performance-v2-selection-review-import.md),
[ADR-0020](../decisions/0020-unified-performance-analytics-v2.md)

## Цель

Добавить воспроизводимый контур повторного тестирования стратегий, чьи текущие
Performance-факты неполны или вызывают сомнения. `RETEST` является независимым
долговечным тегом стратегии и не меняет её `ACTIVE`-состояние, пользовательский
статус отбора и текущий результат до успешного REPLACE.

## Scope

В этой задаче:

- схема Performance DB принимает теги `REJECTED` и `RETEST` с источником тега;
- Strategy ID из листов `HIGH` и `REVIEW` файла
  `Output/performance-v2-period-integrity-audit-2026-09-02.xlsx` однократно
  получают `RETEST`;
- итоговый selection XLSX содержит отдельную редактируемую колонку `RETEST`;
- обратный импорт XLSX атомарно устанавливает или снимает `RETEST` для строк
  импортируемого запуска;
- на вкладке «Стратегии и DD5» появляется экран `CHECK & RETEST`;
- экран показывает точное число `ACTIVE`-стратегий с тегом `RETEST`, предлагает
  диапазон теста и допускает его ручное изменение;
- `CHECK & RETEST` строит точные JSON-снимки из typed-параметров БД, запускает
  существующий native `SINGLE_MODE`, проверяет отчёты и создаёт обычный
  committed Performance inbox;
- `IMPORT & REPLACE` выполняет один проверяемый пакетный REPLACE и снимает тег
  только после успешной замены соответствующей стратегии.

## Non-goals

- автоматическое удаление стратегий или старой истории;
- новый тестер, второй формат отчёта или второй inbox-контракт;
- автоматический запуск RETEST при старте панели;
- изменение формул raw Performance, DD5 и A/B доходности;
- частичная публикация одного повреждённого REPLACE-пакета.

## Теги и схема БД

Внутренняя схема Performance DB повышается с `3` до `4` одной транзакционной
миграцией. `strategy_tags` хранит:

```text
(strategy_id, tag, source, source_ref, updated_at_utc)
tag IN ('REJECTED', 'RETEST')
```

Существующие `REJECTED` переносятся без потери значения с
`source='SELECTION_REVIEW'` и прежним review ID в `source_ref`. Источники
`RETEST`: `PERIOD_INTEGRITY_AUDIT`, `SELECTION_REVIEW` и `RETEST_WORKFLOW`.
Миграция не меняет strategy/result/action/equity/window facts и не требует
пересчёта кеша.

Первичная разметка audit-файла выполняется с проверкой уникальности ID и
наличия каждой стратегии в текущем экземпляре БД. Повторный запуск идемпотентен.

## XLSX round-trip

`RETEST` — не значение `User Status`, а отдельный ортогональный признак.
В `All candidates` добавляется колонка `RETEST`:

- пусто — тег должен отсутствовать;
- `RETEST` — тег должен присутствовать;
- формулы и любые другие значения запрещены.

Экспорт заполняет колонку по текущему тегу. Успешный обратный импорт в той же
транзакции, что и review ledger, синхронизирует RETEST только для Strategy ID
этого workbook. Остальные стратегии не затрагиваются.

## Выбор стратегий и окна

В работу попадают только `ACTIVE`-стратегии с тегом `RETEST` и существующим
current result. Счётчик читается из БД и не зависит от чекбоксов/порядка Pareto.

Окно по умолчанию — `(report_start_utc, report_end_utc)` одного текущего
результата из RETEST-набора с максимальной календарной длительностью;
равенства разрешаются по Strategy ID. Пользователь может задать обе даты
вручную. Обязательные проверки:

- ISO-даты и `start < end`;
- для каждого symbol известна listing date;
- listing dates may be later than the common tester start; the importer applies the five-day warm-up and derives the effective start per symbol;
- набор RETEST не изменился между подготовкой и запуском импорта.

## JSON и manifest

### Сопоставимость PnL и DD

`PnL/30` остаётся существующей доходностью, приведённой к 30 календарным дням.
`max_drawdown_pct` всегда остаётся фактической сырой просадкой соответствующего
исследованного окна: её запрещено масштабировать, экстраполировать или иначе
нормализовать по длительности. Поэтому `PnL DD5/30` остаётся
`PnL/30 * 5 / max_drawdown_pct`; это сопоставление нормализованного PnL с
известным сырым DD, а не прогноз DD за 30 дней. Сырые итоговые PnL и DD
сохраняются как audit/provenance-поля и не являются selection objectives.

### Сопоставимость selection windows

`filter_low_trades` использует `Trades/30d`, а не сырое число сделок. Сырая
`total_pnl_pct` не экспортируется в selection XLSX; она остаётся только audit
полем БД. Правила best-trade, holding, first shift, Close MA, plateau points и
capital proxy не меняются.

Time-consistency использует ту же календарную длительность
`(report_end_utc - report_start_utc)`, что и `Trades/30d`: при периоде
`>= 28` дней — четыре равных окна, при `>= 21` и `< 28` — три, иначе статус
`UNAVAILABLE`. Последняя граница всегда равна `report_end_utc`. Для каждого
окна используется `PnL/30`; положительным считается только строгое значение
`> 0`. `NO_TRADES` исключается из оцениваемого знаменателя; missing или другое
недоступное окно делает весь результат `UNAVAILABLE`. Итоговые статусы:
`PASS` при `>=3/4` либо `>=2/3`, `FAIL` при недостаточном числе положительных
оценённых окон, `UNAVAILABLE` без исключения стратегии. XLSX показывает для
него `N/A`; при трёх окнах Q4 не создаётся и не запрашивается.

Версия cache metrics поднимается до `performance-window-v2.2`, чтобы старые
четырёхоконные записи не использовались для этой логики. Новая задача не
меняет схему БД и не удаляет старые cache rows.

`src/mrs3/posttest.py` — устаревший legacy DD5 workflow и удаляется отдельной
задачей вместе с его вызовами и тестами; v2 selection является единственным
поддерживаемым контуром этого отбора.

### Listing-date warm-up and effective research window

`SINGLE_MODE` keeps one exact batch `test_start`/`test_end`; a later-listed
symbol is not given a per-strategy tester range. Listing values are UTC: a date-only value means
`00:00:00Z`; every timestamp with no unambiguous UTC offset is invalid. The
named constant `LISTING_WARMUP_HOURS = 120` is stored in provenance. For each
report, importer derives:

```text
effective_start_utc = max(reported_start_utc, listing_date_utc + 120 hours)
effective_end_utc = reported_end_utc
```

The HTML report must still match the batch range exactly. `reported_*` is
retained as report provenance; `effective_*` is the researched Performance
period published for that strategy.  The importer excludes every trade whose
open timestamp is before `effective_start_utc`, including a trade that closes
after the warm-up. It retains only actual parsed wallet/equity observations in
the effective range; when those observations are absent, equity-derived
drawdown is unavailable rather than synthesized from trades. It recalculates
persisted PnL, fees, trade count and balances from the retained lifecycles;
excluded trades contribute zero PnL, fees, volume or equity effect. Raw
drawdown is never scaled or extrapolated. Changing only the stored start date
while retaining whole-report aggregates is forbidden.

If a listing date is absent or invalid, `effective_start_utc >= effective_end_utc`,
or no retained trade exists, import leaves that strategy unchanged and retains
its RETEST tag; it does not abort other valid strategies. Reason codes are
`LISTING_MISSING`, `LISTING_INVALID`, `EFFECTIVE_RANGE_EMPTY`,
`NO_EFFECTIVE_TRADE`. The batch publishes one CSV
and XLSX failure report with strategy identity, reported/effective ranges,
listing provenance and reason; panel status exposes a safe link to it. Listing
dates are read from configured `listing_dates_path` (normally `Input/dates.xlsx`),
never from a hard-coded path.

The Performance import request/inbox contract therefore carries the configured
listing-dates path, and the published result retains both ranges. Panel-originated
imports also carry the server-owned project root as the first trusted base for
that relative path and retain the inbox parent as a compatibility fallback;
direct inbox imports use the inbox parent. Absolute paths and parent traversal
remain invalid.
The typed action/equity rows and all canonical result metrics describe only the
effective range; the unmodified HTML remains the immutable whole-report
evidence. Persisted provenance contains reported/effective range, raw and
normalized listing date, warm-up hours, excluded-trade
count and exclusion reason. Pre-amendment records have null effective/listing
provenance; readers must not infer that effective and reported ranges match.

### Канонические шаблоны

Все отслеживаемые шаблоны стратегий находятся только в
`templates/strategies/`:

- `source-v6-mrs2/long.json` и `source-v6-mrs2/short.json` принадлежат контуру
  первичного MRS2-тестирования для последующего импорта отчётов в Source v6;
- `retest-mrs3/base.json` принадлежит RETEST-контуру MRS3 и содержит прототипы
  обеих сторон.

Конфигурации тестера также являются каноническими шаблонами и находятся в
`templates/tester/`:

- `mrs2/config_tester_long.json` и `mrs2/config_tester_short.json` — профили
  обычного MRS2-тестирования с соответствующим parameter-mining составом;
- `mrs3/config_tester.json` — профиль обычного MRS3 Fast/SINGLE_MODE и RETEST.

`Input/` остаётся каталогом пользовательских входных данных и не является
источником шаблонов. `panel_workflow.strategy_templates` для LONG и SHORT
указывает на канонический MRS3 base. Локальный MRS2 testing выбирает
канонические side-specific strategy и tester-config файлы напрямую. Неявные
fallback на старые имена из `Input/` запрещены.

Для удалённого MRS2 используются те же strategy-профили: их JSON-содержимое
семантически совпадает с прежними `Input/Bybit_long.json` и
`Input/Bybit_short.json`. Старые файлы `Input/config_tester_*_standart.json`
остаются только как локальное происхождение и больше не читаются runtime.

SHORT multiplier strings сохраняются в legacy-формате до отдельной проверки
реальным тестером; диапазон индексов canonical-профиля приведён к 19 значениям.

Для каждой стратегии JSON строится из текущих `strategies` и
`strategy_orders`, используя существующий шаблон соответствующей стороны и
существующий MRS3 renderer. Имя стратегии сохраняется без изменений. После
рендера typed identity повторно сравнивается с БД: symbol, side, timeframe,
Close MA, число ордеров, Open MA, shift, lot и plateau diagnostics должны
совпасть точно.

Файлы публикуются атомарно в настроенный `Output/strategies`; manifest проходит
обычный `validate_strategy_manifest`. Пакет RETEST может содержать стратегии из
разных исходных analysis run, поэтому manifest/inbox содержит
`strategy_analysis_run_ids` для каждого JSON. Старый общий `analysis_run_id`
сохраняется как идентификатор самого тестового пакета. Обычные manifests без
карты продолжают использовать общий run ID.

## Тестер и полнота отчёта

Используется только существующий native `SINGLE_MODE`. Перед запуском его общий
writer загружает канонический `templates/tester/mrs3/config_tester.json` и
создаёт runtime `config_tester.json`, изменяя только выбранные
`StartDate`/`EndDate`, `single_mode=true`, `use_runs=false`,
`max_parallel_runs` и обязательные HTML/settings/trades/balance-секции.

`max_parallel_runs` в каждом созданном профиле равен
`tester_runner.max_parallel_submissions` из `config.local.json`. Это единый
источник числа воркеров тестера для обычного MRS2, обычного MRS3 и RETEST;
`direct_materialization.workers` и `duckdb_import.workers` к тестеру не
относятся. Текущий runtime-файл не используется как шаблон и не задаёт
состав профиля.

Обычный batch также записывает в точный путь `tester_runner.tester_config` из
`config.local.json`; это единственное runtime-назначение для тестера, даже
если путь находится не в корне bot.

Удалённые MRS2-запуски требуют только положительного
`tester_runner.max_parallel_submissions` для переноса числа воркеров; полный
локальный preflight `tester_runner` для них не выполняется.

Отчёт принимается только если одновременно:

- embedded strategy name и настройки совпадают с JSON-снимком;
- report range точно равен выбранному диапазону;
- присутствует актуальная Performance-v2 структура;
- число объявленных и разобранных строк действий совпадает;
- wallet/equity ряды согласованы и лежат внутри включительного report range.

Проверка точного report range выполняется и при сборе отчётов, и повторно на
границе Performance import по `test_start`/`test_end` inbox. Дата последней
сделки не подменяет календарную границу теста.

`CHECK & RETEST` заканчивается созданием committed inbox и не меняет БД.
Отдельная кнопка `IMPORT & REPLACE` является явным подтверждением мутации.

## Пакетный REPLACE

Импорт строит mapping `strategy_name -> strategy_id` на сервере из committed
RETEST job; browser не является источником identity. Перед транзакцией заново
проверяются RETEST-тег, current result и typed identity каждой строки.

Весь пакет заменяется одной транзакцией. Ошибка одной стратегии откатывает
пакет и сохраняет все RETEST-теги. После успешной замены старого current result,
actions и equity для каждой строки её `RETEST` удаляется в той же транзакции.
Новые Strategy ID не создаются. `REJECTED` и review history не меняются.

## Экран и состояния

Экран показывает:

- точный RETEST count;
- окно по умолчанию и два редактируемых `<input type="date">`;
- `CHECK & RETEST`, `IMPORT & REPLACE`;
- общий progress bar и текущий этап;
- completed/total, batch, active tester jobs, retries, failed names;
- путь committed inbox и результат REPLACE.

Основные этапы: `READING_DB`, `GENERATING_JSON`, `VALIDATING_MANIFEST`,
`UPDATING_TESTER_CONFIG`, `BOT_START`, `BOT_RUN`, `REPORT_COLLECTION`,
`RETRY_MISSING`, `VALIDATING_REPORTS`, `INBOX_READY`, `IMPORTING`,
`REPLACING`, `READBACK_VERIFIED`, `COMMITTED`, `FAILED`.

`IMPORT & REPLACE` неактивна до `INBOX_READY`. Повторный refresh страницы не
стирает серверный job/status; новый DB count читается при загрузке экрана и
после успешного импорта.

On panel load, no previous RETEST tester or import job is activated and
`IMPORT & REPLACE` remains disabled. A previously committed native RETEST job
may be shown as an unactivated candidate only. The explicit `CHECK & RETEST`
action first queries the durable job registry, chooses the newest committed
native RETEST job, and calls `verify-inbox`; this rechecks the configured
`tester_runner.report_dir` (normally `my_test`) and strategy directory and
rebuilds the handoff metadata from reusable artifacts. A successful check
activates that job and enables `IMPORT & REPLACE` without rerunning the tester.
If there is no reusable committed job, the current `ACTIVE` RETEST rows are
validated and the existing native `SINGLE_MODE` retest algorithm is started.
If the previous import for that committed inbox is `FAILED` or `CANCELLED`,
`IMPORT & REPLACE` may be retried for the same inbox; a previous `COMMITTED`
import remains non-repeatable.

The tester remains the sole owner of the standard report directory configured
by `tester_runner.report_dir` (normally `hb/tester/report/my_test`). A
`SINGLE_MODE` inbox stores report filenames as metadata and does not copy or
archive HTML files. Import resolves those filenames against the configured
report directory, verifies the manifest hash, and empties the contents of the
exact configured report directory, tester strategy directory, and project
`Output/strategies` directory only after the replacement transaction commits.
The directories themselves and their parents are retained; no reports are
copied or archived during cleanup.
The tester strategy directory is validated as a strict child of the configured
`tester_runner.bot_root`; if one of the three cleanups fails after commit, the
panel returns a non-fatal warning naming the failed root.
New `SINGLE_MODE` values are one filename component (no absolute path,
separators, `.` or `..`); older absolute values remain readable only when
their resolved file is inside the configured report directory and passes the
same regular-file, reparse-point, and SHA-256 checks. Missing or changed
reports fail before any database mutation or cleanup.

## Native startup and status heartbeat

The existing native runner is the sole owner of the configured `hb_c.exe`
process and local port. Before starting a batch it may terminate only a live
process whose resolved executable path exactly matches the configured
`executable_path`; a process that has not bound the port yet is still cleaned
up. A listener owned by another executable remains a hard safety error.

The worker publishes its `BOT_START` snapshot before starting the process and
refreshes it at the configured poll interval while startup is pending. The
snapshot keeps the existing callback and panel-job journal contract and adds
`progress.startup_elapsed_seconds` and `progress.active`. A startup timeout
includes the started PID, child return code and observed listener PIDs, and
the started child is cleaned up before the job becomes `FAILED`.

## Acceptance evidence

- v3->v4 migration сохраняет старые REJECTED и все Performance facts;
- audit HIGH+REVIEW дают ожидаемый уникальный RETEST-набор и повторная разметка
  ничего не дублирует;
- XLSX может установить и снять RETEST, не меняя статус стратегии;
- mixed-analysis-run manifest импортируется через REPLACE без ослабления typed
  checks;
- неверный report range не попадает в inbox/import;
- пакетный REPLACE сохраняет Strategy ID, заменяет current result и очищает
  RETEST только после commit;
- ошибка одного файла откатывает весь пакет и сохраняет RETEST;
- панель отображает count, окно и прогресс и блокирует преждевременный import.
