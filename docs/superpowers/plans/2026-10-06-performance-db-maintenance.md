# Обслуживание PerformanceDB — план реализации v4.1

**Основание:** [спецификация](../../specs/2026-10-06-performance-db-maintenance.md)
**Статус:** реализация завершена; независимый Claude Opus 5 review пройден; scoped feature commit создан в local `main`.
**Review:** Planner v4 received PLAN_APPROVED from the independent Advisor;
the root integrated its non-blocking implementation clarifications here.

## Граница разрешения

Пользователь разрешил выполнить этот план в текущей ветке `main` и создать
отдельный scoped feature-коммит. Проверки с удалением ограничены синтетическими
schema-v9 fixtures под `C:\TEMP`; реальная PerformanceDB остаётся неизменной.

## Объём и файлы

- Создать `src/mrs3/performance_v2_maintenance.py` для аудита pair scope,
  каталога, preview, проверки целей и последовательного удаления.
- Дополнить `src/mrs3/panel.py`: координация writers, методы контроллера,
  временное состояние preview/job/progress и API каталога, preview, apply и
  статуса.
- Обновить `src/mrs3/panel_web/index.html`, `app.js` и `app.css` карточкой 9.
- Добавить проверки в `tests/test_performance_v2_maintenance.py`,
  `tests/test_panel_performance_v2.py` и `tests/test_panel_static_ui.py`.
- Оставить схему на v9. Не менять `performance_v2_prune.py`, lifecycle статуса,
  импорт, tester или market data ради обслуживания. Координационная правка
  другого writer допустима только если аудит подтвердит необходимость.

## 1. Аудит данных и конкурирующих writers

1. Составить карту таблиц v9: прямые столбцы символа, pair scope через strategy,
   result, selection и review IDs, связи plateau. Прямой `symbol` есть в
   `strategies`, `selection_runs` и `strategy_actions`. Проверить, что
   `strategy_actions.symbol` совпадает с символом родительской стратегии через
   `result_id`; при несоответствии остановить каталог/preview с диагностикой,
   не скрывать остаток и не удалять его как другую пару. Если инвариант
   подтверждён, полный каталог образуют `strategies.symbol` и
   `selection_runs.symbol`; все остальные pair-scoped строки достижимы через
   эти владельцы. Отдельно описать strategy orders, rejection sources и цепочку
   selection/review.
2. Проверить residue по каждой pair-scoped таблице после полного удаления.
   Для выбранных пар не остаётся pair-scoped строк; исключение — plateau,
   используемые orders невыбранных стратегий. Сравнить все строки
   невыбранных пар с исходными; допустимое глобальное отличие только в
   `import_files` и `import_runs`.
3. Проследить каждый writer PerformanceDB: import, tester/retest, selection,
   recalculation, date-prune и Panel jobs. Зафиксировать единый порядок
   получения `PanelController._performance_v2_writer_lock` и
   `PerformanceV2WriterLock`, проверить отсутствие обратного порядка в других
   путях. Обслуживание держит оба общих lock от revalidation preview до конца
   apply и использует одно writable DuckDB соединение. Каждый другой writer
   берёт те же shared locks либо не может стартовать во время apply; если путь
   нельзя надёжно координировать, apply завершается конфликтом.
4. Проверить конкуренцию в обе стороны: уже работающий writer блокирует или
   отклоняет apply; новый writer, стартующий посреди apply, блокируется или
   получает ясный конфликт. Проверить, что обслуживание не открывает второе
   writable соединение и не запускает параллельные DELETE.

## 2. Каталог и preview

1. После отдельного разрешения на реализацию начать с узких failing tests.
   Каталог — полный отсортированный список точных символов из `strategies` и
   `selection_runs`, включая пару только с selection run. Отдельно проверить
   диагностику для несовпадающего `strategy_actions.symbol`. Сервер принимает
   только непустой список символов из актуального каталога и тип операции;
   клиентские strategy/result IDs не принимать.
2. `Удалить Rejected` выбирает только текущий эффективный `User Status ==
   REJECTED` по действующим manual review/tag и независимому sticky equity
   source согласно ADR-0056. Проверить каждый источник отдельно и вместе,
   смену результата, stale review/selection без активного основания и
   `Auto Status`. Resolver и lifecycle статуса не менять.
3. Групповыми SQL рассчитать точные количества по таблицам и выбранным парам.
   Rejected затрагивает только `strategy_actions`, `strategy_equity` и
   `optimizer_prepared_inputs`. Полное удаление охватывает все pair-scoped
   строки и plateau только если после удаления выбранных orders у них не
   останется ссылок. `import_files` и `import_runs` считать один раз глобально,
   отдельно от пар.
4. Оба preview включают каждую выбранную пару, в том числе с нулём строк. Такие
   пары не добавляют строки в общий pair-scoped denominator и не становятся
   целью pair-scoped DELETE.
5. Preview read-only: не создаёт файлов, таблиц или строк. В памяти держать
   токен, операцию, точный выбор и fingerprint целей (IDs, владельцы, counts
   по таблицам/парам и глобальный журнал). При подтверждении под locks и на
   том же соединении повторно вычислить цели. Любое изменение, даже IDs при
   тех же counts, требует нового preview; повторный/неизвестный токен
   отклонять.

## 3. Последовательное удаление

1. Rejected удаляет только строки из трёх разрешённых таблиц деталей. Сохранить
   поля стратегии и typed settings: symbol, side, timeframe, close MA, order
   count и для каждого order open MA, shift и lot; сохранить компактные
   `strategy_results`, `window_metrics`, `equity_quality_metrics`, действующие
   rejection evidence и всю selection/review history. Проверить, что равный
   или более узкий импорт остаётся deduped, а допустимый широкий импорт
   восстанавливает details.
2. Для полного удаления соблюдать зависимости: `selection_review_rows`,
   `selection_review_imports`, `selection_results`, `selection_runs`;
   `strategy_actions`, `strategy_equity`, `optimizer_prepared_inputs`,
   `window_metrics` и иные result facts/caches; `strategy_results`; tags и
   rejection sources; `strategy_orders`; `strategies`; затем plateau без
   ссылок оставшихся orders. Удалить глобальные `import_files` перед
   `import_runs`, включая историю невыбранных пар. Непустой выбор запускает
   эту глобальную очистку и при нулевом числе pair-scoped строк у выбранной
   пары.
3. Выполнять по одному DELETE за раз. Завершённые операторы могут commit до
   позднего сбоя, общий rollback не обещать. После каждого успешного commit
   сверять фактический count с расчётом под теми же locks; только подтверждённые
   строки отражать в прогрессе. При расхождении остановиться с диагностикой.
4. Строка `Удалено XX из NNN` измеряется физическими pair-scoped строками:
   `NNN` — точное число уникальных строк из подтверждённого preview, `XX` —
   фактически committed строки. Глобальные строки журнала идут отдельным
   счётчиком и не входят в этот denominator. Общие plateau нельзя посчитать
   дважды; preview отдельно показывает общий count таких строк, если они
   относятся к нескольким выбранным парам.
5. Для CPU/read запросов использовать только существующий
   `duckdb_import.workers`, ограниченный 16 потоками. Группировать SQL по
   множеству пар; не добавлять настройку workers, второй writer или
   параллельные DELETE.

## 4. API и статус выполнения

1. Добавить API каталога, preview, apply и job status. Apply быстро возвращает
   job ID. Во время активного apply новые catalog/preview/apply запросы
   получают ясный conflict response; status polling продолжает работать.
2. В памяти хранить фазу/таблицу, committed counts по таблице и паре, общий
   pair-scoped `deleted/previewed`, отдельный global journal count, время
   старта, terminal elapsed и ошибку. Status endpoint читает только память;
   финальные значения остаются до следующего apply. Параллельное применение
   отклонять.
3. Статусная строка показывает фазу, реальное `Удалено XX из NNN`, отдельный
   global journal count и общее elapsed с начала apply. Счётчик и полоса
   обновляются только по завершённым DELETE/commit, без оценки или
   интерполяции; итоговые значения сохраняются после завершения.
4. Ошибка API/UI указывает фазу и таблицу, исходное применимое сообщение
   DuckDB, фактически committed per-table/per-pair/global counts и elapsed.
   Сообщение не должно утверждать, что частично committed операция откатилась.

## 5. Карточка 9

1. Разместить `9. Обслуживание PerformanceDB` после карточки 8 в «Стратегии и
   DD5», включая порядок, собираемый `app.js`. Начально видна кнопка
   «Показать список пар».
2. По нажатию показать полный сортированный каталог в четыре колонки.
   Использовать существующий checkbox 16 px и выравнивание с подписью. В
   подписи убирать только конечный `USDT`; value и запрос сохраняют точный
   символ БД.
3. «Выделить все» и «Снять выделение» расположить над сеткой; «Удалить
   Rejected» и «Удалить полностью» — под ней. Первые две кнопки действуют на
   весь каталог.
4. Обе операции сначала открывают preview, затем отдельное подтверждение.
   Изменение выбора/действия инвалидирует preview. Stale ответ предлагает
   получить новый preview.
5. Использовать `.progress-block`, `.progress-track`, `.card-status` и
   `role="status"`, без отдельного виджета. На узкой ширине проверить
   читаемость и отсутствие перекрытий.

## 6. Проверки после отдельного разрешения

Все destructive проверки: preview, apply, residue scan, reimport/dedup,
concurrency и workers benchmark — только на синтетических schema-v9 DuckDB
фикстурах под отдельным `C:\TEMP` root на тест/запуск, с удалением временных
файлов после. Ни fixture, ни harness не открывает реальную пользовательскую
или репозиторную PerformanceDB на запись. Добавить guard: известные production
пути отклоняются, а каждый writable target обязан находиться под fixture root.
Аудит реальной базы возможен только read-only или на копии, без DELETE;
проверка read-only режима входит в guard.
Обычные DuckDB sidecar-файлы не считать продуктовым backup.

- **Schema/preview:** до и после preview/apply сравнить schema version, таблицы
  и колонки; подтвердить отсутствие новых полей/таблиц, `cleanup_state`,
  `deleted_at_utc` и persistent progress. Preview не меняет counts ни одной
  таблицы и не создаёт файлов. Apply не создаёт backup/copy/staging/snapshot.
- **Rejected:** field-level assertions на сохранённые identity/settings,
  compact results/metrics, rejection evidence, history и effective status.
  Только три detail/cache таблицы меняются; остальные строки идентичны.
  Проверить повторный нулевой preview и оба import сценария.
- **Full delete:** selection-only пара остаётся в каталоге до удаления её
  pair-scoped rows; residue scan по каждой pair-scoped таблице; shared/orphan
  plateau; невыбранные строки эквивалентны исходным, кроме глобального
  журнала. Global counts показаны ровно один раз; обе таблицы журнала очищены.
- **API/failure/concurrency:** неверный символ, пустой выбор, подмена ID,
  повторный/stale токен (в том числе изменённые IDs при прежнем count), активный
  и новый во время apply writer, один writable connection, отсутствие
  обратного lock order. Late DB failure подтверждает прежние commits, точные
  counts/time, фазу/таблицу, исходное сообщение и отсутствие rollback claim.
- **UI:** начальное состояние, card/button order, markup/style reuse, четыре
  колонки, точный symbol в запросе, label без USDT, нулевые строки, отдельное
  подтверждение, progress format и удержание терминального/ошибочного статуса.
  Проверить, что completed count не растёт до завершённого DELETE.
- **Performance:** одинаковые fixtures при workers=1 и настроенном workers;
  identical per-table/per-pair counts и конечные строки (полный compare или
  hash), elapsed и peak RSS. Если DELETE не ускоряются, оставить один writer
  и зафиксировать измеренное ограничение.
- Тесты запускать только через `.venv\Scripts\python.exe -m pytest ...`,
  `TEMP`, `TMP` и `--basetemp` направлять под C: TEMP и очищать после. Никаких
  live PerformanceDB/source DB/tester/market-data mutations. После разрешённой
  реализации — `git diff --check`, осмотр staged diff, независимое code review
  компактным самодостаточным ASCII пакетом и re-review исправлений.

## 7. Документация и завершение после разрешения

Новый ADR утверждает для этой функции немедленный физический DELETE без marker
column, timestamp column, cleanup-log row, backup, copy, staging, snapshot или
auto-restore artifact. Он сужает прежние требования только для этой функции;
старые ADR не переписывать, семантику ADR-0056 сохранить. Отдельно записать
глобальную очистку `import_files` и `import_runs`. После проверенной реализации
обновить спецификацию, PRD и `progress.md`; README менять только если меняется
публичный запуск. Один scoped conventional commit — только после независимого
`CODE_REVIEW_PASS`.

## Evidence реализации (2026-10-06)

- Реализованы schema-v9 сервис каталога/preview/apply, transient Panel API/job,
  writer coordination, карточка 9, отдельное подтверждение и status/progress
  с фактическими committed counts, elapsed и исходным сообщением ошибки.
- Root verification: `.venv` suites для maintenance, importer, Panel API и
  static UI — **428 passed, 4 skipped** за 327.76 s; 78 существующих pandas
  fragmentation warnings. Пропуски относятся к недоступной symlink capability.
  pytest cache отключён; временный root под `C:\TEMP` удалён.
- После исправлений stale-preview response, terminal catalog refresh и нулевого
  progress bar повторно прошла static UI проверка: **156 passed** за 6.82 s.
- На одинаковых синтетических full-delete fixtures workers=1 и workers=8
  дали совпадающий конечный SHA-256, per-pair/per-table и global counts.
  Время составило 1.084 s против 1.126 s, peak RSS — 135,213,056 B и
  137,973,760 B: параллельные DELETE не ускорились, поэтому оставлен один writer;
  настройка применяется для read queries.
- `git diff --check` прошёл. Реальная PerformanceDB не открывалась на запись.
- Финальная проверка после review fixes: targeted Panel maintenance и static UI —
  **15 passed**; recovery/progress сценарии покрыты в этом наборе. Unit tests
  maintenance service — **42 passed**. Независимый Claude Opus 5 повторный review
  Panel backend — `CODE_REVIEW_PASS`; предыдущие service-пакеты S1–S3 также
  получили `CODE_REVIEW_PASS`. Отдельный scoped feature-коммит создан в local
  `main`.
